"""Bounded SQS CPU worker. AWS credentials come from the instance profile."""

import argparse
import hashlib
import json
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import boto3
from botocore.config import Config
from models import InvalidSample, Models


def invoke(client, function, operation, job_id, **fields):
    response = client.invoke(
        FunctionName=function,
        Payload=json.dumps({"operation": operation, "job_id": job_id, **fields}).encode(),
    )
    with response["Payload"] as body:
        data = json.load(body)
    if response.get("FunctionError"):
        raise RuntimeError("worker API rejected operation")
    return data


def download(claim, host, path):
    url = urlsplit(claim["url"])
    size = claim["size_bytes"]
    if url.scheme != "https" or url.netloc != host or not 0 < size <= 52_428_800:
        raise InvalidSample("unexpected audio source")
    with urlopen(claim["url"], timeout=20) as response:
        data = response.read(size + 1)
    if len(data) != size or hashlib.sha256(data).hexdigest() != claim["sha256"]:
        raise InvalidSample("audio checksum did not match")
    path.write_bytes(data)


def process(sqs, service, config, message, models):
    job_id = json.loads(message["Body"])["job_id"]
    receipt = message["ReceiptHandle"]
    args = (service, config.worker_api)
    claim = invoke(*args, "claim", job_id)
    state = claim["state"]
    if state == "terminal":
        sqs.delete_message(QueueUrl=config.queue_url, ReceiptHandle=receipt)
        return False
    if state != "claimed":
        return False
    attempt = claim["attempt"]
    stop = threading.Event()
    lost = threading.Event()

    def heartbeat():
        while not stop.wait(30):
            try:
                if invoke(*args, "heartbeat", job_id, attempt=attempt)["state"] != "running":
                    lost.set()
                    return
                sqs.change_message_visibility(
                    QueueUrl=config.queue_url, ReceiptHandle=receipt, VisibilityTimeout=150
                )
            except Exception:
                lost.set()
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="umsal-cpu-") as folder:
            audio = Path(folder) / "audio.wav"
            download(claim, config.audio_host, audio)
            if claim.get("pipeline_version") == "cpu-2":
                result = (
                    models.recording_audio_prep(audio)
                    if claim["stage"] == "AUDIO_PREP"
                    else models.recording_text_analysis(
                        audio, claim["speaker_id"], claim["speaker_intervals"]
                    )
                )
            elif claim["stage"] == "AUDIO_PREP":
                result = models.audio_prep(audio)
            else:
                result = models.text_analysis(audio, claim["speaker_id"])
        if not lost.is_set():
            state = invoke(*args, "complete", job_id, attempt=attempt, output=result)["state"]
    except Exception as error:
        print(json.dumps({"event": "analysis_error", "kind": type(error).__name__}), flush=True)
        if not lost.is_set():
            state = invoke(
                *args,
                "fail",
                job_id,
                attempt=attempt,
                retryable=not isinstance(error, InvalidSample),
            )["state"]
    finally:
        stop.set()
        thread.join()
    if state == "terminal":
        sqs.delete_message(QueueUrl=config.queue_url, ReceiptHandle=receipt)
        print(
            json.dumps(
                {
                    "stage": claim["stage"],
                    "state": state,
                    "seconds": round(time.monotonic() - started, 2),
                }
            ),
            flush=True,
        )
    return True


def instance_identity():
    request = Request(
        "http://169.254.169.254/latest/api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
    )
    with urlopen(request, timeout=2) as response:
        token = response.read().decode()
    request = Request(
        "http://169.254.169.254/latest/meta-data/instance-id",
        headers={"X-aws-ec2-metadata-token": token},
    )
    with urlopen(request, timeout=2) as response:
        return response.read().decode()


def run(sqs, service, args, models, instance_id):
    deadline = time.monotonic() + args.deadline_seconds
    idle = time.monotonic() + args.idle_seconds
    while time.monotonic() < deadline:
        try:
            messages = sqs.receive_message(
                QueueUrl=args.queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=10,
                VisibilityTimeout=150,
            ).get("Messages", [])
            for message in messages:
                if process(sqs, service, args, message, models):
                    idle = time.monotonic() + args.idle_seconds
            if not messages and time.monotonic() >= idle:
                if not args.control_function:
                    return
                result = invoke(
                    service, args.control_function, "idle", None, instance_id=instance_id
                )
                if result["state"] == "stop":
                    print(json.dumps({"event": "worker_exit", "reason": "idle"}), flush=True)
                    return
                idle = time.monotonic() + args.idle_seconds
        except Exception as error:
            print(json.dumps({"event": "worker_error", "kind": type(error).__name__}), flush=True)
            time.sleep(3)
    print(json.dumps({"event": "worker_exit", "reason": "deadline"}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-url", required=True)
    parser.add_argument("--worker-api", required=True)
    parser.add_argument("--audio-host", required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--control-function")
    parser.add_argument("--idle-seconds", type=int, default=60)
    parser.add_argument("--deadline-seconds", type=int, default=900)
    args = parser.parse_args()
    config = Config(connect_timeout=4, read_timeout=30, retries={"total_max_attempts": 2})
    sqs = boto3.client("sqs", config=config)
    service = boto3.client("lambda", config=config)
    instance_id = instance_identity() if args.control_function else None
    run(sqs, service, args, Models(args.models), instance_id)


if __name__ == "__main__":
    main()

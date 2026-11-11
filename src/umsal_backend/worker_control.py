"""Queue-driven capacity control. Invoked only by Scheduler and the CPU worker role."""

import json
import os
import re

import boto3
from botocore.config import Config

QUEUE_COUNTS = (
    "ApproximateNumberOfMessages",
    "ApproximateNumberOfMessagesNotVisible",
    "ApproximateNumberOfMessagesDelayed",
)


def reconcile(sqs, scaling, queue_url: str, group_name: str, event: dict) -> dict:
    operation = event.get("operation")
    instance_id = event.get("instance_id")
    if operation not in {"tick", "idle"} or (
        operation == "idle"
        and not (isinstance(instance_id, str) and re.fullmatch(r"i-[a-f0-9]{17}", instance_id))
    ):
        raise ValueError("invalid capacity request")
    groups = scaling.describe_auto_scaling_groups(AutoScalingGroupNames=[group_name])[
        "AutoScalingGroups"
    ]
    if len(groups) != 1 or groups[0]["MaxSize"] != 1 or groups[0]["MinSize"] != 0:
        raise RuntimeError("unexpected CPU capacity configuration")
    group = groups[0]
    if operation == "idle" and instance_id not in {i["InstanceId"] for i in group["Instances"]}:
        return {"state": "stop"}
    attributes = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=list(QUEUE_COUNTS))[
        "Attributes"
    ]
    # Count in-flight and delayed messages too. Queue observations can lag, so only
    # the worker's own idle report releases its scale-in protection.
    backlog = sum(int(attributes[name]) for name in QUEUE_COUNTS)
    desired = int(backlog > 0)
    if group["DesiredCapacity"] != desired:
        scaling.set_desired_capacity(
            AutoScalingGroupName=group_name, DesiredCapacity=desired, HonorCooldown=False
        )
    state = "keep_running"
    if operation == "idle" and not backlog:
        scaling.set_instance_protection(
            AutoScalingGroupName=group_name, InstanceIds=[instance_id], ProtectedFromScaleIn=False
        )
        state = "stop"
    print(
        json.dumps({"operation": operation, "backlog": backlog, "desired": desired, "state": state})
    )
    return {"state": state}


def handler(event, _context):
    config = Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 2})
    return reconcile(
        boto3.client("sqs", config=config),
        boto3.client("autoscaling", config=config),
        os.environ["UMSAL_QUEUE_URL"],
        os.environ["UMSAL_CPU_GROUP"],
        event,
    )

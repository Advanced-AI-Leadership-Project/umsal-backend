import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from umsal_backend import worker_control

INSTANCE = "i-" + "a" * 17


class Queue:
    def __init__(self, visible=0, inflight=0, delayed=0):
        self.counts = [visible, inflight, delayed]

    def get_queue_attributes(self, **kwargs):
        return {
            "Attributes": dict(zip(worker_control.QUEUE_COUNTS, map(str, self.counts), strict=True))
        }


class Scaling:
    def __init__(self):
        self.group = {
            "MinSize": 0,
            "MaxSize": 1,
            "DesiredCapacity": 0,
            "Instances": [{"InstanceId": INSTANCE, "ProtectedFromScaleIn": True}],
        }
        self.calls = []

    def describe_auto_scaling_groups(self, **kwargs):
        return {"AutoScalingGroups": [self.group]}

    def set_desired_capacity(self, **kwargs):
        self.group["DesiredCapacity"] = kwargs["DesiredCapacity"]
        self.calls.append(kwargs)

    def set_instance_protection(self, **kwargs):
        self.calls.append(kwargs)


def test_repeated_queue_ticks_do_not_launch_duplicate_capacity():
    queue, scaling = Queue(1), Scaling()
    for _ in range(2):
        worker_control.reconcile(queue, scaling, "queue", "cpu", {"operation": "tick"})
    assert scaling.calls == [
        {"AutoScalingGroupName": "cpu", "DesiredCapacity": 1, "HonorCooldown": False}
    ]


def test_empty_tick_keeps_busy_worker_protected_until_its_own_idle_report():
    queue, scaling = Queue(), Scaling()
    scaling.group["DesiredCapacity"] = 1
    worker_control.reconcile(queue, scaling, "queue", "cpu", {"operation": "tick"})
    assert scaling.group["DesiredCapacity"] == 0
    assert all("ProtectedFromScaleIn" not in call for call in scaling.calls)
    response = worker_control.reconcile(
        queue, scaling, "queue", "cpu", {"operation": "idle", "instance_id": INSTANCE}
    )
    assert response == {"state": "stop"}
    assert scaling.calls[-1] == {
        "AutoScalingGroupName": "cpu",
        "InstanceIds": [INSTANCE],
        "ProtectedFromScaleIn": False,
    }


@pytest.mark.parametrize("counts", [(1, 0, 0), (0, 1, 0), (0, 0, 1)])
def test_idle_report_with_pending_work_keeps_protection_and_capacity(counts):
    scaling = Scaling()
    response = worker_control.reconcile(
        Queue(*counts), scaling, "queue", "cpu", {"operation": "idle", "instance_id": INSTANCE}
    )
    assert response == {"state": "keep_running"}
    assert scaling.group["DesiredCapacity"] == 1
    assert all("ProtectedFromScaleIn" not in call for call in scaling.calls)


def test_an_instance_outside_the_group_cannot_change_capacity():
    scaling = Scaling()
    response = worker_control.reconcile(
        Queue(1), scaling, "queue", "cpu", {"operation": "idle", "instance_id": "i-" + "b" * 17}
    )
    assert response == {"state": "stop"} and not scaling.calls


def test_queue_read_failure_never_becomes_an_empty_queue():
    scaling = Scaling()
    scaling.group["DesiredCapacity"] = 1
    with pytest.raises(KeyError):
        worker_control.reconcile(
            SimpleNamespace(get_queue_attributes=lambda **kw: {"Attributes": {}}),
            scaling,
            "queue",
            "cpu",
            {"operation": "tick"},
        )
    assert not scaling.calls


@pytest.mark.parametrize("event", [{}, {"operation": "idle"}, {"operation": "delete"}])
def test_invalid_capacity_request_is_rejected(event):
    with pytest.raises(ValueError, match="invalid"):
        worker_control.reconcile(Queue(), Scaling(), "queue", "cpu", event)


def test_capacity_above_one_is_rejected():
    scaling = Scaling()
    scaling.group["MaxSize"] = 2
    with pytest.raises(RuntimeError, match="unexpected"):
        worker_control.reconcile(Queue(1), scaling, "queue", "cpu", {"operation": "tick"})
    assert not scaling.calls


def test_release_failure_propagates_instead_of_acknowledging_shutdown(monkeypatch):
    scaling = Scaling()

    def deny(**kwargs):
        raise RuntimeError("release unavailable")

    monkeypatch.setattr(scaling, "set_instance_protection", deny)
    with pytest.raises(RuntimeError, match="release unavailable"):
        worker_control.reconcile(
            Queue(), scaling, "queue", "cpu", {"operation": "idle", "instance_id": INSTANCE}
        )


def test_lambda_handler_uses_runtime_configuration(monkeypatch):
    scaling = Scaling()
    clients = {"sqs": Queue(1), "autoscaling": scaling}
    monkeypatch.setenv("UMSAL_QUEUE_URL", "queue")
    monkeypatch.setenv("UMSAL_CPU_GROUP", "cpu")
    monkeypatch.setattr(worker_control.boto3, "client", lambda service, **kw: clients[service])
    assert worker_control.handler({"operation": "tick"}, None) == {"state": "keep_running"}
    assert scaling.group["DesiredCapacity"] == 1


@pytest.fixture
def worker(monkeypatch):
    # Lifecycle tests do not load model weights or native ML libraries.
    monkeypatch.setitem(
        sys.modules, "models", SimpleNamespace(InvalidSample=ValueError, Models=object)
    )
    path = Path(__file__).resolve().parents[1] / "workers/cpu/run.py"
    spec = importlib.util.spec_from_file_location("umsal_cpu_worker_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_continues_when_work_arrives_at_the_idle_boundary(worker, monkeypatch):
    now = [0]
    responses = iter([{}, {}, {"Messages": [{"Body": "job"}]}, {}, {}])
    decisions = iter(["keep_running", "stop"])
    processed = []

    def receive(**kwargs):
        now[0] += 10
        return next(responses)

    def invoke(**kwargs):
        payload = json.loads(kwargs["Payload"])
        assert payload["instance_id"] == INSTANCE and payload["operation"] == "idle"
        return {"Payload": io.BytesIO(json.dumps({"state": next(decisions)}).encode())}

    monkeypatch.setattr(worker.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(worker, "process", lambda *args: processed.append(args[3]) or True)
    args = SimpleNamespace(
        queue_url="queue", control_function="control", idle_seconds=20, deadline_seconds=100
    )
    worker.run(
        SimpleNamespace(receive_message=receive),
        SimpleNamespace(invoke=invoke),
        args,
        object(),
        INSTANCE,
    )
    assert processed == [{"Body": "job"}]
    assert now[0] == 50


def test_worker_does_not_report_idle_after_sqs_errors(worker, monkeypatch):
    now = [0]

    def fail(**kwargs):
        now[0] += 10
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(worker.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
    args = SimpleNamespace(
        queue_url="queue", control_function="control", idle_seconds=10, deadline_seconds=20
    )
    worker.run(SimpleNamespace(receive_message=fail), SimpleNamespace(), args, object(), INSTANCE)
    assert now[0] == 20


@pytest.mark.parametrize("state", ["running", "stale"])
def test_native_inference_heartbeat_keeps_or_loses_lease(worker, monkeypatch, state):
    waiting = iter([False, True])
    lost = []
    visibility = []
    clients = {
        "lambda": SimpleNamespace(
            invoke=lambda **kwargs: {"Payload": io.BytesIO(json.dumps({"state": state}).encode())}
        ),
        "sqs": SimpleNamespace(
            change_message_visibility=lambda **kwargs: visibility.append(kwargs)
        ),
    }
    monkeypatch.setattr(worker.boto3, "client", lambda service, **kw: clients[service])
    worker.keep_lease(
        "queue",
        "api",
        "job",
        1,
        "receipt",
        SimpleNamespace(wait=lambda seconds: next(waiting)),
        SimpleNamespace(set=lambda: lost.append(True)),
    )
    assert bool(visibility) == (state == "running")
    assert bool(lost) == (state == "stale")

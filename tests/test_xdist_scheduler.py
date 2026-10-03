"""The pytest-xdist scheduler that keeps slow tests from queueing behind each other (tools/xdist_scheduler.py)."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("xdist")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import xdist_scheduler  # noqa: E402


class Node:
    def __init__(self, name):
        self.gateway = SimpleNamespace(id=name)
        self.shutting_down = False
        self.queue: list[int] = []

    def send_runtest_some(self, indices):
        self.queue.extend(indices)

    def shutdown(self):
        self.shutting_down = True


class Config:
    def __init__(self, workers):
        self.option = SimpleNamespace(tx=[f"{workers}*popen"], loadscopereorder=True)

    def getvalue(self, name):
        return getattr(self.option, name)


def run(collection, slow, workers=2, pick=lambda nodes: nodes[0], stock=False):
    """Drive the scheduler like pytest-xdist does: each step, one busy worker finishes the test it is running."""

    from xdist.scheduler import LoadGroupScheduling

    scheduler = LoadGroupScheduling(Config(workers)) if stock else xdist_scheduler.DurationScheduling(Config(workers), slow=slow)
    nodes = [Node(f"gw{i}") for i in range(workers)]
    for node in nodes:
        scheduler.add_node(node)
    for node in nodes:
        scheduler.add_node_collection(node, collection)
    scheduler.schedule()
    ran = {node.gateway.id: [] for node in nodes}
    held_at_once = 0
    while any(node.queue for node in nodes):
        held_at_once = max([held_at_once] + [sum(collection[i] in slow for i in node.queue) for node in nodes])
        node = pick([n for n in nodes if n.queue])
        index = node.queue.pop(0)
        ran[node.gateway.id].append(collection[index])
        scheduler.mark_test_complete(node, index)
    assert all(node.shutting_down for node in nodes)
    return ran, held_at_once


def test_a_worker_never_holds_two_slow_tests_and_every_test_runs_once():
    collection = ["t.py::fast1", "t.py::fast2", "t.py::fast3", "t.py::slow_a", "t.py::slow_b", "t.py::slow_c", "t.py::fast4"]
    slow = {"t.py::slow_a": 100.0, "t.py::slow_b": 90.0, "t.py::slow_c": 80.0}
    ran, held = run(collection, slow)
    assert held == 1
    assert sorted(sum(ran.values(), [])) == sorted(collection)
    _, held = run(collection, slow, stock=True)
    assert held == 2  # pytest-xdist's own loadgroup queues a slow test behind another on one worker


def test_the_order_tests_conftest_chose_is_kept_and_groups_stay_together():
    collection = ["t.py::a", "t.py::b@g", "t.py::c@g", "t.py::d", "t.py::e@g"]
    ran, _ = run(collection, {}, workers=1)
    # loadgroup would move the group (three tests) to the front; this scheduler keeps the collection order
    assert ran["gw0"] == ["t.py::a", "t.py::b@g", "t.py::c@g", "t.py::e@g", "t.py::d"]
    ran, _ = run(collection, {}, workers=2, pick=lambda nodes: nodes[-1])
    together = [worker for worker, tests in ran.items() if "t.py::b@g" in tests]
    assert all(test in ran[together[0]] for test in ("t.py::b@g", "t.py::c@g", "t.py::e@g"))

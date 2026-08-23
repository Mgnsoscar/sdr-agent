"""Panic ('RF off NOW') must stop a task caught mid-launch (STARTING), not only those
already RUNNING — otherwise a task launching at the instant of the panic could slip
through and go on air a moment later."""
import asyncio
from types import SimpleNamespace

from agent.recovery import panic_stop


class FakeManager:
    def __init__(self, statuses):
        self._statuses = statuses
        self.stopped = []

    def all_statuses(self):
        return self._statuses

    async def stop(self, name, source=""):
        self.stopped.append(name)

    async def stop_oneshots(self, run_id):
        pass


class FakeRunner:
    async def abort_all_active(self, reason):
        return []


class FakeScheduler:
    async def cancel_all_active(self, reason):
        return []


def test_panic_stops_running_and_starting():
    mgr = FakeManager([
        SimpleNamespace(name="live", state="running"),
        SimpleNamespace(name="launching", state="starting"),
        SimpleNamespace(name="idle", state="stopped"),
        SimpleNamespace(name="dead", state="crashed"),
    ])
    res = asyncio.run(panic_stop(mgr, FakeScheduler(), FakeRunner(), "unit-x"))
    assert set(mgr.stopped) == {"live", "launching"}     # starting is included
    assert "idle" not in mgr.stopped and "dead" not in mgr.stopped
    assert set(res.tasks_stopped) == {"live", "launching"}

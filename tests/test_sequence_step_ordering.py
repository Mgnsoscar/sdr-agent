"""Co-timed step ordering in the sequence runner.

When a step that turns the RF output gate ON and a step that SETS POWER fire at the SAME
instant (the motivating case: a power ramp anchored at on-air, whose first point is co-timed
with the RF-on tune), the power must be applied FIRST so the gate opens at the intended level
instead of flashing the stale standing power for one fire. See SequenceRunner._co_time_rank.
"""
from datetime import datetime, timezone
from pathlib import Path

from agent.models import StepFire, TaskConfig
from agent.process_manager import ProcessManager
from agent.sequence_runner import SequenceRunner

REPO_ROOT = str(Path(__file__).resolve().parents[1])

# A calibrated-style transmit script: an absolute --power plus an --rf on/off output gate.
# No name= overrides, so the dests derive from the flags ("power", "rf") — exactly what the
# client keys a step's params on, so the gate dest and the step's param key agree.
GATED_SCRIPT = '''\
from paramkit import Script
s = (Script("tx")
     .number("--power", unit="dBm", default=-50.0, live=True)
     .choice("--rf", options=["on", "off"], default="on", live=True))
args = s.parse()
'''


def _runner(tmp_path):
    script = tmp_path / "tx.py"
    script.write_text(GATED_SCRIPT)
    tasks = {"tx": TaskConfig(name="tx", command=["python3", str(script)],
                              working_dir=str(tmp_path), env={"PYTHONPATH": REPO_ROOT})}
    mgr = ProcessManager(tasks, tmp_path, "unit-a")
    return SequenceRunner(mgr, "unit-a", tmp_path / "seq.json",
                          tmp_path / "runs.json", tmp_path)


T = "2026-09-09T09:00:00+00:00"


def _tune(**params):
    return StepFire(anchor="start", offset_s=0.0, action="tune", task_name="tx",
                    fire_at=T, params=params)


def test_power_step_ranks_before_rf_on(tmp_path):
    r = _runner(tmp_path)
    rf_on = _tune(rf="on")
    ramp_pt = _tune(power=-90.0)                       # a power ramp point
    assert r._co_time_rank(ramp_pt) == 0               # power first
    assert r._co_time_rank(rf_on) == 2                 # rf-on last


def test_co_timed_sort_puts_power_before_rf_on(tmp_path):
    r = _runner(tmp_path)
    rf_on = _tune(rf="on")
    ramp_pt = _tune(power=-90.0)
    # Same fire instant, rf-on listed first (as it would be in the step list) — the sort must
    # still fire the power point first.
    due = [rf_on, ramp_pt]
    due.sort(key=lambda f: (f.fire_at, r._co_time_rank(f)))
    assert [f.params for f in due] == [{"power": -90.0}, {"rf": "on"}]


def test_rf_off_launch_is_not_rf_on_and_power_launch_ranks_first(tmp_path):
    r = _runner(tmp_path)
    # A muted pre-roll launch carries --rf off (NOT an rf-on step) and sets --power.
    launch = StepFire(anchor="start", offset_s=-1.0, action="start", task_name="tx",
                      fire_at=T, args=["--power", "-50", "--rf", "off"], replace_args=True)
    assert r._co_time_rank(launch) == 0                # sets power, gate is OFF → not rf-on
    # A launch that turns the gate ON ranks last.
    on_launch = StepFire(anchor="start", offset_s=0.0, action="start", task_name="tx",
                         fire_at=T, args=["--power", "-50", "--rf", "on"], replace_args=True)
    assert r._co_time_rank(on_launch) == 2


def test_neutral_step_ranks_between(tmp_path):
    r = _runner(tmp_path)
    neutral = _tune(sidelobes=5)                       # neither power nor the gate
    assert r._co_time_rank(neutral) == 1

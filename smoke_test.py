#!/usr/bin/env python3
"""
End-to-end smoke test for the SDR agent's sequence + recovery layer.
Run from your laptop:  python3 smoke_test.py http://hostname.local:8765

It will (against ONE unit):
  1. check health, clock sync, and SDR
  2. register a throwaway test sequence
  3. arm it ~15s out with a short on-air window
  4. watch it go armed -> running -> completed
  5. arm another, then EXTEND the on-air stop while running
  6. arm another, then PANIC stop everything
  7. clean up the test sequence

Nothing here touches your real tasks beyond starting/stopping whatever the
sequence references, so point STEP_TASK at a harmless task (default: rx_flowgraph).
"""
import sys
import time
import json
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://hostname.local:8765"
API_KEY = sys.argv[2] if len(sys.argv) > 2 else ""   # optional

# A task that already exists on the unit and is safe to start/stop repeatedly.
STEP_TASK = "rx_flowgraph"

HEADERS = {"Content-Type": "application/json"}
if API_KEY:
    HEADERS["X-API-Key"] = API_KEY


def call(method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            txt = r.read().decode()
            return r.status, (json.loads(txt) if txt else None)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def utc_in(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def banner(msg):
    print(f"\n{'='*60}\n{msg}\n{'='*60}")


def main():
    banner(f"SDR AGENT SMOKE TEST → {BASE}")

    # 1. Health / clock / SDR
    banner("1. Health, clock sync, SDR")
    _, h = call("GET", "/system")
    print(f"  unit={h['unit_id']}  temp={h.get('cpu_temp_c')}°C  "
          f"clock_synced={h.get('clock_synced')}  utc_now={h.get('utc_now')}")
    _, sdr = call("GET", "/sdr")
    print(f"  SDR detected={sdr['detected']} count={sdr['device_count']}")

    # 2. Register a test sequence
    banner("2. Register throwaway test sequence")
    seq_body = {
        "name": "SMOKE_TEST_SEQ",
        "description": "delete me",
        "steps": [
            {"anchor": "start", "offset_s": -5, "action": "start", "task_name": STEP_TASK},
            {"anchor": "stop",  "offset_s": 0,  "action": "stop",  "task_name": STEP_TASK},
        ],
    }
    st, seq = call("POST", "/sequences", seq_body)
    if st != 200:
        print(f"  FAILED to create sequence: {seq}")
        return
    seq_id = seq["id"]
    print(f"  created {seq_id}")

    # 3. Arm ~15s out, 10s on-air window
    banner("3. Arm 15s out, 10s on-air window — watch it fire")
    arm_body = {"on_air_at": utc_in(15), "on_air_duration_s": 10, "note": "smoke"}
    st, run = call("POST", f"/sequences/{seq_id}/arm", arm_body)
    if st != 200:
        print(f"  FAILED to arm: {run}")
        call("DELETE", f"/sequences/{seq_id}")
        return
    run_id = run["id"]
    print(f"  armed {run_id}: on-air {run['on_air_at']} → {run['on_air_end']}")
    print("  watching (warm-up fires at -5s, so ~10s from now)...")

    last = None
    for _ in range(40):   # up to ~40s
        _, r = call("GET", f"/sequence-runs/{run_id}")
        if r["state"] != last:
            print(f"    [{datetime.now().strftime('%H:%M:%S')}] state={r['state']}")
            last = r["state"]
        if r["state"] in ("completed", "aborted", "cancelled"):
            break
        time.sleep(1)
    print(f"  final state: {last}")

    # 4. Arm + EXTEND while running
    banner("4. Arm, then EXTEND on-air stop while running")
    st, run = call("POST", f"/sequences/{seq_id}/arm",
                   {"on_air_at": utc_in(15), "on_air_duration_s": 10})
    if st != 200:
        print(f"  FAILED to arm (status {st}): {run}")
        call("DELETE", f"/sequences/{seq_id}")
        return
    run_id = run["id"]
    print(f"  armed {run_id}: on-air {run['on_air_at']} → {run['on_air_end']}")
    # wait until it's running
    for _ in range(25):
        _, r = call("GET", f"/sequence-runs/{run_id}")
        if r["state"] == "running":
            break
        time.sleep(1)
    new_end = utc_in(20)
    st, r = call("PATCH", f"/sequence-runs/{run_id}", {"on_air_end": new_end})
    print(f"  extend → status {st}, new on-air end: {r.get('on_air_end')}")
    # let it finish
    for _ in range(30):
        _, r = call("GET", f"/sequence-runs/{run_id}")
        if r["state"] in ("completed", "aborted", "cancelled"):
            break
        time.sleep(1)
    print(f"  final state: {r['state']}")

    # 5. Arm + PANIC
    banner("5. Arm, then PANIC stop")
    st, run = call("POST", f"/sequences/{seq_id}/arm",
                   {"on_air_at": utc_in(15), "on_air_duration_s": 30})
    if st != 200:
        print(f"  FAILED to arm (status {st}): {run}")
        call("DELETE", f"/sequences/{seq_id}")
        return
    run_id = run["id"]
    print(f"  armed {run_id}")
    for _ in range(25):
        _, r = call("GET", f"/sequence-runs/{run_id}")
        if r["state"] == "running":
            break
        time.sleep(1)
    print(f"  state before panic: {r['state']}")
    st, p = call("POST", "/panic")
    print(f"  PANIC → status {st}: tasks_stopped={p.get('tasks_stopped')} "
          f"runs_aborted={p.get('runs_aborted')} events_cancelled={p.get('events_cancelled')}")
    _, r = call("GET", f"/sequence-runs/{run_id}")
    print(f"  run state after panic: {r['state']} (expect aborted)")

    # 6. Cleanup
    banner("6. Cleanup")
    st, _ = call("DELETE", f"/sequences/{seq_id}")
    print(f"  deleted test sequence: status {st}")

    banner("SMOKE TEST DONE")


if __name__ == "__main__":
    main()
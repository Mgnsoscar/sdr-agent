#!/usr/bin/env python3
"""Author the local test unit's sample sequences THROUGH THE CLIENT and write the seed file.

`deploy/run_local.sh` seeds one RF-gated broadcast sequence per mock signal (PRN / chirp / CW)
so each is armable end to end with no hardware. Rather than hand-writing that JSON, this builds
each sequence with the SAME client code the timeline editor's Save uses — the client's timeline
item model (`ui.timeline_model.BarItem`/`RunItem`), its item→step flattener (`items_to_steps`,
wrapped by the hold precompute exactly as `TimelineEditor.steps()`), and the client's `api.models`
`SequenceStep` / `Sequence` — then snapshots the result to `deploy/sample-calibration/sequences.json`.
The agent loads that file verbatim (ids preserved), so a fresh session shows exactly what the client
produced, and the file regenerates deterministically.

The shape (owner's request): the transmit task launches 1 s BEFORE on-air with `--rf off` (muted
pre-roll), a TUNE turns RF ON at the on-air anchor and OFF again at the off-air anchor, and the task
stops 1 s AFTER off-air. `--power` is in each signal's calibrated quantity (dBm for CW; dBm/Hz base
density for the PRN/chirp), mid-range for the seeded calibration; the agent auto-commands the
`atten_set` attenuator to realise the requested delivered power (no explicit attenuator step needed).

Usage:
    python3 deploy/make_sample_sequences.py                 # find sibling ../sdr-client, write the seed
    python3 deploy/make_sample_sequences.py --client /path/to/sdr-client --out /tmp/seqs.json

Needs a sibling `sdr-client` checkout (its authoring modules import Qt-free). No agent or radio.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# One RF-gated broadcast per signal. `args` are the launch values an operator would enter on the
# duration (bar) step's parameter form — flag/value pairs the client passes straight through.
SEQUENCES = [
    dict(
        id="seq-mock-prn", task="mock_prn",
        name="Mock PRN (GPS C/A) — RF-gated",
        description="Launch the GPS C/A mock 1 s before on-air with RF off; RF on at on-air, "
                    "off at off-air; stop 1 s after. No hardware.",
        args=["--prn", "1", "--freq", "1575.42", "--sidelobes", "5", "--power", "-150", "--rf", "off"],
    ),
    dict(
        id="seq-mock-chirp", task="mock_chirp",
        name="Mock chirp / sweep — RF-gated",
        description="Launch the FM-chirp mock 1 s before on-air with RF off; RF on at on-air, "
                    "off at off-air; stop 1 s after. No hardware.",
        args=["--band-mode", "center_bw", "--freq", "1575.42", "--bw", "20", "--rate", "200",
              "--power", "-180", "--rf", "off"],
    ),
    dict(
        id="seq-mock-cw", task="mock_cw",
        name="Mock CW tone — RF-gated",
        description="Launch the CW mock 1 s before on-air with RF off; RF on at on-air, "
                    "off at off-air; stop 1 s after. No hardware.",
        args=["--freq", "1575420000", "--power", "-60", "--rf", "off"],
    ),
]

LEAD_IN_S = 1.0    # task starts this long BEFORE on-air (RF off, muted pre-roll)
TAIL_S = 1.0       # task stops this long AFTER off-air


def build(tlm, m):
    """Build the sequences with the client's authoring code and return api.models.Sequence list."""
    out = []
    for spec in SEQUENCES:
        task = spec["task"]
        # The client's canvas items: a duration bar (start 1 s before on-air, stop 1 s after
        # off-air) plus two tunes toggling --rf at the on-air / off-air anchors.
        items = [
            tlm.BarItem(task_name=task, args=list(spec["args"]), replace_args=True,
                        start_offset=-LEAD_IN_S, stop_offset=TAIL_S),
            tlm.RunItem(task_name=task, action="tune", anchor="start", offset=0.0,
                        params={"rf": "on"}),
            tlm.RunItem(task_name=task, action="tune", anchor="stop", offset=0.0,
                        params={"rf": "off"}),
        ]
        # Exactly TimelineEditor.steps(): the hold precompute (a no-op with no held control view)
        # then the item→step flattener, mapped to api.models SequenceStep.
        try:
            held = tlm.hold_control_quantity(items, lambda *a, **k: None)
        except Exception:                       # noqa: BLE001 — mirror steps()'s fallback
            held = items
        steps = []
        for d in tlm.items_to_steps(held):
            ramp = d.get("ramp")
            steps.append(m.SequenceStep(
                anchor=d["anchor"], offset_s=d["offset_s"], offset_end_s=d.get("offset_end_s"),
                action=m.StepAction(d["action"]), task_name=d["task_name"],
                args=list(d.get("args") or []), replace_args=bool(d.get("replace_args", False)),
                params=dict(d.get("params") or {}),
                ramp=m.RampSpec(**ramp) if ramp else None,
                power_view=d.get("power_view"), power_hold_dest=d.get("power_hold_dest")))
        out.append(m.Sequence(id=spec["id"], name=spec["name"], description=spec["description"],
                              steps=steps, types=["broadcaster"]))
    return out


def main() -> int:
    here = Path(__file__).resolve().parent            # sdr-agent/deploy
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--client", default=str(here.parent.parent / "sdr-client"),
                    help="path to the sdr-client checkout (default: sibling ../sdr-client)")
    ap.add_argument("--out", default=str(here / "sample-calibration" / "sequences.json"),
                    help="output seed file (default: deploy/sample-calibration/sequences.json)")
    args = ap.parse_args()

    client = Path(args.client)
    if not (client / "ui" / "timeline_model.py").is_file():
        print(f"!! no sdr-client checkout at {client} (need ui/timeline_model.py) — "
              f"pass --client /path/to/sdr-client", file=sys.stderr)
        return 2
    sys.path.insert(0, str(client))
    import ui.timeline_model as tlm          # noqa: E402 — client authoring model (Qt-free)
    from api import models as m              # noqa: E402 — client data models

    seqs = build(tlm, m)
    doc = {"sequences": [s.model_dump(mode="json") for s in seqs]}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out} — {len(seqs)} client-authored sequence(s): "
          + ", ".join(s.id for s in seqs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

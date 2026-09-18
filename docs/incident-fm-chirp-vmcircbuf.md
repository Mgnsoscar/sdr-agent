# Incident report — silent transmit stop during a field test

**Date of incident:** September 2026 field test *(fill exact date)*
**Prepared:** 2026-09-18
**Severity:** High (visible test disruption in front of stakeholders; no safety/hardware damage)
**Status:** Root cause characterized; fix designed and phased (see
`docs/rf-fault-recovery.md`). Awaiting go-ahead to build.

---

## What happened

During a live test we ran a **power sweep** — one signal ramping its output power up and then down
over 23 minutes on a single unit. A few minutes in, we noticed on the spectrum analyser that **the
unit had stopped transmitting**, even though the control software still showed the task as
**"running."** Recovering meant stopping the run, building a new plan by hand, estimating where in
the 23-minute ramp to restart, and re-running — a slow, visibly improvised recovery.

## Operational impact

- ~10–15 minutes of test time lost to manual recovery.
- Loss of confidence in front of attending stakeholders.
- The failure was **silent**: nothing alerted the operator; it was caught only by eye.

## Root cause

Two separate issues combined. One is the *technical* fault; the others are *why it hurt so much*.

1. **The technical fault — a shared-memory buffer allocation failure inside the radio software.**
   The signal-processing engine (GNU Radio) moves data through small in-memory buffers held in a
   RAM area called `/dev/shm`. When the transmit program **started up**, it occasionally could not
   allocate one of these buffers and **halted its radio output**, while the surrounding program kept
   running. This is a **startup-time, intermittent** failure — the *same* signal had run flawlessly
   for three hours the day before, and the fault has occurred ~10–12 times, always at startup. It is
   a known class of resource-availability issue in the radio framework, **not a logic bug in our
   code**, and it is **independent of the power ramp itself** (the ramp changes no buffers). The unit
   had been **power-cycled and idle** before the test with **no prior task run**, so it is a
   *transient allocation failure at first launch on a clean system* — not accumulated leftovers. Our
   configuration makes it more likely than it needs to be (the radio framework's buffer backend is
   not pinned on these units), which we can harden; the exact trigger will be **confirmed from the
   unit's own logs and a resource snapshot**, captured automatically once detection ships.

2. **We could not *see* it.** The software judged a task "running" purely by whether its program was
   still alive — not by whether radio was actually going out. A halted-but-alive program therefore
   showed a green "running" status indefinitely.

3. **We could not *recover* cleanly.** There was no way to restart the interrupted run back onto its
   original schedule; recovery required rebuilding the plan and guessing the ramp position.

## The fix (designed, phased)

- **Prevent the fault** (cheap, first): raise the relevant Linux limits on the unit, keep the buffer
  area clean between runs, shut transmit tasks down gracefully, and — for this class of intermittent
  startup failure — **retry automatically**, which succeeds on the first attempt the large majority
  of the time. Because transmit tasks start with a **warm-up lead-in before going on-air**, an
  automatic startup retry typically finishes warming up *before broadcast time* — so the failure
  becomes **invisible to the test**, with at most a benign notice to the operator (a loud alarm is
  raised only if a retry genuinely cannot make the scheduled on-air time).
- **Detect it in seconds:** the transmit program now reports a halted radio, and the unit software
  raises a **loud, unmissable alarm** (sound, on-screen flash, notification) instead of a misleading
  "running" status. It also **captures the unit's resource state at the moment of failure**, so the
  *next* occurrence names its own cause instantly.
- **Recover in one action:** a single **"Restart & re-sync"** operation brings the signal back onto
  its *original* schedule at the *correct* point — computed automatically, not eyeballed — with
  configurable behaviour: fully automatic for simple long runs, or one-click-with-confirmation for
  complex multi-unit tests (where a failed unit can rejoin its still-running peers).

## Why we are confident this closes the gap

The recovery path reuses machinery the system already has (the run's recorded schedule and every
ramp level are stored as absolute timestamps), so re-synchronisation is deterministic. The detection
converts a previously invisible failure into an alarm plus a recorded diagnostic. And the
preventive steps address the underlying resource issue directly. The remaining unknown — the exact
low-level trigger — is resolved by one log retrieval from the affected unit and is prevented in the
meantime regardless of which specific mechanism it was.

## Immediate action items

1. Retrieve the affected unit's archived transmit log and the buffer-area status
   (`/dev/shm` usage, memory-map limit) to confirm the exact trigger.
2. Apply the preventive limit/hygiene changes to the unit image (low risk, high value).
3. Build the detection + alarm + auto-recovery (phased; see the engineering design).

*Engineering detail and the full design: `docs/rf-fault-recovery.md`.*

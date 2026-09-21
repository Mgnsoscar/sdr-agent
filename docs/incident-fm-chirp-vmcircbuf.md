# Incident report — silent transmit during a field test

**Date of incident:** 2026-09-18, 09:10 UTC (unit `broadcaster-1`, Raspberry Pi 5 + B206 mini, agent 1.27.2)
**Prepared:** 2026-09-18 · **Revised:** 2026-09-21 — root cause confirmed from the unit's own logs
and a controlled reproduction. **This revision replaces the 18 September version, whose root-cause
section was wrong.**
**Severity:** High (visible test disruption in front of stakeholders; no safety/hardware damage)
**Status:** Root cause confirmed. The software change that removes it shipped in agent 1.31.1
(current release 1.36.0); the affected unit still runs 1.27.2 and must be updated. Detection, alarm
and one-action restart (built 18–20 September) stay in place. Two further hardenings are
recommended and not yet scheduled (see *Follow-ups*).

---

## What happened

During a live test we ran a **power sweep** — one signal ramping its output power up and then down
over 23 minutes on a single unit. The unit had been rebooted before the test and this was its first
transmission of the day. A few minutes in, the spectrum analyser showed **no signal from the unit**,
while the control software showed the task as **"running"** and the sweep advancing on schedule.
We now know the unit **never transmitted at all during that run**: its radio output was muted from
launch to the operator's abort nine minutes later. Recovery was a manual stop, a hand-built plan
and an estimated restart position. The second attempt, minutes later, worked.

## Operational impact

- ~10–15 minutes of test time lost to manual recovery.
- Loss of confidence in front of attending stakeholders.
- The failure was **silent**: nothing alerted the operator; it was caught only by eye.

## Root cause (confirmed)

**A half-second timing race at launch, on the first launch after a reboot.** Nothing in the radio
software failed.

1. A transmit task is deliberately started **muted, 10 seconds before its on-air time**, so it can
   warm up and then be switched on exactly at on-air. On this unit, the **first launch after a
   reboot took about 10.5 seconds** to become ready (loading the radio's firmware image over USB plus
   the program's own start-up). Later launches take a few seconds.
2. At on-air time the control software sent the task its first two commands — the ramp's first power
   level and **"RF on"** — about half a second **before the task had opened its command channel**.
3. The agent version on the unit (1.27.2) **discarded a command sent to a task that was not yet
   listening, and still recorded the command in the run log as done.** Nothing was shown to the
   operator.
4. Every later ramp command *was* received, but the task keeps its radio muted until it is told
   "RF on", so it climbed the whole ramp in silence while the log showed a normal run.

**How it was confirmed.** The unit's archived logs show the task's start-up banner being copied
into the run log *after* the on-air commands had been sent, i.e. the task was still starting when the
commands went out. The line that the first report took to be the fault (`vmcircbuf_prefs::get
:info: … failed to open`) is an **informational** message that this unit prints on **every** launch;
it appears in five successful runs. The unit's shared-memory readouts show no pressure at all
(`/dev/shm` 1% used, no leaked segments, memory-map limit 16× our own setting). Finally the owner
**reproduced it at will**: reboot the unit → schedule the same plan → identical silent failure;
repeat without a reboot → normal transmission.

**What the 18 September report got wrong.** It read the informational log line as a shared-memory
allocation error and built the "prevention" work around that. Those changes (raised limits, buffer
hygiene, a pinned buffer backend) are harmless and stay, but **they were not the fix**.

## Why it was invisible

- **"Running" meant "the program is alive"**, not "the radio is transmitting".
- **Commands were recorded as sent, not as applied**, and a command sent too early was dropped
  without a trace in the run log.
- **The radio's own log was suppressed** (the transmit program silences it), so the slow image load
  left no record, and **the unit's system journal was not persistent**, so the agent's only record of
  the dropped commands was lost at the next reboot.

## The fix

**Already shipped (agent 1.27.4 → 1.36.0; the unit must be updated):**

- **The dropped command cannot recur.** Since 1.31.1 a command sent before a task is listening is
  **held and delivered as soon as the task is ready** (for up to three minutes), and a command that
  still fails is written into the run log as a failure. This change was made during the September
  review for a related case (a restarted task) and covers this incident by construction; it should be
  verified on the unit with the reboot test after the update.
- **The slow first launch is largely removed.** Since 1.27.4 the unit loads the radio's firmware image
  at boot, so the first transmission of the day no longer pays for it.
- **A silent radio is now detected and alarmed.** The transmit program reports when it is
  transmitting and when its radio halts; the unit raises a loud alarm instead of a green "running",
  and records the radio's own log and the machine's state at the moment of failure.
- **Recovery is one action.** "Restart & re-sync" brings a run back onto its original schedule at the
  computed ramp level, manually or automatically.

**Follow-ups recommended, not yet scheduled** (owner decision, 2026-09-21):

1. **Verify, don't trust:** after an "RF on" command, read back from the task whether it was applied,
   and alarm if not. This closes the exact blind spot this incident lived in.
2. **Check the lead-in at arm time:** record each task's measured start-up time on the unit and warn
   when a plan's warm-up lead-in is shorter than it; lengthen the default lead-in.
3. **Persistent system logs** on the units, so the next diagnosis does not depend on inference.

## Action items

1. **Update every unit's agent to 1.36.0** (the affected unit runs 1.27.2), then redeploy the script
   library. Re-run the reboot test on `broadcaster-1` to confirm.
2. Enable a persistent journal on the units (one command; to be added to provisioning).
3. Schedule follow-ups 1 and 2 above.

*Engineering detail: `docs/rf-fault-recovery.md` §14k (the confirmed root cause and the evidence),
§3.7 (the first-launch cost, anticipated in the original design), §14f #4 (the fix).*

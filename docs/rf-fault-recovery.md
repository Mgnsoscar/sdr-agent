# RF-fault detection & sequence recovery — design

**Status:** DESIGN (approved in principle; build pending owner go-ahead)
**Author:** engineering, with the unit owner
**Date:** 2026-09-18
**Branch:** `claude/system-familiarization-f5mezz` (all three repos)
**Scope:** cross-repo — `sdr-agent` (runtime + detection), `sdr-client` (alerting + recovery UI),
`sdr-scripts` (fault self-report + fast-warm restart)

> Companion: `docs/incident-fm-chirp-vmcircbuf.md` — the one-page incident report for
> non-engineering stakeholders. This document is the engineering spec.

---

## 1. TL;DR

During a field test a **power sweep** (a `--power` ramp up then down over 23 minutes on one unit)
**silently stopped radiating** a few minutes in. The transmit script hit a GNU Radio `vmcircbuf`
(shared-memory buffer) error but **did not exit**, so the agent kept showing the task as *running*
and the operator only noticed by watching the spectrum. Recovery meant **stopping everything,
hand-building a new plan, and eyeballing where in the ramp to restart** — in front of stakeholders.

Two independent gaps turned a recoverable glitch into a bad afternoon:

1. **The agent cannot see a dead-but-alive radio.** Task state is derived *only* from OS process
   exit. A halted-but-alive flowgraph produces no exit, no event, and a green "running" pill.
2. **There is no "restart onto the original schedule" path.** Recovery is manual and imprecise.

This design closes both, and pins down the underlying `vmcircbuf` cause enough to prevent it:

- **Detect** the fault (script self-report + agent watchdog), **alarm loudly**, **stop the dead
  task cleanly while keeping its logs**, and **snapshot the machine's resource state** so the next
  occurrence is self-diagnosing.
- **Recover** in one action that re-syncs to the *original* absolute schedule — the ramp level at
  resume is **computed from the run, not eyeballed** — with per-task / per-sequence / per-plan
  policy for auto vs. operator-confirmed, and a resync-vs-replay choice.
- **Prevent** recurrence: raise the shared-memory ceilings, keep `/dev/shm`/IPC clean around the
  task lifecycle, prefer graceful shutdown, and (optionally) pin a known-good GR buffer backend.
- **Speed recovery** with an optional per-signal IQ-buffer cache (the L1C/L2C 30 s warm-up).

Everything is capability-gated and respects the `argspec.py`/`ramp.py` byte-identical drift guard.

---

## 2. The incident

- **Signal:** `fm_chirp_tx.py` (the FM chirp / sweep), Raspberry Pi 5, single unit, **no other
  tasks** running.
- **What was ramped:** `--power` and the `--rf` on/off gate **only** — no bandwidth/shape change.
- **Symptom:** a `vm circ buff …` line in the task log; **the script kept running**, the task
  showed *running*, the SDR emitted nothing. The operator noticed ~5 min in by the spectrum.
- **When it failed:** at **flowgraph startup** (confirmed by the owner). It is **intermittent** —
  the *same* signal ran cleanly for **3 hours** the day before, and this class of failure has
  occurred before.
- **Recovery (as it happened):** stop the sequence → author a new plan → estimate the ramp
  position → re-run. Slow, manual, and visibly improvised.

---

## 3. Root cause of the `vmcircbuf` failure

### 3.1 What `vmcircbuf` is

GNU Radio moves samples between blocks through **virtual-memory circular buffers**. Each is a
physical buffer **mapped twice into adjacent address space** so reads/writes wrap seamlessly. On
Linux the default backend (`vmcircbuf_mmap_shm_open`) backs each buffer with a POSIX shared-memory
object — a file in **`/dev/shm`** — mapped twice. Every such buffer therefore costs `/dev/shm`
space **and** entries in the process's memory map (`vm.max_map_count`). A `vmcircbuf` *error* is one
of these allocations failing. **These buffers are allocated at flowgraph construction and at every
reconfigure — never while a steady flowgraph runs.**

### 3.2 The two facts that reframe our case

- **A `--power` ramp never reconfigures the flowgraph.** In every RPi script, `--power`/`--gain` →
  `usrp.set_gain()` / `multiply_const_cc.set_k()` (instant, no buffer touch). Only a *shape/filter*
  change (`--sidelobes`) does `tb.lock()/unlock()` + `set_data()`, which rebuilds buffers. Our
  incident was power-only, so **the ramp allocated nothing** — the failure was at **startup**.
- **fm_chirp stages nothing in `/dev/shm`.** It streams its waveform from RAM via
  `blocks.vector_source_c` (not `file_source`) — the same fix the GPS scripts adopted. So the
  failing buffer was **GNU Radio's own inter-block `vmcircbuf` in `/dev/shm`**, independent of any
  IQ data we stage. (Several Galileo scripts *do* stage 32–33 MB IQ files into `/dev/shm`; fm_chirp
  does not, which is why staging location is not the culprit here.)

### 3.3 The failure signature and the honest verdict

Startup-time, **intermittent**, clears on a fresh day, recurs across sessions → **an occasionally
unavailable resource at flowgraph construction**, not a deterministic bug and not the ramp.

Candidate mechanisms (need the error string to disambiguate):

| # | Mechanism | Fits? | Confirm with |
|---|-----------|-------|--------------|
| A | `/dev/shm` space momentarily low at launch | plausible | `df -h /dev/shm` at fault |
| B | Leaky **`sysv_shm`** fallback backend: hard-killed prior runs orphan System V segments (freed only by `IPC_RMID`/reboot), hitting the `SHMMNI` segment cap → intermittent startup failure that a reboot clears | plausible **iff** that backend is in use | `ipcs -m`; the error string names the backend |
| C | `vm.max_map_count` / `ulimit -n` ceiling at construction | less likely for one small flowgraph | `/proc/sys/vm/max_map_count`, `ulimit -n` |
| D | Startup **race** with a prior run tearing down (shm/USB not fully released) | possible after a stop-then-start | timing in the log |

Note the default `mmap_shm_open` backend **`shm_unlink`s each object immediately**, so it is
self-cleaning on process death — **simple orphan build-up is unlikely with that backend**. Orphan
accumulation (mechanism B) is real only if a leakier backend (`sysv_shm`) is selected. **We cannot
pin the exact mechanism without the error line** — the log was not retrievable at design time.

**This uncertainty is itself the point:** the reason we cannot root-cause it now is the same reason
we are building detection — the error scrolled past unread and nothing captured the machine's state
at that instant. Phase 1 below makes the **next** occurrence self-diagnosing.

### 3.4 Prevention (safe regardless of which mechanism)

1. **Raise the ceilings** on the unit image: `vm.max_map_count` (e.g. → 262144), confirm `/dev/shm`
   is amply sized (Pi 5 default tmpfs is ~50 % RAM — verify it is not a small custom mount), and a
   generous `ulimit -n` for the agent's children.
2. **Graceful shutdown first.** Stop a transmit task with **SIGTERM + a grace period**; `SIGKILL`
   only as a last resort, and always **sweep stale shm/IPC afterwards**. (The Phase-1 done-watcher
   makes clean exits the norm, which is itself preventative.)
3. **Agent `/dev/shm`/IPC hygiene around the task lifecycle** — a **confirmed, concrete gap** (§3.5),
   not just a precaution: before launching a transmit task, and after a hard kill, remove staged
   shared-memory artifacts whose owning PID is dead (`/dev/shm/gal_*`/`gr-*`, and if `sysv_shm` is in
   use, `ipcs`/`ipcrm` orphans). This breaks the hang→SIGKILL→orphan→startup-failure ratchet.
4. **Pin a known-good buffer backend** if the captured error implicates one:
   `~/.gnuradio/prefs/vmcircbuf_default_factory` (e.g. `mmap_shm_open` vs `mmap_tmpfile`).
5. **For this fault class, auto-restart is highly effective.** An *intermittent startup* failure
   almost always clears on the first retry, so the default policy (§7) recovers it with little
   operator involvement.

### 3.5 Confirmed hygiene gap: staged `/dev/shm` files leak on a hard kill

Independent of the exact `vmcircbuf` mechanism, code review found a real, fixable leak that
*contributes to `/dev/shm` pressure* and matches the "poor buffer cleanup" hypothesis:

- The Galileo streamed signals (`gal_e5`/`gal_e6`/`gal_prs_tx.py`) stage a **32–33 MB IQ file into
  `/dev/shm`** (`tempfile.mkdtemp(dir="/dev/shm")`) and remove it only via
  **`atexit.register(shutil.rmtree, …)`**.
- `atexit` **does not run on `SIGKILL`**, a C++ abort (`SIGABRT`/`SIGSEGV`), or power loss.
- The agent's `ManagedProcess.stop()` is `SIGTERM` → wait **10 s** → **`SIGKILL`**. A hung/wedged task
  (its `finally: tb.wait()` blocks — exactly the fault we detect) never exits in 10 s, so it is
  **`SIGKILL`ed → the 32 MB file is orphaned.**
- The agent's `_cleanup()` unlinks the task's **control socket** but **does nothing about `/dev/shm`**,
  and nothing sweeps it at boot — so orphans persist until reboot.
- The leak is **cumulative across the unit's uptime**, not concurrent: a Galileo run force-killed in an
  earlier session shrinks the `/dev/shm` headroom a later fm_chirp startup needs, even with "no other
  tasks running." A reboot clears `/dev/shm` (consistent with "fine the next day").
- Note GNU Radio's own default backend (`mmap_shm_open`) **unlinks immediately**, so GR's *own*
  buffers do not leak on kill — the leak is strictly the **staged IQ files**, which nonetheless
  compete with GR for the same `/dev/shm`.

**Severity is unit-dependent** (Pi 5 `/dev/shm` ≈ 50 % RAM; a small GR flowgraph failing means
`/dev/shm` was nearly full, needing substantial accumulation or a small tmpfs). **Ruled out for the
reported incident:** the unit was **power-cycled** (which wipes the `/dev/shm` tmpfs) and **no task
ran between boot and the test**, so there were no orphans to accumulate. This leak is therefore a
real bug worth fixing for kill-heavy sessions, but **not** the cause of *this* fresh-boot failure —
see §3.6.

**Fixes (Phase 0):** (a) an agent `/dev/shm` sweep in `_cleanup()` **and** at boot **and** before each
launch, removing staged dirs whose owning PID is dead; (b) a SIGTERM handler in the scripts that also
`rmtree`s the staging dir (belt-and-suspenders on the graceful path); (c) longer term, move the
remaining `file_source` stagers off `/dev/shm` to in-RAM `vector_source_c` (as GPS/fm_chirp already
did) or a swept, size-capped location.

### 3.6 Fresh-boot, first-launch signature — the remaining explanation

The reported incident was a **power-cycle → arm → (2 h idle) → first task launch → startup
`vmcircbuf`**. That rules out every *accumulation* mechanism (orphaned `/dev/shm`, `sysv_shm`
segments, cross-session pressure — all cleared by the power-cycle) and points to a **transient GNU
Radio buffer-allocation failure at flowgraph construction on a clean system**. Contributing factors
found in the code/deploy config:

- **GR's buffer backend is not pinned, and the Pi service does not set `HOME`.** GNU Radio picks its
  `vmcircbuf` backend by *probing* on first use (allocating test buffers) and caching the choice in
  `~/.gnuradio/prefs/vmcircbuf_default_factory`. The X410 unit sets `HOME=/root`
  (`deploy/x410/install.sh`); the **Pi service (`deploy/sdr-agent.service`) sets neither `HOME` nor a
  backend pin**, and no seeded prefs exist in the repo — so GR's selection and the startup probe are
  left to defaults. The probe and the first real allocation both run **at startup**, matching the
  signature.
- **Large, high-rate buffers are more exposed.** fm_chirp runs at **61.38 Msps**, sizing its GR
  buffers larger than a low-rate signal's; a bigger double-mapped allocation is a bit more prone to an
  occasional transient failure. Consistent with the fault appearing on the sweep.
- **Partly irreducible.** A rare transient allocation failure at construction cannot be fully
  engineered away; hence the primary cure is **detect-as-crash + auto-restart within the warm-up
  lead-in** (§7.0), which makes it a non-event regardless of mechanism.

**Fixes (Phase 0):** pin the backend explicitly in the task launch env
(`GR_VMCIRCBUF_DEFAULT_FACTORY`, or seed the prefs file) so no probe runs at startup; set a stable,
writable `HOME`/`GR_PREFS_PATH` for launched tasks (as X410 does); raise the ceilings (§3.4). The
Phase-1 resource+backend snapshot (§6.3) will confirm the exact mechanism on the next occurrence.

### 3.7 Related first-launch cost: the FPGA image load (and a "started after on-air" miss)

Owner-reported, and likely the **same first-launch window**: the first SDR-driving task after a
power-cycle spends a long, variable time on **UHD loading the FPGA bitstream/firmware onto the USRP**
("installing image"). Only the first open after power-on pays it (later tasks reuse the loaded image
until the next power-cycle); over USB on a Pi it is slow and variable, and it has once made a signal
**start after its on-air time**.

- **Nothing pre-images the device today.** `GET /sdr` shells `uhd_find_devices`, which only
  *enumerates* — it does **not** load the image (`system._probe_sdr`). The image loads inside the
  first transmit task's flowgraph construction.
- **Shared window with §3.6.** The first flowgraph after boot does enumerate → **load the FPGA
  image** → init UHD → **allocate GR `vmcircbuf`s**, all at once — the heaviest, most contended
  construction of the session and the most likely moment for a transient buffer-allocation failure.
  Two distinct failures (slow-but-succeeds vs. a hard allocation failure) sharing one trigger window;
  the image load plausibly aggravates the buffer allocation.

**Fix (Phase 0): pre-image the SDR early.** Run a lightweight device *open* (`uhd_usrp_probe`, which
opens the device and loads the image — not `uhd_find_devices` — or a tiny no-op flowgraph) **at boot
and/or when a plan is armed**, while no task holds the device (respect the device-busy collision
noted for `/sdr`). Then the real task warms up fast and predictably (fixes the "started after
on-air" miss) and its flowgraph construction is lighter (de-risks §3.6).

**Design implications** (also §7.0): warm-up budgeting must treat the **first task after boot** as
special (pre-imaging removes the special case; otherwise pad its lead-in), and the **on-air-miss
backstop must cover a warm-up overrun**, not only a crash — so a too-slow first warm-up is a
detected, alarmed event.

---

## 4. The two problems, grounded in the current code

### 4.1 Detection gap (`sdr-agent`)
- `ManagedProcess._watch` (`process_manager.py`) blocks on `await proc.wait()` and reacts **only to
  process exit**. There is no health poll. A halted-but-alive flowgraph → state stays `RUNNING`
  forever, no `CrashEvent`, no log scan. (`ProcessStatus.state` is exit-driven, `models.py`.)
- No task↔run coupling: the sequence runner would keep firing tune steps at a dead task (each
  swallowed by `_fire_step`).
- The `vm circ buff` text lands in the merged `current.log` but is **read only after an exit**
  (`_fire_crash_event` tails 20 lines). `GET /sdr` (`uhd_find_devices`) reports the device present
  even when streaming is dead, and is unsafe to poll while a task holds the device.

### 4.2 Recovery gap (`sdr-agent` + `sdr-client`)
- `arm` refuses a past window ("first step would fire in the past"); the client blocks mid-window
  arming ("Arming mid-window isn't supported yet"). So you cannot re-arm onto the original,
  now-partly-elapsed schedule.
- **But the ingredients exist:** the persisted `SequenceRun` keeps the original absolute
  `on_air_at`/`on_air_end` and every ramp level as an absolute-timed `StepFire` with its `params`;
  `hold_now` already marks unfired steps `fired_actual="skipped"` (the reusable "skip the past"
  primitive); `_co_time_rank` already orders "set power before RF-on" (no hot blip); `_gate_precommand`
  repositions the attenuator and mutes. `recovery.py` has the elapsed-time math (currently latent).
  So **the ramp level at any instant is derivable, not eyeballed.**

---

## 5. Design — Detection (always on, loud)

### 5.1 Layer 1 — script self-report (`sdr-scripts`, authoritative)
Every RPi `*_tx.py` runs `tb.start(); while not stop: ctrl.drain(); time.sleep(0.1)`; `tb.wait()`
sits only in `finally`. GNU Radio **does not re-raise** a halted-flowgraph exception to Python — it
logs, halts, and `tb.wait()` returns normally. So the fault signal is: **`tb.wait()` returned while
nobody set `stop`.**

- Add a shared **`paramkit/txhealth.py`** with `watch_flowgraph(tb, stop)`: a daemon thread that
  calls `tb.wait()`; if it returns with `stop` unset, print a structured
  `HEALTH state=faulted reason="flowgraph halted"` line, set `stop`, and make `main()` **return
  non-zero**. This converts a silent hang into a real crash the agent's existing pipeline handles
  (CRASHED, `CrashEvent` carrying the `vm circ buff` tail, restart machinery).
- Adopted uniformly by all ~10 scripts (one helper, no per-script copy-paste). `argspec.py`/`ramp.py`
  untouched → drift guard intact.
- **Follow-up (optional):** a UHD async-metadata monitor (port `x410_engine._monitor_async`) that
  counts **sustained** underflow/late/seq-error and reports it via a new `{op:"health"}` on the
  `paramkit.live` control socket — for the rarer *true wedge* where `tb.wait()` never returns.
  Underflow rule: **sustained pile-up**, not incidental blips (a few during a buffer swap are
  normal).

### 5.2 Layer 2 — agent watchdog (`sdr-agent`, backstop)
A dedicated monitor coroutine started in `lifespan` (~2 s cadence; task health is not sub-second),
over the tasks a live run owns (`_tasks_owned_by_active_runs()`) plus `RUNNING` procs:

- **Log-content scan:** extend `LogManager` with "read new bytes since offset" (it already
  byte-offset-follows in `stream`) + a per-task offset on `ManagedProcess`; match new bytes against
  a curated fault-pattern list (`vmcircbuf`, `boost::interprocess`, `std::runtime_error`, the
  Layer-1 `HEALTH state=faulted` marker, a **sustained** underflow flood). Catches the true-wedge
  case the done-watcher can miss.
- **Heartbeat poll** (if Layer-1's `{op:"health"}` is present) over the existing `get_params`
  transport; must return watcher-maintained flowgraph state, never merely "the socket answered."
- **Silence** (`last_output_at`) is *advisory only* — some scripts are silent mid-run, and
  `cw_drift` prints a wall-clock progress line even when RF is dead (a false heartbeat).

### 5.3 Health model + transport
- Keep `ProcessState` **exit-driven** (do not overload it — a new value would race the exit machine
  and collide with genuine `CRASHED`). Add **separate health fields** to `ProcessStatus` /
  `ManagedProcess` (plain field writes, the ManagedProcess no-lock convention): `health:
  "ok"|"stalled"|"rf_fault"|"unknown"`, `health_detail`, `last_output_at`, and a **resource
  snapshot** (`shm_used`, `shm_total`, `map_count`, `map_max`, `rss`, `vmcircbuf_backend`) captured
  at fault (§6.3).
- Fire a **new SSE event** (`TaskHealthEvent`, mirroring `CrashEvent` through `EventDispatcher.fire`).
  Route it in the client's `webhook/classify.py`. **The fault must arrive over SSE, not only the
  3 s poll** — the Library sequence panel refreshes only on SSE events.
- **Couple into the run:** on a confirmed `rf_fault` of a task an active run owns, stamp `run.fault`,
  stop firing tunes at the dead task, and emit `sequence_rf_fault`. (Whether to add a terminal
  `SequenceState.FAULTED` vs. an `rf_fault` field on a still-`RUNNING` run is an implementation
  choice for Phase 2; a field is lower-risk.)

### 5.4 Loud alert (`sdr-client`)
`main_window._on_alert` is today a stub (expand feed + log; an explicit "Sound / window flash can be
added here later" TODO). Implement it: **sound + window flash/raise + OS notification + a persistent
top banner**, plus a distinct red/panic **fault pill** on `_TaskRow` / `_SequenceRow` / `_PlanRow` /
`UnitCard` / a new `timeline_tab` `faulted` entry-state, and add the fault type to
`alert_feed._ALERT_TYPES` + `_describe`. A **"View fault log"** affordance opens the archived log
(§6.2).

---

## 6. Design — On-fault behaviour

### 6.1 Auto-drop RF, free the channel
On a confirmed hard fault the RF is already dead, so **stop the hung task** to free the single TX
channel for a clean restart. Stop = **SIGTERM + grace period, escalating to SIGKILL** for a true
wedge (a halted flowgraph may not honour SIGTERM). Only the unambiguous Layer-1 halt (or a
corroborated Layer-2 signal) triggers the auto-drop; a lone transient underflow burst does not.

### 6.2 Keep the logs
The faulted `current.log` is **archived to `run_<ts>.log` by `LogManager.rotate` on the next start**
(last 10 archives / 7 days retained). The fault event carries the **archived path + the last lines**
for immediate on-screen clarity, and the client's "View fault log" opens it. Nothing is lost to the
restart.

### 6.3 Resource snapshot (the durable diagnostic)
At fault detection, the agent captures and attaches to the fault record: `df /dev/shm` (used/total),
the process's `map_count` vs `vm.max_map_count`, RSS, `ulimit -n`, the selected `vmcircbuf` backend,
and (if `sysv_shm`) an `ipcs -m` summary. **This is what makes the next `vmcircbuf` self-diagnosing**
and turns "I hope that was the last time" into a measurable outcome.

---

## 7. Design — Recovery (two independent knobs)

Recovery is governed by **two orthogonal choices**, per the owner's decisions.

### 7.0 Recovery depends on WHEN the fault hits

The right recovery — and how invisible it can be — depends on where in the run the fault lands.

- **Pre-roll (before on-air) — the common startup-`vmcircbuf` case.** The task launches with a
  warm-up lead-in and RF muted; if it faults here, **nothing has gone on-air yet**, so recovery is
  the *simple* case: **relaunch the crashed task** and leave the run's future fires (the scheduled
  RF-on tune, the ramp, the STOP) untouched on the original timeline — no resync math, no level
  reconstruction. The agent knows the task's launch instant and the RF-on tune's `fire_at`, so it
  computes the **remaining lead-in** and:
  - if `now + restart + warm-up ≤ RF-on` → **RF comes on-air on schedule; a benign notice, not an
    alarm** (the operator need not act — the fault is masked);
  - else → **loud alarm** (on-air will be missed/late; the retry can't warm up in time).
  As a backstop, if RF-on fires and the task is not confirmed radiating — whether from a crash **or a
  warm-up overrun** (e.g. a first-boot FPGA image load, §3.7) — alarm. The **fast-warm IQ
  cache (§8)** shrinks the warm-up, widening the set of faults that recover silently — decisive for
  the slow-warming L1C/L2C; for fm_chirp (near-instant build) a restart fits any normal lead-in.
  **This case is fully covered by Phase 1 detection + a task-level auto-restart — the mid-run resync
  machinery below is not needed for it.**
- **Mid-run (after on-air).** The full `restart_run` path (§7.3): skip the past, reconstruct the
  level, relaunch muted-then-gated, and apply the resync-vs-replay choice (Knob B).
- **Exit vs. hang.** A construction-time failure typically **exits non-zero** (already a crash the
  agent sees today); a just-after-start halt **hangs** (the Layer-1 done-watcher converts it to a
  non-zero exit). Either way it becomes a detectable crash, so the same recovery applies.

### 7.1 Knob A — who triggers recovery (configurable, by level)

**Task-level** (a standalone task, or the task-editor default):
- An **"Auto-restart on fault"** checkbox, set in the **Run… form and in the create/edit-task
  form**. This is **independent of any sequence/plan policy** — it governs only a task run on its
  own.
- **Checked** → the agent auto-restarts the task **with the exact parameters it had when it
  crashed**, no operator prompt. (For a standalone fixed-parameter task this is simply a relaunch;
  for an intermittent *startup* fault the retry almost always succeeds.)
- **Unchecked** → **notify the operator** (sound/flash) with a **"restart?" prompt**; no automatic
  action.

**Sequence / plan level:**
- Policy is set in the **sequence editor / plan editor**. **A plan's policy governs the sequences it
  runs** (a sequence used inside a plan uses the plan's policy).
- **Default = auto-restart with resync** ("sync-in": rejoin the original schedule automatically).
- **Alternative = operator-confirmed restart.** When confirm is chosen, on a fault the operator is
  offered the **two semantics** below (Knob B).

### 7.2 Knob B — what "recover" means (the operator's choice at confirm time)

- **Resync (rejoin the schedule).** Keep the original `on_air_at`/`on_air_end`; skip the missed
  slice; bring the signal up at the level it *should* be at **now**; continue and stop on the
  original timeline. → the multi-unit case: one unit's SDR died, restart just that unit's task and
  it **rejoins the still-running peers**; off-air unchanged. Cost: the missed slice is a silence
  gap.
- **Replay-forward (restart from the crash point).** Resume at the crash level and play the
  **remaining** profile forward from now; the whole sweep is delivered, the end shifting later by
  the downtime. → single-unit / content-complete tests. Guard: shifting the end can collide with the
  next plan on the channel — warn/refuse, or offer to truncate at the original off-air (which
  degrades to resync's ending).

> For a **startup** fault (like the incident), nothing has transmitted yet, so resync and
> replay coincide at the beginning of the (schedule-positioned) run — recovery is simply a clean
> relaunch. The two modes differ only for a **mid-run** fault.

### 7.3 The resync math (no eyeballing)
Server-side `POST /sequence-runs/{id}/restart` → `SequenceRunner.restart_run(run_id, now)`, adjacent
to `proceed`/`hold-now`, does it all atomically:

1. Ensure the faulted predecessor is terminal (abort it; frees the channel span so the overlap guard
   won't refuse the re-arm on its own window).
2. **Keep the original** `on_air_at`/`on_air_end`/`plan_id` (resync) — or shift them by the downtime
   (replay).
3. **Skip the past:** mark every `StepFire` with `fire_at <= now` as `fired_actual="skipped"` (the
   existing `hold_now` sentinel; prevents the tick from burst-firing the whole elapsed up-ramp in
   one pass).
4. **Reconstruct the level:** `L_now` = the value of the latest tune fire with `fire_at <= now`
   carrying the swept param. A ramp is a staircase of held levels, so the last-passed level is
   *exactly* what a never-faulted peer transmits now — no interpolation.
5. **Relaunch clean, no hot blip:** insert synthetic `fire_at=now` fires — a `start` that relaunches
   the task **RF-muted** through `_gate_precommand` (attenuator repositioned at the carrier), then a
   `tune` to `L_now`, then RF-on. `_co_time_rank` guarantees the level is set before the gate opens.
6. **Future fires untouched** — the rest of the ramp and the STOP keep their original absolute
   `fire_at`s (resync), so the run continues in sync.

### 7.4 Multi-unit plans
Restart is **per unit**: a faulted unit re-arms against *its own* original `on_air_at` and rejoins
its peers. A "Restart plan" fans out per faulted item. (Resolving each unit's own `run_id` across a
plan arm is a pre-existing client TODO to finish here.)

---

## 8. Design — Fast-warm restart (optional, per signal)

Some signals (L1C, L2C) spend ~30 s generating their IQ buffer; a naive relaunch pays that again,
widening the warm-up gap that makes resync imperfect. Add a **per-signal IQ-buffer cache** in
`paramkit`:
- Keyed on **waveform-shape params only** (PRN, sidelobes, rate, filter…). `--power`/`--gain`/`--freq`
  are applied *downstream* and are **not** part of the key — which is exactly why caching is safe.
- Stored **off `/dev/shm`** (don't compete with the very resource that failed), bounded LRU.
- On relaunch, load instead of regenerate → 30 s warm-up collapses to a file read. Biggest win for
  L1C/L2C and for resync's warm-up hole.
- **Speeds recovery; does not prevent the fault** (the `vmcircbuf` is GR's buffer, not the IQ vector).
  Prevention is §3.4.

---

## 9. Cross-repo changes (by repo)

**`sdr-scripts`**
- `paramkit/txhealth.py` (`watch_flowgraph`) adopted by every RPi `*_tx.py` `main()`.
- Optional: UHD async underflow monitor + `{op:"health"}` on `paramkit.live`.
- Optional: per-signal IQ-buffer cache helper; opt-in on the slow generators (L1C/L2C).
- A script-internal-timeline sweep (`cw_drift`) additionally needs an absolute-anchor
  (`SDR_TASK_T0_EPOCH`/`--elapsed-offset`) to re-enter mid-sweep — not needed for agent-driven
  `--power` ramps.

**`sdr-agent`**
- `process_manager.py`: health monitor coroutine; health fields + resource snapshot on
  `ManagedProcess`; `TaskHealthEvent` via the dispatcher; auto-drop-RF with SIGTERM→SIGKILL
  escalation; `/dev/shm`/IPC hygiene around task start/stop.
- `log_manager.py`: "read new bytes since offset".
- `sequence_runner.py`: task→run fault coupling; `restart_run(run_id, now)` (skip-past, `L_now`
  reconstruct, muted relaunch, resync vs replay).
- `main.py`: `POST /sequence-runs/{id}/restart`.
- `models.py`: `ProcessStatus` health fields; `TaskHealthEvent`; `SequenceRun.fault`;
  `TaskConfig.auto_restart_on_fault` + `max_restarts` (default **2**); a recovery-policy field on the
  arm request / plan.
- `config.py`: capabilities `task-rf-health`, `sequence-restart` (+ any policy cap); bump
  `AGENT_VERSION` from `1.27.3`; assert in `tests/test_meta_endpoint.py`.
- **Deploy/ops:** raise `vm.max_map_count` / verify `/dev/shm` size / `ulimit -n` in the unit image;
  optional GR-prefs backend pin.

**`sdr-client`**
- `api/models.py`: mirror health fields + `TaskHealthEvent` + `SequenceRun.fault`.
- `api/client.py`: `restart_sequence_run(run_id)`; capability constants.
- `webhook/classify.py`: route the new event.
- `ui/theme.py`: a loud fault status color.
- `ui/unit_detail.py` / `ui/sequences_panel.py` / `ui/plans_tab.py` / `ui/unit_card.py` /
  `ui/timeline_tab.py`: fault pills, "Restart & resync" controls (mode choice on confirm), "View
  fault log".
- `ui/main_window.py`: implement `_on_alert` (sound / flash / OS notification / banner).
- `ui/run_task_dialog.py` + the task editor: the **Auto-restart on fault** checkbox.
- `ui/sequence_editor.py` / `ui/plan_editor.py`: the recovery-policy control (auto+resync default;
  operator-confirmed; plan overrides sequence).

---

## 10. Capability gating & drift guard

- New client-visible behaviour adds a **capability string** to `AGENT_CAPABILITIES` and **bumps
  `AGENT_VERSION`**; the client feature-gates on the exact string (a safety gate: never offer the
  restart button / send the new flag to an older agent that would 400). Behaviour-only pieces bump
  the version with no capability so OTA can push them.
- The resync math lives in `sequence_runner.py` (agent-only). **`ramp.py` and `argspec.py` stay
  byte-identical across the two repos** — the drift guard (`tests/test_shared_source_drift.py`) must
  remain green.

---

## 11. Phasing / rollout

- **Phase 0 — prevention & ops (cheap, parallel).** Raise ceilings; **pin the GR `vmcircbuf`
  backend + set a stable `HOME`/`GR_PREFS_PATH`** for launched tasks (§3.6); **pre-image the SDR at
  boot/arm** (§3.7); agent `/dev/shm`/IPC hygiene + graceful shutdown (§3.5). Directly attacks the
  incident's fresh-boot cause and the "started after on-air" warm-up overrun.
- **Phase 1 — recognize + alarm + safe-stop + keep-logs + snapshot.** Script done-watcher; agent
  watchdog + health field/event + run coupling; client loud alert + fault pill + "View fault log".
  *Independently solves "recognize it" and makes the next fault self-diagnosing.*
- **Phase 2 — restart & resync/replay.** `restart_run` + endpoint; client one-click with the two
  modes; the per-task/sequence/plan policy controls.
- **Phase 3 — fast-warm IQ cache + unattended auto-restart-with-breaker (budget 2).**

Each phase is independently useful and capability-gated.

## 12. Test plan (per phase)

- **P0:** an orphan-sweep unit test (agent removes only dead-PID shm); a launch after a simulated
  hard kill succeeds.
- **P1:** `watch_flowgraph` turns a halted `tb` into a non-zero exit (fake `gr`); the agent monitor
  flips a task to `rf_fault` from a seeded log line and fires the SSE event without an exit; the run
  couples the fault + stops tuning the dead task; the client renders the pill + alert from the event.
- **P2:** `restart_run` skips past fires, reconstructs `L_now` from `run.steps`, inserts the muted
  relaunch + level + RF-on, and leaves future fires on their original `fire_at`s (resync) or shifts
  them (replay); the past-window/overlap guards are bypassed for the own-window path; the client
  offers the correct policy/mode controls and gates on the capability.
- **P3:** the IQ cache round-trips on shape-key and ignores power/freq; the auto-restart breaker
  stops after 2 and alarms.

## 13. Decisions log (owner-approved)

- Detection always-on and **loud**; SSE-routed so every panel sees it.
- On fault: **auto-drop RF** (stop the dead task; SIGKILL escalation) **but keep the logs**;
  snapshot resources.
- **Task policy:** an Auto-restart checkbox in the Run…/task form; independent of sequence/plan
  policy; checked → restart with the **exact crash-time params**, no prompt; unchecked → notify +
  ask to restart.
- **Sequence/plan policy:** default **auto-restart with resync**; alternative **operator-confirmed**,
  which then offers **replay-forward** (whole thing from the crash point) vs **resync** (restart what
  crashed and rejoin). Set in the sequence/plan editor; **a plan overrides its sequences**.
- **Auto-restart budget:** default **2**.
- **Pre-roll recovery:** a fault before on-air is auto-restarted (simple relaunch); if the retry
  warms up before the RF-on instant, RF goes on-air **on schedule with a benign notice, not an
  alarm** — alarm only when the retry can't make on-air in time. This makes the rare startup
  `vmcircbuf` a non-event (Phase 1 + task auto-restart; no mid-run resync needed).
- **Underflow rule:** fault on **sustained** pile-up, not incidental blips.
- **Root cause:** startup-time GR `vmcircbuf` allocation failure (GR's own `/dev/shm` buffers, not
  staged IQ); mechanism to be confirmed from the captured error; prevented by §3.4 regardless.

## 14. Open items

- Retrieve the archived `run_<ts>.log` from the affected unit + `df /dev/shm` /
  `cat /proc/sys/vm/max_map_count` / `ipcs -m` / the GR backend → **confirm the exact mechanism** and
  finalize §3.3.
- Confirm the Pi 5 image's current `/dev/shm` size, `vm.max_map_count`, and `ulimit -n`.
- Decide `SequenceState.FAULTED` (terminal) vs. an `rf_fault` field on a still-`RUNNING` run.
- Multi-unit per-item `run_id` resolution for plan-level restart.

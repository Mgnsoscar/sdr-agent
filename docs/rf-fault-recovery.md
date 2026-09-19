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
4. **Pin a known-good buffer backend** if the captured error implicates one — but note the scripts
   set `GR_DONT_LOAD_PREFS=1` (§3.6), so a `~/.gnuradio/prefs/…` pin is **ignored**. Pin it in the
   task **launch env** instead — the GNU Radio config-override env var for `[vmcircbuf]
   default_factory` (`GR_CONF_VMCIRCBUF_DEFAULT_FACTORY` in current GR; verify against the deployed
   version) — or drop `GR_DONT_LOAD_PREFS` and seed the prefs file (`mmap_shm_open` vs `mmap_tmpfile`).
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

**Fixes (Phase 0) — SHIPPED (1.27.4), with a refinement:** the two halves meet in a shared
`paramkit/txstage.py`. (a) The scripts stage under a TAGGED, PID-bearing name
(`/dev/shm/sdrtx-<pid>-<signal>-…`) via `txstage.staging_dir(signal)`; (b) the agent
`txstage.sweep_orphans()` removes ONLY those tagged names whose owning PID is **dead**, run in
`ManagedProcess._cleanup()` (post-exit / post-SIGKILL), before every managed launch, and once at
boot (`main._boot_prevention`). The tag is what makes the sweep safe — it never touches a live
sibling's buffers, GR's own `vmcircbuf_*` objects, or a foreign owner's shm. **Refinement over the
original plan:** this agent-side dead-PID sweep *supersedes* a per-script SIGTERM `rmtree` handler —
it is strictly more robust, because it also reclaims a **`SIGKILL`/crash** orphan (no handler runs
then), which is exactly the RF-fault path. The scripts keep their existing `atexit`/`finally`
cleanup as the graceful backstop. (c) Longer term, move the remaining `file_source` stagers off
`/dev/shm` to in-RAM `vector_source_c` — still a follow-up. **Deferred within Phase 0:** the x410
`*_channel.py` stagers (bare `mkstemp`, delete-after-load, tiny exposure) are not yet on the tagged
prefix — the agent sweep only reclaims tagged orphans, so migrate them when convenient.

### 3.6 Fresh-boot, first-launch signature — the remaining explanation

The reported incident was a **power-cycle → arm → (2 h idle) → first task launch → startup
`vmcircbuf`**. That rules out every *accumulation* mechanism (orphaned `/dev/shm`, `sysv_shm`
segments, cross-session pressure — all cleared by the power-cycle) and points to a **transient GNU
Radio buffer-allocation failure at flowgraph construction on a clean system**. Contributing factors
found in the code/deploy config:

- **The scripts disable GR prefs, the backend is not pinned, and the Pi service sets no `HOME`.**
  Every transmit script sets `GR_DONT_LOAD_PREFS=1` (a repo-wide `os.environ.setdefault`, to skip a
  slow pref scan), so GNU Radio **never reads `~/.gnuradio/prefs/`** — it selects its `vmcircbuf`
  backend from its compiled default on every launch, and a prefs-file pin would be ignored. The X410
  unit sets `HOME=/root` (`deploy/x410/install.sh`); the **Pi service (`deploy/sdr-agent.service`)
  sets no `HOME`**, so a launched task's `~` is undefined/varies (and if any code still touches
  `~/.gnuradio`, that path is unstable). The default backend's first real allocation runs **at
  startup**, matching the signature; nothing forces a known-good backend, and nothing is pinned per-env.
- **Large, high-rate buffers are more exposed.** fm_chirp runs at **61.38 Msps**, sizing its GR
  buffers larger than a low-rate signal's; a bigger double-mapped allocation is a bit more prone to an
  occasional transient failure. Consistent with the fault appearing on the sweep.
- **Partly irreducible.** A rare transient allocation failure at construction cannot be fully
  engineered away; hence the primary cure is **detect-as-crash + auto-restart within the warm-up
  lead-in** (§7.0), which makes it a non-event regardless of mechanism.

**Fixes (Phase 0):** pin the backend explicitly in the task **launch env** — because
`GR_DONT_LOAD_PREFS=1` is set, this must be the GNU Radio config-override env var
(`GR_CONF_VMCIRCBUF_DEFAULT_FACTORY` in current GR; verify against the deployed version), **not** a
`~/.gnuradio/prefs` file (which is not read); or drop `GR_DONT_LOAD_PREFS` and seed the prefs file.
Set a stable, writable `HOME` for launched tasks (as X410 does). Raise the ceilings (§3.4). The
Phase-1 resource+backend snapshot (§6.3) confirms the effective backend + mechanism on the next
occurrence — including whether `GR_DONT_LOAD_PREFS` left GR on a different default than we assume.

### 3.7 A *separate* first-launch cost: the FPGA image load (and a "started after on-air" miss)

Owner-reported, and a **distinct** first-launch cost — *not* the same failure as §3.6: the first
SDR-driving task after a power-cycle spends a long, variable time on **UHD loading the FPGA
bitstream/firmware onto the USRP** ("installing image"). Only the first open after power-on pays it
(later tasks reuse the loaded image until the next power-cycle); over USB on a Pi it is slow and
variable, and it has once made a signal **start after its on-air time**.

- **This incident's log showed only `vmcircbuf`, no image line — but the absence proves nothing.**
  Every script sets `UHD_LOG_CONSOLE_LEVEL=off` (a repo-wide `os.environ.setdefault`), which
  **suppresses UHD's console output entirely**, the "installing image" line included. So its absence
  does **not** tell us whether the FPGA image loaded during the incident — we simply couldn't see it.
  That suppression is itself a **diagnostic blind spot** (fix below).
- **Treat the two as independent, merely co-located.** The FPGA image load and a `vmcircbuf`
  allocation failure both fall in the **first flowgraph construction after boot**, so they share a
  time window — but the incident evidence does not link them, and they are different failure modes:
  the image load is a *slow-but-succeeds* warm-up cost, the `vmcircbuf` is a *hard allocation
  failure*. Each is fixed on its own merits; neither fix depends on the other having been the cause.
  (Pre-imaging does lighten that first construction, which can only help §3.6, but we do **not** claim
  the image load caused the buffer failure.)
- **Nothing pre-images the device today.** `GET /sdr` shells `uhd_find_devices`, which only
  *enumerates* — it does **not** load the image (`system._probe_sdr`). The image loads inside the
  first transmit task's flowgraph construction.

**Fix (Phase 0): pre-image the SDR early.** Run a lightweight device *open* (`uhd_usrp_probe`, which
opens the device and loads the image — not `uhd_find_devices` — or a tiny no-op flowgraph) **at boot
and/or when a plan is armed**, while no task holds the device (respect the device-busy collision
noted for `/sdr`). Then the real task warms up fast and predictably (fixes the "started after
on-air" miss) and its flowgraph construction carries less concurrent work.

**Fix (Phase 0/1): stop flying blind on UHD.** Keep the console quiet but route UHD's log to a **file**
at a useful level for launched tasks (`UHD_LOG_FILE` + a non-`off` `UHD_LOG_FILE_LEVEL`), so the image
load, UHD init warnings, and any device error are **captured on disk** even though
`UHD_LOG_CONSOLE_LEVEL` stays `off`. The Phase-1 fault snapshot (§6.3) attaches that UHD log next to
the resource state, so the next event shows both the GR buffer state *and* what UHD was doing.

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
the process's `map_count` vs `vm.max_map_count`, RSS, `ulimit -n`, the **effective `vmcircbuf`
backend** (the launch env's `GR_CONF_VMCIRCBUF_DEFAULT_FACTORY` and — since `GR_DONT_LOAD_PREFS=1`
means GR reads no prefs file — whatever compiled default GR actually used, via a one-shot
`gnuradio-config-info --prefs`), the task's `HOME`, and (if `sysv_shm`) an `ipcs -m` summary. It also
attaches the **UHD log file** (§3.7 fix) so a device error suppressed on the console is on record.
**This is what makes the next `vmcircbuf` self-diagnosing** and turns "I hope that was the last time"
into a measurable outcome.

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
  set a stable `HOME` in the Pi service (`deploy/sdr-agent.service`, as X410 already does); pin the GR
  buffer backend in the launch env via `GR_CONF_VMCIRCBUF_DEFAULT_FACTORY` (**not** the prefs file —
  `GR_DONT_LOAD_PREFS=1` is set in the scripts); pre-image the SDR at boot/arm (§3.7); route UHD logs
  to a file (`UHD_LOG_FILE`, non-`off` file level) so device errors are captured despite
  `UHD_LOG_CONSOLE_LEVEL=off`.

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

- **Phase 0 — prevention & ops (cheap, parallel). ✅ SHIPPED (`AGENT_VERSION 1.27.4`, no capability).**
  Raise ceilings; **pin the GR `vmcircbuf` backend via `GR_CONF_VMCIRCBUF_DEFAULT_FACTORY` in the
  launch env** (not the prefs file — `GR_DONT_LOAD_PREFS=1`) **+ set a stable `HOME`** for launched
  tasks (§3.6); **pre-image the SDR at boot** (§3.7); **route UHD logs to a file** so the console-off
  blind spot doesn't hide device errors (§3.7); agent `/dev/shm` hygiene (§3.5). Directly attacks the
  incident's fresh-boot cause and the "started after on-air" warm-up overrun. **What shipped — see the
  "Phase 0 — BUILT" section below for the full change list; two items deferred: arm-time pre-image
  (needs a device mutex — Phase 1 territory) and the x410-stager tagged-prefix migration.**
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
- **Env correction (found in the scripts):** every transmit script sets `GR_DONT_LOAD_PREFS=1` and
  `UHD_LOG_CONSOLE_LEVEL=off` (repo-wide `os.environ.setdefault`). So (a) the backend pin must be a
  launch-env `GR_CONF_*` override, **not** a prefs file (which GR never reads); and (b) the FPGA
  "installing image" line is suppressed, so its **absence in this incident's log is not evidence** the
  image didn't load — hence UHD logs get routed to a file (§3.7) and the FPGA cost is treated as
  independent of the `vmcircbuf` (§3.7), not its cause.

## 14a. Phase 0 — BUILT (`AGENT_VERSION 1.27.4`, no capability; branch `claude/system-familiarization-f5mezz`)

The prevention/ops layer, shipped cross-repo. Behaviour-only version bump so OTA can push the agent
code; **the deploy/sysctl/HOME hardening reaches field units only via a re-provision / migrate, NOT
the "Update agent…" button** (an OTA restart never re-installs the unit file or runs sysctl).

**`sdr-agent` — new shared helper**
- **`paramkit/txstage.py`** (pure stdlib, shared by scripts + agent): `SHM_PREFIX="sdrtx-"`,
  `staging_dir(signal)` (tagged+PID `mkdtemp` under `/dev/shm` + `atexit` rmtree), `sweep_orphans()`
  (remove only dead-PID `sdrtx-*` entries — never a live/foreign/untagged object), `_pid_alive`.

**`sdr-agent` — agent code**
- **`config.py`**: `TASK_HOME` (default `STATE_DIR`, a guaranteed-writable home), `GR_VMCIRCBUF_FACTORY`
  (default `mmap_shm_open`, applied via `GR_CONF_VMCIRCBUF_DEFAULT_FACTORY`; `""` omits the pin),
  `UHD_LOG_FILE_LEVEL` (default `info`; `""` omits UHD file logging), `SHM_SWEEP_ENABLED`,
  `PREIMAGE_ON_BOOT`/`PREIMAGE_TIMEOUT_S`. `AGENT_VERSION 1.27.3 → 1.27.4`.
- **`process_manager.py`**: `_launch_env_pins(task_dir)` merged at all **three** launch env-build
  sites (`start`, `run_oneshot`, `_launch_oneshot_wait`) **between** `os.environ` and `cfg.env` —
  so the pins beat ambient but `cfg.env`/`req.env_overrides` still win (HOME + GR backend +
  `UHD_LOG_FILE`/level, the last a per-task file next to `current.log`). `_sweep_shm_orphans()`
  (flag-gated, best-effort) called **before each managed launch** and in **`_cleanup()`**
  (post-exit / post-SIGKILL). `place_ramp`/`argspec`/`ramp` untouched (drift guard intact).
- **`system.py`**: `pre_image_sdr()` / `_preimage_sdr()` — a best-effort `uhd_usrp_probe` OPEN
  (loads the FPGA image), NOT `uhd_find_devices`; a no-op when the tool isn't on PATH (dev/CI/mock).
- **`main.py`**: in `lifespan`, `_boot_sweep()` runs the `/dev/shm` sweep **before `_manager.startup()`**
  (so no live/autostart task's buffers are swept), then `_preimage_when_idle()` is fired **DETACHED**
  (`asyncio.create_task`, **never awaited**) **after** startup. Detaching is a deliberate safety
  choice from a `/code-review` finding: a wedged USB SDR can leave `uhd_usrp_probe` unkillable in
  uninterruptible **D-state** sleep, which a `subprocess.run` timeout cannot reap (`kill()` then an
  *un-timed* `wait()`) — so **awaiting** it on the lifespan critical path could hang boot forever and
  leave the unit unreachable with no remote recovery, the exact wedged-hardware case this feature
  defends against. Detached, boot always completes; the task self-bounds with an outer
  `asyncio.wait_for` and skips when a task already holds the device. (A truly D-state probe still
  leaks one background executor thread until the I/O returns — harmless; the agent runs normally.)

**`sdr-agent` — deploy/ops**
- **`deploy/99-sdr-agent.conf`** (new): `vm.max_map_count = 262144`, installed to `/etc/sysctl.d/`
  from **all four** install paths — `provision_install.sh`, `migrate_layout.sh`, `x410/install.sh`,
  and the classic repo-root `install.sh` (+ a non-fatal `/dev/shm` size check on the Pi paths).
  Auto-bundled (`build_bundle.sh` copies `deploy/` recursively).
- **Service units — both hardened** with `HOME` + `GR_CONF_VMCIRCBUF_DEFAULT_FACTORY=mmap_shm_open` +
  `LimitNOFILE=65536`: the OTA `deploy/sdr-agent.service` (Pi) AND the classic root `sdr-agent.service`
  (a `/code-review` follow-up closed that gap). **`deploy/x410/install.sh`**: `LimitNOFILE` + the GR
  pin in the unit heredoc (HOME already set). **`deploy/run_local.sh`**: the GR pin in the dev env.

**`sdr-scripts`** — the 13 RPi loop-file stagers (Galileo/GLONASS/BeiDou/iridium) + the 4 FIFO
stagers (gps_l1p/gps_l2p/white_noise/gaussian_noise) now stage via `txstage.staging_dir(...)`
(tagged, sweepable), fixing two copy-paste tag bugs (`gps_l1p`, `glonass_of`) in passing. The
incident script `fm_chirp` and the GPS `vector_source` scripts stage nothing → untouched.

**Tests** (`sdr-agent`, suite 519 → 537): `tests/test_txstage.py` (sweep removes only dead-PID
tagged orphans; keeps live/foreign/unparseable; a launch after a simulated hard kill reclaims the
orphan; the disable flag), `tests/test_launch_env_pins.py` (pins at all three sites; HOME beats
ambient; cfg.env/req.env_overrides beat the pins; blank config omits the key), `tests/test_preimage.py`
(uses `uhd_usrp_probe` not `uhd_find_devices`; no-op without the tool; boot wiring sweeps-then-images,
skips when disabled, never raises). `sdr-scripts` 99 pass (byte-compile + suite). **Verified live**:
the agent boots clean headless — `_boot_prevention` runs, the pre-image no-ops (no radio on PATH),
`/health` returns ok, no traceback.

**Deferred within Phase 0** (documented, not built): arm-time pre-image (a scheduled arm can fire
while another run holds the device — safe only with a real device mutex, which is Phase-1 territory
alongside the `/sdr` device-busy gate the probe also lacks); the x410 `*_channel.py` tagged-prefix
migration (bare `mkstemp`, delete-after-load, minimal exposure).

**Rollout:** OTA-push 1.27.4 for the agent code; **re-provision / migrate** each unit (or re-provision
via the client with a rebuilt bundle) to apply the sysctl + service-env hardening. Rebuild the client
bundle from 1.27.4 (`deploy/build_bundle.sh`) + re-stage into `sdr-client/bundles/`. Before shipping
the GR backend pin to the field, confirm the override var name against the deployed GR with
`gnuradio-config-info --prefs` (§14) — an unknown name is a harmless no-op, but confirm to be sure.

## 14b. Phase 1 — BUILT (`AGENT_VERSION 1.28.0`, capability `task-rf-health`; branch `claude/system-familiarization-f5mezz`)

Detection + loud alarm + auto-drop-RF + a self-diagnosing fault snapshot, shipped cross-repo. The
detection is LAYERED so no single blind spot hides a halt, and health is a **separate axis** from the
exit-driven `ProcessState` (a halted flowgraph is still process-RUNNING — its fault can't be a
`ProcessState` value without racing the exit machine).

**`sdr-agent` — detection core**
- **`paramkit/txhealth.py`** (new, shared): `watch_flowgraph(tb, stop, *, reason=…, stream=…)` — a
  daemon thread that calls `tb.wait()`; GNU Radio does NOT re-raise a halted flowgraph to Python, so
  `tb.wait()` RETURNING with the `stop` flag still UNSET IS the fault signal. On that, it prints the
  `FAULT_MARKER` line (`HEALTH state=faulted reason="…"`, flushed whole), sets `stop`, and latches
  `.faulted=True` (a `Watcher`); the script then `return 1 if _health.faulted else 0`. A clean stop
  (`stop` already set) is a no-op. The marker + non-zero exit is Layer 1; a whole-line atomic write
  means the agent's log scan can't split the token.
- **`process_manager.py`** — the Layer-2 **watchdog**: `_health_loop` (every `HEALTH_POLL_S`≈2 s)
  scans each RUNNING, not-yet-alarmed task's NEW log bytes (`ManagedProcess.log.read_since`, inode/
  truncation-safe) for a `HEALTH_FAULT_PATTERNS` signature (the marker, `vmcircbuf`, `boost::interprocess`)
  — this is the ONLY path for the true-wedge case (no exit + no done-watcher, e.g. the FIFO-excluded
  scripts). A hit → `proc._flag_rf_fault(detail)` (idempotent latch: sets `health=RF_FAULT`, captures
  the snapshot, fires the event, calls the runner's `_fault_hook`) → auto-drop RF via `proc.stop()`
  (SIGTERM→grace→SIGKILL; idempotent so it never collides with an abort/deadman). `_watch`'s crash
  branch routes an rf-fault exit (health already flagged OR the log tail matches) to `_flag_rf_fault`
  instead of a plain crash event, so the Layer-1 exit path stamps health too. `_fire_health_event`
  puts a `TaskHealthEvent` on the SSE stream (fire-and-forget). `set_fault_hook` is wired in `main.py`
  AFTER both the manager and runner exist.
- **`sequence_runner.py`** — `on_task_fault(task_name, detail)` COUPLES a task fault into the run that
  owns it (under `self._lock`, via the refactored `_live_tasks_of`): stamps `run.fault`/`fault_task`/
  `fault_at`, marks that task's un-fired steps `"skipped"` (the `hold_now` sentinel — the run stops
  re-commanding a dead task), persists, and fires `sequence_rf_fault`. An rf_fault FIELD on a still-
  RUNNING run — NOT a terminal `SequenceState` (lower-risk; the actual restart is Phase 2).
- **`system.py`** — `capture_fault_snapshot(pid, uhd_log, task_dir, log_path)` (async wrapper
  `fault_snapshot`, off-loop via the executor): a best-effort resource snapshot that READS THE PHASE-0
  ENV WORK BACK — the effective `GR_CONF_VMCIRCBUF_DEFAULT_FACTORY` (env vs `gnuradio-config-info
  --prefs` compiled default), `/dev/shm` used/total, `/proc/pid/maps` count vs `vm.max_map_count`, RSS,
  `RLIMIT_NOFILE`, `HOME`, `ipcs -m` (only when a SysV backend is implicated), the per-task UHD-log
  tail — written to `snapshot_<ts>.json` beside the run log (`snapshot_path` set BEFORE the write).
  Every subprocess is `timeout`-bounded + guarded; never raises. `log_manager.cleanup` prunes
  `snapshot_*.json` on the same keep-N/max-age policy as the run logs.
- **`models.py`** — `TaskHealth` enum (only OK/RF_FAULT set in P1; STALLED/UNKNOWN reserved),
  `ProcessStatus.health`/`health_detail`/`last_output_at`, `FaultSnapshot`, `TaskHealthEvent`,
  `SequenceRun.fault`/`fault_task`/`fault_at`, and the `sequence_rf_fault` webhook type. All defaulted
  (skew-safe). **`config.py`** — `HEALTH_WATCH_ENABLED`/`HEALTH_POLL_S`/`HEALTH_FAULT_PATTERNS`; the
  `task-rf-health` capability; `AGENT_VERSION 1.27.4 → 1.28.0`. `argspec`/`ramp` untouched.

**`sdr-scripts`** — the 30 CLEAN-set RPi scripts (repeat=True/continuous, incl. the incident `fm_chirp`)
adopt `watch_flowgraph(tb, stop)` after `tb.start()` + the gated return. The FIFO caution-set
(`gps_l1p`/`gps_l2p`/`white_noise`/`gaussian_noise`, repeat=False) is **EXCLUDED** — a normal EOF also
returns `tb.wait()` with `stop` unset and would read as a FALSE fault; they rely on the agent watchdog.

**`sdr-client`** — the loud alarm + fault surfacing (all client-only): `api/models.py` mirrors the
health axis field-for-field (defaulted); `webhook/classify.py` routes `task_health` to `TaskHealthEvent`
with a branch BEFORE the generic `task_`→`TaskEvent` rule; `ui/main_window.py` implements the alarm —
`_on_alert` (beep + taskbar flash, every alert) and the fault-only `_on_fault` (un-minimise + raise +
a persistent system-tray balloon), all headless/no-tray no-op-safe; `ui/fault_detail_dialog.py` (new)
renders the snapshot as a self-diagnosis (backend / `/dev/shm` / VMA maps / fd limit, `vmcircbuf`
suspects flagged) + the log tail, opened by double-clicking the alert-feed fault row; a fault overrides
the state pill (red "RF FAULT") on the task row, the sequence row, and wins the fleet card's task line;
`ui/theme.py` `rf_fault` colour; `ui/timeline_model.py` `task-rf-health` gate (reserved for the Phase-2
Restart button — the P1 pill/alarm render unconditionally, since they only reflect data an older agent
never sends).

**Tests**: `sdr-agent` 540 → 553 (`test_txhealth.py`, `test_task_health.py`, `test_fault_snapshot.py`,
`test_meta_endpoint.py`); `sdr-scripts` 99 → 102 (`test_txhealth_adoption.py` — 30 clean adopters, the
4 FIFO excluded, mocks unaffected); `sdr-client` 1108 → 1128 (`test_rf_fault_ui.py`). **Verified live**
end-to-end: the fault marker injected into a running mock task → `rf_fault` health + auto-drop + a
snapshot recovering the P0 env work (`mmap_shm_open` backend, HOME) from the live task's `/proc/pid/environ`;
an ordinary crash (no fault signature) does NOT false-positive. **Adversarial review** (find→verify,
5 dimensions): two findings refuted (a split-token log miss — whole-line atomic writes + the redundant
exit path; an abandoned shutdown auto-drop — `shutdown` reaps every RUNNING proc via its own idempotent
`stop` gather) and two LOW findings FIXED, both in the self-diagnosis machinery: (1) `system._count_maps`
read `/proc/<pid>/maps` in text mode and caught only `OSError`, so a non-UTF-8 mapped pathname would
raise `UnicodeDecodeError` (a `ValueError`) out of the "never raises" snapshot and lose it — now it
reads BINARY and counts `b"\n"` (the sibling helpers `_read_int_file`/`_tail_file` already guarded the
same way); (2) the client fault dialog flagged a leaky backend on the env value alone, so a `sysv_shm`
COMPILED default with the P0 env pin disabled went unflagged — now the suspect check keys on the
EFFECTIVE backend (`env or compiled`). Both with regression tests (suite 554 / 1129).

**Rollout:** OTA-push 1.28.0 (behaviour + the new capability). No re-provision needed for detection
(the watchdog + done-watcher are agent/script code); the Phase-0 sysctl/service-env hardening still
needs a re-provision, unchanged. Rebuild the client bundle from 1.28.0 + re-stage into
`sdr-client/bundles/`. **Deferred to Phase 2**: `SequenceRunner.restart_run` + `POST …/restart`,
resync/replay, the `sequence-restart` capability, and the client Restart button.

## 14c. Phase 2 — BUILT (`AGENT_VERSION 1.29.0`, capability `sequence-restart`; branch `claude/system-familiarization-f5mezz`)

Operator-driven recovery — one click brings a faulted run back on air at the level it should be at,
with the resync/replay choice (§7.2). The unattended auto-restart trigger + fast-warm cache stay
**Phase 3** (§11: "fast-warm + unattended auto").

**`sdr-agent` — `SequenceRunner.restart_run(run_id, RestartRequest)`** (adjacent to proceed/hold_now).
A run whose task faulted is still RUNNING with `run.fault`/`fault_task` stamped and that task's un-fired
steps `'skipped'` (Phase-1 `on_task_fault`). restart_run recovers **IN PLACE** on that same run (no
re-arm, no channel-guard re-run, the run log stays open). It runs under two short locks — validate +
snapshot, release the lock to STOP the faulted process, then re-validate + plan + commit atomically:
1. **Reconstruct the born-at state** (`_relaunch_start_fire`) — walk the faulted task's fires in time
   order up to `now`, rebuild the launch args at its latest launch-like fire, then overlay every counted
   tune/ramp point's live params. It is generalised over WHATEVER the task swept (power, gain, a bridge
   param), and `--power` is baked SPEC-INDEPENDENTLY (via `_LEVEL_FALLBACK_FLAGS`) so the level survives
   even if the argspec is momentarily unreadable. **resync** counts the fired-OR-skipped points ≤ now
   (the SCHEDULE'S staircase position now — the exact point a never-faulted peer holds, even across a
   down-time that spanned several ramp steps); **replay** counts only the actually-fired points (the
   CRASH level). A ramp is a staircase of held levels, so this is exact — no interpolation, no eyeballing.
2. **Re-instate the faulted task's SKIPPED fires** so the run continues and STOPS — only the faulted
   task's fires, never a healthy peer's. **resync**: only the still-FUTURE ones (`fire_at > now`), on
   their ORIGINAL `fire_at` (rejoin the schedule; missed points stay missed). **replay**: EVERY skipped
   fire, shifted later by the down-time `now − fault_at`, with `on_air_end` floated too, so the whole
   remaining profile plays.
3. **Guard, before any mutation** (so a refusal is atomic — no half-recovered run): a non-open-ended run
   with **no future STOP** to recover into is refused (relaunching would leave RF on) — resync past
   off-air is pointed at replay. A **replay** whose shifted CHANNEL span (off-air + its stop tail, via
   `_channel_end`) would overlap another active run is refused via `_guard_replay_channel` over
   `_active_span`.
4. **Relaunch** with ONE synthetic `start` fire at `now`: the reconstructed launch command with the RF
   gate at its RECONSTRUCTED state (the schedule's gate at `now` — on mid-transmission, MUTED in the
   pre-roll / cool-down), so the task is **born transmitting at exactly the peer level** — the attenuator
   is positioned at the carrier BEFORE the process starts (`start → _gate_precommand(cmd=)`), no blip.
5. **Clear `run.fault`** — recovered.

> **As-built deviations from §7.3, both deliberate** (from the pre-build understand-map's two traps):
> (a) **In-place, not abort-and-re-arm.** §7.3 step 1 ("abort the predecessor, re-arm on its own
> window") assumed the pre-Phase-1 model where a fault made the run terminal. Phase 1 chose a
> non-terminal `fault` FIELD on a still-RUNNING run *specifically so Phase 2 could recover in place,
> lower-risk* — so there is no predecessor to abort and no overlap guard to fight. (b) **One
> launch-at-level fire, not the 3-fire muted-then-gated dance.** A relaunch-muted → tune-to-level →
> RF-on sequence at a co-timed `fire_at=now` is fragile: the ordering trap (a bare muted start ranks
> AFTER the power tune in `_co_time_rank`) and the socket-bind trap (`set_params` fires before the
> relaunched script binds its control socket → silently dropped → the level never applies) both bite.
> Launching directly at the level carries it on the launch command (no post-launch tune), giving the
> same no-blip guarantee (attenuator positioned before start) with no socket race. The re-instated
> FUTURE ramp tunes are safe — they fire at their own later `fire_at`, well after the relaunched script
> is up.

> **Adversarial review outcome** (find→verify, all dimensions, each finding verified against the code).
> A first review of the initial build surfaced **12 confirmed defects (A–H, some merged)**, all fixed in
> the shipped `restart_run` and each pinned by a regression: **A** a late restart past off-air dropped the
> STOP/RF-off → RF left on (now the no-future-STOP guard in step 3); **B** the level reconstruction handled
> only `--power`, so a `--gain` ramp relaunched at the wrong/over-power level (now generalised over the
> swept param); **C** an unfiltered past-due skip loop swept a healthy PEER task's just-due step (removed —
> `on_task_fault` already skipped the faulted task, peers are untouched); **D** resync used the last FIRED
> level, not the last SCHEDULED ≤ now (stale when the down-time spanned ramp points); **E** replay dropped
> ramp points scheduled DURING the down-time (now re-instates the whole remainder); **F** the replay
> collision guard raised AFTER mutating → a refused replay corrupted the run (moved before any mutation);
> **G** the collision guard used `on_air_end+shift`, not `_channel_end` (missed the stop tail); **H** no
> re-validation after the un-locked pre-stop (now re-checks state + fault + fault_task in the second lock).
> A **re-review of the rewrite** confirmed **2 further findings**, both fixed: (1, LOW) with the level
> reconstruction routed through the argspec, a transient `spec=None` reverted the relaunch to the launch
> `--power` (a hot over-power blip if the launch sat above the crash level) → `--power`/`--gain` now bake
> spec-independently via `_LEVEL_FALLBACK_FLAGS`; (2, MED) the relaunch unconditionally FORCED the RF gate
> ON, so a fault in the muted pre-roll or the cool-down tail un-muted early → the gate is now RECONSTRUCTED
> from the schedule (muted stays muted; the re-instated RF-on tune / the STOP drives it).
>
> **Re-verification round** (the re-review's own verify pass had partially aborted on an API session limit;
> re-run in full against the fixed code): of its 12 un-adjudicated findings, 2 came back ALREADY-FIXED (the
> gate-forcing pair above), 8 REFUTED (a HOLDING-time fault is cleanly refused by `_check_restartable`; a
> second concurrent fault is a Phase-1 `on_task_fault`-coupling limitation, not restart's; run-mode ramps
> aren't persistent RF transmitters; the rest already covered), and **2 CONFIRMED** and fixed: (i, LOW) the
> `_dest_flag_map` (bridge-param) reconstruction path had no test that discriminates it from the level
> fallback → a `--bw`-sweep regression added; (ii, LOW correctness) the `_fire_step` completion check had no
> `run.fault` guard, so a MULTI-task run whose HEALTHY peer finished flipped to COMPLETED with the fault
> unrecovered (and restart refuses a non-RUNNING run) → the check now also requires `not run.fault`, keeping
> a faulted run RUNNING (restartable) until restart clears the fault or the operator aborts (Phase-1 already
> dropped its RF, so no hazard).

Endpoint `POST /sequence-runs/{id}/restart` (`RestartRequest{mode, restart_at}`; 404 unknown / 409
not-RUNNING | no-fault | no-STOP-to-recover-into | replay-collision | fault-changed-under-us). `config.py`:
capability `sequence-restart`, `AGENT_VERSION 1.28.0 → 1.29.0`. `models.py`: `RestartRequest` + the
`sequence_restart` webhook type. `argspec`/`ramp` untouched.

**`sdr-client`** (client-only): `api/models.py` `RestartRunRequest{mode}`; `api/client.py`
`restart_sequence_run`; `ui/timeline_model.py` `SEQUENCE_RESTART_CAPABILITY` + `sequence_restart_supported`;
a **"Restart"** button on a FAULTED run row (`ui/sequences_panel.py` `_SequenceRow`, and `ui/plans_tab.py`
`_PlanRow` — which also gains the RF-FAULT pill it was missing), gated on BOTH `task-rf-health` (the
fault must be detectable) AND `sequence-restart`; `_on_restart` poses the resync/replay choice and posts
the restart. Multi-unit plan restart recovers the first faulted unit's run (single-unit exact) — the
per-unit fan-out is the known TODO, mirroring the plan-export run-id TODO.

**Tests**: `sdr-agent` 554 → 573 (`test_sequence_restart.py`, 19 — level reconstruction incl. launch-level
fallback; resync re-instates future fires + the relaunch shape; replay shifts fires + off-air; replay
collision refusal; the 404/409 guards; a **LIVE** end-to-end that faults a real ramping task at −70,
restarts it, and drives it to completion on schedule; PLUS the review regressions — resync uses the
schedule level at now (D), replay resumes from the crash level + re-instates the whole remainder (E),
the swept param is baked not just power (B), a healthy peer step is not skipped (C), resync past off-air
is refused but replay recovers (A), an open-ended run recovers without a STOP, the replay collision uses
the stop tail and is atomic (F+G), the second-lock re-validation (H), the level survives an unreadable
argspec (re-review 1), the pre-roll / cool-down relaunch stays muted (re-review 2), a bridge param (--bw)
is reconstructed via the argspec (re-verify i), and a faulted multi-task run does not auto-complete when
its healthy peer finishes (re-verify ii)) +
`test_meta_endpoint`. `sdr-client` 1129 → 1136 (`test_restart_ui.py` — model + wrapper post; the gate;
the Restart button visibility on both rows; the resync/replay/cancel routing; the plan fault pill +
`_fault_run_for`).

**Rollout:** OTA-push 1.29.0; the client bundle rebuilt from 1.29.0. **Deferred to Phase 3**: the
task-level Auto-restart-on-fault checkbox + the sequence/plan auto-restart policy (unattended trigger,
budget 2) and the fast-warm IQ cache (§8).

## 14. Open items

- Retrieve the archived `run_<ts>.log` from the affected unit + `df /dev/shm` /
  `cat /proc/sys/vm/max_map_count` / `ipcs -m` / the GR backend → **confirm the exact mechanism** and
  finalize §3.3.
- Confirm the deployed GNU Radio version's exact `vmcircbuf` factory override name
  (`GR_CONF_VMCIRCBUF_DEFAULT_FACTORY` vs. an older form) with `gnuradio-config-info --prefs`, and
  which compiled default it falls to under `GR_DONT_LOAD_PREFS=1`.
- Confirm the Pi 5 image's current `/dev/shm` size, `vm.max_map_count`, and `ulimit -n`.
- Decide `SequenceState.FAULTED` (terminal) vs. an `rf_fault` field on a still-`RUNNING` run.
- Multi-unit per-item `run_id` resolution for plan-level restart.

# RF-fault detection & sequence recovery — design

**Status:** BUILT (P0–P3b, 1.27.4 → 1.36.0). **Root cause CONFIRMED 2026-09-21 — read §14k first.**
The `vmcircbuf` theory this document was written around (§1–§3.6) is **refuted** by the unit's own
logs and a controlled reproduction: the incident was a **tune-before-bind race on the first launch
after a reboot** (the "started after on-air" miss that §3.7 anticipated as a separate item), fixed by
the tune deferral of §14f #4. §1–§3 are kept as the design-time record.
**Author:** engineering, with the unit owner
**Date:** 2026-09-18
**Branch:** `claude/system-familiarization-f5mezz` (all three repos)
**Scope:** cross-repo — `sdr-agent` (runtime + detection), `sdr-client` (alerting + recovery UI),
`sdr-scripts` (fault self-report + fast-warm restart)

> Companion: `docs/incident-fm-chirp-vmcircbuf.md` — the one-page incident report for
> non-engineering stakeholders. This document is the engineering spec.

---

## 1. TL;DR

> **Correction (2026-09-21, §14k):** the transmit script did NOT hit a buffer error and the unit did
> not "stop" transmitting — it never started. The agent's on-air tunes (`power` + `rf on`) were sent
> ~0.5 s before the freshly launched script had bound its control socket (a ~10.5 s cold first launch
> against a 10 s pre-roll); agent 1.27.2 dropped them silently, and the script's gate never opened.
> The paragraph below is the design-time reading.

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
  *(§14k: that line is `vmcircbuf_prefs::get :info: …` — a GNU Radio INFO message this unit prints on
  every launch, present in five successful runs. Not the fault.)*
- **When it failed:** at **flowgraph startup** (confirmed by the owner). It is **intermittent** —
  the *same* signal ran cleanly for **3 hours** the day before, and this class of failure has
  occurred before. *(§14k: "intermittent" = the FIRST launch after a reboot; a warm relaunch works.)*
- **Recovery (as it happened):** stop the sequence → author a new plan → estimate the ramp
  position → re-run. Slow, manual, and visibly improvised.

---

## 3. Root cause of the `vmcircbuf` failure

> **Retained as the design-time hypothesis — REFUTED by §14k.** None of the mechanisms in §3.3 were
> in play (`/dev/shm` 1 % used, no leaked SysV segments, `vm.max_map_count` 1,048,576, no allocation
> error in any log). §3.7, written as a "separate" first-launch cost, describes the actual incident.

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

**RESOLVED (§14k):** none of A–D. The log line was informational; the fault was the on-air tunes
being sent before the script's control socket existed.

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
4. **Pin a known-good buffer backend** if the captured error implicates one — *[design-era text;
   SUPERSEDED by §14f #1: verified against the upstream 3.8/3.10 sources, GR selects the vmcircbuf
   backend from the `vmcircbuf_default_factory` pref FILE under the task HOME and never from an env
   var, and `GR_DONT_LOAD_PREFS` does not govern it (3.10's `prefs.cc` has no such check at all) —
   the agent WRITES that file per launch]* — but note the scripts
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

**Fixes (Phase 0):** *[SUPERSEDED by §14f #1 — the pin is the `vmcircbuf_default_factory` pref file
the agent writes per launch at both GR generations' locations; the env var below is inert]* pin the
backend explicitly in the task **launch env** — because
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
backend** (*as built (§14f #1): the `vmcircbuf_default_factory` pref FILE read back from the task's
HOME — `FaultSnapshot.vmcircbuf_backend_pref`; the env var and any `gnuradio-config-info --prefs`
entry are reported only as extras, the stock runtime conf has no such entry*), the task's `HOME`, and (if `sysv_shm`) an `ipcs -m` summary. It also
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

> **BUILT — as an IN-PLACE speed-up, not a disk cache (measurement-driven pivot, owner-approved).**
> This §8 assumed "L1C/L2C ~30 s IQ generation"; measured on the dev box, the current numpy-vectorized
> generators are fast — **L1C ~0.5 s, L2C `--loop cm` ~0.3 s** — so a cache there is pointless. Only
> **L2C `--loop full`** (the default: the bit-exact 1.5 s CL loop) is slow: **~14 s** (build 7 + filter 7),
> a **736 MB** buffer, ~30-60 s on a Pi. A per-shape 736 MB disk cache was rejected (the SD footprint, and
> a 736 MB SD read ≈ the build it saves); instead L2C's generation itself was sped up, which helps EVERY
> launch with no disk cost and lower peak RAM. **`sdr-scripts` `gps_l2c_tx.py`:** `build_l2c_buffer` returns
> the base as REAL float32 (it IS real BPSK ±1, Q=0 — complex64 doubled the memory traffic for nothing;
> 6.9→3.2 s), and `_circular_convolve` gained a real-FFT (`rfft`/`irfft`) overlap-add that accumulates
> straight into the real slots of the complex64 output (`out.real`; imag stays 0 — no separate accumulator,
> no float→complex copy; ~6.8→~2-3 s). Net **~13.6 s → ~5.9 s (~2.3×)**, output numerically IDENTICAL
> (max|diff| 1.2e-7 ≈ −138 dB across shapes incl. the 92 M loop; `filt.imag` exactly 0; `--self-test`
> unchanged). No agent/client change; `argspec`/`ramp` untouched. Test: `sdr-scripts`
> `tests/test_l2c_fast_filter.py`. Record: `sdr-scripts/CLAUDE.md`.

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

## 14d. Phase 3 — BUILT (`AGENT_VERSION 1.30.0`, capability `sequence-auto-restart`; branch `claude/system-familiarization-f5mezz`)

UNATTENDED recovery — a faulted run whose recovery policy is **"auto"** is restarted by the agent's own
tick, with no operator or client present, so a **scheduled / overnight** run recovers itself. This is
the trigger half of §7.1 built on top of the Phase-2 `restart_run` mechanism (§14c); the fast-warm IQ
cache (§8) stays deferred. Decisions locked with the owner: the trigger is **agent-side** (the box
recovers itself even with no GUI connected); a run-owned rf-fault is recovered **only via the run
policy** (never also via the raw crash-restart supervisor — that would double-transmit); and the budget
**resets after a healthy interval** rather than a hard lifetime cap.

**`sdr-agent` — `SequenceRunner._service_auto_restart(now)`**, called once per `_tick` (~0.25 s) after
`_service_holds`. Like `_service_holds`, it **COLLECTS under `self._lock`, then ACTS after releasing** —
`restart_run` itself takes the non-reentrant `self._lock` and releases it mid-call for the pre-stop, so
awaiting it inside the lock would deadlock the tick loop.
1. **Pass 1 (under the lock)** prunes `_auto_inflight`/`_auto_gaveup` to live runs, then per run:
   - **Healthy-settle reset** — a RECOVERED run (`not run.fault` and `auto_restart_count > 0`) whose
     `auto_restart_task` reads healthy (`_task_healthy` = the task is running AND `ProcessStatus.health
     == OK`) for `AUTO_RESTART_HEALTHY_RESET_S` has its counter zeroed, so an INDEPENDENT fault later in
     a long run gets a fresh budget (not a lifetime cap). Not-healthy restarts the settle timer
     (`auto_restart_healthy_since` cleared). A successful restart clears the marker too, so each attempt
     re-measures the window from scratch.
   - **Select or trip** — a **RUNNING**, `restart_policy == "auto"` run with `run.fault`/`fault_task`
     set and not already in `_auto_inflight`: if `auto_restart_count < AUTO_RESTART_BUDGET` it is added
     to `to_restart` (and to `_auto_inflight`, so a later tick can't re-fire it while the current
     restart is still awaiting); else its breaker trips once (`_auto_gaveup`, `to_trip`). A HOLDING
     fault is left alone (`restart_run` refuses a non-RUNNING run — selecting it would raise every
     tick); `confirm`/`manual` are left for the operator.
2. **Pass 2 (lock released)** — for each `to_restart`, `await restart_run(run_id,
   RestartRequest(mode, restart_at=now))`. On success: increment `auto_restart_count`, stamp
   `auto_restart_task`, clear the settle marker, persist, and fire a **QUIET `sequence_auto_restart`**
   event (annotated `attempt n/budget`). On a raised refusal (resync past off-air / a replay channel
   collision — a real breaker condition): trip via `_auto_restart_gaveup`. Each `to_trip` also runs
   `_auto_restart_gaveup`, which re-fires the **LOUD `sequence_rf_fault`** once (the event the client's
   alarm/pill already handle) and leaves the run **RUNNING-faulted for the operator's manual Restart**.
   `_auto_inflight`/`_auto_gaveup` are in-memory; the durable breaker is the **persisted**
   `auto_restart_count`, so a reload mid-episode never resurrects an exhausted run.
3. **No double-transmit** — `process_manager._watch`'s rf-fault EXIT branch now `return`s right after
   `_flag_rf_fault` (before the crash-restart supervisor), so a run-owned rf-fault is recovered ONLY by
   `restart_run` and the raw supervisor never puts a second process on the single TX channel. (The exit
   is fully recorded first — `_cleanup`, the `ExitRecord`, `state=CRASHED` — the `return` only skips the
   `restart_on_crash` relaunch.) An ordinary crash keeps the crash-restart path unchanged.

`arm()` stamps `req.restart_policy`/`restart_mode` onto the run (default `"manual"`). `models.py`:
`SequenceRun` gains `restart_policy`/`restart_mode`/`auto_restart_count`/`auto_restart_task`/
`auto_restart_healthy_since` (all defaulted, persisted); `ArmSequenceRequest` gains
`restart_policy`/`restart_mode` (default `"manual"`, so a pre-Phase-3 client and the reloaded-run path
never gain autonomy by surprise); the `sequence_auto_restart` webhook type. `config.py`:
`AUTO_RESTART_ENABLED` (global kill-switch), `AUTO_RESTART_BUDGET` (default 2), `AUTO_RESTART_HEALTHY_
RESET_S` (default 60; each `0` disables its limit), capability `sequence-auto-restart`, `AGENT_VERSION
1.29.0 → 1.30.0`. `argspec`/`ramp` untouched (drift guard intact).

**`sdr-client`** (client-only): `api/models.py` — `Sequence`/`CreateSequenceRequest` gain
`recovery_policy`/`recovery_mode` (authored), `ArmSequenceRequest` gains `restart_policy`/`restart_mode`
(the agent wire names), `SequenceRun` mirrors the five auto-restart runtime fields, `PlanItem` gains
`recovery_policy`/`recovery_mode` (`""` = inherit its sequence). `ui/timeline_model.py` —
`SEQUENCE_AUTO_RESTART_CAPABILITY` + `sequence_auto_restart_supported`; `resolve_arm_recovery(client,
policy, mode)` DOWNGRADES `"auto"` to `"manual"` when the unit lacks the capability (so a run never
over-claims autonomy); `fault_pill(run)` decides the row pill (red **RF FAULT** on a fault — an
auto-policy fault that gave up notes the attempt count in its tooltip; amber **AUTO-RESTART ×n** on a
recovered run; None otherwise). `ui/sequence_editor.py` — a recovery combo (operator-restart /
auto-resync / auto-replay), saved onto the request; `_auto_restart_block()` gates saving an auto policy
to a unit lacking the capability (the Library / a manual policy is never blocked). The arm paths carry
the resolved policy: `sequences_panel._arm_at` (from the sequence), `plans_tab._item_recovery` (item
override else inherit the stored sequence) wired into `_arm_plan`, and `timeline_tab._arm_scheduled`
(the schedule — the PRIMARY unattended surface). `ui/theme.py` — an amber `auto_restart` status.

**Tests**: `sdr-agent` 573 → 599 (`test_sequence_auto_restart.py`, 26 — auto-restart fires + recovers
(quiet, no loud alarm); budget exhaustion trips loudly once; a restart refusal trips durably (count→
budget), not a retry loop; a HOLDING fault is not auto-restarted; confirm/manual/globally-disabled are
left alone; the healthy-settle reset zeroes + persists the budget; the settle window floor; the marker
restarts if the task is unhealthy; `reset_s=0` lifetime cap; the budget middle rung + flap→trip;
`budget=0` unlimited; replay-mode through the auto path; the peer-fault per-run budget; the concurrency
guard (`_RestartInProgress`) + the recovered / in-progress quiet stand-down; the RF-safety defer while
the process is alive + `restart_run`'s live-process refusal; a manual restart resets the breaker; the
agent Sequence policy round-trip; arm stamps the policy; the real `_task_healthy`; the process-manager
suppression asserts the fault is FLAGGED then the raw relaunch is suppressed; an ordinary crash still
restarts) + `test_meta_endpoint`. `sdr-client` 1136 → 1151 (`test_auto_restart_ui.py`, 15 — model
defaults + round-trip; the capability gate; the auto→manual downgrade; the fault/recovery pill decision;
`_arm_at` / `_item_recovery` / the plan + schedule arm paths carry the resolved policy; the LibraryClient
policy round-trip; the sequence-editor combo load/save + `_on_save` copies the policy + save gate; the
row pills).

> **Adversarial review outcome** (two find→verify passes, all dimensions — RF-safety, concurrency/
> deadlock, the breaker/reset logic, cross-repo wire consistency, and test adequacy). Confirmed defects,
> all fixed + pinned:
> - **HIGH (concurrency).** A manual `POST /restart` racing the auto trigger could BOTH pre-stop +
>   relaunch the same run (the loser's stop could kill the winner's fresh process), and the auto path
>   misread the loser's refusal as a breaker trip → a spurious LOUD alarm on a just-recovered run. Fix:
>   a per-run **`_restart_inflight`** guard held across `restart_run`'s released-lock window (a second
>   caller refuses cleanly with `_RestartInProgress`), and the auto except-handler now stands down
>   QUIETLY on `_RestartInProgress` or a run whose fault was already cleared / is no longer RUNNING —
>   only a still-RUNNING-and-faulted run is a genuine refusal that trips.
> - **HIGH (RF-safety, cross).** For the true-wedge fault, the Phase-1 watchdog's auto-drop `stop()`
>   takes ~10 s to SIGKILL a SIGTERM-ignoring flowgraph (state STOPPING, so `is_running()` is already
>   False); the auto trigger could relaunch a SECOND process onto the channel during that window — the
>   exact double-transmit the suppression prevents. Fix: a ground-truth **`ProcessManager.is_process_
>   alive`** (`returncode is None`); the auto path DEFERS selection while the faulted process is still
>   alive, and `restart_run` refuses a relaunch over a live process (backstop for a manual Restart).
> - **MEDIUM (cross, feature-defeating).** The authored `recovery_policy` never survived the round-trip —
>   the agent's `Sequence`/`CreateSequenceRequest` AND the client's `LibraryClient` both DROPPED it — so
>   every armed run (and every plan/schedule INHERIT) silently resolved to "manual". Fix: all three now
>   persist + round-trip `recovery_policy`/`recovery_mode`.
> - **MEDIUM (breaker state).** A manual Restart now RESETS the breaker (fresh budget + clears the
>   give-up latch — a tripped run isn't left permanently un-auto-restartable after one operator touch;
>   the auto trigger passes `reset_budget=False`, it is consuming the budget). A refusal give-up is made
>   DURABLE (`auto_restart_count → budget`) so a doomed restart isn't re-selected every tick. The
>   healthy-settle reset now PERSISTS + clears the latch, and its window is FLOORED to `3·HEALTH_POLL_S`
>   so a `reset_s` below the fault-detection latency can't defeat the breaker.
> - **Test coverage** (both suites): the suppression asserts the fault is still FLAGGED before the raw
>   relaunch is suppressed; the real `_task_healthy`; the budget middle rung + the full flap→trip
>   staircase; `budget=0` unlimited; `reset_s=0` lifetime cap; replay-mode through the auto path; the
>   peer-fault per-run budget; the happy-path quiet-only (no loud alarm); the concurrency guard + the
>   recovered / in-progress stand-down; the RF-safety defer + refusal; the agent + LibraryClient policy
>   round-trip; the editor save copies the policy.
>
> Otherwise the RF-emission invariants were VERIFIED clear (no other double-transmit path; a HOLDING /
> confirm / manual policy is never auto-transmitted; no path leaves RF on with nothing to stop it; the
> breaker always trips under default config; a reload aborts a RUNNING faulted run, so the in-memory
> latches can't resurrect an exhausted run). One LOW is documented-not-fixed: in a multi-task run, if the
> auto-restarted task's on-air window ends before the healthy-settle window elapses, the per-run counter
> doesn't reset — a conservative deviation (fewer unattended restarts, earlier operator hand-off), no
> RF-safety consequence.

**Rollout:** OTA-push 1.30.0; the client bundle rebuilt from 1.30.0. `SDR_AUTO_RESTART=0` on a unit
disables the unattended trigger fleet-wide (a faulted "auto" run then waits for a manual Restart, exactly
like "confirm"). **Deferred (Phase 3b)**: the standalone-task "Auto-restart on fault" checkbox (a
`TaskConfig.auto_restart_on_fault` + an owned-query so `process_manager` relaunches a task that ISN'T
owned by a run) and the fast-warm IQ cache (§8).

## 14e. Phase 3b — STANDALONE task auto-restart — BUILT (`AGENT_VERSION 1.31.0`, capability `task-auto-restart`; branch `claude/system-familiarization-f5mezz`)

The **task-level** half of §7.1's Knob A: a task run **on its own** (not inside a sequence/plan) that
RF-faults is relaunched by the agent **with the exact parameters it faulted under**, no operator present.
This is orthogonal to the Phase-3 run policy — it governs only a standalone task — and the two are kept
strictly apart by an **owned-query** so a run-owned fault is never recovered twice (a double-transmit).
The fast-warm IQ cache (§8) stays deferred. `argspec`/`ramp` untouched (drift guard intact).

**`sdr-agent`.**
- **`models.py`** — `TaskConfig.auto_restart_on_fault` (bool, default False) + `max_fault_restarts` (int,
  default 2 — the rolling budget within `restart_window_s`, 0 = unlimited; the pre-relaunch delay reuses
  `restart_delay_s`). `StartRequest.auto_restart_on_fault` (`Optional[bool]`, default None) — a PER-LAUNCH
  override so the Run… form can flip it for one run without editing the stored task. All defaulted (a
  pre-Phase-3b client / reloaded task never gains autonomy).
- **`process_manager.ManagedProcess._maybe_auto_restart_standalone()`** — the decision + relaunch. Stands
  down unless: the effective flag is on (`_auto_restart_override` if the launch set one, else
  `config.auto_restart_on_fault`); the master kill-switch `config.AUTO_RESTART_ENABLED` is on; and the
  **owned-query says no active run owns this task** (checked BEFORE and AFTER the settle delay — a run may
  (re-)arm the task meanwhile). Then a rolling-window budget (`max_fault_restarts` within
  `restart_window_s`) — give up on a fast fault loop, keep recovering an intermittent one (old attempts
  age out). Ground-truth safety gates before `start()`: never over a `RUNNING`/`STARTING` task, never over
  a process whose `returncode is None` (still alive). Relaunches with the **remembered `_last_request`**
  (`start()` stamps it) so the recovered task transmits at the exact parameters, not the bare config
  default.
- **Two detection paths, exactly one relaunch.** Layer 1 — a **natural non-zero EXIT** (the done-watcher
  forced it): `_watch`'s rf-fault branch relaunches, but **only when `not _stop_requested`** (a wedge the
  watchdog auto-dropped has `_stop_requested` set, so `_watch` defers). Layer 2 — the **true wedge** (no
  exit): `_scan_task_health` auto-drops RF via `stop(operator=False)` then fires the relaunch **detached**
  (so the ~`restart_delay_s` settle can't stall the watchdog scanning other tasks). The relaunch **awaits
  the old `_watch` task first**, so `start()` can never race the old exit handling (`_cleanup` of the old
  run's log fh + control socket) and corrupt the new run. An in-flight latch (`_fault_restart_inflight`)
  is the belt-and-suspenders backstop.
- **Operator vs. auto-drop.** `stop()` gained `*, operator=True`; the internal auto-drop passes
  `operator=False`. Only an operator/API stop sets the new **`_operator_stop_requested`** flag, which
  aborts a pending relaunch — so an operator stopping the faulted task wins, while the auto-drop that
  frees the channel does NOT cancel its own recovery.
- **Owned-query wiring.** `ProcessManager.set_owned_query(query)` (applied to every current + future proc,
  mirroring `set_fault_hook`); `main.py` lifespan wires it to **`SequenceRunner.tasks_claimed_by_active_runs`**
  (a public, no-await accessor: the tasks active runs DRIVE, plus every not-yet-fired launch they will still
  perform — broader than `_tasks_owned_by_active_runs`; the review's §14f corrected this line, which named a
  method that never existed). `shutdown()` cancels any pending relaunch so a faulted task can't launch a
  fresh process mid-teardown (both detection paths since §14f).
- **Persistence.** `_spec_to_entry` now emits `auto_restart_on_fault` (and `max_fault_restarts` when
  non-default) so the flag survives the `tasks.yaml` write → `load_tasks()` reload — the primary authoring
  path (a review HIGH: without it the client's saved checkbox reverted and the task never auto-restarted).
- **`config.py`** — capability `task-auto-restart`, `AGENT_VERSION 1.30.0 → 1.31.0`; the master
  kill-switch reuses `AUTO_RESTART_ENABLED` (`SDR_AUTO_RESTART=0` disables BOTH the run-level trigger and
  this).

**`sdr-client`** (client-only): `api/models.py` mirrors `TaskConfig.auto_restart_on_fault`/
`max_fault_restarts` + `StartRequest.auto_restart_on_fault` (all defaulted, skew-safe).
`ui/timeline_model.py` — `TASK_AUTO_RESTART_CAPABILITY` + `task_auto_restart_supported(client)`.
`ui/task_editor.py` — an **"Auto-restart on fault"** checkbox: the offline library always offers it (a
stored definition; the agent is the deploy-time backstop), a live unit gates it on the capability
(`/info`), seeds it from the stored task, and `_on_save` writes it **only when the box is enabled**, so
editing an unrelated field on an unsupported / unreachable unit preserves a flag set for capable units
(a review MEDIUM). `ui/run_task_dialog.py` — the same checkbox in the Run… footer, shown only when the
unit advertises the capability, seeded from the stored task, sending the per-launch override on
`StartRequest`.

**Tests**: `sdr-agent` 599 → 626 (`test_task_auto_restart.py`, 27 — the decision matrix (off / kill-switch
/ claimed-by-a-run / owned-query failure); the per-launch override both ways; both detection paths relaunch
and don't double-fire; the operator-stop vs. auto-drop distinction; the rolling budget (give-up + re-arm +
`budget=0` unlimited); the in-flight latch; the relaunch awaits the old watcher; the relaunch routes
through the launch hook; the `tasks.yaml` persistence round-trip + default-budget omission; `start()`
re-arms the breaker flag; the claimed-query counts live AND pending launches) + `test_meta_endpoint`.
`sdr-client` 1151 → 1166 (`test_task_auto_restart_ui.py`, 15 — the model mirror + StartRequest override;
the capability constant + gate; the task-editor checkbox (library / supported / unsupported / edit-seed /
edit-on-old-unit / save round-trip / preserve-on-unsupported / preserve-when-info-fails); the Run-form
checkbox (hidden / shown+seeded / sends the override / None when unsupported)).

> **Adversarial review outcome** (two independent find→verify passes — RF-safety / double-transmit,
> concurrency/deadlock, the two detection paths, the budget/breaker logic, capability gating, model skew,
> and persistence). Confirmed defects, all fixed + pinned:
> - **HIGH (persistence, feature-defeating).** `main.py::_spec_to_entry` dropped `auto_restart_on_fault`
>   on every structured `tasks.yaml` write (create / update / library deploy), so the client's saved
>   checkbox reverted on reload and the standalone task never auto-restarted. Fix: `_spec_to_entry`
>   persists it (+ `max_fault_restarts` when non-default); a round-trip test (`_spec_to_entry` →
>   `tasks.yaml` → `load_tasks`) pins it.
> - **HIGH (concurrency / RF-safety).** In the wedge path, relaunching from `_scan_task_health` while the
>   old `_watch` was still settling could let `start()`'s new run race the old exit's `_cleanup` (closing
>   the new run's log fh / unlinking its control socket) or let the old `_watch` take its CRASHED branch
>   against the NEW process. Fix: `_watch` relaunches only a natural exit (`not _stop_requested`); the
>   wedge relaunch is the sole one there, is DETACHED (so it can't stall the watchdog), and **awaits the
>   old watcher** before `start()`; the in-flight latch backstops the race.
> - **MEDIUM (double-transmit).** The owned-query counted only a run's FIRED launches, so a run ARMED
>   during the standalone's settle delay (its start not yet fired) was invisible → the standalone
>   relaunched the task, then the run's own launch collided (a dropped run launch at the wrong level, or
>   a genuine second one-shot on the channel). Fix: the gate is now `tasks_claimed_by_active_runs` — any
>   task an active run is driving OR is still going to launch (an un-fired start/run step) — re-checked
>   after the delay; a task the run already stopped is not claimed, so a genuinely standalone task still
>   recovers.
> - **MEDIUM (client clobber).** Editing an unrelated field on an unsupported unit — or one whose `/info`
>   never arrived — coerced the disabled checkbox's False into the saved spec, stripping an auto-restart
>   set for capable units. Fix: `_on_save` writes the flag only when the box is enabled (supported +
>   confirmed), else `_orig_entry` preserves the stored value.
> - **LOW/MED (RF level).** The relaunch called `ManagedProcess.start` directly, skipping the manager's
>   `_gate_precommand` — so if the attenuator moved between fault and relaunch (an intervening tune /
>   another calibrated task) the recovered task could deliver the wrong (possibly over-) power. Fix: a
>   launch hook routes the relaunch through `ProcessManager.start` (positions the attenuator + carries
>   the carrier) — a faithful reproduction; a bare start remains the isolation/test fallback.
> - **LOW (observability).** `fault_restart_giving_up` was never cleared, so after one breaker trip the
>   give-up ERROR was suppressed and a UI reading the flag misreported a recovered task. Fix: `start()`
>   re-arms it (alongside its crash-loop twin).
> - **Hardening (boot window).** The fault/owned/launch hooks are now wired BEFORE `_manager.startup()`
>   (the runner is constructed first — side-effect-free), so a task faulting during autostart consults a
>   real (empty) query instead of the None-skip path; `_runner.startup()` (abort in-flight runs) still
>   runs after the manager is up.
>
> RF-emission invariants otherwise VERIFIED clear: a run-owned fault is never also relaunched here (the
> claimed-query, re-checked after the delay, and `_flag_rf_fault` awaits the run-coupling hook first); the
> relaunch never runs over a live process (the `returncode` gate); an operator stop always wins (the auto-
> drop uses `operator=False`); the two detection paths fire exactly one relaunch (the `_stop_requested`
> split + the in-flight latch, no `await` between the latch check and set); the budget breaker gives
> exactly `max_fault_restarts` relaunches then trips; a pending relaunch is cancelled on shutdown. One
> documented limitation: the standalone relaunch does not gate on an operator MANUALLY starting a
> *different* transmit task during the fault window (a shared-channel coordination gap that predates this
> feature and applies to any direct start); the run-collision case above IS gated.

**Rollout:** OTA-push 1.31.0; no re-provision (agent code only), rebuild the client bundle from 1.31.0.
`SDR_AUTO_RESTART=0` disables it alongside the run-level trigger. **Phase 3b, other half — DONE (revised
by measurement):** the "fast-warm" §8 shipped as an IN-PLACE ~2.3× speed-up of the one genuinely-slow
generator (`sdr-scripts` `gps_l2c_tx.py` `--loop full`), not a disk cache — L1C / L2C-cm measured ~0.3-0.5 s,
so a cache was pointless; see §8's BUILT note. **Phase 3b (and the RF-fault recovery arc P0–P3b) is now
complete.**

## 14f. Adversarial review of P0–P3b — 31 findings FIXED (`AGENT_VERSION 1.31.1`, no capability; branch `claude/system-familiarization-f5mezz`, cross-repo)

**Method.** Eleven review dimensions (P0 · P1 · P2 reconstruction · P2 guards · P3 · P3b · asyncio concurrency ·
RF-emission safety · client/contract skew · cross-repo consistency + tests · DSP), one reviewer agent each →
41 raw findings → 32 unique (7 defects found independently by 2–4 dimensions; the abort-vs-restart race by four)
→ each adversarially verified by independent skeptics instructed to default to refuted (HIGH: a code-walk **and**
a reproduction agent that wrote and ran a pytest against the real runner/manager) → **30 confirmed, 2 refuted**
(#26 D-state consequence doesn't follow; #29 trigger unreachable) → a synthesis pass re-checked every fix against
the code and the tests → a completeness critic named what the dimension split couldn't see, one of which (#33)
was confirmed by hand. Full record: the review report + `findings.json` kept with the session. Every fix is
pinned by `tests/test_review_fixes.py` (30 tests; the five HIGH ones are the verifiers' reproductions inverted,
over real subprocesses), plus the scripts/client tests named below. Agent suite 626 → 656; client 1166 → 1179;
scripts 106 → 110.

**The headline correction — the P0 GR pin was inert (#1, HIGH).** Verified against the upstream sources
(maint-3.8 + maint-3.10 `gnuradio-runtime/lib/vmcircbuf.cc`, `vmcircbuf_prefs.cc`, `sys_paths.cc`): GR selects
the backend by reading a per-key **pref FILE** named `vmcircbuf_default_factory` whose content is a factory
NAME (`gr::vmcircbuf_mmap_shm_open_factory`) — 3.8 at `$HOME/.gnuradio/prefs/`, 3.10 under `userconf()` =
`$GR_PREFS_PATH` | `$XDG_CONFIG_HOME/gnuradio` | `$HOME/.config/gnuradio` (| the legacy `$HOME/.gnuradio`).
That code never consults the `GR_CONF_<SECTION>_<OPTION>` env override (it exists only in `gr::prefs`) and is
NOT governed by `GR_DONT_LOAD_PREFS` — §3.6's "verify against the deployed version" was never done, and
§14a/CLAUDE.md asserted the env var as "the only pin GR reads". With no valid file GR probes createfilemapping →
**sysv_shm** → mmap_shm_open → mmap_tmpfile and persists the first that works — on Linux `sysv_shm`, the leaky
suspect — so every unit ran the incident configuration while the P1 snapshot and the client dialog reported the
inert env value as the effective backend and skipped `ipcs`. **Fix:** `process_manager._launch_env_pins` now
writes the pref file (idempotently, per launch) at BOTH locations under the pinned task HOME via
`_pin_gr_vmcircbuf_pref` and pins `GR_PREFS_PATH=<HOME>/.config/gnuradio` so 3.10 is deterministic regardless
of an ambient `XDG_CONFIG_HOME`; `system.capture_fault_snapshot` reads that file back
(`_read_vmcircbuf_pref`, from the task's `/proc/<pid>/environ` HOME/GR_PREFS_PATH, else the configured HOME)
into a new **`FaultSnapshot.vmcircbuf_backend_pref`** (the EFFECTIVE backend; a missing file adds the note
"no vmcircbuf pref file — GR chose its own backend" and triggers the `ipcs -m` capture); the client dialog keys
on it. The `GR_CONF_*` env var stays exported as documentation. **Rollout consequence:** the pin is now AGENT
code, so it reaches field units by the OTA "Update agent…" push — no re-provision needed for the backend fix
(the service-unit env var is inert and harmless either way).

**RF left on (cardinal sin 2).**
- **#33 (HIGH, critic's gap, confirmed by hand)** PANIC did not abort a pending standalone relaunch: a faulted
  task reads `crashed` (exit path, sleeping its settle delay inside `_watch`) or `stopped` (wedge path, detached
  `_relaunch_task`), and `recovery.panic_stop` only stopped `running`/`starting` — the task came back on air
  AFTER panic, with every run already aborted so nothing would stop it. → new
  **`ProcessManager.cancel_pending_relaunches()`** (sets every proc's operator-stop flag, cancels a pending
  `_relaunch_task`, cancels a CRASHED watcher that is in its delay), called by `panic_stop` right after the
  run/event aborts and by `shutdown()`.
- **#6 (HIGH, four dimensions)** abort racing a restart relaunch: `_abort_run` stopped only `is_running()` tasks
  and flipped ABORTED *after* the loop, so a relaunch fire the tick had already collected came up RF-on in an
  ABORTED run whose STOP never fires. → `_abort_run` leaves the active states FIRST (under the lock), stops
  everything **`is_live`** (RUNNING, STARTING, or the OS process still alive) with a second sweep, and
  `_fire_step` (a) re-checks the run under the lock before stamping/acting and (b) stops a launch that
  completed into an already-dead run (the abort can land while the launch is parked in the attenuator
  pre-command — the case no sweep can see).
- **#10** `stop()` during STARTING was lost (no process to signal → `_cleanup` + "success", then `start()`
  resumed into RUNNING with the log closed and the socket unlinked). → a `_spawned` event: `stop()` waits for
  the spawn (bounded 30 s) and `start()` kills the child it just spawned when it sees `_stop_requested`, raising
  "stopped during its launch".
- **#12** `shutdown()` cancelled only the wedge-path `_relaunch_task`; the exit-path relaunch slept inside the
  CRASHED task's `_watch` and spawned into the teardown. → a manager `_shutdown_flag` set FIRST, checked after
  every restart/settle sleep; `cancel_pending_relaunches()`; STARTING procs stopped too.
- **#11** an operator Stop during the relaunch's ≤ 5 s attenuator pre-command was a no-op (and `proc.start()`
  then cleared the flag). → `stop(operator=True)` cancels a pending `_relaunch_task`; `ProcessManager.start`
  re-checks the operator-stop / shutdown flags AFTER `_gate_precommand` for an auto-restart source.

**Wrong level / hot relaunch (cardinal sin 3).**
- **#2 (HIGH)** the standalone relaunch used `_last_request` verbatim — a task the operator live-tuned to
  `rf off` / −100 dBm came back RF-on at the launch level. → `ProcessManager.set_params` records every applied
  value in `proc._live_applied` (cleared on start); `relaunch()` bakes them onto the launch args by the argspec
  flags (`agent/cmdargs.overlay_live_params`, the gate via its own flags) and STANDS DOWN (logged) if the task
  was tuned but the argspec is unreadable — never a hot guess.
- **#5 (HIGH)** `hold_now` and `on_task_fault` stamped the SAME `"skipped"` sentinel, so a resync restart counted
  the fast-forward-skipped up-ramp points as the schedule's level and relaunched at the ramp TOP the operator
  skipped past (reproduced: −30 relaunched over −70 on air). → `hold_now` stamps **`"skipped:hold"`**;
  `_counts_at_cutoff` counts only the fault sentinel `"skipped"` (and only for resync); `_live_tasks_of` treats
  both as not-a-launch; the restart plan never re-instates a hold-skipped fire.
- **#4 (HIGH)** a re-instated tune landing before the relaunched script bound its control socket (every real
  generator binds AFTER its IQ build — seconds on a Pi) was fired into nothing and marked fired: a down-ramp held
  the higher level for a dwell, a lost RF-on left a "recovered" run muted while the healthy-settle zeroed the
  budget. → `_fire_step` **DEFERS** a tune (leaves it un-fired for the next tick) while
  `ProcessManager.tune_ready` says the socket is absent and the task is younger than
  `CTRL_BIND_GRACE_S` (30 s, config); replayed fires are floored to `now + 1 ms` so none sort before the
  synthetic START; the auto trigger passes a FRESH `restart_at` instead of the tick's stale `now`.
- **#18** with `spec=None` the RF gate and a tuned bridge param had no fallback: a muted-pre-roll launch
  relaunched `--rf off` mid-transmission (recovered-but-silent, budget reset). → `_relaunch_start_fire` raises
  **`_RestartDeferred`** when the schema is unreadable AND the run tuned a dest without a spec-independent
  fallback; the auto trigger stands down for the tick (no trip, retry next), a manual POST maps to 409.
- **#16** the pre-stop ran BEFORE the guards that can refuse, so a refused Restart killed a task the operator had
  restarted by hand. → `restart_run` dry-runs the plan, the guards and the reconstruction in the FIRST lock
  (`_plan_restart`), refuses a task started by hand after the fault (`_started_after`) instead of pre-stopping
  it, and re-plans at commit as the backstop.
- **#15** a never-restarted faulted run "owned" its dead task forever, so arm guard A0 exempted a HAND-started
  instance. → `_tasks_owned_by_active_runs` excludes `run.fault_task` while faulted; the owned-query
  (`tasks_claimed_by_active_runs`) still claims it explicitly, so the standalone path stands down.

**Recovery that never happens / undetected (cardinal sin 4).**
- **#3 (HIGH)** `apply_sequences` (PUT /library, the client's only library→unit path) rebuilt
  `Sequence(id,name,description,steps)`, dropping `recovery_policy`/`recovery_mode` — the overnight schedule armed
  `manual` while the operator authored `auto`. → `seq.model_copy(deep=True)`. Client **#21**: the library drift
  fingerprints ignored the policy and the auto-restart flag, so a policy-only change never showed as drift
  (`state/library_sync.py` + `tests/test_library_sync_policy.py`, 12 tests). **#24** the agent `PlanItem` lacked
  the recovery fields the client's carries → mirrored.
- **#13 / #14** (one root cause) `_scan_task_health` decided "faulted", awaited the slow `_flag_rf_fault`, then
  `stop()`ed whatever the proc held by then: an exited process (Layer 1) → `stop()`'s CRASHED branch cancelled
  the exit-path `_watch` mid-`_flag_rf_fault` (alarm + run coupling lost — a silent, unrecovered fault); an
  already-relaunched task → the healthy relaunch SIGTERMed and relaunched again (two budget slots per fault).
  → the scan captures the process before the read and **returns if it is no longer the RUNNING one** (before
  the flag and again before the stop: `_scan_stale`); `stop()` cancels a CRASHED watcher only while it is in
  a restart/settle DELAY (`_in_restart_delay`), never mid-exit-handling.
- **#7 (scripts)** all 30 adopters tore down with `finally: ctrl.close(); tb.stop(); tb.wait()` WITHOUT setting
  `stop`, so ANY Python exception in the loop read as an RF fault (false marker; crash-restart bypassed; an
  auto run burned its budget relaunching a deterministic traceback). → `stop.set()` first in every adopter's
  teardown; `tests/test_txhealth_adoption.py` pins it statically for all 30 and behaviourally on `cw_tx.py`.
- **#17** `has_future_stop` scanned only the fault-SKIPPED plan, so a fault taken while HOLDING then Proceed
  (an un-skipped future STOP) was refused forever and tripped the auto breaker. → an un-fired future STOP in
  `run.steps` counts too. **#31** `_service_holds` counted fault-skipped fires as window-A done and parked a
  faulted run into HOLDING (unrestartable) → requires `not run.fault`.

**Runaway recovery.**
- **#19** `_task_healthy` was true from spawn, so a slow generator faulting at the end of a warm-up ≥ the settle
  window had its counter zeroed every cycle — an unbounded relaunch loop with the radio silent (the incident's
  own fault class). → `paramkit.txhealth.watch_flowgraph` prints **`HEALTH state=transmitting`** when called
  (right after `tb.start()` in every adopter); the watchdog stamps `proc.transmitting_at` on it;
  `ProcessManager.task_transmitting_confirmed` requires it for a script whose source uses `watch_flowgraph`
  (`expects_tx_marker`, read off the script once) and keeps the running-and-OK rule for scripts without it
  (FIFO stagers, mocks, x410); `_task_healthy` consults it.
- **#20** with `AUTO_RESTART_BUDGET=0` a REFUSED restart was retried every tick forever (pre-stop each time). →
  a tripped run (`_auto_gaveup`) is never re-selected.
- **#28** a task claimed ONLY by a not-yet-fired launch got no recovery from either side. → the standalone path
  now distinguishes a DRIVEN claim (stand down: the run's policy owns it) from a PENDING one
  (`SequenceRunner.tasks_pending_launch_by_active_runs`, wired via `set_pending_query`) and waits the latter out
  (`_wait_out_run_claim`, bounded by `restart_window_s`). **#27** the owned-query now also counts a HOLDING
  run's deferred window-B start/run definitions.

**Operability / config / docs.** **#9** the boot pre-image's single pre-check let a task launched during the
≤ 55 s probe collide on the SDR → `ProcessManager.device_free` (an Event) is cleared around the probe and
awaited by `start`/`run_oneshot`. **#8** `PREIMAGE_TIMEOUT_S <= 0` now disables the pre-image (it spawned and
SIGKILLed the probe). **#25** `HEALTH_POLL_S <= 0` now disables the watchdog (it spun). **#23** `uhd.log` is
rotated per run (`LogManager.rotate_uhd` → `uhd_<ts>.log`) and pruned with the run logs. **#30** the
reconstruction mirrors `_build_command` for `replace_args=True, args=[]` (the configured `--power` survived).
**#22 (scripts)** `_circular_convolve`'s unreachable `m >= n` branch refuses loudly instead of truncating the FIR.
**#32** §14e named a non-existent `owned_task_names` (corrected above). Shared arg helpers moved to
`agent/cmdargs.py` (the `SequenceRunner` statics delegate).

**Refuted, for the record.** #26 (the watchdog blocking on a D-state process leaves later wedges undetected):
with one radio per unit no later task can wedge until the same USB reset frees the device. #29 (the stale `now`
across a ≤ 10 s pre-stop): the trigger — a manual Restart finding the faulted process still RUNNING — is
unreachable, since the fault is stamped inside `_flag_rf_fault` and the auto-drop holds the process in STOPPING
before any restart can see it (and `restart_run` refuses over a live process anyway).

**Still open from the critic (not fixed here, by design or scope):** the x410 tree is outside the entire
detection stack (its engine's stdout is not the channel task's log — a scope note, RPi-only detection);
`_reconcile_on_startup` aborts an active `auto` run on an agent restart/OTA update with no morning-after alarm
(documented Phase-1 fail-safe); `HEALTH_FAULT_PATTERNS` substring precision (`"vmcircbuf"` matches any mention;
a false hit auto-drops a healthy transmitter — no false hit exists in today's scripts); wall-clock steps vs
`restart_at`/replay shift on an RTC-less Pi; a systematic "0 = disable" sweep of the remaining knobs.

## 14g. Every parameter + the script's elapsed time — BUILT (`AGENT_VERSION 1.32.0`, no capability; branch `claude/system-familiarization-f5mezz`, cross-repo)

Owner follow-up after the §14f review: *"make sure that this is not limited to the power in a ramp, but
that the process is restarted with all parameters correct, even if there's no ramp"* and *"for signals
that use time-dependent scripts, such as the cw drift, the script should declare something that lets it
be restartable at the correct time"*. Two gaps, both closed.

### Every parameter, on both restart paths

The run-owned reconstruction (`SequenceRunner._relaunch_start_fire`, §7) baked only NUMERIC tuned values
back onto the relaunch — a tuned choice/string (a drift mode, a modulation, the RF gate handled apart)
silently reverted to the launch value, while the standalone relaunch (§14e, `ProcessManager.relaunch`)
already baked everything through `cmdargs.overlay_live_params`. Both paths now use that ONE helper: the
RF gate via its own flags, every other tuned dest with known argspec flags by value (numbers formatted
`%g`, strings as-is; a store_true trigger such as cw_drift's `--restart` is an EVENT, not state, and is
never re-fired), `--power`/`--gain` via `LEVEL_FALLBACK_FLAGS` when the argspec is momentarily
unreadable. A run that ramps nothing was always covered — its launch args ARE its state — and is now
pinned by a test (`test_restart_with_no_ramp_reproduces_every_launch_and_tuned_param`).

**Hand-tuned parameters are carried too.** A value the operator applied through the Tune… dialog
during a run is recorded on the process (`_live_applied`, review fix #2) but is NOT in `run.steps`, so
the schedule walk never saw it. The reconstruction now merges `ProcessManager.live_applied(task)` for
every dest the schedule NEVER drives (the crash-time live state is the truth there — a hand-lowered
power on a fixed-power run, a hand-muted gate); a dest the schedule DOES drive follows the schedule's
position at the cutoff (a hand-tune of a ramped power is superseded by the ramp's next point anyway).
Merged only when the run's counted epoch launched the task (the record belongs to that process); the
`_RestartDeferred` (#18) check runs over the merged state, so a hand-tuned choice with an unreadable
schema defers too.

### The script-declared elapsed time (`is_elapsed`)

A time-dependent script owns a clock the agent cannot see: cw_drift moves the carrier as
`drift_freq(now − t0, …)` from its launch. Relaunching it "with the right parameters" restarted the drift
from the START frequency. The existing `TaskConfig.resumable` / `--start-offset` mechanism is
operator-configured per task and never reached the restart paths. Now the SCRIPT declares it:

- **paramkit** `Param.is_elapsed` (`number(..., is_elapsed=True)` / `integer(...)`, emitted by
  `to_dict`): the ONE parameter that takes the seconds already elapsed on the script's own timeline.
  The script owns the semantics (it shifts its clock by that much at launch). Extracted by the static
  `agent/argspec.py` (mirrored byte-identically to `sdr-client/api/argspec.py`; drift guard green).
- **`agent/cmdargs.py`** `elapsed_param(spec)` / `elapsed_of_args(args, p)` (the LAST occurrence, like
  argparse; junk/negative → 0) / `bake_elapsed(args, p, s)` (ms resolution, via the flag the launch
  used, else the canonical first flag).
- **Run-owned restart** — `_relaunch_start_fire(..., elapsed_at=)`: the counted launch's own elapsed +
  `(elapsed_at − that launch's actual instant)`; `restart_run` passes **`now` for resync** (rejoin the
  schedule as it stands) and **`fault_at` for replay** (the rest of the profile is shifted by the
  down-time, so the clock is too). A fault-skipped launch counted by resync runs from its SCHEDULED
  instant (`_fire_instant`). The synthetic relaunch fire carries the baked value, so a SECOND fault
  chains correctly (500 carried + 40 s run → 540).
- **Standalone relaunch** — `ProcessManager.relaunch`: launch elapsed + `proc.age_s()` (seconds since
  the faulted spawn — resync semantics; a standalone drift rejoins its own wall-clock, it has no
  schedule to shift). Unknown spawn time ⇒ left as launched. The launch request is otherwise untouched
  (`env_overrides`, custom args).
- **`build_resume_request`** — a non-`resumable` task whose script declares the marker is resumable by
  contract: the arm-time `resume_offset_s` is injected via that flag (the operator-configured
  mechanism is unchanged; a script with no marker still gets an empty request).
- **`sdr-scripts` `cw_drift_tx.py`** `--elapsed` (`-Elapsed`, seconds, min 0, default 0,
  `is_elapsed=True`): `t0 = monotonic() − elapsed`, and the tone is BORN at that point — the top block
  is built at `f0 = drift_freq(elapsed, …)` in its LO window (`plan_lo`) with the SDR gain folded THERE
  (the attenuator split stays pinned at the START carrier, where the agent positions it from `--freq`),
  so nothing is emitted at the start frequency first. The banner reports `resumed at : N s into the
  drift → f MHz`. `--restart` (the live trigger) still re-runs from the start.

**Limitations (documented, not fixed).** A live `--restart` trigger fired before the fault is not
replayed — the elapsed counts from the launch, not the trigger (a per-script semantic; declare a marker
for it if a script ever needs it). With `spec=None` the elapsed cannot be baked (no flags known); the
restart proceeds as before (the #18 deferral covers only TUNED dests) — the argspec is memoised on the
first successful read, so this is a script-file-vanished condition. The FIFO `--duration` stagers
(`gps_l1p`/`gps_l2p`/noise) have a finite timeline too and could declare the marker later.

**Tests.** `tests/test_restart_all_params.py` (12: a tuned choice + carrier + trigger; a no-ramp run
reproduces every parameter; the hand-tune merge + schedule-wins; no merge without a counted launch;
resync-now / replay-fault elapsed; accumulation across a prior relaunch; a fault-skipped launch counts
from its scheduled instant; no marker ⇒ nothing invented; the standalone relaunch + untouched request;
`build_resume_request`; the marker through paramkit + argspec; the cmdargs helpers), the existing
paramkit/argspec marker tests extended, `sdr-scripts/tests/test_cw_drift.py` (schema; the banner; the
REAL `main()` in-process: born at the resume point, clock continues from `--elapsed`). Agent 656 → 668;
scripts 110 → 112; client 1179 (mirror only).

**Rollout — ORDER MATTERS.** paramkit ships INSIDE the agent release, and `cw_drift_tx.py` now calls
`number(..., is_elapsed=True)`: on a unit still running agent ≤ 1.31.1 that script CRASHES at
`build_script()` on every launch (`TypeError: unexpected keyword argument 'is_elapsed'`) while the
agent's static upload validator and the client's static reader both accept the file (§14h C-1). So:
**OTA-update EVERY unit to 1.32.0 FIRST, then deploy the library.** The agent advertises the
capability **`paramkit-is-elapsed`** and the client REFUSES to ship a script whose static argspec
carries the marker to a unit without it (`api/script_markers.py` + `AgentClient.upload_script` /
`deploy_library`, the same shape as the `CAL_*` gates), so the wrong order is refused per unit with the
reason instead of bricking the drift. (§14i adds a second marker, `resets_elapsed` → capability
`paramkit-resets-elapsed`, 1.33.0, and §14j a third, `is_clock_origin` → `paramkit-clock-origin`, 1.34.0 —
the same gate, the same order.) No other client change (the new `--elapsed` renders as an
ordinary launch field, default 0).

## 14h. Second adversarial review — the §14f fixes + §14g re-reviewed; 30 findings FIXED (`AGENT_VERSION 1.32.0`, capability `paramkit-is-elapsed`; branch `claude/system-familiarization-f5mezz`, cross-repo)

Seven parallel reviewers (RF-safety of the new reconstruction · elapsed-time math + cw_drift ·
abort/stop/shutdown concurrency · detection→recovery decision path · GR pin / boot / ops ·
tests-as-specification · cross-repo consistency), each reproducing its findings against the real
runner where it could (their repro pytests live outside the repo). Verdict: the §14f mechanisms hold in
the interleavings they were built for; what broke was the same state machine one step outside them, and
two genuine HIGHs. Everything below is fixed and pinned (`tests/test_review_fixes_2.py`, 31 tests, plus
the extended §14g tests; agent 668 → 702, scripts 112 → 114, client 1179 → 1185).

**HIGH.**
- **C-1 (cross-repo) — the 1.32.0 library crashed cw_drift on a ≤ 1.31.1 unit.** See the §14g
  rollout note: capability `paramkit-is-elapsed` + the client's marker deploy gate
  (`api/script_markers.py`, `tests/test_script_marker_gate.py`).
- **C1 (concurrency) — `start()` over a STOPPING slot spawned a SECOND process.** A stop's ≤ 10 s
  SIGTERM grace left the slot STOPPING with the process alive; `ManagedProcess.start()` refused only
  RUNNING/STARTING, so a manual/scheduler/restart launch put a second transmitter on the channel and
  the stop's continuation then SIGKILLed / cleaned up the WRONG one (the old one stayed on air,
  untracked). Now `start()` refuses a STOPPING slot / a still-alive process ("still stopping"), `stop()`
  binds the process it began on, `ProcessManager.restart()` waits the stop out (`wait_stopped`).
- **W1 (decision path) — resync relaunched INSIDE a scheduled OFF gap.** A sequence launching one task
  twice (START…STOP…gap…START…STOP) faulted in epoch 1 and restarted in the gap got a synthetic START
  at `now` with epoch 1's parameters — RF through a scheduled silence, then epoch 2's START refused
  "already RUNNING" so epoch 1's signal ran through epoch 2's whole window. The reconstruction now walks
  STOP fires too: when the latest counted fire is a STOP, `_relaunch_start_fire` returns None — the
  fault is cleared and the re-instated second START relaunches on schedule (replay, which counts fired
  fires only, still resumes the crash point).

**MEDIUM.**
- **C2** at one instant a run-2 START (a power-carrying muted launch, rank 0) sorted BEFORE run-1's
  STOP of the same task — the START was refused, stamped fired, the STOP then killed the task and run 2
  ran "on air" with nothing transmitting (the 2 s-gap day-schedule packing). `_tick` sorts a STOP
  before any launch at the same instant; a failed START is now loud (W4).
- **C3/O1** a launch parked before `proc.start()` (the boot pre-image gate, up to 55 s; the attenuator
  pre-command) read STOPPED, so a Stop / PANIC found nothing and the transmitter came up AFTER the
  operator stopped it. `stop()` already latched `_operator_stop_requested` on an idle slot; PANIC/
  shutdown now bump `ProcessManager._panic_epoch`; `start()`/`run_oneshot` re-check both after their
  gates for EVERY source (a fresh operator launch clears an older intent first); the scheduler stops a
  launch that completes after its event was cancelled.
- **C4** an exception/cancel inside the STARTING window (missing cwd/interpreter, ENOMEM, a log
  OSError, a cancelled relaunch mid-exec) stranded the slot STARTING for ever: every later stop waited
  30 s, abort/PANIC/shutdown stalled, the task was unstartable until an agent restart. `start()` wraps
  the window and settles it (kill a spawned child, release `_spawned`, cleanup, re-raise); a stop that
  times out on a stranded slot settles it too; the wait is 10 s.
- **C5** `hold_now` landing while a window-A launch was in its pre-command flipped the run HOLDING and
  `_fire_step`'s post-launch check read HOLDING as "dead" and stopped the task the Hold froze. HOLDING
  is kept.
- **W2** a #4-deferred tune outlived its run's STOP and fired into the SUCCESSOR run's process on the
  same task (a leaked cool-down `rf off` muted the next plan). A due tune whose task the run no longer
  owns — its own STOP fired after its launch, or a later run launched the task — is dropped as
  `"skipped:stale"`, never deferred; **W3** `CTRL_BIND_GRACE_S` 30 → 180 s (L2C-full binds after ~14 s
  here, 30–60 s on a Pi, plus a cold FPGA load) and a lost tune/launch is annotated in the run log.
- **W4** a committed relaunch whose `start` FAILED left a "recovered" run (fault cleared, budget
  consumed, quiet event sent) with a dead task and no alarm; a failed START now couples an RF fault into
  the run (`on_task_fault("launch failed: …")`, loud), and a failed standalone relaunch re-raises the
  health alarm.
- **W5** a SECOND task faulting in an already-faulted run was recovered by nobody (the run policy keys
  on one `fault_task`; the standalone path stood down as "driven"). It is now coupled (its steps
  skipped, the alarm raised, the fault text appended) and released from the driven claim so its own
  Auto-restart-on-fault checkbox may act (`_extra_faulted`).
- **O2** with the watchdog disabled (`HEALTH_POLL_S ≤ 0`) nothing read the transmitting marker, so the
  breaker's healthy-settle never reset and the third fault of the night got no auto-restart;
  `task_transmitting_confirmed` falls back to running-and-OK when the watchdog is off.
- **R1** a hand tune applied AFTER the schedule's last counted set of the same dest was discarded (the
  relaunch un-muted / raised a task the operator had silenced): `set_params` stamps
  `_live_applied_at`; a driven dest takes the hand value when it is later than the schedule's last
  counted set. **R2** the live record was merged even when it belonged to ANOTHER process (a hand start
  after the fault; a first-epoch process under a resync-counted later launch): merged only when the
  process's `started_at` lies between the counted launch fire and the fault.
- **R3/E3** an arm-time resume injection (`build_resume_request` — the marker, or the configured
  `--start-offset` / env mode) lived in `StepFire.resume_offset_s`, not `args`, so the reconstruction
  dropped it: the drift resumed that many seconds early. The launch is rebuilt exactly as `_fire_step`
  built it; the marker path accumulates; the arg-mode flag is advanced by the time run; env mode rides
  the synthetic fire's `resume_offset_s`.
- **E1/R4** `%g` formatting rounded every reconstructed number to 6 significant digits (a 1602.5625
  MHz carrier → 1602.56; an integer 1234567 → `1.23457e+06`, refused by an integer param) →
  `cmdargs.num_text` (exact repr, whole numbers plain, integer-kind rounded); **E2/R5** an
  `integer(..., is_elapsed=True)` marker was baked as a ms float its parser refused → whole seconds.

**LOW (all fixed unless noted).** C6 shutdown reaps a STOPPING slot whose process is alive + the
auto-drop stop is shielded from the health task's cancel; C7 the wedge relaunch awaits the old watcher
under `asyncio.shield`; W6 a `_RestartDeferred` retry is bounded (`AUTO_RESTART_DEFER_TICKS`, 240 ≈
60 s) then trips loudly; W7 a RUNNING-faulted run whose channel span is over completes (`_channel_over`)
and releases its claim; W8 `hold_now` refuses a faulted run; W9 `expects_tx_marker` never memoises a
miss and `reload()` clears it; W10 the transmitting marker is stamped only for the process the bytes
came from; W11 `_started_after` counts a launch still in flight; O3/O4 the GR pref file is compared
byte-exact (3.10 exact-matches; a trailing newline made GR probe and persist sysv_shm) and written
atomically; O5 `_gnuradio_default_factory`'s docstring corrected (the stock conf has no such entry);
O6 the boot pre-image skips when a task is launching (`is_live`) and `restart()` honours the gate;
E4 an injection-only START over an empty-args replace launch appends (keeps the configured command);
E7/R6 `--flag=value` tokens are read/rewritten, a dangling last flag gets its value; C-4 the client's
"no pref file" hint names the pin knob. **Documented, not fixed:** ~~a live `--restart` trigger fired
before the fault is not replayed (E5)~~ — FIXED in §14i (the owner's normal workflow fires it AT on-air); the
baked elapsed runs ahead by the difference between a cold and a warm launch's latency (a few s, E6);
the tick loop still fires launches serially, so a launch parked on the boot gate delays other runs'
fires for that window (O1, boot only); `spec=None` cannot bake the elapsed (G13, pinned as "proceeds
without it"); the client's `api/models.py` was committed as a whole-file CRLF→LF rewrite in dd88953
(cosmetic; no EOL policy yet, C-3); no mock declares `is_elapsed`, so the headless unit cannot exercise
the bake end-to-end (C-5). **Tests-as-spec critic:** 14 §14f mechanisms had no discriminating test;
those now pinned in `test_review_fixes_2.py`: a real `start()` clears the live record (G3); an operator
stop cancels a pending relaunch while the auto-drop does not (G7); the shutdown flag ALONE stands a
crash-restart down (G4); a stop never cancels a CRASHED watcher outside its delay (G6); `run_oneshot`
waits on the device gate (G8); `_wait_out_run_claim` gives up after its window (G10); a replay never
re-instates a hold-skipped fire and floors a due-but-unfired one past now (G1/G9); `_fire_step` on an
aborted run stamps nothing (G2); cw_drift's calibrated gain is folded at the resume frequency with the
split pinned at the start carrier (G12, scripts); the client's hidden-checkbox assertion uses
`isHidden()` (G16).

## 14i. The elapsed-RESET trigger — `resets_elapsed` (`AGENT_VERSION 1.33.0`, capability `paramkit-resets-elapsed`; branch `claude/system-familiarization-f5mezz`, cross-repo)

The owner's standard drift workflow: launch `cw_drift` **X seconds before on-air with `--rf off`**, then
AT on-air fire `rf on` **and `--restart`**, so the drift genuinely begins at T0. That makes the trigger the
NORMAL case, not the rare one §14h documented: a restart that counted the elapsed from the launch resumed
the drift X seconds too far along. Now the script declares which live trigger restarts its own clock:

- **paramkit** `Param.resets_elapsed` (`flag(..., resets_elapsed=True)`, emitted by `to_dict`); extracted
  by the static `agent/argspec.py` (mirrored byte-identically to `sdr-client/api/argspec.py`).
  `cmdargs.resets_elapsed_dests(spec)` / `is_reset_fire(params, dests)` (a truthy value — bool True or
  "on"/"true"/"1"/"yes" — on a reset dest).
- **Run-owned restart** (`_relaunch_start_fire`): the walk keeps `clock_at` = the instant of the LAST
  counted tune firing a reset trigger (a launch resets it to None). With a `clock_at` the baked elapsed is
  `elapsed_at − clock_at` (0 at the trigger — the launch's own `--elapsed` / resume offset no longer
  applies); without one, the launch rule stands. A fault-skipped trigger counts for **resync** (the
  schedule says the drift restarted at T0) and not for **replay** (what actually ran). The trigger itself
  is a bool and is never re-fired on the relaunch (`overlay_live_params` skips bools).
- **Standalone relaunch** (`ProcessManager.relaunch`): the elapsed is `now − the last applied reset
  trigger's _live_applied_at` when one was applied to the faulted process (`_last_reset_applied_at`),
  else the launch elapsed + `age_s()` as before.
- **`cw_drift_tx.py`** marks `--restart` `resets_elapsed=True`.
- **Rollout/skew**: the same hard rule as §14g — the kwarg crashes an older paramkit at `build_script()`,
  so the agent advertises `paramkit-resets-elapsed` and the client's marker gate refuses the script to a
  unit without it (a 1.32.0 unit lacks it). **OTA every unit to 1.33.0 first, then deploy the library.**

Tests: `tests/test_restart_all_params.py` (the marker through paramkit/argspec/cmdargs; the owner's exact
shape — START 5 s before on-air muted, `rf on` + `restart` at T0, fault at T0+30: resync bakes 130 s and
replay 30 s, NOT +5; a launch begun 500 s in and then restarted counts from the trigger; a fault-skipped
trigger counts for resync only; the standalone path counts from the last applied trigger, else the spawn),
`sdr-scripts/tests/test_cw_drift.py` (schema: exactly one `resets_elapsed` param), the client gate test
(a script using both markers needs both capabilities). Agent 702 → 707; scripts 114; client 1185.

## 14j. An ABSOLUTE clock origin — `is_clock_origin` (`AGENT_VERSION 1.34.0`, capability `paramkit-clock-origin`; branch `claude/system-familiarization-f5mezz`, cross-repo)

The owner's scenario: the drift starts at T0 via the `--restart` trigger, faults at T0+10 s, and the operator
presses "Restart and rejoin" at T0+50 s. §14g/§14i put the relaunch at the right point **up to the launch
latency**: the agent baked `--elapsed = elapsed_at − clock_at` at the instant it DECIDED to relaunch, and the
script only started its own clock some seconds later (attenuator pre-command, spawn, UHD open, flowgraph
build — a few seconds on a Pi), so the resumed drift trailed the never-faulted one by exactly that latency.
The owner asked for it to be exact, and to work in BOTH the scheduled (run-owned) and the independent-task
(standalone) case. The fix moves the elapsed computation to the one place that knows when the clock really
starts — the script — and gives it an ABSOLUTE reference instead of a relative one:

- **paramkit** `Param.is_clock_origin` (`number(..., is_clock_origin=True)`, emitted by `to_dict`); extracted
  by the static `agent/argspec.py` (mirrored byte-identically to `sdr-client/api/argspec.py`). The value is
  the Unix instant (UTC seconds) the script's own timeline began; 0 = unset. **When set it overrides the
  relative `is_elapsed` value**: the script computes `elapsed = time.time() − origin` at the moment its clock
  actually starts, so whatever the launch took is absorbed. `cmdargs.clock_origin_param(spec)` /
  `bake_clock_origin(args, param, origin_unix)` (ms resolution, via the param's own flags).
- **The script REPORTS its origin** — `paramkit.txhealth.CLOCK_MARKER = "CLOCK origin="` +
  `report_clock_origin(origin_unix)` (one flushed line on stdout). `cw_drift_tx.py` prints it once when its
  clock starts (`time.time() − elapsed0`, so a launch that itself resumed part-way reports the true origin)
  and AGAIN from the `--restart` handler (the timeline restarted NOW). The agent's watchdog scan
  (`_scan_task_health`, the same read that catches the HEALTH markers) records the LAST marker into
  `ManagedProcess.clock_origin` (`_last_clock_origin`; reset on `start()`), exposed as
  `ProcessManager.clock_origin(task)`. So the agent holds the exact instant the faulted process's clock
  last (re)started — not its own estimate of it.
- **Run-owned restart** (`_relaunch_start_fire`, after the elapsed bake): the schedule's origin is the counted
  reset trigger's instant (`clock_at`) else the counted launch instant minus the launch's own `--elapsed`.
  The process's REPORTED origin replaces it when the live record belongs to this launch
  (`_live_record_is_this_launch`) and no counted trigger lies after it (a fault-SKIPPED trigger never fired
  in the process, so resync follows the schedule there; a reported origin ≥ `clock_at − 1 s` is the trigger's
  own report and wins). **resync** bakes that origin unchanged — the relaunch lands exactly where the
  never-faulted drift would be at the moment the script's clock starts, whether the operator pressed the
  button at T0+50 s or T0+500 s. **replay** shifts the origin by the down-time (`now − fault_at`), exactly as
  it shifts the rest of the profile, so the drift resumes from the crash point. `--elapsed` is still baked
  (the fallback for a script whose origin is 0, and what an operator reads in the log).
- **Standalone relaunch** (`ProcessManager.relaunch`): the reported origin when the process gave one, else
  the applied reset trigger's instant (`now − _last_reset_applied_at`), else the spawn minus the launch's
  own elapsed (`now − age_s − launch_elapsed`). Same override rule in the script, so the independent-task
  case is exact too.
- **Skew rule**: identical to §14g/§14i — the kwarg crashes an older paramkit at `build_script()`, so the
  agent advertises **`paramkit-clock-origin`** and the client's marker gate (`api/script_markers.py`)
  refuses the script to a unit without it (a 1.33.0 unit lacks it). **OTA every unit to 1.34.0 first, then
  deploy the library.** A hand-set `--clock-origin` is meaningless (leave it 0; `--elapsed` is the manual
  knob). Clock skew between the unit and anything else does not enter: the origin is written and read on
  the SAME unit's wall clock.
- **Limitations**: a script that never reports (no marker) falls back to the agent's reconstruction, which
  is what §14i delivered (exact to the launch latency); a wall-clock step on the unit between the report
  and the relaunch moves the resume by that step (NTP slew is fine).

Tests: `tests/test_restart_all_params.py` (+4: the marker through paramkit/argspec/cmdargs and the scan
recording the script's report, last one wins; a run restart bakes the REPORTED origin — resync exact, replay
shifted by the down-time; the fallback to the schedule's origin when the record isn't this launch's / the
trigger was fault-skipped; the standalone relaunch bakes the reported origin, else the reconstruction),
`tests/test_meta_endpoint.py` (the capability), `sdr-scripts/tests/test_cw_drift.py` (+2: the real `main()`
with `--clock-origin` 5400 s ago ignores a contradicting `--elapsed`, reports the marker at start and again
on `--restart` with the restarted origin; 0 = unset), the client gate test (`is_clock_origin` →
`paramkit-clock-origin`; a script using all three markers needs all three capabilities). Agent 707 → 711;
scripts 114 → 116; client 1185.

## 14k. ROOT CAUSE CONFIRMED (2026-09-21) — a tune-before-bind race on the first launch after boot; not `vmcircbuf`

The unit (`broadcaster-1`, Pi 5 + B206 mini, **agent 1.27.2** at the time, pre-P0 scripts) became
reachable on 2026-09-21. Its logs plus a controlled reproduction settle the mechanism. The
supervisor's one-pager (`docs/incident-fm-chirp-vmcircbuf.md`) was rewritten accordingly.

### Evidence retrieved

- **Task log** `logs/Sweep/run_20260918T092150Z.log` (the file is named at ROTATION, i.e. the next
  launch; the run itself was 09:09:50 → 09:19:32 UTC): the script's banner (`RF : OFF (muted)`,
  `power (target) −156.63`, `→ gain 0.00`) followed by exactly one line —
  `vmcircbuf_prefs::get :info: /tmp/.config/gnuradio/prefs/vmcircbuf_default_factory failed to open:
  bad true, fail true, eof true` — and nothing else. That is the expected output of a healthy run at
  that script version: nothing prints after the banner (tune acknowledgements go over the control
  socket), and the `vmcircbuf` line is GR's INFO message printed from inside `tb.start()` when no
  backend pref file exists (the unit's `gnuradio-config-info --prefs` has `[log] log_level = info`,
  `log_file = stdout`; the pre-P0 service set no `HOME`, so GR's `appdata_path()` fell back to
  `/tmp`). `grep -l` finds the identical line in **five successful runs**.
- **Readouts:** `df /dev/shm` 8.3 GB / 400 KB used (1 %); `vm.max_map_count` 1,048,576; `ipcs -m`
  empty; `ulimit -n` 1024. No allocation error anywhere → every §3.3 mechanism (A–D) is out.
- **Sequence run log** `logs/_sequences/seq_4a5b15d4/run_20260918T092121Z.log`: armed 07:13:31;
  `[09:09:50] ▶ start Sweep` (`--rf off`, the muted 10 s pre-roll); `[09:10:00] ◈ Sweep • Power`
  (−91 dBm/Hz, the ramp's first point) then `[09:10:00] ◈ Sweep • RF on`; `[09:10:01] ON AIR (T0)`;
  **then** the task's banner lines (`Sweep: …`, incl. `RF : OFF (muted)` and the vmcircbuf line);
  then a ramp point every 20 s up to −35 dBm/Hz at 09:19:20; `[09:19:32] aborted: cancelled by
  operator`. No `⚠` annotation of any kind.
- **Journal:** empty for that day — Raspberry Pi OS keeps journald in RAM by default; the unit had
  been rebooted. The agent logs only to stdout → journald, so the agent-side record was lost.

### The timing argument (why the banner's position is the clue)

`SequenceRunner._tick` runs, in order: `rl.collect()` (copy any NEW task-log lines into the run log)
→ fire the due steps (their blocks) → `_emit_on_air` (`ON AIR (T0)`). The banner landed in the run
log AFTER `ON AIR (T0)`, so at the `collect()` of the tick that fired the two on-air tunes the banner
did not yet exist in the task log. In `fm_chirp_tx.py` the control socket is bound
(`script.live_control(args)`, line 636) two lines after the banner is flushed (line 634). Hence the
two tunes (sent ≈09:10:00.6–1.0, each after its attenuator pre-command) went out while the script
was still starting up: **the launch took ≈10.5 s from spawn (09:09:50) to the banner, against a
10 s pre-roll.** The script's own start-up (Python + GNU Radio imports, the B206 open incl. the
firmware/FPGA image load after a power-cycle, the clock-rate change, the buffer + 6753-tap filter)
is what filled those seconds; UHD's "loading image" lines were invisible because every script sets
`UHD_LOG_CONSOLE_LEVEL=off` and 1.27.2 had no UHD file log.

### What agent 1.27.2 did with a tune sent too early

`_ctrl_rpc` → `socket.connect(path)` on a not-yet-existing socket → `RuntimeError("task does not
expose live parameters (or isn't ready yet)")` → `_fire_step`'s `except Exception` → `logger.error`
**only** (the journal). The tune's block had already been written to the run log BEFORE the RPC, and
the `⚠ tune … FAILED: …` annotation did not exist until 1.31.1. **So the run log is byte-identical
whether the tune got through or not.** Both on-air tunes — the −91 dBm/Hz power point and `rf on` —
were lost. Every later ramp point found the socket bound and was accepted, but the script STAGES a
power change while muted (`apply_change("power")`: with `state["rf_on"]` False the new gain is kept
in `state` and never sent to the radio), so the gate never opened. The radio streamed zeros for
9.5 min while the process, its control thread and the run log all looked healthy. The Phase-2 note
"the control-socket-bind race silently drops the tune" (§14c) described this exact path without
knowing it was the incident.

### Reproduction (owner, 2026-09-21)

Reboot the unit → schedule the same plan → identical log, no RF, ever, though every tune "fires".
Run it again without a reboot → normal transmission. The first launch after boot is the slow one.

### What fixed it, and when

- **§14f #4 (1.31.1, 2026-09-20):** `_fire_step` DEFERS a tune while `ProcessManager.tune_ready`
  reports the socket file absent and the process younger than `CTRL_BIND_GRACE_S` (180 s); the next
  tick retries; the `due` sort keeps the power point before `rf on`. Written for the restart relaunch,
  it covers the cold first launch by construction. **Not yet verified on hardware** — run the reboot
  test after the OTA.
- **§14f (1.31.1):** a tune that still fails is annotated `⚠ tune <task> FAILED: <reason>` in the
  run log; **#19:** `HEALTH state=transmitting` is printed once `tb.start()` returns.
- **P0 (1.27.4):** `pre_image_sdr()` at boot loads the B206 image so the first launch is warm; the
  per-launch `uhd_<ts>.log` records how long the open took.
- **What P0's shared-memory work bought for THIS incident: nothing.** The sweep, the ceilings, the
  backend pref file are harmless and stay; they addressed a mechanism that was never in play.

### Gaps that remain (deliberately deferred by the owner, 2026-09-21)

1. **Accepted ≠ applied.** The sequence path sends `wait=0`; a tune the script's control thread
   accepts but its main loop never drains (a wedge inside `tb.start()`, or a dropped-then-deferred
   gate tune applied late) is invisible. Fix: after a gate tune, read `get_params()["applied"]` back
   ~1 s later and couple an RF fault when the gate isn't on.
2. **The deferral is silent** (the block just carries its later stamp) — annotate "deferred N s,
   waiting for the control socket". And the **co-timed edge**: if the socket appears between a
   deferred power point and its co-timed `rf on` within one tick, the gate opens at the launch power
   for one tick — hold back a task's remaining co-timed tunes once one is deferred.
   **→ Both addressed by §14l (1.36.1):** a pile-up is one co-timed batch, power before RF-on, and
   the run log names how late the task came up and how many points it skipped.
3. **Pre-roll vs. measured launch time.** Record each task's launch-to-bind time; warn at arm when a
   sequence's lead-in is shorter than the task's worst observed cold start; lengthen the client's
   default lead-in for an RF-gated launch (10 s was not enough here).
4. **Persistent journald** on the units (`mkdir -p /var/log/journal`) — provisioning.
5. Cosmetic: the chirp banner's `power (achieved on grid)` is the SDR alone with the attenuator at
   rest (`power_for_gain` without `applied_db`) — misleading on an attenuator chain.

## 14l. A late task rejoins its schedule at the CURRENT level (`AGENT_VERSION 1.36.1`, no capability)

**Owner test (2026-09-21, fleet on 1.36.0):** a plan whose warm-up was deliberately far too short for
the L2C full loop (a 736 MB build, ≈20 s on the Pi). The §14f #4 deferral did its job — the on-air
tunes waited for the control socket and `HEALTH state=transmitting` appeared — but the run log then
showed every point the ramp had passed while the task was building fired within two seconds
(−81.5, −79.5, −77.5, −75.5, −73.5 dBm/Hz between 13:36:19 and 13:36:21), with the `rf on` landing
between the first and the second: the task swept through every missed level with the gate open, and
the co-timed edge noted in §14k bit exactly as predicted. Owner: *"if there's been multiple fires of
the same parameter, it doesn't need to fire them all when it jumps back in."*

**Change (`sequence_runner._collapse_piled_tunes`, called by `_tick` before firing):**

- The due TUNES of one (run, task) with two or more members form a **batch** once `tune_ready`
  says the task is ready (or its bind grace is spent). While it is still binding the group is left
  alone — the pile-up keeps deferring and keeps growing until the moment it can be applied.
- Per **parameter set** (the tune's `params` keys) only the **latest** point survives; the earlier
  ones are stamped **`"skipped:superseded"`** and persisted. A different parameter (`bw` beside
  `power`, the `rf` gate) is its own set and survives.
- The survivors are **re-timed to the batch's latest fire instant** and sorted by `_due_sort_key`
  (a STOP first, then `_co_time_rank`: power 0 → neutral 1 → RF-on 2), so the gate opens **at the
  level the schedule is at now** — the stale-launch-power blip of the co-timed edge is gone.
- One run-log line per batch: `⏭ <task> came up N s late — M superseded power point(s) skipped;
  rejoining the schedule at its current level` (N counted from the earliest missed fire).
- **The new sentinel's consumers:** `_counts_at_cutoff` counts `skipped:superseded` for **resync**
  (the schedule's position, like a fault-skipped fire) and never for replay (it never transmitted);
  the restart re-instatement re-instates only `"skipped"` (unchanged); `_tune_target_stale`,
  `_fire_instant`, `_tasks_owned_by_active_runs`, `_maybe_complete` already treat any `skipped*` as
  not-fired/done. `run_table` now filters **every** `skipped*` sentinel (it compared `!= "skipped"`,
  so a `skipped:hold` / `skipped:stale` step could export a phantom last row — a latent bug).
- Launches/stops, a task with a single due tune, and a run whose task is still binding are ordered
  exactly as before.

Tests: `tests/test_deferred_tune_collapse.py` — the batch deterministically (per-parameter
collapse; a foreign STOP keeps its instant and goes first; power → bw → RF-on), a lone tune and a
still-binding task left untouched, the sentinel's consumers incl. the export, and a LIVE run over
the 2 s-bind script with a 0.5 s pre-roll: the overrun points are superseded, RF-on fires after the
first power point that did fire, the schedule continues to −50, the log names the skip. Suite
714 → 718. Still open from §14k: the arm-time pre-roll check and persistent journald.

## 14. Open items

- ~~Retrieve the archived `run_<ts>.log` from the affected unit + `df /dev/shm` /
  `cat /proc/sys/vm/max_map_count` / `ipcs -m` / the GR backend → confirm the exact mechanism~~ —
  **DONE 2026-09-21, §14k**: not `vmcircbuf` at all.
- ~~Confirm the deployed GNU Radio version's exact `vmcircbuf` factory override name~~ — DONE: GR 3.10
  on the unit (`gnuradio-config-info --prefs` lists no vmcircbuf entry at all; it reads the pref FILE,
  §14f #1); `[log] log_level = info` is why the INFO line prints.
- ~~Confirm the Pi 5 image's current `/dev/shm` size, `vm.max_map_count`, and `ulimit -n`~~ — DONE:
  8.3 GB / 1,048,576 / 1024.
- **Verify the §14f #4 deferral on hardware** with the owner's reboot test after the OTA to 1.36.0.
- Follow-ups deliberately deferred by the owner (2026-09-21), see §14k "gaps": gate-tune read-back
  → RF fault; arm-time pre-roll check + a longer default lead-in; persistent journald; a deferral
  annotation in the run log; the co-timed deferral edge.
- Decide `SequenceState.FAULTED` (terminal) vs. an `rf_fault` field on a still-`RUNNING` run.
- Multi-unit per-item `run_id` resolution for plan-level restart.

# sdr-agent — Claude working notes

The **on-unit HTTP agent** (FastAPI) that runs on each SDR box: launches/monitors transmit
tasks, serves status, stores the unit's power **calibration**, and resolves it per signal. Part
of a three-repo system: **`sdr-agent`** (this), **`sdr-client`** (PyQt6 GUI), **`sdr-scripts`**
(the transmit scripts). `paramkit/` is shared pure-Python used by the agent AND the scripts.

## Environment setup (container starts without deps)
```bash
pip3 install numpy pytest PyQt6 httpx pydantic zeroconf websocket-client PyYAML paramiko \
             fastapi uvicorn "ruamel.yaml" starlette psutil python-multipart inotify-simple
```

## Run the tests (always green on `main`)
```bash
python3 -m pytest -q            # ~361 tests
```
A few `paramkit`/`argspec` test files are also runnable directly (`python3 tests/<file>.py`);
the drift guard is pytest-only.

## Run it live, headless (agent + real client, no hardware)
Exercise the agent against the real PyQt6 client on one machine, no SDR/Pi and no monitor —
full recipe + gotchas in **`docs/local-integration-run.md`**. Two committed helpers:
```bash
bash deploy/run_local.sh                                  # agent → 0.0.0.0:8765 (run BACKGROUNDED)
python3 ../sdr-client/tools/screenshot.py --tab units --out /tmp/units.png   # or ../sdr-client/tools/run_local.sh
```
`run_local.sh` stages a FLAT `<base>/scripts/` dir from the sibling `sdr-scripts` (the agent serves a flat
dir; the repo nests scripts) and starts `uvicorn agent.main:app` with dev env (`SDR_AGENT_BASE`/
`SDR_STATE_DIR` under `/tmp/sdr-local`). A connected client reads `/info`, opens the SSE stream, and shows
the unit **online** ("clocks: synced ✓"); `sdr: none` / `temp —` are the expected no-hardware readouts.
A Claude session should launch the agent with Bash `run_in_background: true`, never as a foreground server.
It also seeds a realistic **sample calibration** (`deploy/sample-calibration/{calibration.json,components.yaml}`
→ the unit's `data/`) so the unit is calibrated by default: Source flatness (TX-bias `source_bias`) → cable
→ programmable attenuator (0–95 dB / 0.25 step, −4.5 dB insertion) → output cable (SDR gain 0–89.75 / 0.25).
The first cable is also the measurement de-embed cable (TX bias + every signal). Exactly **three test
signals**, one per family: `cw_tone` (dBm), `GPS C/A (1.023 Mcps)` and `Chirp/Sweep` (both spectral density
dBm/Hz, ceiling gauged through a full-/total-power law). `run_local.sh` also seeds a `tasks.yaml` wiring
three NO-HARDWARE **mock transmit tasks** — `mock_prn`/`mock_chirp`/`mock_cw` (from `sdr-scripts`
`mock_gps_ca_code_1.023Mcps_tx.py` / `mock_fm_chirp_tx.py` / `mock_cw_tx.py`, each mirroring the real
script's params + `CAL_POWER_LAWS`) + `atten_set` (`mock_atten.py`) — so all three are ARMABLE (real power
card, arm/hold/proceed) with no radio. It also copies a `sequences.json` seed (from
`deploy/sample-calibration/`) with **one RF-gated sequence per signal**
(`seq-mock-prn`/`seq-mock-chirp`/`seq-mock-cw`): the task launches 1 s before on-air with `--rf off`
(muted pre-roll), a TUNE turns RF ON at the on-air anchor and OFF at off-air, and the task stops 1 s after
off-air; `--power` is calibrated mid-range and the agent auto-commands `atten_set` to realize it. That
seed is **authored through the client** — `deploy/make_sample_sequences.py` builds each with the client's
own timeline authoring code (`ui.timeline_model` → `items_to_steps`, as `TimelineEditor.steps()` does) +
`api.models`, so a fresh session shows exactly what the client produces (regenerate: `python3
deploy/make_sample_sequences.py`, needs a sibling `sdr-client`). See `docs/local-integration-run.md`;
view it with `screenshot.py --tab calibration`.

## Cross-repo invariants (do not break)
- **Drift guard (`tests/test_shared_source_drift.py`):** `agent/argspec.py` and `agent/ramp.py`
  MUST stay **byte-identical** to `sdr-client/api/argspec.py` and `sdr-client/api/ramp.py`. The
  test finds a sibling `sdr-client/` checkout; if you touch one side, mirror it.
- **Power-law mirror (manual):** `paramkit/power_law.py` is copied verbatim to
  `sdr-client/state/power_law.py` (pure stdlib for Python/JS parity). Keep them in step.
- **Capabilities + version:** a new client-visible feature adds a string to
  `AGENT_CAPABILITIES` and bumps `AGENT_VERSION` (both in `agent/config.py`); `test_meta_endpoint.py`
  asserts the capability set. The client feature-gates on these exact strings. Current version is
  in `config.py` (`1.36.1`: a late task rejoins its schedule at the CURRENT level — a pile-up of
  deferred tunes collapses per parameter, power before RF-on; behaviour only; `1.36.0`: stacked sequences —
  arm guard A is task-aware — capability `sequence-stacking`; `1.35.0`: plan-level anchoring replica — Phase: plan editor redesign — capability
  `plan-item-anchors`; `1.19.0`: edit-while-holding — Phase 3c — `POST …/proceed` honours
  `ProceedRequest.steps` behind `sequence-hold-edit`; `1.18.0`: Fast-Forward-to-Hold — Phase 3b —
  `POST …/hold-now` behind `sequence-hold-now`; `1.17.0`: the Hold-step HOLDING runtime — Phase 1 —
  behind `sequence-hold` (added 1.16.0): a hold-aware arm parks at the hold and `POST …/proceed`
  resolves the post-hold window).

## Where things live
- `agent/calibration.py` (~1.7k lines) — the **calibration resolver**. `resolve(unit_doc, …,
  signal_id)` runs **per signal** and returns a `ResolvedCalibration`; `to_public_dict()` is the
  artifact the client/script consume; `summarize`/the `/calibration` view builds per-signal
  bounds + artifact. Key concepts: measured/derived **planes**, **limits** (dBm ceilings on
  stage boundaries), reading **bridges** (reported/limiting: same/law/own via
  `paramkit/power_law.py`), and per-signal **measurement** `{quantity, unit}`
  (`_measurement_of`, published as the operating quantity/unit; its family gauges the bridges).
- `agent/config.py` — `AGENT_VERSION`, `AGENT_CAPABILITIES`, interpreter/defaults.
- `agent/main.py` — the FastAPI surface (`/info`, `/calibration`, `/calibration/validate`,
  `/files`, task control). `agent/models.py` — pydantic models.
- `paramkit/` — shared math: `power_law.py` (bridge/law evaluation), `achievable.py` (the true
  achievable gain/power grid), `calkit.py` (script-side fold), `argspec.py` (static param
  extraction, drift-guarded).
- `docs/calibration.md`, `docs/calibration-v2.md` — the authoritative model + artifact schema.

## Calibration model (one paragraph)
A unit is a **chain of planes** (measured SDR output → derived hops). Each **signal** declares
its **measurement** (a `{quantity, unit}`: dBm or a spectral density dBm/Hz·kHz·MHz) and, per
key, reported/limiting reading **bridges**. The operator sets `--power` in the measured quantity;
declared **laws** (affine in log10 of task params; `in`/`out` families abs↔density) convert
between quantities. Safety **limits** are dBm ceilings on stage boundaries; the LIMITING reading
is always dBm so one stage ceiling gauges every signal. `resolve()` folds all this at a
representative frequency for scalar read-outs and publishes the full artifact for runtime re-fold.

## MERGED TO `main` (all three repos, fast-forward, 1.34.0) — field rollout: OTA every unit's agent FIRST, then deploy the library; rebuild the client bundle from 1.34.0. Nothing in the RF-fault arc has run on real hardware yet (mock scripts + fake gnuradio + the headless unit only).

## Current state — a LATE task rejoins its schedule at the CURRENT level (1.36.1, no capability) (branch `claude/system-familiarization-f5mezz`, agent-only)
Owner test on the updated fleet (1.36.0): a plan with a deliberately too-short warm-up on the L2C full loop
(736 MB build, ~20 s). The §14f #4 deferral worked — the on-air tunes waited for the control socket — but then
every tune that had piled up while the task was building fired within two seconds (−81.5 → −73.5 dBm/Hz in
five steps), with the RF-on landing between the first and the second. Owner: "if there's been multiple fires
of the same parameter, it doesn't need to fire them all when it jumps back in." Now `_tick` routes the due
list through **`_collapse_piled_tunes(due, now)`** (record: `docs/rf-fault-recovery.md` §14l): the due TUNES
of one (run, task) with ≥ 2 members form a batch ONCE the task is ready or its bind grace is spent
(`tune_ready`; still binding → left alone, the pile-up keeps deferring); per parameter set only the LATEST
point survives, the earlier ones are stamped **`"skipped:superseded"`** (a new sentinel: never transmitted —
`_counts_at_cutoff` counts it for RESYNC like a fault-skipped fire (the schedule's position) and never for
replay; the restart re-instatement, `_tune_target_stale`, `_fire_instant`, `_tasks_owned_by_active_runs` and
`_maybe_complete` treat it like every other skip; `run_table` now drops EVERY `skipped*` sentinel — before, a
`skipped:hold`/`:stale` step could export a phantom last row); the survivors are re-timed to the batch's
latest fire instant and sorted by `_due_sort_key` (stop first, then `_co_time_rank`: power → neutral →
RF-on), so the gate opens at the level the schedule is at NOW, never at a stale one; the run log gets ONE
line per batch (`⏭ <task> came up N s late — M superseded power point(s) skipped; rejoining the schedule at
its current level`). Launches/stops and a task with a single due tune are ordered exactly as before.
`AGENT_VERSION 1.36.0 → 1.36.1`. Tests: `tests/test_deferred_tune_collapse.py` (the batch deterministically:
per-parameter collapse, a foreign stop first, power before bw before RF-on; a lone tune / a still-binding task
untouched; the sentinel's consumers incl. the export; and LIVE over the slow-bind script — the overrun points
are superseded, RF-on fires after the first power point that did fire, the schedule continues, the log says
so). Suite 714 → 718.

## Current state — the field incident's ROOT CAUSE is CONFIRMED (2026-09-21, docs only; no code, no version)
The unit (`broadcaster-1`, agent 1.27.2) became reachable; its logs + the owner's controlled reproduction
(reboot → same plan → identical silent failure; re-run without reboot → works) REFUTE the `vmcircbuf`
theory the whole RF-fault arc was written around. The truth: a **tune-before-bind race on the first launch
after a reboot** — the cold launch took ≈10.5 s (B206 image load + imports) against the 10 s muted pre-roll,
the agent's on-air `power` + `rf on` tunes were sent ≈0.5 s before `fm_chirp_tx.py` bound its control socket,
agent 1.27.2 dropped them with only a `logger.error` (no run-log annotation; the journal was volatile),
and the script stages a power tune while muted → the gate never opened; the radio streamed zeros for 9.5 min
with a healthy process. The tell was the task's banner being copied into the run log AFTER `ON AIR (T0)`
(`_tick`: collect → fires → on-air marker). The `vmcircbuf_prefs::get :info:` line is GR INFO output on
every launch (five healthy runs carry it); `/dev/shm` 1 %, `ipcs -m` empty, `max_map_count` 1,048,576.
**Already fixed by §14f #4 (1.31.1)** — the `tune_ready` deferral (`CTRL_BIND_GRACE_S`) — plus the
`⚠ tune … FAILED` annotation, the P0 boot pre-image and `HEALTH state=transmitting`; NOT yet verified on
hardware (run the reboot test after the OTA). P0's shared-memory work bought nothing for this incident
(harmless, kept). Record: **`docs/rf-fault-recovery.md` §14k**; the supervisor one-pager
`docs/incident-fm-chirp-vmcircbuf.md` was rewritten. Deferred by the owner: gate-tune read-back → RF fault,
a deferral annotation + the co-timed edge, an arm-time pre-roll check + longer default lead-in, persistent
journald. The DESIGN section below ("Field incident: … `vmcircbuf`") is the design-time reading.

## Current state — STACKED sequences: arm guard A is task-aware (1.36.0, capability `sequence-stacking`) (branch `claude/system-familiarization-f5mezz`, cross-repo)
Owner decision (plan-editor round 3): "right now the rule should allow stacking of sequences — the only user is
me, and I know what's compatible" (a fully task-aware rule is the eventual goal). Before, `sequence_runner.arm`'s
guard A refused ANY run whose channel span (lead-in … stop tail) overlapped another active run on the unit
("one TX channel per unit"). Now it refuses only when the two runs both LAUNCH the same task — a task runs
once, so that arm could never work (the older guard A0 "task(s) already running" stays as it was). New static
`_launched_task_names(steps)` = the task names of the START/RUN steps (fires or defs); the guard intersects the
new run's launches with each active run's `steps` + `window_b_steps`, skips a run with no shared launch (they may
stack — a tune-only run stacks on anything), and refuses an overlapping shared launch with the old message shape
("cannot arm: on-air window overlaps run <id> … and both launch task(s) 'tx' — a task runs once; this run would
occupy the channel …, counting its launch lead-in … and its stop tail …; leave a gap (sequences launching
different tasks may stack)"). `AGENT_VERSION 1.35.0 → 1.36.0`, capability `sequence-stacking` (the client's plan
editor marks a stacked pair AMBER and notes when a unit's cached `/info` lacks it — an older agent still refuses
the overlap, so OTA before arming a stacked plan). `argspec`/`ramp` untouched. Tests: `tests/test_arm_guards.py`
(`test_sequences_launching_different_tasks_may_stack`: a second task, a one-shot and a tune-only run all stack over
a running `tx` window; a second `tx` launch in the window is refused naming the task and arms once moved past
it) + `test_meta_endpoint`. Suite 713 → 714; client 1212 → 1214.

## Current state — plan REPLICA carries the client's plan-level anchors (1.35.0, capability `plan-item-anchors`) (branch `claude/system-familiarization-f5mezz`, cross-repo)
The client's PLAN EDITOR was redesigned (see `sdr-client/CLAUDE.md`, spec `sdr-client/docs/plan-editor-mockup.html`):
every unit's sequences on one timeline, each a collapsible frame embedding the real sequence editor, and
"anything anchors to anything" — a plan item's on-/off-air hangs off the plan's anchors (`"plan"`, the old
meaning), another item's edge (`"item"`) or a step's edge in another item (`"step"`); a step may hang off a
step / window edge in another item (`SequenceStep.anchor_item`). Plans are client-only but REPLICATED to units
(`/plans`, `ClientStateStore`), so `agent/models.py` `PlanItem` gains `id`, `on_air_anchor`/`off_air_anchor` +
`*_anchor_item`/`*_anchor_edge`/`*_anchor_step`, `expanded`, and `SequenceStep.anchor_item` — STORAGE ONLY: the
client compiles the graph to absolute instants (and rewrites cross-item step anchors to on-air offsets of their
own sequence) before every arm, so an `ArmSequenceRequest` never carries them and the runtime never reads them.
An older agent drops the fields → a plan using anchors reads as drifted until the unit is OTA'd. `AGENT_VERSION
1.34.0 → 1.35.0`, capability `plan-item-anchors` (informational; the client doesn't gate on it). Tests:
`tests/test_plan_replica_anchors.py` (+2) + `test_meta_endpoint`. Suite 711 → 713; client 1185 → 1206.

## Current state — an ABSOLUTE clock origin makes the resume EXACT (1.34.0, capability `paramkit-clock-origin`) (branch `claude/system-familiarization-f5mezz`, cross-repo)
Owner ask: a drift faulting at T0+10 s and restarted-and-rejoined at T0+50 s must land where the never-faulted
drift would be — exactly, not "minus the launch latency" — in BOTH the scheduled and the independent-task case.
Record **`docs/rf-fault-recovery.md` §14j**. paramkit `number(..., is_clock_origin=True)` (→ `Param.is_clock_origin`,
`to_dict`, `agent/argspec.py` — **mirrored to `sdr-client/api/argspec.py`**): the ABSOLUTE Unix instant the script's
timeline began; when > 0 the script computes `elapsed = time.time() − origin` itself when its clock starts, so the
launch's seconds never shift it. The script REPORTS its origin (`paramkit.txhealth.CLOCK_MARKER` / `report_clock_origin`,
once at clock start and again on a reset trigger); the watchdog scan records the last one into
`ManagedProcess.clock_origin` (`_last_clock_origin`, `ProcessManager.clock_origin(task)`). `cmdargs.clock_origin_param` /
`bake_clock_origin`. `_relaunch_start_fire` bakes the reported origin when the live record is this launch's and no
counted trigger lies after it, else the schedule's (`clock_at`, else launch − launch elapsed); resync bakes it
unchanged (exact), replay shifts it by `now − fault_at`. `ProcessManager.relaunch`: reported → the applied
trigger's instant → spawn − launch elapsed. `cw_drift_tx.py --clock-origin`. Same skew rule → capability
`paramkit-clock-origin` + the client marker gate; **OTA to 1.34.0 first, then deploy the library**. Tests:
`tests/test_restart_all_params.py` (+4). Suite 707 → 711; scripts 116; client 1185.

## Current state — the elapsed-RESET trigger marker `resets_elapsed` (1.33.0, capability `paramkit-resets-elapsed`) (branch `claude/system-familiarization-f5mezz`, cross-repo)
Owner workflow: launch cw_drift X s BEFORE on-air with `--rf off`, then AT on-air fire `rf on` + `--restart`
so the drift begins at T0 — so a restart must count the elapsed from that trigger, not the launch (§14h's
"documented limitation" E5 was the owner's normal case). Record **`docs/rf-fault-recovery.md` §14i**. paramkit
`flag(..., resets_elapsed=True)` (→ `Param.resets_elapsed`, `to_dict`, `agent/argspec.py` — **mirrored to
`sdr-client/api/argspec.py`**); `cmdargs.resets_elapsed_dests`/`is_reset_fire`; `_relaunch_start_fire` keeps
`clock_at` = the last counted reset-trigger fire (resync counts a fault-skipped one, replay doesn't) and bakes
`elapsed_at − clock_at` (the launch's `--elapsed`/resume offset no longer applies); `ProcessManager.relaunch`
counts from the last applied trigger's `_live_applied_at` (`_last_reset_applied_at`). `cw_drift_tx.py --restart`
declares it. Skew: the same crash-on-old-paramkit rule → capability `paramkit-resets-elapsed` + the client marker
gate; **OTA to 1.33.0 first, then deploy the library**. Tests in `tests/test_restart_all_params.py` (+5). Suite 702 → 707.

## Current state — SECOND adversarial review of the RF-fault arc: 30 findings FIXED (1.32.0, capability `paramkit-is-elapsed`) (branch `claude/system-familiarization-f5mezz`, cross-repo)
Seven parallel reviewers re-reviewed the §14f fixes + the §14g work; record **`docs/rf-fault-recovery.md` §14h**.
Suite 668 → 702 (`tests/test_review_fixes_2.py`, 31); scripts 112 → 114; client 1179 → 1185. Two HIGHs:
- **`start()` over a STOPPING slot spawned a second process** (C1): `ManagedProcess.start` refuses a STOPPING
  slot / a live process, `stop()` binds its process, `restart()` waits it out (`wait_stopped`). And **resync
  relaunched inside a scheduled OFF gap** (W1: a two-epoch task): `_relaunch_start_fire` walks STOPs and
  returns None when the schedule holds the task off — the re-instated START relaunches on time.
- **Cross-repo skew (C-1, HIGH):** `cw_drift_tx.py`'s `is_elapsed=` kwarg CRASHES on a ≤1.31.1 unit's paramkit.
  New capability **`paramkit-is-elapsed`**; the client refuses to deploy a marker-bearing script to a unit
  without it (`api/script_markers.py`, gate in `AgentClient.upload_script`/`deploy_library`). **Rollout
  order: OTA every unit to 1.32.0 FIRST, then deploy the library.**
- MEDIUMs: a STOP sorts before a co-timed START of the same task (C2); a launch parked on the pre-image gate /
  pre-command honours a Stop/PANIC (`_panic_epoch`, C3/O1); a failed launch settles the slot (C4); HOLDING keeps a
  completing launch (C5); a stale deferred tune is dropped `"skipped:stale"`, never fired into the successor run
  (W2), `CTRL_BIND_GRACE_S` 30 → 180 s (W3); a failed START couples an RF fault (W4); a second fault in a faulted
  run is coupled + released to its own checkbox (W5); the disabled watchdog keeps the settle on running-and-OK
  (O2); the hand-tune merge is time-ordered (`_live_applied_at`, R1) and process-scoped (`started_at` within the
  launch→fault window, R2); an arm-time resume injection survives a restart (R3); exact numeric text
  (`cmdargs.num_text`, no `%g`) + integer-kind elapsed (E1/E2). LOWs + documented limitations in §14h.

## Current state — restart reconstructs EVERY parameter + a script-declared ELAPSED time (1.32.0, no capability) (branch `claude/system-familiarization-f5mezz`, cross-repo)
Owner follow-up to the review: a restarted process must come back with ALL parameters correct (not only a
ramped power, and also with no ramp), and a time-dependent script (cw_drift) must declare something that lets
it resume at the right point. Record: **`docs/rf-fault-recovery.md` §14g**. Suite 656 → 668; scripts 110 → 112.
- **Every parameter, both paths:** `SequenceRunner._relaunch_start_fire` now bakes through the SAME
  `cmdargs.overlay_live_params` as the standalone `ProcessManager.relaunch` (numbers AND choices/strings;
  the gate via its flags; a store_true trigger never re-fired). It also MERGES `ProcessManager.live_applied(task)`
  (hand tunes via the Tune… dialog, not in `run.steps`) for every dest the schedule never drives; a
  schedule-driven dest follows the schedule's position at the cutoff. Only when the counted epoch launched the task.
- **`is_elapsed` marker:** paramkit `Param.is_elapsed` (`number/integer(..., is_elapsed=True)`, in `to_dict`),
  extracted by `agent/argspec.py` (**mirrored to `sdr-client/api/argspec.py`** — drift guard). `agent/cmdargs.py`
  `elapsed_param`/`elapsed_of_args`/`bake_elapsed`. Run-owned restart: launch elapsed + (`elapsed_at` − the
  counted launch's actual instant; `now` for resync, `fault_at` for replay; a skipped launch from its scheduled
  `fire_at` via `_fire_instant`); chains across a prior relaunch. Standalone: launch elapsed + `proc.age_s()`.
  `build_resume_request` treats a marker-declaring script as resumable. `AGENT_VERSION 1.31.1 → 1.32.0`,
  capability `paramkit-is-elapsed` (the client's deploy gate — see the review note above; OTA first).
- **`sdr-scripts` `cw_drift_tx.py --elapsed`** (`-Elapsed`, s, default 0): `t0 = monotonic() − elapsed`; the
  top block is built AT the resume frequency (LO window + NCO + gain folded there; the attenuator split stays
  pinned at the start carrier); banner `resumed at`. Limitations: a live `--restart` trigger before the fault
  isn't replayed; `spec=None` can't bake the elapsed. Tests: `tests/test_restart_all_params.py` (12) + the
  marker tests; scripts `tests/test_cw_drift.py` (schema, banner, the real `main()` in-process).

## Current state — RF-fault arc ADVERSARIAL REVIEW: 31 findings FIXED (1.31.1, no capability) (branch `claude/system-familiarization-f5mezz`, cross-repo)
A full multi-agent review of P0–P3b (11 dimensions → 41 raw → 32 unique → 30 confirmed by independent skeptics,
5 HIGH ones by reproduction against the real runner; 2 refuted; +1 gap from the completeness critic confirmed by
hand) found the arc **not shippable**: RF-left-on races, hot relaunches, an inert prevention control. All 31 are
fixed and pinned; the record with every finding, fix and the verification method is **`docs/rf-fault-recovery.md`
§14f**. Suite 626 → 656 (`tests/test_review_fixes.py`, 30 tests — the HIGH ones are the verifiers' reproductions
inverted over real subprocesses); client 1166 → 1179; scripts 106 → 110. `argspec`/`ramp` untouched.
- **#1 the P0 GR vmcircbuf pin was INERT.** Verified against upstream 3.8/3.10: GR reads a **pref FILE**
  `vmcircbuf_default_factory` (a factory name) under the task HOME — never the `GR_CONF_*` env var, and not
  governed by `GR_DONT_LOAD_PREFS`; with no file it probes and persists **sysv_shm** (the leaky suspect). Now
  `_launch_env_pins` → `_pin_gr_vmcircbuf_pref` writes it at both the 3.8 and 3.10 locations + pins
  `GR_PREFS_PATH`; the snapshot reads it back as **`FaultSnapshot.vmcircbuf_backend_pref`** (the effective
  backend); the client dialog keys on it. **This ships by OTA** (agent code) — the earlier "needs a re-provision"
  note was about an env var that never did anything. The `GR_CONF_*` var stays as documentation.
- **RF left on:** #33 PANIC now cancels pending standalone relaunches (`ProcessManager.cancel_pending_relaunches`,
  also in `shutdown`); #6 `_abort_run` goes ABORTED FIRST, stops everything `is_live` (twice), and `_fire_step`
  re-checks the run under the lock + stops a launch that completed into a dead run; #10 `stop()` during
  STARTING waits for the spawn (`_spawned`) and `start()` kills what it spawned; #12 a manager `_shutdown_flag`
  checked after every restart/settle sleep; #11 an operator stop cancels `_relaunch_task` and
  `ProcessManager.start` re-checks it after the pre-command.
- **Hot relaunch:** #2 `set_params` records `_live_applied`, `relaunch()` bakes it onto the args
  (`agent/cmdargs.overlay_live_params`) or stands down if the schema is unreadable; #5 `hold_now` stamps
  **`"skipped:hold"`** (distinct from the fault sentinel `"skipped"`) so resync never bakes a fast-forwarded
  ramp top; #4 `_fire_step` DEFERS a tune while `tune_ready` says the socket isn't bound within
  `CTRL_BIND_GRACE_S` (new knob, 30 s), replayed fires are floored past `now`, the auto trigger passes a fresh
  `restart_at`; #18 `_RestartDeferred` (quiet retry / 409) when `spec=None` and a non-fallback dest was tuned;
  #16 `restart_run` dry-runs plan + guards + reconstruction in the FIRST lock (`_plan_restart`) and refuses a
  hand-started task instead of pre-stopping it; #15 a faulted task is no longer "owned" for arm guard A0.
- **Never recovered / undetected:** #3 `apply_sequences` keeps the whole `Sequence` (recovery policy!); #21
  (client) drift fingerprints include the policy + auto-restart flag; #24 agent `PlanItem` recovery fields;
  #13/#14 `_scan_task_health` returns if the process it read is no longer the RUNNING one (`_scan_stale`) and
  `stop()` cancels a CRASHED watcher only in its delay (`_in_restart_delay`); #7 (scripts) all 30 adopters
  `stop.set()` before `tb.stop()`; #17 an un-fired future STOP counts for `has_future_stop`; #31 a faulted run
  never parks into HOLDING.
- **Runaway:** #19 `txhealth.watch_flowgraph` prints **`HEALTH state=transmitting`**, the watchdog stamps
  `transmitting_at`, and the healthy-settle requires it for a script using `watch_flowgraph`
  (`expects_tx_marker`/`task_transmitting_confirmed`); #20 a tripped run is never re-selected under budget 0;
  #28 a PENDING-only run claim is waited out (`tasks_pending_launch_by_active_runs` → `set_pending_query`,
  `_wait_out_run_claim`), #27 window-B launches count as claims.
- **Operability:** #9 `ProcessManager.device_free` gates launches while the boot pre-image holds the SDR;
  #8 `PREIMAGE_TIMEOUT_S<=0` disables; #25 `HEALTH_POLL_S<=0` disables; #23 `uhd.log` rotated per run +
  pruned; #30 empty `replace_args` keeps the configured command; #22 (scripts) the L2C `m>=n` branch refuses;
  #32 the doc's `owned_task_names` corrected. Shared arg helpers: `agent/cmdargs.py`.
- **Open (critic, not fixed):** x410 is outside the detection stack; `_reconcile_on_startup` aborts an active
  auto run on an agent restart without a morning-after alarm; `HEALTH_FAULT_PATTERNS` substring precision;
  wall-clock steps; a full "0 = disable" knob sweep. **Rollout:** OTA-push 1.31.1 (the GR pin included);
  rebuild the client bundle; the scripts' `stop.set()` + marker changes deploy with the library.

## Current state — RF-fault RECOVERY (Phase 3b — STANDALONE task auto-restart): COMPLETE (1.31.0, capability `task-auto-restart`) (branch `claude/system-familiarization-f5mezz`, cross-repo)
The task-level half of §7.1's Knob A: a task run **on its own** (not inside a run) that RF-faults is
relaunched by the agent **with the exact parameters it faulted under**, no operator present — but ONLY
when the task is NOT claimed by an active sequence/plan run (a run-owned fault is the run policy's, Phase
3; letting both act = a double-transmit). Design + record: `docs/rf-fault-recovery.md` §7.1 + §14e.
Orthogonal to the Phase-3 run policy; the two are kept apart by an **owned-query**. Suite 599 → 626. The
fast-warm IQ cache (§8) stays deferred. `argspec`/`ramp` untouched.
- **`process_manager.ManagedProcess._maybe_auto_restart_standalone()`** — the decision + relaunch. Stands
  down unless the effective flag is on (`_auto_restart_override` if the launch set one via `StartRequest`,
  else `config.auto_restart_on_fault`), the master kill-switch `AUTO_RESTART_ENABLED` is on, and the
  **owned-query** (`SequenceRunner.tasks_claimed_by_active_runs`, checked BEFORE and AFTER the settle
  delay) says no active run drives-or-will-launch the task. Rolling-window budget (`max_fault_restarts`
  within `restart_window_s`; 0 = unlimited). Ground-truth gates before relaunch: never over a RUNNING/
  STARTING task, never over a process whose `returncode is None`. Relaunches with the remembered
  `_last_request` (exact params) **through the manager launch hook** (`ProcessManager.relaunch` → `.start`
  → `_gate_precommand` repositions the attenuator + carries the carrier), else a bare `start` (isolation).
- **Two detection paths, exactly one relaunch.** Layer 1 — a NATURAL non-zero EXIT (`_watch`'s rf-fault
  branch) relaunches, but only when `not _stop_requested`. Layer 2 — the TRUE WEDGE (`_scan_task_health`
  auto-drops RF via `stop(operator=False)`, then fires the relaunch DETACHED so the watchdog stays
  responsive; the relaunch **awaits the old `_watch`** first so `start()` can't race its `_cleanup`). The
  `_fault_restart_inflight` latch (no `await` between its check and set) is the backstop.
- **Operator vs. auto-drop.** `stop()` gained `*, operator=True`; the auto-drop passes `operator=False`.
  Only an operator/API stop sets the new `_operator_stop_requested`, which aborts a pending relaunch — so
  an operator stopping the faulted task wins, while the channel-freeing auto-drop doesn't cancel its own
  recovery. `start()` re-arms `restart_giving_up` AND `fault_restart_giving_up`.
- **`models.py`** — `TaskConfig.auto_restart_on_fault` (bool=False) + `max_fault_restarts` (int=2);
  `StartRequest.auto_restart_on_fault` (`Optional[bool]`=None, the Run-form per-launch override); all
  defaulted. **`main.py::_spec_to_entry`** now persists the flag (+ budget when non-default) so it
  survives the `tasks.yaml` round-trip. Lifespan wires `set_fault_hook`/`set_owned_query`/`set_launch_hook`
  BEFORE `_manager.startup()` (runner constructed first). **`config.py`** — capability `task-auto-restart`,
  `AGENT_VERSION 1.30.0 → 1.31.0`; kill-switch reuses `AUTO_RESTART_ENABLED`.
- **`sdr-client`** (client-only): the `TaskConfig`/`StartRequest` mirror, `TASK_AUTO_RESTART_CAPABILITY` +
  `task_auto_restart_supported`, an "Auto-restart on fault" checkbox in the task editor (library always
  offers it; a live unit gates on `/info` + seeds + writes only when enabled, preserving a stored flag on
  an unsupported/unreachable unit) and the Run… form (per-launch override on `StartRequest`). See its
  CLAUDE.md.
Tests: `tests/test_task_auto_restart.py` (27) + `test_meta_endpoint`. **Adversarial review** (two find→
verify passes, all dims): confirmed + fixed — **HIGH** `_spec_to_entry` dropped the flag (never persisted
→ reverted on reload) → persist + round-trip test; **HIGH (concurrency/RF)** the wedge relaunch could race
the old `_watch`'s cleanup → detached + awaits the watcher, `_watch` relaunches only a natural exit;
**MEDIUM (double-TX)** the owned-query saw only FIRED launches, so a run arming mid-delay collided →
`tasks_claimed_by_active_runs` counts pending launches too; **MEDIUM** the client clobbered a stored flag
when `/info` failed → write only when the box is enabled; **LOW/MED** the relaunch skipped `_gate_precommand`
(attenuator) → routes through the launch hook; **LOW** `fault_restart_giving_up` never reset → `start()`
re-arms it. RF-emission invariants otherwise VERIFIED clear. One documented limitation: the relaunch
doesn't gate on an operator manually starting a DIFFERENT transmit task mid-fault (a pre-existing shared-
channel gap). **Phase 3b other half — DONE (measurement-driven pivot):** the "fast-warm" §8 shipped as an
IN-PLACE ~2.3× speed-up of the one genuinely-slow generator (`sdr-scripts` `gps_l2c_tx.py` `--loop full`,
~14→6 s), not a disk cache — L1C / L2C-cm measured ~0.3-0.5 s so a cache was pointless (see
`docs/rf-fault-recovery.md` §8 BUILT + `sdr-scripts/CLAUDE.md`). RF-fault recovery P0–P3b is COMPLETE.
**Rollout:** OTA-push 1.31.0;
`SDR_AUTO_RESTART=0` disables it alongside the run-level trigger; rebuild the client bundle from 1.31.0.

## Current state — RF-fault RECOVERY (Phase 3 — UNATTENDED auto-restart): COMPLETE (1.30.0, capability `sequence-auto-restart`) (branch `claude/system-familiarization-f5mezz`, cross-repo)
The trigger half of §7.1 on top of Phase-2 `restart_run`: a faulted run whose recovery policy is **"auto"**
is restarted by the agent's OWN tick, no operator/client present — a scheduled/overnight run recovers
itself. Design + full record: `docs/rf-fault-recovery.md` §7.1 + §14d. Owner-locked: agent-side trigger;
run-owned faults recovered ONLY via the run policy (never also the raw crash-restart supervisor →
double-transmit); budget resets after a healthy interval (not a lifetime cap). Suite 573 → 599. The
fast-warm IQ cache (§8) + the standalone-task checkbox stay **Phase 3b**. `argspec`/`ramp` untouched.
- **`sequence_runner._service_auto_restart(now)`** — per `_tick` (~0.25 s) after `_service_holds`.
  COLLECTS under `self._lock`, ACTS after releasing (like `_service_holds` — `restart_run` re-takes the
  non-reentrant lock for its pre-stop, so awaiting it inside would deadlock the tick). Pass 1 (locked):
  prune `_auto_inflight`/`_auto_gaveup` to live runs; per run — (a) **healthy-settle reset**: a RECOVERED
  run (`not run.fault`, `auto_restart_count>0`) whose `auto_restart_task` reads healthy (`_task_healthy` =
  running AND `ProcessStatus.health==OK`) for `AUTO_RESTART_HEALTHY_RESET_S` has its counter zeroed (an
  independent later fault gets a fresh budget); not-healthy restarts the settle timer; (b) **select or
  trip**: a RUNNING `restart_policy=="auto"` run with `fault`/`fault_task` set, not in `_auto_inflight` —
  if `auto_restart_count < AUTO_RESTART_BUDGET` → `to_restart` (+ `_auto_inflight`, so a later tick can't
  re-fire while awaiting), else trip once (`_auto_gaveup` → `to_trip`). HOLDING/confirm/manual left for
  the operator. Pass 2 (unlocked): `await restart_run(run_id, RestartRequest(mode, restart_at=now))`; on
  success increment the count, stamp `auto_restart_task`, clear the marker, persist, fire a QUIET
  `sequence_auto_restart`; on a raised refusal (resync-past-off-air / replay collision) trip via
  `_auto_restart_gaveup`. Each `to_trip` runs `_auto_restart_gaveup` → re-fires the LOUD
  `sequence_rf_fault` ONCE, leaves the run RUNNING-faulted for a manual Restart. The persisted
  `auto_restart_count` is the durable breaker (a reload never resurrects an exhausted run).
- **`process_manager._watch`** — the rf-fault EXIT branch now `return`s right after `_flag_rf_fault`
  (before the `restart_on_crash` supervisor), so a run-owned rf-fault is recovered ONLY by `restart_run`,
  never double-transmitted. The exit is fully recorded first (`_cleanup`/`ExitRecord`/`state=CRASHED`);
  the `return` only skips the raw relaunch. An ordinary crash keeps the crash-restart path.
- **`models.py`** — `SequenceRun.restart_policy`/`restart_mode`/`auto_restart_count`/`auto_restart_task`/
  `auto_restart_healthy_since` (all defaulted, persisted); `ArmSequenceRequest.restart_policy`/
  `restart_mode` (default `"manual"` — a pre-Phase-3 client / reloaded run never gains autonomy);
  **`Sequence`/`CreateSequenceRequest.recovery_policy`/`recovery_mode`** (the agent MUST persist +
  round-trip the authored policy — the client reads it back to resolve what to arm; a plan/schedule item
  inherits it); the `sequence_auto_restart` webhook type. `arm()` stamps the run policy;
  `create/update_sequence` carry the authored policy. **`config.py`** — `AUTO_RESTART_ENABLED`
  (kill-switch), `AUTO_RESTART_BUDGET` (2), `AUTO_RESTART_HEALTHY_RESET_S` (60; each 0 disables its
  limit), capability `sequence-auto-restart`, `AGENT_VERSION 1.29.0 → 1.30.0`.
- **`sdr-client`** (client-only): `recovery_policy`/`recovery_mode` on Sequence/CreateSequenceRequest
  (+ the `LibraryClient` round-trip), `restart_policy`/`restart_mode` on ArmSequenceRequest, the 5
  runtime fields on SequenceRun, PlanItem inherit-or-override; `timeline_model.resolve_arm_recovery`
  (auto→manual downgrade when unsupported) + `fault_pill` + `sequence_auto_restart_supported`; a
  sequence-editor recovery combo + save gate; every arm path (sequences/plan/**schedule** — the primary
  unattended surface) carries the resolved policy; an amber `auto_restart` pill. See its CLAUDE.md.
Tests: `tests/test_sequence_auto_restart.py` (26) + `test_meta_endpoint`.
**Adversarial review** (two find→verify passes, all dims): confirmed + fixed — **HIGH** a manual Restart
racing the auto trigger (loser's pre-stop / misread refusal false-alarmed a recovered run) → a per-run
`_restart_inflight` guard + the auto except stands down quietly on `_RestartInProgress` / a
recovered-or-non-RUNNING run; **HIGH (RF-safety)** the pre-stop no-ops while the watchdog is mid-stop
(STOPPING ~10 s to SIGKILL a wedged flowgraph) so the auto trigger could double-transmit → a ground-truth
`ProcessManager.is_process_alive` + the auto path DEFERS while alive + `restart_run` refuses over a live
proc; **MEDIUM** the authored policy was dropped on store (agent + `LibraryClient`) → all persist it now;
**MEDIUM** a manual Restart resets the breaker (fresh budget, clears the latch; auto passes
`reset_budget=False`); a refusal give-up made DURABLE (count→budget); the settle reset persists + is
floored to `3·HEALTH_POLL_S`. RF-emission invariants otherwise VERIFIED clear. One LOW documented-not-
fixed (a multi-task run's counter doesn't reset if the restarted task's window ends before the settle —
conservative). **NEXT — Phase 3b**: the standalone-task Auto-restart-on-fault checkbox
(`TaskConfig.auto_restart_on_fault` + an owned-query so `process_manager` relaunches a task NOT owned by
a run) + the fast-warm IQ cache. **Rollout:** OTA-push 1.30.0; `SDR_AUTO_RESTART=0` disables the trigger
fleet-wide (a faulted "auto" run then waits for a manual Restart, like "confirm").

## Current state — RF-fault RECOVERY (Phase 2): COMPLETE (1.29.0, capability `sequence-restart`) (branch `claude/system-familiarization-f5mezz`, cross-repo)
Operator-driven recovery: one "Restart" click brings a faulted run back on air at the level it should
be at, with a resync/replay choice. Design + full record: `docs/rf-fault-recovery.md` §7 + §14c. The
UNATTENDED auto-restart trigger + fast-warm cache stay **Phase 3** (§11). Suite 554 → 573. `argspec`/
`ramp` untouched (drift guard intact).
- **`sequence_runner.restart_run(run_id, RestartRequest)`** (adjacent to `proceed`/`hold_now`). A run
  whose task faulted is still RUNNING with `run.fault`/`fault_task` stamped + that task's un-fired steps
  `'skipped'` (Phase-1 `on_task_fault`). restart_run recovers **IN PLACE** (no re-arm, no channel-guard
  re-run, run log stays open), under two locks (validate+snapshot → release to STOP the faulted proc →
  re-validate+plan+commit atomically): (1) **reconstruct the born-at state** (`_relaunch_start_fire`) —
  walk the faulted task's fires ≤ now, rebuild the launch args, overlay every counted tune/ramp point;
  generalised over WHATEVER it swept (power/gain/bridge), `--power` baked SPEC-INDEPENDENTLY
  (`_LEVEL_FALLBACK_FLAGS`) so the level survives a transient `spec=None`. `resync` counts fired-OR-skipped
  ≤ now (the SCHEDULE'S staircase level now, even across a down-time spanning ramp steps); `replay` counts
  fired-only (the CRASH level). (2) **re-instate the faulted task's SKIPPED fires** (only that task, never
  a peer): `resync` re-instates the FUTURE ones (`fire_at>now`) on their original `fire_at`; `replay`
  re-instates the WHOLE remainder shifted by the downtime `now-fault_at`, floating `on_air_end`. (3)
  **guards, BEFORE any mutation** (atomic refusal): a non-open-ended run with no future STOP is refused
  (else RF stays on) — resync past off-air is pointed at replay; a `replay` whose shifted CHANNEL span
  (off-air + its stop tail via `_channel_end`) overlaps another active run is refused (`_guard_replay_
  channel`); the second lock re-checks state/fault/fault_task. (4) **relaunch** with ONE synthetic `start`
  fire at `now`: the reconstructed command with the RF gate at its RECONSTRUCTED state (the schedule's
  gate at now — on mid-transmission, MUTED in the pre-roll/cool-down), attenuator positioned at the
  carrier BEFORE start, no blip; (5) clear `run.fault`.
- **Two deliberate as-built deviations from the doc's §7.3** (from the understand-map's traps, both
  documented in §14c): (a) **in-place, not abort-and-re-arm** — Phase 1 chose a non-terminal `fault`
  FIELD on a still-RUNNING run precisely so Phase 2 recovers in place (the doc's "abort the predecessor"
  assumed the older fault→terminal model); (b) **one launch-at-level fire, not the 3-fire
  muted-then-gated dance** — a co-timed relaunch→tune→rf-on at `fire_at=now` is fragile (the
  `_co_time_rank` ordering trap + the control-socket-bind race silently drops the tune). Launching
  directly at the level carries it on the launch command → same no-blip guarantee, no race.
- **`main.py`** `POST /sequence-runs/{id}/restart` (404 unknown / 409 not-RUNNING|no-fault|no-STOP|
  collision|fault-changed). **`models.py`** `RestartRequest{mode='resync'|'replay', restart_at}` + the
  `sequence_restart` webhook type. **`config.py`** capability `sequence-restart`, `AGENT_VERSION 1.28.0 →
  1.29.0`.
- **`sdr-client`** (client-only): `restart_sequence_run`, a "Restart" button on a FAULTED run row
  (sequence + plan; the plan row also gains the RF-FAULT pill), the resync/replay choice, gated on both
  `task-rf-health` + `sequence-restart`. See its CLAUDE.md.
- **Adversarial review** (find→verify, all dims): a review of the first build found **12 confirmed
  defects (A–H)**, ALL fixed + pinned (A late-restart RF-left-on → the no-future-STOP guard; B swept param
  not just power; C peer step wrongly skipped → task-scoped; D resync uses the schedule level at now; E
  replay re-instates the whole remainder; F+G collision guard before mutation, using the stop-tail
  `_channel_end`; H second-lock re-validation). A **re-review of the rewrite** confirmed **2 more**, both
  fixed: (1, LOW) a transient `spec=None` reverted the relaunch to the launch `--power` (hot over-power) →
  spec-independent power/gain bake; (2, MED) the relaunch FORCED RF on → the gate is now RECONSTRUCTED from
  the schedule (muted pre-roll/cool-down stays muted). A **re-verification round** (the re-review's verify
  pass had partially aborted on a session limit; re-run in full against the fixed code) closed its 12
  un-adjudicated findings: 2 ALREADY-FIXED (the gate pair), 8 REFUTED (HOLDING-fault cleanly refused;
  multi-fault coupling is a Phase-1 `on_task_fault` limit; run-mode ramps aren't RF transmitters; rest
  covered), **2 CONFIRMED + fixed** — (i) a bridge-param (`--bw`) reconstruction test (the `_dest_flag_map`
  path had no discriminating test); (ii, correctness) the `_fire_step` completion check lacked a
  `run.fault` guard, so a MULTI-task run whose HEALTHY peer finished flipped to COMPLETED with the fault
  unrecovered (restart refuses a non-RUNNING run) → the check now also requires `not run.fault`, keeping a
  faulted run RUNNING/restartable (Phase-1 already dropped its RF).
Tests: `tests/test_sequence_restart.py` (19: reconstruction incl. launch-level fallback; resync/replay
shapes; the collision refusal; 404/409 guards; a **LIVE** end-to-end faulting a real ramp at −70 →
restart → completion; + a regression per review finding A–H, both re-review findings, and both re-verify
findings — `--bw` bridge reconstruction + a faulted multi-task run not auto-completing) +
`test_meta_endpoint`. **NEXT — Phase 3**: the task Auto-restart-on-fault checkbox + sequence/plan
auto-restart policy (unattended, budget 2) + the fast-warm IQ cache. **Rollout:** OTA-push 1.29.0.

## Current state — RF-fault DETECTION (Phase 1): COMPLETE (1.28.0, capability `task-rf-health`) (branch `claude/system-familiarization-f5mezz`, cross-repo)
Detect a dead-but-alive GNU Radio flowgraph (a halt that does NOT exit — the SDR is silent while the
task reads RUNNING), alarm loudly on the client, auto-drop RF, and capture a self-diagnosing resource
snapshot. Design + full change list: `docs/rf-fault-recovery.md` §14b "Phase 1 — BUILT". Detection is
LAYERED so no single blind spot hides a halt; **health is a SEPARATE axis from `ProcessState`** (a
halted flowgraph is still process-RUNNING — its fault can't be a `ProcessState` value without racing
the exit machine). Suite 537 → 553. `argspec`/`ramp` untouched (drift guard intact).
- **`paramkit/txhealth.py`** (new, shared) — `watch_flowgraph(tb, stop, *, reason=…, stream=…)`: a
  daemon thread that calls `tb.wait()`; GR does NOT re-raise a halted flowgraph to Python, so
  `tb.wait()` RETURNING with `stop` still UNSET IS the fault signal. It prints `FAULT_MARKER`
  (`HEALTH state=faulted reason="…"`, flushed whole), sets `stop`, latches `.faulted=True`; the script
  does `return 1 if _health.faulted else 0`. Marker + non-zero exit = Layer 1.
- **`process_manager.py`** — Layer 2 **watchdog** `_health_loop` (every `HEALTH_POLL_S`≈2 s) scans each
  RUNNING not-yet-alarmed task's NEW log bytes (`log.read_since`, inode/truncation-safe) for a
  `HEALTH_FAULT_PATTERNS` signature (marker / `vmcircbuf` / `boost::interprocess`) — the ONLY path for
  the true-wedge case (no exit + no done-watcher). A hit → `_flag_rf_fault(detail)` (idempotent latch:
  `health=RF_FAULT`, snapshot, event, `_fault_hook`) → auto-drop RF via `proc.stop()` (SIGTERM→SIGKILL;
  idempotent). `_watch`'s crash branch routes an rf-fault exit (health flagged OR log tail matches) to
  `_flag_rf_fault` too. `_fire_health_event` puts a `TaskHealthEvent` on the SSE stream. `set_fault_hook`
  wired in `main.py` after the manager + runner exist.
- **`sequence_runner.py`** — `on_task_fault(task_name, detail)` COUPLES a task fault into the owning run
  (under `self._lock`, via the refactored `_live_tasks_of`): stamps `run.fault`/`fault_task`/`fault_at`,
  marks that task's un-fired steps `"skipped"` (the `hold_now` sentinel), persists, fires
  `sequence_rf_fault`. An rf_fault FIELD on a still-RUNNING run — NOT a terminal `SequenceState` (the
  restart is Phase 2).
- **`system.py`** — `capture_fault_snapshot(...)` (async wrapper `fault_snapshot`, off-loop): a
  best-effort snapshot that READS THE PHASE-0 ENV WORK BACK — effective `GR_CONF_VMCIRCBUF_DEFAULT_FACTORY`
  (env vs `gnuradio-config-info --prefs` compiled), `/dev/shm` used/total, `/proc/pid/maps` count vs
  `vm.max_map_count`, RSS, `RLIMIT_NOFILE`, HOME, `ipcs -m` (only if a SysV backend is implicated), the
  UHD-log tail → `snapshot_<ts>.json` beside the run log (`snapshot_path` set BEFORE the write). Every
  subprocess `timeout`-bounded + guarded; never raises. `log_manager.cleanup` prunes `snapshot_*.json`.
- **`models.py`** — `TaskHealth` (only OK/RF_FAULT set in P1; STALLED/UNKNOWN reserved),
  `ProcessStatus.health`/`health_detail`/`last_output_at`, `FaultSnapshot`, `TaskHealthEvent`,
  `SequenceRun.fault`/`fault_task`/`fault_at`, the `sequence_rf_fault` webhook type (all defaulted).
  **`config.py`** — `HEALTH_WATCH_ENABLED`/`HEALTH_POLL_S`/`HEALTH_FAULT_PATTERNS`, capability
  `task-rf-health`, `AGENT_VERSION 1.27.4 → 1.28.0`.
- **`sdr-scripts`** — the 30 CLEAN-set RPi scripts (repeat=True/continuous, incl. `fm_chirp`) adopt
  `watch_flowgraph(tb, stop)`; the 4 FIFO stagers (`gps_l1p`/`gps_l2p`/`white_noise`/`gaussian_noise`,
  repeat=False) are EXCLUDED (a normal EOF returns `tb.wait()` with `stop` unset → a FALSE fault; they
  rely on the watchdog). See its CLAUDE.md.
- **`sdr-client`** — the loud alarm (`main_window._on_alert` beep+flash / `_on_fault` raise+tray), the
  fault-snapshot diagnosis dialog, fault pills on the task/sequence rows + fleet card, `task-rf-health`
  gate (Phase-2 Restart only). See its CLAUDE.md.
Tests: `tests/test_txhealth.py`, `tests/test_task_health.py`, `tests/test_fault_snapshot.py`,
`test_meta_endpoint.py` (asserts the capability). Suite 537 → 554. **Verified LIVE**: the marker
injected into a running mock task → rf_fault + auto-drop + a snapshot recovering the P0 env work
(`mmap_shm_open`, HOME) from `/proc/pid/environ`; an ordinary crash does NOT false-positive.
**Adversarial review** (find→verify, all dims): two findings REFUTED (a split-token log miss + an
abandoned-shutdown auto-drop — whole-line atomic writes + the redundant exit path; `shutdown` reaps
every proc via its own idempotent stop gather) and two LOW findings FIXED — **`system._count_maps` now
reads `/proc/<pid>/maps` in BINARY** and counts `b"\n"`, so a mapped file with a non-UTF-8 pathname
can't raise `UnicodeDecodeError` out of the best-effort snapshot (the sibling helpers already guarded
it; `_count_maps` was the lone outlier); and the **client fault dialog now flags a leaky sysv_shm
COMPILED default when the GR env pin is unset** (was env-only). Both with regression tests. **NEXT —
Phase 2**: `restart_run` + `POST …/restart`, resync/replay,
`sequence-restart`. **Rollout:** OTA-push 1.28.0; no re-provision for detection (agent/script code);
rebuild the client bundle from 1.28.0.

## Current state — RF-fault PREVENTION (Phase 0): COMPLETE (1.27.4) (branch `claude/system-familiarization-f5mezz`, cross-repo)
The prevention/ops layer of the RF-fault design (`docs/rf-fault-recovery.md` §14a "Phase 0 — BUILT").
Behaviour only, NO capability (`AGENT_VERSION 1.27.3 → 1.27.4`); `argspec`/`ramp` untouched (drift
guard intact). Suite 519 → 537.
- **`paramkit/txstage.py`** (new, shared pure-stdlib) — `staging_dir(signal)` stages under a TAGGED,
  PID-bearing name `/dev/shm/sdrtx-<pid>-<signal>-…`; `sweep_orphans()` reclaims ONLY dead-PID
  `sdrtx-*` entries (never a live sibling, a foreign object, or GR's own `vmcircbuf_*`). The tag is
  the contract between the scripts (which stage) and the agent (which sweeps).
- **`process_manager.py`** — `_launch_env_pins(task_dir)` merged at all THREE launch env sites
  (`start`/`run_oneshot`/`_launch_oneshot_wait`) BETWEEN `os.environ` and `cfg.env`, so the pins beat
  ambient but `cfg.env`/`req.env_overrides` still win: `HOME` (stable+writable), the GR vmcircbuf
  backend (`GR_CONF_VMCIRCBUF_DEFAULT_FACTORY` — *superseded by §14f #1: GR reads a pref FILE the
  agent now writes; the env var is inert* — was believed the only pin GR reads, since scripts set
  `GR_DONT_LOAD_PREFS=1`), and per-task UHD file logging (`UHD_LOG_FILE` next to `current.log` +
  level, capturing the FPGA image load while the console stays off). `_sweep_shm_orphans()`
  (flag-gated, best-effort) runs before each managed launch and in `_cleanup()` (post-exit/SIGKILL).
- **`system.py`** — `pre_image_sdr()` opens the SDR via `uhd_usrp_probe` (loads the FPGA image), NOT
  `uhd_find_devices`; no-op when the tool isn't on PATH. **`main.py`** — `_boot_sweep()` (before
  `_manager.startup()` so no live task's buffers are swept) + `_preimage_when_idle()` fired **DETACHED**
  (`create_task`, never awaited) after startup, gated on the device being free + hard-bounded by
  `wait_for`. Detaching is deliberate (a `/code-review` finding): a wedged USB SDR can leave
  `uhd_usrp_probe` unkillable in D-state, and awaiting it on the lifespan path could hang boot and
  brick the unit — the exact fault this defends against; detached, boot always completes.
- **`config.py`** — new knobs `TASK_HOME`/`GR_VMCIRCBUF_FACTORY`/`UHD_LOG_FILE_LEVEL`/
  `SHM_SWEEP_ENABLED`/`PREIMAGE_ON_BOOT`/`PREIMAGE_TIMEOUT_S` (each `""`/`0` disables its pin/step).
- **Deploy** — new `deploy/99-sdr-agent.conf` (`vm.max_map_count=262144`) installed by
  `provision_install.sh`/`migrate_layout.sh`/`x410/install.sh` (+ a non-fatal `/dev/shm` size check);
  `sdr-agent.service` gains `HOME=/root` + the GR pin + `LimitNOFILE=65536`; `x410/install.sh` gains
  `LimitNOFILE` + the GR pin; `run_local.sh` mirrors the GR pin. Auto-bundled. **NB: the deploy/sysctl/
  service-env hardening reaches field units via a RE-PROVISION / migrate, NOT the OTA "Update agent…"
  button** (which only restarts the service).
- **`sdr-scripts`** — the 13 RPi loop-file + 4 FIFO stagers now use `txstage.staging_dir` (see its
  CLAUDE.md); `fm_chirp` + GPS vector_source scripts stage nothing (untouched).
Tests: `tests/test_txstage.py`, `tests/test_launch_env_pins.py`, `tests/test_preimage.py`. Verified
live: the agent boots clean headless (pre-image no-ops with no radio, `/health` ok, no traceback).
**DEFERRED within P0**: arm-time pre-image (needs a device mutex — P1) + the x410-stager tag migration.
**NEXT — Phase 1**: detect (script `txhealth.watch_flowgraph` done-watcher + agent watchdog) + a loud
alarm + a fault-time resource/backend/UHD-log snapshot (which reads the P0 env work back). Full
design + phasing in `docs/rf-fault-recovery.md`.

## Current state — RF-fault detection & sequence recovery: DESIGN — P0–P2 BUILT (1.29.0); P3 pending (branch `claude/system-familiarization-f5mezz`, cross-repo)
*(Design-time reading — REFUTED 2026-09-21, see the top section + `docs/rf-fault-recovery.md` §14k.)*
Field incident: an `fm_chirp` `--power` sweep (Pi 5) hit a GNU Radio **`vmcircbuf`** (shared-memory
buffer) error **at startup** but the script did NOT exit, so the agent showed the task RUNNING while the
SDR sent nothing; recovery was a manual plan-rebuild + eyeballed ramp position. Root cause + fix are
written up in **`docs/rf-fault-recovery.md`** (engineering design) and **`docs/incident-fm-chirp-vmcircbuf.md`**
(one-page for stakeholders). NOT built yet. In one paragraph: liveness is exit-code-only
(`ManagedProcess._watch` blocks on `proc.wait()`), so a halted-but-alive flowgraph is invisible; the fix
LAYERS detection (a `paramkit/txhealth.watch_flowgraph` done-watcher that turns a silent GR halt into a
non-zero exit — GR doesn't re-raise to Python, so `tb.wait()` returning with `stop` unset IS the signal —
plus a ~2 s agent log-scan/heartbeat watchdog over `_tasks_owned_by_active_runs()`), stamps a NEW
`ProcessStatus.health`/`TaskHealthEvent` + couples the fault into the run, ALARMS loudly (implement the
`sdr-client` `main_window._on_alert` stub), auto-drops RF (SIGTERM→SIGKILL) while KEEPING the archived log
+ a resource snapshot (`/dev/shm`, `vm.max_map_count`, backend) so the next one self-diagnoses, and
RECOVERS via a new `SequenceRunner.restart_run` + `POST /sequence-runs/{id}/restart` that reuses the
persisted original `on_air_at`/`on_air_end`, marks `fire_at<=now` fires `"skipped"` (the `hold_now`
sentinel), reconstructs the current ramp level from the last past tune fire (NOT eyeballed), and relaunches
muted-then-gated (`_gate_precommand`/`_co_time_rank`, no hot blip). Recovery has two owner-set knobs:
trigger (task-level Auto-restart checkbox → relaunch with crash-time params; sequence/plan policy default
auto+resync, else operator-confirmed with replay-forward vs resync; plan overrides sequence) and semantics
(resync = rejoin the original schedule / replay = restart from the crash point, whole run shifts later);
auto budget default 2. Prevention (the actual `vmcircbuf` cause): raise `vm.max_map_count`/`/dev/shm`,
agent `/dev/shm`+IPC hygiene around the task lifecycle, graceful shutdown, optional GR backend pin. Optional
fast-warm IQ-buffer cache (L1C/L2C 30 s regen) keyed on shape params only. Gated by NEW capabilities
`task-rf-health` + `sequence-restart` (version bump from `1.27.3`); `argspec`/`ramp` untouched (drift guard).
Phasing: P0 prevention/ops · P1 detect+alarm+safe-stop+snapshot · P2 restart&resync/replay · P3 fast-warm+
unattended auto. OPEN: retrieve the unit's archived `run_<ts>.log` + `df /dev/shm`/`vm.max_map_count`/`ipcs`
to confirm the exact startup mechanism (default `mmap_shm_open` self-cleans, so a leaky `sysv_shm` fallback
or `/dev/shm` pressure is the suspect).

## Current state — arm a whole day of scheduled plans at once (1.27.3): COMPLETE (branch `claude/arm-multiple-scheduled-plans`, stacked on 1.27.2; client "Arm all" in `sdr-client`)
Owner ask: four non-overlapping plans in the schedule; arm ALL of them and have them start/stop on
their own. Scheduled runs already fire at absolute times, so the blockers were the two arm guards in
`sequence_runner.arm` (both refused a LATER, disjoint window):
- **A0 ("task(s) already running")** fired whenever the plan's task was on air — even when it was on
  air BECAUSE of an earlier scheduled run whose window ends before the new one. New
  **`_tasks_owned_by_active_runs()`**: task names an ARMED/RUNNING/HOLDING run has LAUNCHED (a fired
  `start`/`run`, not `"skipped"`) and not yet STOPPED (no fired `stop` after it). A0 now exempts those
  (guard A decides — the run's channel span is known); a task started by hand / by another owner, a
  merely-armed run's task, or a task the run already stopped and someone restarted, is still refused.
- **A (window overlap)** counted the launch lead-in (`earliest_fire`, e.g. the START 1 s before on-air)
  but NOT the stop tail (the STOP 1 s after off-air), so two back-to-back client-authored windows
  (`START −1 s` / `STOP +1 s`) were refused when touching, yet ACCEPTED with a 1 s gap — where run 1's
  STOP would land on run 2's freshly launched task. New static **`_channel_end(on_air_end, fires)`** =
  `max(on_air_end, last fire_at)` (None when open-ended); `_active_span` (now a classmethod) uses it
  for every active run and `arm` uses it for the new run's `new_end`, so a gap of lead-in + tail is
  required and the refusal names it (message still carries `overlaps run <id>`, plus "this run would
  occupy the channel HH:MM:SS–HH:MM:SS UTC, counting its launch lead-in … and its stop tail …; leave
  a gap").
`config.py` bumps `AGENT_VERSION 1.27.2 → 1.27.3` (behaviour only, NO capability — an older agent just
refuses the later arm the old way; the client's "Arm all" works against any agent, one entry at a time).
`argspec`/`ramp` untouched (drift guard intact). Tests: `tests/test_arm_guards.py` (`_channel_end`; a
later disjoint window arms while an earlier run is ON AIR; an overlapping one is still refused; a task
running outside any run / after its run stopped it is still refused; touching + 1 s-gap windows collide
with the tail reason, a 2 s gap arms; four disjoint hour windows arm in one go). Suite 513 → 519.
Client side: `sdr-client/ui/timeline_tab.py` "Arm all (N)" on the schedule tab (see its CLAUDE.md).

## Current state — the attenuator is realized at the task's CARRIER, not `center_freq_hz` (1.27.2): COMPLETE (branch `claude/active-freq-consistency`, agent + scripts)
Owner report: "suddenly the calibrations I have done are off by about the cable loss of the cable I
used while measuring" — after adding frequency-dependent cable tables to the chain (many copies of
the same cable, all but one bypassed — bypass verified irrelevant). De-embed math, bypass and the
panel round-trip all checked out numerically; the real cause was a SPLIT mismatch on a chain with a
programmable attenuator: the agent realized the attenuator at the signal's `center_freq_hz`
(`_gate_precommand` → `active_settings(name, power)` with NO frequency; nothing ever set
`SDR_CAL_FREQ_HZ`), while the script folded its SDR gain at the LIVE carrier. `AchievableGrid.realize`
picks the closest exact hit, so the SDR/attenuator split jumps with frequency on a frequency-dependent
chain (sample cal: 83.0/21.5 at 1500 MHz vs 80.5/19.5 at 1575 MHz → 2 dB off) — the two halves belonged
to different realizations. Fixed, behaviour only (`AGENT_VERSION 1.27.1 → 1.27.2`, no capability):
- **`tune_log.freq_hz_of(spec, values)`** (new, shared) — a task's carrier in Hz: the script's
  `CAL_FREQ_PARAM` value (a launch's args / a tune) else its schema default, scaled by the param's
  DECLARED unit (MHz on every shipped script — GPS `-Center-frequency`, CW `--freq`; a Hz-declared
  param folds right too). None without a freq param.
- **`process_manager`** — `_freq_from_command(cmd, spec)` reads it off the launch command (last flag
  wins). `_gate_precommand` keeps `freq_hz` in the per-task gate state (seeded from the command,
  updated by a tune of the freq dest) and passes it to `active_settings` / `_mute_settings`;
  `set_params` fires the precommand on a CARRIER retune too (not only power / RF gate). `start` /
  `restart` go through `_with_launch_freq`: the launch env gets `SDR_CAL_FREQ_HZ` (an explicit task
  config / request value wins) so the injected artifact's v1 curve + bounds fold at the carrier.
- **`run_table.build_task_table`** realizes each row at the carrier in effect on THAT row (the
  `power_realizer` closure already took a `freq`), so the exported SDR gain / attenuation columns
  reproduce what the unit commanded.
- **`calibration.resolve`** (latent second bug) — the measurement de-embed and the source-bias ZERO now
  anchor at the signal's `center_freq_hz` (`meas_freq`) whatever `freq_hz` the caller folds the
  read-outs at. Before, an explicit fold frequency re-zeroed the flatness AT the carrier (erasing the
  correction exactly where the tone was) and evaluated the bench cable there — so an artifact resolved
  at the carrier modelled the SDR differently from one resolved without, and `realize` split
  differently. Now both agree at every frequency.
- **`paramkit/calkit.PowerMap`** — `has_actives`, `pinned_applied(power, freq)` (the realization's
  total applied dB — what the agent commands) and `gain_for_power` / `power_for_gain(...,
  applied_db=)` fold the SDR gain with the components PINNED there, for a script whose frequency
  moves WITHOUT the agent (`sdr-scripts` `cw_drift_tx.py` pins at the start carrier; re-realizing per
  frequency would hop the assumed attenuation by whole steps while the hardware stayed put). A no-op
  without actives. Verified: the pinned fold reproduces `realize` exactly (0 dB) across insertion
  loss / engagement / gain steps.
`argspec`/`ramp` untouched (drift guard intact); no client change (it never set the env). Tests:
`tests/test_active_freq_consistency.py` (unit scaling; the command carrier; the split genuinely
moves; a launch positions the attenuator at the command's carrier, a carrier retune repositions it,
power / RF tunes keep it; the launch env carries the carrier and an explicit env wins; the export
realizes per row), `tests/test_calibration_freq_anchor.py` (bias zero + de-embed stay at
`center_freq_hz`; the artifact models the SDR at a carrier identically from either resolve; the split
no longer depends on the fold frequency), `tests/test_calkit_pinned.py`. Suite 498 → 513. Docs:
`docs/calibration-v2.md` §12.3 + §9/§11 notes. **Rollout:** OTA-push 1.27.2; no re-calibration and
no client change needed. The drift script's pin needs paramkit ≥ 1.27.2 on the unit.

## Current state — proceed's off-air lands after the WHOLE post-hold content (`/code-review` fixes, 1.27.1): COMPLETE (branch `claude/step-to-step-anchoring`, agent-only)
A `/code-review` of the ramp-pause work found three `proceed`-path defects (two pre-existing since
Phase 1). Fixed in `sequence_runner.py`, behaviour only — `AGENT_VERSION 1.27.0 → 1.27.1`, no capability:
- **Zero dwell for the last post-hold level.** `content_s` was the LAST hold-anchored FIRE, so
  `on_air_end` landed exactly on it and the STOP fired the same tick — the top of a resumed crossing ramp
  (or the bottom of a hold-anchored down-ramp) was touched, never held (the very defect fixed for `both`
  ramps in 1.25.2 / step-anchor ends in 1.25.1). Now the forward extent = `ramp.min_on_air_duration`
  over the hold defs re-anchored `start` (a ramp's FULL span, last dwell included) and, for the resumed
  remainder, `offset_s + dwell_s` — `StepFire.dwell_s` is a new optional field stamped by
  `_split_fires_at_hold` from the ramp's uniform spacing (to the previous point of the same task + tuned
  keys).
- **Stop-anchored window-B content resolved BEFORE `T_resume`.** `content_s` ignored stop-anchored defs,
  so a stop-anchored down-ramp longer than the hold-anchored content (or with none) placed its points in
  the past and `_tick` burst-fired them plus the STOP. Now `on_air_end = T_resume + forward + backward`
  where backward = `ramp.min_on_air_duration(stop_defs)` — the off-air work lands AFTER the post-hold
  work, which is exactly the picture the client draws (off-air floats past both groups, sum not max).
- **`patch_on_air_end` dropped every hold/enter fire of a proceeded run** (it rebuilt `run.steps` via
  `_resolve_steps` without `hold_at`/`enter_at`; HTTP-only, no client button). It now refuses a
  `hold_aware` run (`ValueError` → 400: "cannot move the on-air end of a Hold run").
Tests: `tests/test_sequence_hold_ramp_pause.py` (the resumed remainder's dwell is carried; off-air after
a stop-anchored down-ramp with/without hold content + a hold-anchored ramp's last dwell; PATCH refused
with the fires intact), `tests/test_sequence_hold_runtime.py::test_proceed_resolves_a_window_b_ramp`
(off-air at last fire + hold; the bottom level is still transmitting before it). Suite 496 → 498.

## Current state — a ramp ACROSS the Hold is PAUSED there and resumes after proceed: COMPLETE (branch `claude/step-to-step-anchoring`, cross-repo)
Owner question: a ramp can be placed so its middle lies inside the Hold window; reject it, or let it
hold? Decision (owner-approved): ALLOW it with "the pause freezes the ramp" semantics — a Hold means
time stops, so the ramp holds the level it had reached and continues after Proceed, shifted by the
pause's length. Before this the agent kept the whole ramp in window A and `_service_holds` waited
for `window_a_done`, so the pause was silently DELAYED until the ramp finished (the hold offset was
missed); validation only checked a start-anchored step's START against the Hold. Agent side:
- **`_split_fires_at_hold(fires, hold_time)`** (new static) splits the resolved window-A fires at the
  pause instant: at/before it → `run.steps`; after it → re-tagged `anchor="hold"`, `offset_s` = seconds
  past the pause. `arm` (hold mode) applies it right after `_resolve_steps` and stores the remainder as
  the new **`SequenceRun.paused_fires`** (`models.py`, persisted). Validation keeps every other window-A
  step at/before the hold, so only ramp points ever land there; a ramp STARTING after the hold is still
  rejected. `_service_holds` is untouched — window A now genuinely ends at the pause.
- **`proceed`** re-bases each paused fire to `T_resume + offset_s` (so the ramp resumes where it left
  off, shifted by the pause) and counts them toward the post-hold content that fixes `on_air_end`;
  `paused_fires` is cleared once scheduled. **Edit-while-holding** (`req.steps`) re-derives the remainder
  from the EDITED window A (re-resolve against T0 + `hold_at_offset_s`, split again), so retargeting
  the crossing ramp's top while holding takes effect. **`hold_now`** is unchanged (jump-the-clock rule:
  what would have fired before the pause is skipped, the deferred remainder still resumes).
- **`config.py`** capability **`sequence-hold-ramp-pause`** + `AGENT_VERSION 1.26.0 → 1.27.0` (a safety
  gate: the client refuses to save / hold-aware-arm a crossing ramp on a ≤1.26 agent, which would delay
  the pause). The schedule/plan path compiles the Hold out (the ramp runs straight through) — no gate.
  `argspec`/`ramp` untouched (drift guard intact). Tests: `tests/test_sequence_hold_ramp_pause.py`
  (validation, the split incl. a point AT the pause staying in window A, edit-while-holding
  re-derivation, and a LIVE run that freezes the mock task at 30, holds it, and reaches 40 after
  proceed) + `test_meta_endpoint.py`. Suite 491 → 496. Client side + design note:
  `sdr-client/docs/sequence-hold-step.md` §5.7, `sdr-client/CLAUDE.md`.

## Current state — `anchor="enter"`: a window-A step timed from the Hold's ENTER instant (the pause's start): COMPLETE (branch `claude/step-to-step-anchoring`, cross-repo)
Owner ask (v3 #4): in the client's Hold WINDOW a ramp's END should anchor to the LEFT edge (where the
pause begins), while a start / a tune anchors to the resume edge. Agent side:
- **`_validate_steps`** accepts `anchor="enter"`: requires a HOLD marker and `offset_s <= 0` (nothing may
  reach INTO the pause — the run is holding then). `models.py` documents the value.
- **`_split_hold_windows`** routes it to WINDOW A (only `hold`/`stop` go to window B) — it's known at arm.
  **`_lead_offset`** counts `hold_off + offset_s` (an enter step may precede on-air like any lead-in).
- **`_resolve_steps(..., enter_at=)`** — `arm` passes `enter_at = on_air_at + hold_at_offset_s` in hold
  mode; pass 1 places a point at `enter_at + offset_s` (fire anchor `"enter"`), and **`_resolve_ramp`** uses
  the STOP layout (`place_ramp("stop", offset_s, …)` — backward, the last level's hold ENDS at the pause +
  offset) rebased to `enter_at`. Without `enter_at` (not a hold-aware arm) an enter step produces no fire;
  a non-hold-aware arm with a Hold is refused anyway, and the client's schedule/plan path compiles the
  anchor out (`collapse_hold`) before sending.
- **`config.py`** capability **`sequence-hold-enter`** + `AGENT_VERSION 1.25.3 → 1.26.0` (a safety gate: a
  ≤1.25 agent 400s on the unknown anchor value, so the client only sends it to a ≥1.26.0 unit).
  `argspec`/`ramp` untouched (drift guard intact). Tests: `tests/test_sequence_hold_enter.py` (validation,
  window split, lead offset, point + ramp placement from the pause, a hold-aware arm schedules it in
  window A) + `test_meta_endpoint.py` asserts the capability. Suite 485 → 491. Client side:
  `sdr-client/CLAUDE.md` "owner-testing round 3".

## Current state — `SequenceStep.anchor_own_edge` pass-through (a ramp tied by its END): COMPLETE (branch `claude/step-to-step-anchoring`, cross-repo)
Client authoring metadata for step anchors: `anchor_own_edge` ("start" default | "end") says which of
the STEP'S OWN edges the client ties to the target (only a ramp has two — an end-tied ramp's END sits
at the target edge and the ramp runs backward from it). The runtime NEVER reads it: `offset_s` is
always the step's START offset from the target edge (for an end tie the client sends end offset −
duration), so `_resolve_steps`/`_resolve_ramp` are byte-identical. Added to `models.py` so the field
survives a store/reload round-trip (an older agent drops it; the client then reloads the ramp
start-tied at identical timing). `config.py` bumps `AGENT_VERSION 1.25.2 → 1.25.3` (pass-through
only, no capability). `argspec`/`ramp` untouched (drift guard intact). Tests:
`tests/test_sequence_step_anchor.py` (round-trip + default; an end-tied ramp fires exactly like a
start-tied one with the same `offset_s`). Suite 483 → 485. Client side: `sdr-client/CLAUDE.md`
"owner-testing round 2".

## Current state — a window-filling ("both") ramp holds its LAST level before off-air: COMPLETE (branch `claude/step-to-step-anchoring`, cross-repo)
Owner ask: a dual-anchor ("both") ramp that fills the on-air window reached its top level exactly AT
off-air (0 hold) — the top was only touched at the edge, never transmitted. Now it HOLDS its last level
one dwell before off-air, like a single-anchor / "stop" ramp. Fix in the **drift-guarded**
`ramp.resolve_ramp` window branch (mirrored byte-identically in `sdr-client/api/ramp.py`): the window is
divided by LEVELS, not intervals — `hold = D / N` (N = number of levels), so the last value fires at
`D − hold` and is held over `[D − hold, D]` (off-air). `place_ramp` is UNCHANGED (it already places the
last value at `offset_s + (N−1)·hold`); `duration_s` still equals the full window `D` (the ramp still
fills it), so `min_on_air_duration` and the client canvas geometry (`ramp_span` draws the bar across the
window) are unaffected — only the internal fire spacing changed. Every level now gets its dwell (the
first level is held at the start too, exactly like a single-anchor ramp). The `hold_s` sub-case honours
the requested dwell (`N = round(D/hold_s)` levels). `config.py` bumps `AGENT_VERSION 1.25.1 → 1.25.2`
(behaviour-only, no capability). Tests: `tests/test_ramp.py` (`test_dual_anchor_uses_window_for_duration`
now 30 levels/29 intervals; `test_place_both_holds_the_last_level_before_the_window_end` — last fires at
window−hold), `tests/test_sequence_ramp.py` (`test_both_anchor_ramp_fills_window` top at 50 held to 60;
`..._respects_insets` top at 45 held to 55). Verified live: a `0→9` steps=3 ramp across a 30 s window
reaches 9 at 22.5 s and holds it 7.5 s to off-air.

## Current state — a ramp's step-anchor END edge = after the final level's hold: COMPLETE (branch `claude/step-to-step-anchoring`, cross-repo)
Owner ask: when a step is anchored to a RAMP's `end`, the ramp's LAST level must be held its full dwell
before the ramp is "finished" — the dependent shouldn't fire the instant the top level is reached. Root
cause: **`_resolve_steps._record_edges`** recorded a step's end edge as `max(fire_at)`, which for a ramp
is its LAST tune FIRE — so a dependent on the ramp's end fired at the top level's fire with ZERO hold.
Fix (agent-only): for a `ramp` target with ≥2 fires, the end edge is now `last fire + one hold` (the
uniform fire spacing `ts[-1] − ts[-2]`), i.e. the ramp's full-duration end where the final level's hold
completes; a step anchored to the end fires after that hold. Every other step type (point/tune/run) is
byte-identical (end = its single fire), and a ramp's START edge is unchanged (first fire). `config.py`
bumps `AGENT_VERSION 1.25.0 → 1.25.1` (behaviour-only, NO new capability — part of the step-anchor
feature already gated at ≥ 1.24.0; the bump lets OTA push it). `place_ramp`/`ramp.py`/`argspec`
untouched (drift guard intact). Tests: `tests/test_sequence_step_anchor.py::
test_ramp_end_edge_is_after_the_final_levels_hold` (last fire + hold + offset). **Client** (`sdr-client`):
this REVERSES the earlier code-review "finding #1" (which had aligned the client's end edge DOWN to the
agent's last-fire) — the client keeps `_ramp_duration` (= `last fire + hold` = full duration) at its
three end-edge sites, so client + agent now agree at the ramp's full-duration end. Verified live: a step
anchored to a `0→9`, steps=3, duration 6 s ramp's end (hold 1.5 s, last fire 4.5 s) fires at 6.0 s.

## Current state — step anchors accept a NEGATIVE offset (fire before the referenced edge): COMPLETE (branch `claude/step-to-step-anchoring`, cross-repo)
Owner ask (drawn on a 4-step sketch): a step anchored to another step should be able to fire BEFORE its
target's edge, not only at/after it — exactly like a start/stop anchor's warm-up lead-in (which the owner
already uses to start a duration task a few seconds before on-air). The earlier Phase-1 rule forbade a
negative `offset_s` on a step anchor (the "ordering invariant"); the owner reversed that. Agent side:
- **`sequence_runner._validate_steps`** — the `offset_s < 0` rejection for a step anchor is REMOVED (the
  topological `_resolve_steps`/`_point_fire` already placed `edge + offset` for any sign — arithmetic;
  only validation blocked it). The graph must still be ACYCLIC (unchanged); `end > start` within a
  ramp/bar is still enforced by `resolve_ramp`/the duration checks. A negative-offset dependent resolves
  before its anchor and the whole run stays globally time-ordered.
- **`config.py`** capability **`sequence-step-anchor-negative`** + `AGENT_VERSION 1.24.0 → 1.25.0` (safety
  gate: a ≤1.24 agent 400s on a negative step offset, so the client only sends one to a ≥1.25.0 agent).
  `argspec`/`ramp` untouched (drift guard intact). Tests: `tests/test_sequence_step_anchor.py`
  (`test_negative_step_offset_is_accepted_and_fires_before_the_edge` — a step anchored to another's start
  at −3 s fires 3 s before it, still time-ordered; the old negative-reject parametrize case dropped),
  `test_meta_endpoint.py` asserts both step-anchor capabilities. Suite 480 (count unchanged — one reject
  case became an accept case). **Verified LIVE cross-repo** against the owner's 4-step layout (authored
  through the client's `items_to_steps`, resolved through the agent runtime): Step1@0:30, Step2 anchored
  to Step1 −0:30 → fires 0:00, Step3&4 anchored to Step2 +0:30 → fire 0:30 — matches the sketch exactly.
  Client side (`sdr-client`): the dialogs/canvas author a negative step offset, the canvas routes such a
  dependent entered from the RIGHT with a left-pointing arrow (and flips a two-sided pin's caption to the
  clear side), and the save/arm gate enforces `sequence-step-anchor-negative`.

## Current state — step-to-step anchoring Phase 1 (agent runtime): COMPLETE (branch `claude/step-to-step-anchoring`, agent side; client next)
Owner ask: anchor a step not only to on-air/off-air/hold but to ANOTHER step's edge — e.g. a ramp
after another ramp's end — so editing the first moves everything downstream (a dependency graph).
Decisions: full DAG (any step → any step's start/end + offset); PHASED (Phase 1 = tunes/ramps/tasks,
the Hold stays start-anchored; Phase 2 makes the Hold itself step-anchorable). Agent side (this):
- **`models.py`** `SequenceStep` gains `id` (stable, client-assigned), `anchor="step"`, `anchor_step_id`,
  `anchor_edge` ("start"|"end"); `offset_s` is the offset from that edge. Additive/back-compat.
- **`sequence_runner._resolve_steps`** now resolves in TWO passes: pass 1 = root anchors
  (start/stop/both/hold) exactly as before (byte-identical for any sequence with no step anchor); pass 2
  = step-anchored steps resolved TOPOLOGICALLY — a step fires once its target's edges `(first_fire,
  last_fire)` are known (target may be a root or an earlier step-anchored step, so chains resolve). A
  step-anchored ramp runs forward from the edge (`_resolve_ramp` gained a `base_at`, mirroring the
  hold case); a point step fires at `edge + offset`. No-progress remainder (unknown/cyclic target) is
  logged + dropped (validation catches it first).
- **`_validate_steps`** allows `anchor="step"`, requires a known `anchor_step_id` + valid `anchor_edge`,
  rejects self-anchor, CYCLES (walk the source→target graph), a step anchor in a Hold-bearing sequence
  (Phase 1), and — **ordering invariant (owner rule)** — a NEGATIVE `offset_s` on a step anchor (a
  dependent never precedes its target; the offset runs FORWARD from the referenced edge, so `offset >= 0`
  keeps a moved anchor from silently invalidating its dependents). `end > start` within a ramp/bar stays
  enforced by `resolve_ramp` / the duration checks. The client clamps drags to keep this true; the agent
  is the backstop for an API-/plan-authored sequence.
- **`config.py`** capability **`sequence-step-anchor`** + `AGENT_VERSION 1.23.1 → 1.24.0` (safety gate:
  an older agent can't resolve the new anchor). `place_ramp`/`ramp.py`/`argspec` untouched (drift guard
  intact). Tests: `tests/test_sequence_step_anchor.py` (point end/start/chain; a ramp's end edge = its
  last point; no-step-anchor byte-identical; validation: unknown target / self / bad edge / missing id /
  cycle / step+Hold / negative offset). Suite 469 → 480. **NEXT — Phase 1 client** (`sdr-client`): the step-editor anchor
  picker ("another step → its start/end + offset"), canvas geometry that positions a step-anchored item
  at its target's edge (so dragging the target moves dependents) + round-trip (`uid↔id`), cycle
  prevention, the `sequence-step-anchor` save/arm gate, and the temporal power walk ordered by resolved
  time.

## Current state — run-log export: "On-air offset [s]" column (signed Δt from T0): COMPLETE (branch `claude/export-onair-offset-column`, agent-only)
Owner ask: the spreadsheet export should carry a column right after Time saying how long BEFORE or AFTER
T0 (the on-air instant) each step fired — from the on-air anchor only, not stop/hold. Done agent-side in
**`agent/run_table.py`**: `_columns` now emits `_Col("On-air offset [s]", "t0_offset")` at index 1 (right
after Time); `build_task_table` gained an `on_air_at` kwarg and fills that cell per row with
`_offset_s(fired_actual, on_air_at)` — SIGNED seconds (negative before on-air, positive after), rounded
to ms via `_fnum(…, 3)`, blank when either timestamp is unparseable. Like Time, it's filled AFTER the
row-per-change dedupe (the placeholder `""` sits in the compared `body`, so the ever-varying offset can't
spuriously un-dedupe a no-op tune). `agent/sequence_runner.build_log_table` passes `on_air_at=run.on_air_at`.
**No client change** — the client's `_localize_table` special-cases only the leading Time column and passes
`cols[1:]`/`row[1:]` through, so after its Timezone/Date/Time expansion the offset lands right after Time
(verified: `Timezone,Date,Time,On-air offset [s],…`). `config.py` bumps `AGENT_VERSION 1.23.0 → 1.23.1`
(export-shape/behaviour only, NO capability — an older client just shows the extra column; the bump lets
OTA push it). `argspec`/`ramp` untouched (drift guard intact). Tests: `tests/test_run_table.py`
(signed seconds before/at/after T0; a no-op tune still drops its row; blank without a T0). Suite 467 → 469.

## Current state — the deployed script LIBRARY survives an agent update (+ negative-cache recovery): COMPLETE (branch `claude/scripts-survive-agent-update`, agent-only)
Owner report: after an agent OTA update the deployed library looked WIPED — a plan/sequence run
immediately after updating logged the raw one-line fallback and exported only the three time columns;
re-deploying the library from the client fixed it, EXCEPT if you RAN a plan before re-deploying, after
which re-deploying no longer fixed it (a restart was needed). Owner need: colleagues must be able to
just update, and the scripts/tasks/sequences/plans they already have keep working — no post-update
ritual. Two root causes, both fixed:
- **Bug A — scripts lived inside the swapped-out release.** `SCRIPTS_DIR` was `BASE_DIR/scripts`,
  i.e. INSIDE the OTA release dir that an update replaces (`BASE_DIR` is the `current` symlink). Every
  other part of the library (tasks/sequences/plans/calibration) already persists in `STATE_DIR`;
  scripts were the one part that didn't, so an update stranded them in the old release. **Fix:** scripts
  moved to the PERSISTENT `SCRIPTS_DIR = STATE_DIR/scripts` (`config.py`; `BUNDLED_SCRIPTS_DIR =
  BASE_DIR/scripts` is the release's shipped defaults, empty in the bundle by design). `main.py`
  `SCRIPTS_DIR = cfg.SCRIPTS_DIR` (reported via `/info` `scripts_dir`, so the client bakes new-task
  command paths there). New **`main._seed_scripts_dir()`** runs once at boot (in `lifespan`, before
  tasks load): a no-op when `SCRIPTS_DIR` already holds any `.py` (steady state / classic single-dir
  install where `SCRIPTS_DIR == BUNDLED_SCRIPTS_DIR`); otherwise it **MIGRATES the previous release's
  `scripts/`** into the persistent dir (via `Updater.previous_version()`/`release_dir`, falling back to
  any other release still carrying scripts when the `previous` marker is missing) — so a FIELD unit
  upgrading from a pre-persistent agent KEEPS its library on the very next update — else **SEEDS** the
  release's bundled defaults (fresh install). Best-effort, never raises. Because a relocated script no
  longer sits next to `paramkit`, the launch now prepends `BASE_DIR` to `PYTHONPATH`
  (`process_manager._ensure_paramkit_on_path`, wired into all three launch env-build sites) so
  `import paramkit` still resolves, and `_resolve_script_path` (launch) + `_read_script_source` (spec)
  fall back to searching `SCRIPTS_DIR` for a task still baked with the OLD release-local path (covers a
  pruned previous release). Deploy scripts: `provision_install.sh` + `migrate_layout.sh` now move a
  classic install's `scripts/` into `$SHARED/scripts` (was dropped on the classic→OTA switch);
  `run_local.sh` stages into the persistent `$STATE/scripts` to mirror the real layout. No client change
  (it already reads `scripts_dir` from `/info`).
- **Bug B — the argspec cache poisoned a transient miss.** `_script_spec` cached a `None` when a
  script was momentarily absent (e.g. wiped by Bug A) and `reload()` never dropped the cache, so a
  library re-deploy couldn't restore the run-log/export until a restart — the "run before deploy →
  deploy no longer fixes" report. **Fix:** `_script_spec` memoises ONLY a hit (never a miss, so the
  next read retries), and `reload()` clears `_script_specs` (and `_active_flags`) so a re-uploaded
  script is re-read fresh.
- **`config.py`** bumps `AGENT_VERSION 1.22.1 → 1.23.0` (behaviour only, NO capability — the client
  needs no new gate; the bump just lets OTA push the fix onto deployed units). `argspec`/`ramp`
  untouched (drift guard intact). Tests: `tests/test_scripts_persist.py` (seed/migrate/no-op/fallback/
  classic-install/broken-layout + `_ensure_paramkit_on_path` + the SCRIPTS_DIR resolve fallback);
  `tests/test_script_folders.py` (negative-cache recovery + reload drops the cache). Suite 456 → 466.
  **Verified LIVE end-to-end** against a synthetic OTA layout (stranded library in a previous release):
  on boot the agent migrated the library into the persistent dir, `/scripts` + `/scripts/<name>/params`
  resolved the full 8-param C/A surface + laws, the launch resolved the task both while the old release
  was kept AND after it was pruned (SCRIPTS_DIR fallback), and a real armed PRN sequence exported an
  11-column, 4-row `log-table` (base density + both law views + realized SDR gain/atten + live params +
  derived readouts + fixed PRN/freq) — the "only three time columns" symptom gone.
- **Field rollout note:** OTA-push 1.23.0; on each unit's first 1.23.0 boot the library migrates
  automatically (no re-deploy). Merged to `main` with the rest of the branch at 1.32.0 (owner
  go-ahead given); the client bundle must be rebuilt from the released agent (`deploy/build_bundle.sh`)
  + re-staged into `sdr-client/bundles/` for the "Update agent…"/"Provision unit" flows to ship it.

## Current state — co-timed steps: a power step fires before RF-on (no gate-open blip): COMPLETE (branch `claude/sdr-logging-export-wvrni4`, agent-only)
Owner report: a sequence launches a duration task with a fixed `--power` (RF off, muted pre-roll),
then AT T0 both turns RF ON and starts a power ramp (e.g. −90 → −50). For ~one fire the output flashed
the LAUNCH power (the ramp's top, −50) before the ramp's first point (−90) landed — a real hot burst
(the export showed a −50 row then a −90 row ~110 ms apart). Root cause: the RF-on tune and the ramp's
first point are BOTH at on-air offset 0 → identical `fire_at`; `_resolve_steps`/`_tick` sort by
`fire_at` with a STABLE sort, so the RF-on (earlier in the step list) fired first and the agent un-muted
at the stale standing `--power` (−50); the ramp's first point (−90) only landed on the next fire (the
tick awaits each fire, so ~110 ms later). Fix (agent-only, owner-requested rule): at the SAME fire
instant a step that SETS POWER fires BEFORE a step that turns the RF output gate ON. `_tick` now sorts
by `(fire_at, SequenceRunner._co_time_rank(step))` — power-setter 0, neutral 1, rf-on 2 — so the power
tune/ramp-point runs first (updating the intended level while still muted) and RF-on then opens the gate
at that level. `_co_time_rank` classifies via the task's RF gate (`ProcessManager._rf_gate`,
`paramkit.rf`): a tune/ramp point carrying `--power` in `params` or a launch carrying `-Power`/`--power`
= power (0); a step driving the gate dest / gate flag to an ON value = rf-on (2); an OFF pre-roll launch
is NOT rf-on. Never raises (a resolution problem → neutral). `config.py` bumps `AGENT_VERSION 1.22.0 →
1.22.1` (behaviour only, no capability — the bump lets OTA push it to units). Tests:
`tests/test_sequence_step_ordering.py` (power ranks 0 / rf-on 2 / neutral 1; a co-timed sort puts the
power point first; an rf-off launch isn't rf-on; a power launch ranks first). Suite 450 → 454. Verified
live: the user's start(−50, rf off)/tune(rf on)@0/ramp(−90→−50)@0 now opens the gate at −90 (first
RF-on row = the ramp start), no −50 flash. Operator note: no re-author needed — the tool no longer
requires matching the launch `--power` to the ramp's start.

## Current state — run-log / export spec resolves a subfolder-filed script (regression fix): COMPLETE (branch `claude/sdr-logging-export-wvrni4`, agent-only)
Owner report (after the RF-gate work): a PLAN/sequence run of a real signal logged every launch param
on ONE raw fallback line (`▶ start CA -Power -50 -Center-frequency 1575.42 …`), a calibrated `--power`
logged only its base value (no law quantities), and the spreadsheet export contained only the three
time columns. Root cause is NOT the RF work — it's a long-standing path mismatch the RF/export features
merely exposed: **`ProcessManager._script_spec` read the script at the task command's LITERAL `.py`
path**, while the LAUNCH (`_build_command` → `_resolve_script_path`) and the client's authoring
(`GET /scripts/{name}/params` → `_resolve_script`, a recursive basename search) both relocate a script
filed into an organizational subfolder by its basename. So a task whose script lives in a subfolder
(e.g. `<scripts>/PRN GPS/gps_ca…​.py`, referenced flat) LAUNCHES fine and the client shows the full power
card, but `_script_spec` gets `OSError → None` → `tune_log_context`/`build_log_table` fold with **no
spec** → `format_launch_step` gets empty values → the runner's one-line fallback, tune steps show base
only, and `run_table._columns` emits just `Time` (the client then localizes it to Timezone/Date/Time —
the "only three time columns"). The tell in the log was the headers `◈ CA • Rf` / `• Power`
(dest.capitalize(), NOT the metavar `RF`), proving `spec is None`.
Fix (agent-only): `_script_spec` now reads the source via a new static
`ProcessManager._read_script_source(script, working_dir)` — try the command path (absolute / relative to
working_dir), then fall back to locating the basename under its directory via the SAME
`_resolve_script_path` the launch uses. So the run-log/export spec is read from the exact file that
actually runs, and matches what the client authored against. Byte-identical for a script already at its
command path (the common/flat case). No version/capability bump (agent-rendered output only; behaviour
only IMPROVES for the previously-broken subfolder case). Tests: `tests/test_script_folders.py`
(`_read_script_source` finds a nested script → non-empty spec; flat + missing + relative-to-working_dir).
Suite 440 → 450 (also picks up the RF-gate tests). Verified live: the CA mock filed into `PRN GPS/`
with a flat command path exports 11 full columns + a grouped launch block (was 3 time columns + the raw
fallback line). Immediate operator note: it needs no re-deploy of scripts — restarting the fixed agent
is enough; the script can stay in its subfolder.

## Current state — spreadsheet run-log export (agent side: per-change table endpoint): COMPLETE (branch `claude/hold-step-phase-0-wwwxf7-lty0i5`, cross-repo; client side in `sdr-client`)
Owner ask: export a ran sequence/plan's log as a spreadsheet — one ROW PER STATE CHANGE (a tune that
changes nothing adds no row), every parameter in its own column, and (for multi-unit plans) one SHEET
PER UNIT; the run-row "Export log…" offers the last ≤10 runs to pick from; any run with fired steps is
exportable. The **agent** builds the per-run table (reusing the run-log math so the sheet matches the
log); the **client** turns each unit's tables into an .xlsx (see `sdr-client/CLAUDE.md`).
- **`agent/run_table.py`** (new, pure) — `build_task_table(task, steps, spec, artifact, realize,
  freq_hz)` reconstructs one duration task's time-series: walks the run's fired start/tune steps in
  order, carries the effective param state, and emits a row only when the resolved row differs from the
  previous (dedupe). Columns: Time · every power quantity (base measured + each law view, declared
  order) · realized **SDR gain [dB]** + **Attenuation [dB]** · each LIVE param (excl. power/gain) with
  any visible derived readout right after it · then the FIXED params (PRN, Frequency) as constant
  columns. `--power`/`--gain` never get their own column (covered by the quantities / SDR gain); an
  on/off param renders `0/1` under a "… on" header. Reuses `tune_log` for the quantity/derived fold.
- **`agent/process_manager.py`** — `power_realizer(task)` → a closure `power → {sdr_gain_db, atten_db}`
  (the calibration resolved ONCE, reused per row) via `resolve_from_files(...).realize`.
- **`agent/sequence_runner.py`** — `build_log_table(run_id)` → `{run_id, sequence_name, state,
  on_air_at, tables:[…]}` (one table per duration task; `tune_log_context` + `power_realizer` per task).
- **`agent/main.py`** — `GET /sequence-runs/{id}/log-table` (404 unknown).
- **`config.py`** — capability **`sequence-log-table`** + `AGENT_VERSION 1.20.0 → 1.21.0` (the client
  gates its "Export log…" button on the capability). `argspec`/`ramp` untouched (drift guard intact).
  Tests: `tests/test_run_table.py` (quantity + realized + fixed columns; row-per-change drops a no-op
  tune; RF 0/1; enbw-tracked full power; uncalibrated → no quantity/realized cols) + `test_meta_endpoint`
  asserts the capability. Suite 435 → 438. Endpoint validated live end-to-end.

## Current state — sequence run log: calibrated tune AND start/run steps show every power quantity (grouped, atomic): COMPLETE (branch `claude/hold-step-phase-0-wwwxf7-lty0i5`, agent-only)
Owner ask: a TUNE step that changes a calibrated `--power` logged only the raw base wire value (a
spectral density in dBm/Hz), and a START step dumped the raw `--flag value` command line — both
unreadable in the quantities the operator thinks in. Now:
- a **TUNE** step renders one grouped block PER changed parameter (`◈ task • ParamName`); a calibrated
  `--power` lists the base measured quantity AND every declared power-law view; any other parameter
  shows its value plus any VISIBLE derived readout that tracks it.
- a **START/RUN** step renders ONE header (`▶ start task` / `⚡ run task`) then a labelled row per
  launch parameter, in command order — a calibrated `--power` expands to all its quantities and a
  visible derived field is listed directly under the parameter it tracks (no `--flag value` dump).
```
[07:55:36] ▶ start mock_prn                     [07:55:39] ◈ mock_prn    •    Power
            1         • PRN                                  -99.6548 dBm/Hz  • Spectral density
      1575.42 MHz     • Center frequency                    -39.6299 dBm     • Full signal power (…)
            5         • Sidelobes                                 -40 dBm     • Main-lobe integrated power
       12.276 MHz     • Passband bandwidth
    -104.6548 dBm/Hz  • Spectral density
     -44.6299 dBm     • Full signal power (filter passband)
          -45 dBm     • Main-lobe integrated power
           on         • RF
```
Agent-only (the run log is server-rendered text the client just displays). Pieces:
- **`agent/tune_log.py`** (new, pure/dependency-light) — `format_tune_step(...)` (per-changed-param
  blocks) and `format_launch_step(task, values, spec, artifact, clock, glyph)` (one block, all launch
  params). Fold uses `paramkit.power_law` for the law views and a small ported `eval_formula`
  (linear/table/…) for derived fields — incl. the HIDDEN bridge (`enbw_mhz`) the full-power law keys
  on. Base unit is the artifact's `operating_unit`; each law's own `unit`/`name`; labels come from a
  param's capitalised metavar flag (`-Center-frequency` → "Center frequency").
- **`agent/sequence_log.py`** — `RunLog.emit_block(text)` writes the whole block in ONE append (+
  `clock()`), so two steps firing the same instant never interleave line-by-line.
- **`agent/sequence_runner.py`** `_fire_step` — tune → `_tune_block` (+ `_effective_params`, walking
  the run's prior start/tune steps to carry bridge params a power-only tune didn't set); start/run →
  `_launch_block` (+ `_launch_params`, base command args merged with the step's, `replace_args`
  honoured). ANY failure falls back to the old one-line annotation (the log never breaks a run).
- **`agent/process_manager.py`** — `tune_log_context(task)` → (cached argspec, resolved public
  artifact); `_script_spec` caches `extract_params` per script.
- **`config.py`** — `AGENT_VERSION 1.19.0 → 1.20.0` (no capability — the client parses nothing; older
  agents just show the old one-line format). `place_ramp`/`argspec`/`ramp` untouched (drift guard
  intact). Tests: `tests/test_sequence_tune_log.py` (every quantity; derived readout; one grouped
  block per tune step; the start block lists every param in order with `--power` expanded; uncalibrated
  fallback; two blocks don't interleave). Suite 427 → 435.

## Current state — provisioning: pip `--ignore-installed` past apt-managed transitive deps (Python 3.13): COMPLETE (branch `claude/provision-python313-fix`)
Provisioning a fresh **Python 3.13** Pi aborted at the pip step: `typing_extensions` is an
**apt/dpkg** package there (`/usr/lib/python3/dist-packages`, no `RECORD` file). The pinned stack
(`fastapi`/`pydantic`/…) resolves a `typing_extensions` **NEWER** than the apt-shipped `4.13.2`, so
pip tries to **uninstall the apt copy first** — impossible → `no RECORD file was found … installed by
debian`, aborting the whole provision. **First fix (WRONG, superseded): dropping `--upgrade`.** It
didn't work — the resolver picks the newer version because a requirement *needs* it, not because of
an upgrade flag, so plain `pip install -r requirements.txt` still tried the impossible uninstall (the
owner confirmed the identical error persisted, both via the client and running pip directly on the
Pi). **Correct fix (deploy scripts only, no agent code/version change): add `--ignore-installed`** to
the online fallback in `deploy/provision_install.sh` (and to the classic `install.sh`). pip then
installs every requirement + its transitive deps FRESH into `/usr/local/lib/python3.x/dist-packages`
WITHOUT trying to uninstall anything — the pip copies precede `dist-packages` on `sys.path`, so they
shadow the apt `typing_extensions`/`PyYAML`; `psutil` stays apt-only (not in `requirements.txt`, so
pip never touches it) and the `==` pins are still enforced. The offline pass (`--no-index`) runs
first and is unaffected — with no candidates it never tries to uninstall, and on a fresh
internet-connected Pi it fails fast to the online fallback (the bundle ships no wheelhouse — by
design). Immediate Pi-side unblock for a unit stuck now: `sudo pip3 install --break-system-packages
--ignore-installed -r /opt/sdr-agent/requirements.txt`. The client bundle (`sdr-agent-<ver>.tar.gz`,
gitignored build artifact from `deploy/build_bundle.sh`) must be rebuilt + re-staged into
`sdr-client/bundles/` for the client's "Provision unit" flow to ship the fixed script.

## Current state — Hold step runnable in PLANS (single-unit, operator-present): agent test-only
Client-side scope expansion (see `sdr-client/CLAUDE.md`): a single-unit, operator-present PLAN armed
directly now honours the Hold (arms the unit `hold_aware` with an inline plan-local step copy + a
`plan_id`), while multi-unit plans and the unattended schedule still compile the Hold out. The agent
already supports this — `arm` accepts ANY `hold_aware` arm (it never keys the decision on `plan_id` or
inline `steps`; it refuses only a Hold armed WITHOUT `hold_aware`, §5.2/§7) and stamps `plan_id` onto the
run — so there is **no agent code change**, only a regression test pinning the contract the client now
relies on: `tests/test_sequence_hold_runtime.py::test_hold_aware_arm_with_inline_steps_and_plan_id_parks`
(a hold-aware arm with inline Hold-bearing steps + a plan_id, over a Hold-free STORED sequence, parks at
the hold, carries the plan_id, and proceeds). Suite 426 → 427.

## Current state — Hold-step client authoring fixes (window-B ramps + duration tasks): agent test-only
Client-side Hold UI fixes (see `sdr-client/CLAUDE.md`) let an operator anchor a RAMP (the down-ramp)
AND a DURATION task's START to the Hold — both are window-B, `anchor="hold"`. The agent already
resolves these (a hold-anchored ramp via `_resolve_ramp(hold_at=…)`, and a `START(anchor="hold")` like
any window-B step through `_split_hold_windows` → `proceed`), so there is **no agent code change** — only
a regression test pinning the contract: `tests/test_sequence_hold_model.py::
test_split_hold_windows_routes_window_b_start_and_ramp` (a window-B START + ramp land in window B, the
on-air START stays in window A). Suite 425 → 426.

## Current state — Hold step Phase 3c (edit-while-holding): COMPLETE (branch `claude/hold-step-phase-0-wwwxf7`, cross-repo)
Design §6.4. `SequenceRunner.proceed` now honours `ProceedRequest.steps`: when the operator edits the
post-hold window while holding and sends the **full edited sequence**, `proceed` runs `_validate_steps`
on it and re-extracts window B via `_split_hold_windows` (window A has already fired and is ignored),
instead of the window B stored at arm. Absent `req.steps`, behaviour is byte-identical (stored window
B). One-line change in `proceed` (the `wb_defs` source). `config.py` bumps `AGENT_VERSION 1.18.0 →
1.19.0` and adds capability **`sequence-hold-edit`** (a ≤1.18 agent silently ignores `req.steps` and
runs the stored window B, so the client gates its window-B edit UI on this string — else an edit would
be lost). Tests: `tests/test_sequence_hold_runtime.py::test_proceed_honours_edited_window_b_steps`
(arm→hold→proceed with an edited window-B tune 21→33; the resumed run fires the EDITED target) +
`test_meta_endpoint.py` asserts the capability; suite 424 → 425. Client side (Phase 3c, `sdr-client`):
`ui/hold_edit_dialog.py::HoldEditDialog` (hosts the timeline editor on the running sequence, OK returns
the edited full step list), an **"Edit…"** row button on a HOLDING run gated on `sequence-hold-edit`
(`ui/sequences_panel.py`; the edit is held per-run in `_wb_edits` and sent as `ProceedRequest.steps` on
the next Proceed, cleared once the run leaves HOLDING), and `SEQUENCE_HOLD_EDIT_CAPABILITY`. **The Hold
step is now feature-complete** (Phases 0–3). `place_ramp`/`ramp.py` untouched (drift guard intact).

## Current state — Hold step Phase 3b (Fast-Forward-to-Hold): COMPLETE (branch `claude/hold-step-phase-0-wwwxf7`, cross-repo)
Design §5.4. `POST /sequence-runs/{id}/hold-now` → `SequenceRunner.hold_now(run_id)` jumps a RUNNING
hold-aware run straight to its Hold NOW, without waiting out the rest of window A — for the real
workflow (loss-of-lock happens far below the estimated ramp top, so the remaining run-up is pure
waste). Requires `run.state == RUNNING`, `hold_aware`, a pending Hold (`hold_at_offset_s` set,
`held_actual` None); else `ValueError` → **409**. It marks every un-fired `run.steps` entry
`fired_actual = "skipped"` (a sentinel — `fired_actual` is only ever tested `is (not) None`, never
parsed, so this is safe; the up-ramp simply stops emitting further TUNE points and the task holds its
CURRENT live value), transitions `RUNNING → HOLDING`, stamps `held_actual = now` (so the `max_hold_s`
deadman runs from here), and emits `sequence_hold` (annotated "fast-forward"). From there `proceed`,
the deadman, abort and restart-abort all behave exactly as for a run that reached its hold on its own.
`agent/main.py` adds the endpoint (404 unknown / 409 wrong-state); `config.py` bumps
`AGENT_VERSION 1.17.0 → 1.18.0` and adds capability **`sequence-hold-now`** (the client gates its
"Hold now" button on it). Tests: `tests/test_sequence_hold_runtime.py`
(`test_hold_now_fast_forwards_to_the_hold` — fast-forward mid-run-up skips the remaining window-A
tune, holds the current value, then proceeds to window B; `test_hold_now_requires_a_running_hold_aware_run`
— refuses a Hold-free and an already-holding run) + `test_meta_endpoint.py` asserts the capability;
suite 422 → 424. Client side (Phase 3b, `sdr-client`): `api/client.py::hold_now_sequence_run` +
`SEQUENCE_HOLD_NOW_CAPABILITY` + a "Hold now" row button in `ui/sequences_panel.py` (shown only on a
RUNNING hold-aware not-yet-held run when the agent advertises the capability). `place_ramp`/`ramp.py`
untouched (drift guard intact). **NEXT — Phase 3c**: edit-while-holding (the agent's `proceed`
honours `ProceedRequest.steps` + a client window-B edit flow, §6.4).

## Current state — Hold step Phase 1 (HOLDING runtime): COMPLETE (branch `claude/hold-step-phase-0-wwwxf7`, agent-only)
Design doc lives in the client repo: **`sdr-client/docs/sequence-hold-step.md`** (cross-repo spec +
owner decisions; Appendix A is the Phase 0 checklist, §5/§13 the runtime). A **Hold** sequence step
pauses a running sequence at the hold, holding state exactly, until the operator proceeds (the GNSS
loss-of-lock/reacquire test with an unknown 2–10 min receiver-restart wait). The Hold is a **third
anchor** (`anchor="hold"`) splitting a run into window A (fixed at arm) and window B (resolved only at
**proceed**, relative to the resume instant); v1 is **single-unit, operator-present (Library) only,
and a no-op in the schedule**. **Phase 0** shipped the data model + validation vocabulary
(`StepAction.HOLD`, `SequenceState.HOLDING`, `anchor="hold"`, the `SequenceRun`/`ArmSequenceRequest`
Hold fields, `ProceedRequest`, `_validate_steps` rules, `sequence-hold` capability @ `1.16.0`).
**Phase 1** replaces the temporary Phase-0 arm guard with the real HOLDING runtime — `agent/`:
- **`sequence_runner.py`** — a hold-aware `arm` (`req.hold_aware` + a HOLD present) resolves **only
  window A** (start-anchored work, via `_split_hold_windows`), arms the run **open-ended** (no
  scheduled off-air), and stores `hold_at_offset_s` + the deferred window-B defs on the run. A
  Hold-bearing arm that is **not** `hold_aware` is refused (the scheduled path compiles the Hold out
  client-side — §7). `_service_holds` in `_tick` transitions `RUNNING → HOLDING` once window A has
  fired and `T0 + hold_at_offset_s` is reached (emits `sequence_hold`, RF holds its last value), and
  enforces the **`max_hold_s` deadman** (auto-abort + `sequence_hold_timeout`; 0 = unlimited).
  **`proceed(run_id, ProceedRequest)`** resolves window B from `T_resume` (`_resolve_steps`/
  `_resolve_ramp` gained a `hold_at` base; a hold-anchored ramp reuses `place_ramp`'s forward layout
  rebased to `T_resume`), sets `on_air_end = T_resume + window-B content`, appends the fires, and
  returns `HOLDING → RUNNING` (emits `sequence_proceed`). A HOLDING run is abortable
  (`cancel_or_abort`), abort-on-restart (`_reconcile_on_startup`), and counts as active for
  delete/overlap/panic (new `_ACTIVE_STATES`). `place_ramp`/`ramp.py` were **not** touched (drift
  guard intact) — the hold ramp reuses the start layout.
- **`main.py`** — `POST /sequence-runs/{id}/proceed` (409 if not holding).
- **`models.py`** — `SequenceRun.window_b_steps` (deferred window-B defs); `SequenceWebhook.type`
  documents the new event kinds.
- **`config.py`** — `AGENT_VERSION` `1.16.0 → 1.17.0` (runtime behind the same `sequence-hold`
  capability; the bump lets OTA push the working runtime to 1.16.0 units).
Tests: `tests/test_sequence_hold_runtime.py` (park-then-proceed, a window-B down-ramp, the deadman,
abort-while-holding, proceed-requires-holding, restart-abort, and a Hold-free run straight through);
`test_sequence_hold_model.py` updated (the Phase-0 guard became the non-hold-aware arm gate). Suite
398 → 422. **NEXT — Phase 2** (client): the third-anchor canvas + step-editor Hold anchor, the
arm-dialog messaging + Proceed button (reusing `ArmDialog`), the scheduled-path collapse-the-Hold
no-op, an `api/client.py` `proceed` wrapper, and wiring the `sequence-hold` save/arm gate. Design §6–§7.

## Current state — attenuator engagement no longer caps the minimum power: COMPLETE (branch `claude/table-and-ramp-fixes`, cross-repo)
Bug: a signal's minimum achievable power tracked a programmable attenuator's `engage_pct` (lower
engagement → lower min). The engagement % should decide only WHEN the attenuator engages, not the
absolute floor. Root cause in `paramkit/achievable.py` (imported by `agent/calibration.py` resolver
AND `paramkit/calkit.py` transmit fold; mirrored verbatim to `sdr-client/state/achievable.py`):
`AchievableGrid._gain_points` clamped the SDR gain floor to the engagement-threshold gain `_g_thr`,
and `realize`/bounds clamped the target to `_thr − _span`, so the floor was `P_base(_g_thr) −
max_atten` rather than `P_base(min_gain) − max_atten`. Fix: the achievable SET spans the whole SDR
grid to min gain (`_gain_points` floors at `_lo_g`; `realize` clamps at `_s_lo − _span`); the
threshold only steers `realize`'s (gain, reduction) choice — ABOVE it least-reduction (SDR-first,
attenuator at rest), and once the attenuator is MAXED the SDR drops below the threshold (most-
reduction) to extend the low end. So the floor is `SDR@min_gain + attenuator@max` for every
`engage_pct`, and the engaged region still holds the SDR at the threshold. `paramkit/achievable.py`
and `sdr-client/state/achievable.py` kept byte-identical (manual mirror). No new capability, but
`AGENT_VERSION` is bumped to `1.15.1` so the OTA/"Update agent…" flow pushes the corrected resolver
onto already-deployed units (the resolved `min_power_dbm` just becomes correct). Tests:
`tests/test_calibration_active.py`, `tests/test_achievable_grid.py` (min independent of engage_pct;
SDR drops below the threshold once the attenuator is maxed); client `tests/test_power_fold.py`.

## Current state — opt-in measured-curve extrapolation: COMPLETE (branch `claude/calibration-extrapolate`)
A signal's measured curve may set `extrapolate: down|up|both` (default `none`) on its curve entry
(`signals.<id>.curves.<plane>.extrapolate`) to continue the end-segment slope past the measured gain
endpoints, instead of clamping flat — so `--power` can reach a gain that wasn't measured (motivating
case: a clean high-gain measurement extrapolated DOWN, because low gain sits in the analyzer's noise
floor). The commanded gain is still clamped to `[min_gain, ceiling]`, so it extends the *curve*, never
the gain limits. Implemented as a per-curve `_Measured.extrapolate` + a new `_interp_extrap` used by
`power_at` and (since powers are strictly increasing, the same call inverts) `_gain_for_power_on`;
`_interp` stays clamped for frequency/bias tables. Published TOP-LEVEL in the artifact (`extrapolate`)
so it reaches a flat v1 chain too; **`paramkit/calkit.py`** (transmit fold) and **`sdr-client`
`state/power_fold.py`** mirror it via their own `_interp_ex` on the operating anchor, so the range the
operator sees is the range the unit delivers. Gated on capability `calibration-extrapolate`
(`AGENT_VERSION` → `1.15.0`, a safety gate: an older agent clamps → client range wouldn't match). The
client picker is a per-signal dropdown on the measured-points dialog (`ui/calibration_panel.py`;
`_doc_uses_extrapolate`/`_blocks_on_extrapolate`). Byte-for-byte a no-op when every curve is `none`.
Tests: `tests/test_calibration_extrapolate.py`, `tests/test_calkit_extrapolate.py` (agent);
`tests/test_power_fold_extrapolate.py`, `tests/test_calibration_extrapolate.py` (client). Docs:
`docs/calibration.md` §7.5.

## Current state — `provides` derived stand-in (paramkit + argspec): COMPLETE
`paramkit.Param`/`.derived()` gained `provides="<dest>"` — a derived field that stands in for a
parameter a calibration power law keys on when THAT parameter's field is hidden by a mode (the
bandwidth analogue of `is_freq`; e.g. a start/stop sweep span provides `bw` while `--bw` is
hidden). `agent/argspec.py` extracts it (mirrored byte-identically in `sdr-client/api/argspec.py`
— drift guard). No behaviour change (the transmit fold already used the resolved span; the client
honors `provides` for its display fold), but `AGENT_VERSION` is bumped to **1.13.1** so the
OTA/"Update agent…" flow installs the new argspec on units (the client reads `provides` from the
unit's `/scripts/{name}/params`). No new capability — `provides` is backward/forward-compatible
param metadata. Tests: `tests/test_paramkit.py`, `tests/test_argspec_paramkit.py`. Script:
`sdr-scripts` `fm_chirp_tx.py`.

## Current state — per-signal + source-bias measurement de-embed: COMPLETE
Extends the plane-level measurement de-embed (§14) to two per-measurement placements so each
measurement carries its own bench cable. `signals.<id>.curves.<plane>.measurement_deembed` (a
component id or inline table) is preferred over the plane-level default and removed as a CONSTANT at
the signal's `center_freq_hz` (a per-signal power curve is a gain sweep at one frequency) — so a
signal measured later through a different/re-characterized cable is corrected independently while the
others keep theirs (`_build_planes`, line ~1533). `source_bias.measurement_deembed` removes the
flatness-sweep cable FREQUENCY-BY-FREQUENCY (`bias(f) −= L(f)` before the rep-frequency normalization,
in `resolve()` right after the `source_bias` parse) — a constant-loss cable cancels in the
normalization, so only a frequency-dependent bias cable reshapes the flatness. A third placement: an
OWN (separate-measurement) reading — `signals.<id>.limiting`/`reported` `{kind:own,curve:…}` — carries
its own `measurement_deembed`, de-embedded INDEPENDENTLY of the primary: the own curve shares the
node's `offset_db` (which already has the primary de-embed, captured as `_Measured.deembed_applied`),
so `resolve()` shifts the own powers by `(primary − own)` at the signal freq — no own cable inherits
the primary, the same cable equals inheriting, a different cable overrides; only the reading it backs
(the ceiling) moves, never the `--power` axis. All reuse `_deembed_table` (now context-generalized).
Gated on `calibration-deembed-per-signal`, `AGENT_VERSION`
`1.14.0` (a safety gate — a ≤1.13.x agent would leave the loss baked in). Byte-identical when neither
new field is present. Tests: `tests/test_calibration_deembed_per_signal.py`; docs/calibration-v2 §14.1.

## Prior state — stage limits gauged through the limiting reading: COMPLETE
Latest work: a STAGE safety limit (`chain.limits`) is now inverted **through the operating node's
LIMITING reading**, not directly against the measured curve — so one dBm ceiling caps every signal
whatever quantity it is measured in. A constant limiting delta bakes into `gain_ceiling_db`
(`C − Δlim`); a parameter-keyed limiting law is published as a `freq_dependent_limits` entry with
`via_limiting: true`, which `calkit`/`power_fold` re-fold at the live task parameter (the same
re-fold the `limiting.max_dbm` cap already gets). Fixes an over-power footgun (before, a dBm stage
ceiling was compared against a density/main-lobe measurement). Motivating case: GPS C/A `--power`
in main-lobe power, amp limit in total-in-band power, offset keyed on the sidelobe count. Gated on
capability `calibration-limit-through-reading` (agent `1.13.0`, a safety gate). Byte-identical when
no measurement/limiting bridge is in play. Tests: `tests/test_calibration_limit_reading.py`,
`tests/test_calkit_bridges.py`; docs/calibration-v2.md §13.5.

## Prior state — per-signal measurement quantity/unit: COMPLETE
`resolve()` reads `signals.<id>.measurement = {quantity, unit}` and publishes it as the artifact's
operating quantity/unit (`ResolvedCalibration.public_quantity`/`public_unit`); the unit family
validates the reading bridges (a density feeds density→dBm laws; a "same as measurement" limiting
is refused for a density; a limiting law must return dBm). Gated behind capability
`calibration-measurement-quantity` (agent `1.12.0`). Tests:
`tests/test_calibration_measurement.py`, `tests/test_calibration_bridges.py`. Full redesign
record lives in `sdr-client/docs/calibration-ui-redesign.md`.

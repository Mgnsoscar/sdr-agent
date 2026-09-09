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
  in `config.py` (`1.19.0`: edit-while-holding — Phase 3c — `POST …/proceed` honours
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
  automatically (no re-deploy). NOT YET merged to `main` / pushed to field — awaiting owner go-ahead
  (field-critical); the client bundle must be rebuilt from 1.23.0 (`deploy/build_bundle.sh`) + re-staged
  into `sdr-client/bundles/` for the "Update agent…"/"Provision unit" flows to ship it.

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

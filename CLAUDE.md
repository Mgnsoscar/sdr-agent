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

## Cross-repo invariants (do not break)
- **Drift guard (`tests/test_shared_source_drift.py`):** `agent/argspec.py` and `agent/ramp.py`
  MUST stay **byte-identical** to `sdr-client/api/argspec.py` and `sdr-client/api/ramp.py`. The
  test finds a sibling `sdr-client/` checkout; if you touch one side, mirror it.
- **Power-law mirror (manual):** `paramkit/power_law.py` is copied verbatim to
  `sdr-client/state/power_law.py` (pure stdlib for Python/JS parity). Keep them in step.
- **Capabilities + version:** a new client-visible feature adds a string to
  `AGENT_CAPABILITIES` and bumps `AGENT_VERSION` (both in `agent/config.py`); `test_meta_endpoint.py`
  asserts the capability set. The client feature-gates on these exact strings. Current version is
  in `config.py` (`1.18.0`: Fast-Forward-to-Hold — Phase 3b — `POST …/hold-now` behind the new
  `sequence-hold-now` capability; `1.17.0` shipped the Hold-step HOLDING runtime — Phase 1 — behind
  `sequence-hold` (added 1.16.0): a hold-aware arm parks at the hold and `POST …/proceed` resolves
  the post-hold window).

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

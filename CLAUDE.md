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
  in `config.py` (bumped to `1.15.0` for opt-in measured-curve extrapolation).

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

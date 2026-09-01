# Per-unit power calibration v2 — a shared component catalog + frequency-aware passive planes

**Status:** design / spec. For review. No code yet.
**Builds on:** `docs/calibration.md` (v1). Everything there still holds; this document
describes an additive evolution. **v1 documents remain valid and resolve unchanged.**

**Scope:** make the passive stages of the RF chain (cable, antenna, pads)
frequency-dependent and reusable, so that:

1. a cable/antenna is **characterized once** (a loss/gain-vs-frequency table, exactly
   what a VNA sweep produces) and reused across every unit that uses that part;
2. in the field, wiring a unit is **"pick which cable/antenna you plugged in"**, not
   retyping loss numbers into 30 signals × 6 units;
3. the delivered/EIRP power is correct at whatever frequency a signal is actually
   transmitted at — including signals (a chirp, a CW test tone) that are sent at
   several different frequencies.

---

## 1. What v1 can't express

In v1 a cable or antenna is an inline `derived` plane with a single constant
`delta_db`, stated **per unit**, inside `chain.planes`:

```jsonc
"cable_output": { "type": "derived", "from": "amplifier_output", "delta_db": -1.8 }
```

Three problems follow from that one modelling choice:

- **It's frequency-blind.** A 3 m cable's loss at L5 (1176 MHz) is not its loss at L1
  (1575 MHz); a patch antenna's gain differs across bands. One constant can't be right
  for both.
- **A signal is not a fixed frequency.** A chirp or a CW tone is transmitted at many
  frequencies, so the "which frequency do I evaluate the cable at" question can only be
  answered at **transmit time**, not baked per signal.
- **It doesn't scale or match the workflow.** The number belongs to a *cable*, but it
  lives in a *unit's* document, restated for every unit and (if we went per-signal)
  every signal. A field re-measurement would be 30 signals × 6 units of hand edits. The
  real-world action is "we hooked unit 3 up with cable #7" — a single choice.

v2 fixes all three by (a) moving passive parts into a **shared component catalog** and
(b) evaluating their frequency tables at the **runtime transmit frequency**.

---

## 2. The two ideas, and what does *not* change

- **Component catalog (fleet-level).** Cables, antennas, and pads become entries in a
  shared file — characterized once, deployed to every unit like `tasks.yaml` and
  `calibration_defaults.yaml` already are. Each entry carries a `delta_db`-vs-frequency
  table.
- **Passive planes reference a component.** A `derived` plane stops carrying an inline
  constant and instead names a catalog component (or keeps an inline scalar — see §7).
  Its dB is looked up from the component's table at the operating frequency.

**Deliberately unchanged — this keeps the change small and the safety story intact:**

- **Measured planes stay exactly as in v1.** `sdr_output` and `amplifier_output` are
  nonlinear and genuinely unit-specific; you measure them per unit × signal on a power
  meter, not on a VNA. They are **not** frequency tables and are **not** in the catalog.
  (Confirmed scope: only passive components get frequency awareness.)
- **The plane graph, inversion, and clamping are unchanged.** The resolver still walks
  `derived` hops down to a measured anchor and inverts once (v1 §7.3). v2 only changes
  *where each hop's dB comes from* (a table lookup at frequency `f` instead of a
  constant).
- **The `measured` vs `derived` split is unchanged.** "Derived" now means "a passive
  hop, possibly a catalog component"; "measured" still means "a curve measured on this
  box".

---

## 3. The component catalog (fleet file)

A new shared file, deployed beside `tasks.yaml` / `calibration_defaults.yaml`
(working name `components.yaml`; JSON or YAML). Components are **hardware, reusable
across unit types**, so the catalog is flat (not keyed by unit type).

```jsonc
{
  "schema_version": 1,
  "components": {
    "cable_lmr240_3m_a": {
      "kind": "cable",                 // cable | antenna | pad  (label only; UI grouping)
      "description": "3 m LMR-240, connector set A",
      // delta_db as a function of frequency. Negative = loss, positive = gain — same
      // sign convention as v1's scalar delta_db. Strictly increasing in freq_hz.
      "delta_db_by_freq": [
        [1.100e9, -2.30],
        [1.400e9, -2.62],
        [1.600e9, -2.81]
      ]
    },
    "patch_a": {
      "kind": "antenna",
      "description": "6 dBi patch, s/n 014",
      "delta_db_by_freq": [
        [1.170e9, 5.1],
        [1.575e9, 6.0]
      ]
    },
    "pad_3db": {
      "kind": "pad",
      "description": "3 dB SMA attenuator",
      "delta_db_by_freq": [[0, -3.0]]   // one point ⇒ frequency-independent constant
    }
  }
}
```

Rules:

- `delta_db_by_freq` is `[[freq_hz, delta_db], …]`, **≥ 1 point**, strictly increasing
  in `freq_hz`. One point ⇒ a constant hop at all frequencies (how a truly flat pad or a
  v1-style number is expressed). Evaluated by **linear interpolation with endpoint
  clamping** — the same `_interp` the measured curves already use; a frequency outside
  the table's span clamps to the nearest end (and the resolver warns; see §6).
- `kind` is a free label for the editor (group cables vs antennas), never interpreted by
  the math — mirrors how `quantity` is "human context only" in v1.
- The catalog holds **only passive/linear parts**. There is no amplifier or SDR entry;
  those are measured planes on the unit.

The catalog is deployed to units through the **existing Library/deploy path** (the same
plumbing that pushes tasks/scripts fleet-wide), so "characterize once, use everywhere"
is literally one deploy.

---

## 4. Per-unit chain: reference a component instead of a constant

A `derived` plane names a component from the catalog:

```jsonc
"chain": {
  "operating_plane": "antenna_eirp",
  "planes": {
    "sdr_output":       { "type": "measured", "quantity": "total in-band power" },
    "amplifier_output": { "type": "measured", "quantity": "main-lobe power" },
    "cable_output":     { "type": "derived", "from": "amplifier_output", "component": "cable_lmr240_3m_a" },
    "antenna_eirp":     { "type": "derived", "from": "cable_output", "component": "patch_a", "quantity": "EIRP" }
  }
}
```

**Wiring a unit in the field = change `component` on the cable/antenna plane** — one
choice per part, picked from the catalog. Nothing else in the unit's document moves.

**Backward compatibility.** A `derived` plane may still carry an inline `delta_db`
(scalar, frequency-independent) exactly as in v1. Precisely one of `component` /
`delta_db` is present. So:

- every existing v1 document resolves unchanged;
- a brand-new pad you haven't catalogued can still be a quick inline constant;
- migrating a unit to the catalog is swapping `"delta_db": -1.8` for
  `"component": "cable_…"`.

---

## 5. Runtime frequency — where it comes from, where it's applied

Because the transmit frequency is a **runtime** fact, v2 moves the passive fold out of
the agent's one-shot resolve and into the script's `PowerMap`, so it stays correct when
the operator **live-tunes** the frequency or sweeps a chirp.

### 5.1 The script declares its frequency parameter

A transmit script already declares `CAL_SIGNAL_ID`. It now **also** declares which of
its own parameters carries the transmit centre frequency — e.g.:

```python
CAL_SIGNAL_ID  = "gps_l1_mcode"
CAL_FREQ_PARAM = "freq"        # the dest/flag of this script's centre-frequency arg (Hz)
```

- The agent reads `CAL_FREQ_PARAM` from the script's `--describe-params` (the same
  channel it already reads parameter metadata over), and reads the task's configured
  value of that parameter. That gives the agent a **representative** frequency for the
  UI (`--power` bounds "at f") and for validation.
- The **authoritative** evaluation is script-side: the script knows the frequency it is
  tuned to right now (it owns `CAL_FREQ_PARAM`), and hands it to `PowerMap` whenever it
  converts `--power ↔ gain` — including after a live retune. No stale artifact.

This directly answers the requirement: *there is a way for the script to tell the agent
which parameter is the frequency.*

### 5.2 Artifact v2 (what the agent injects into a task)

The injected artifact keeps the v1 fields (so existing scripts still work) and, when
the operating plane sits behind passive hops, ALSO carries what a frequency-aware
`PowerMap` needs to fold at any frequency. It stays `schema_version: 1` and additive —
a v2 consumer detects the new shape by the presence of `anchor_curve` / `passive_hops`,
so there is no version gate anywhere. As implemented:

```jsonc
{
  "schema_version": 1,
  "signal_id": "gps_l1_mcode",
  "operating_plane": "antenna_eirp",
  "quantity": "EIRP",
  "amplitude": 0.8,
  // v1-compat: the operating curve folded at the representative frequency + clamps,
  // so a v1 script keeps working unchanged.
  "curve": [[40,-36.0], …], "min_gain_db": 0.0, "max_gain_db": 74.0,
  "min_power_dbm": …, "max_power_dbm": …,
  // v2: the measured anchor + the passive hops as frequency tables + the split ceiling.
  "anchor_curve": [[40,-36], …],        // the measured anchor plane's gain→power
  "passive_hops": [                     // ordered anchor → operating; PowerMap sums these
    { "plane": "cable_output", "component": "cable_lmr240_3m_a",
      "delta_db_by_freq": [[1.1e9,-2.30],[1.4e9,-2.62],[1.6e9,-2.81]] },
    { "plane": "antenna_eirp", "component": "patch_a",
      "delta_db_by_freq": [[1.17e9,5.1],[1.575e9,6.0]] }
  ],
  "gain_ceiling_db": 74.0,              // the frequency-INDEPENDENT cap (amp protection, on a measured plane)
  "freq_dependent_limits": [            // caps whose plane passes a passive hop, each with its
    { "plane": "antenna_eirp", "max_dbm": 30.0, "reason": "regulatory EIRP",
      "delta_db_by_freq": [[1.1e9,2.8],[1.6e9,3.19]] }   // summed delta from the shared anchor
  ],
  "center_freq_hz": 1.575e9            // representative frequency (on a freq-dependent chain; derived when the signal omits it)
}
```

The agent's representative frequency (for folding `curve` / bounds) is the task's
`SDR_CAL_FREQ_HZ` env when set (the client sources it from the script's
`CAL_FREQ_PARAM`), else the signal's `center_freq_hz`, else — when the signal declares
none on a frequency-dependent chain — a representative one the resolver derives (the
tightest-ceiling breakpoint under a frequency-dependent safety limit, else the breakpoint
midpoint). Either way the artifact carries the chosen `center_freq_hz`, so a v1 script
that folds no frequency of its own uses that safe representative value.

### 5.3 `PowerMap` v2 (script-side)

```
delta(f)          = Σ interp(hop.delta_db_by_freq, f)   over passive_hops
ceiling(f)        = min( gain_ceiling_db,
                         min over freq_dependent_limits of
                             invert(anchor_curve, lim.max_dbm − interp(lim.delta_db_by_freq, f)) )
power_for_gain(g, f) = interp(anchor_curve, g)  +  delta(f)          # g clamped to [min, ceiling(f)]
gain_for_power(p, f) = invert(anchor_curve, p − delta(f)),  clamped to [min, ceiling(f)]
```

- `PowerMap.load(artifact)` builds this; the script calls `gain_for_power(p, f)` /
  `power_for_gain(g, f)` with its **current** frequency (the value of its
  `CAL_FREQ_PARAM`). On a live-tune of the frequency, it re-evaluates — nothing cached
  at the wrong `f`. With no `f` given it uses the artifact's `center_freq_hz`.
- Every frequency-dependent limit shares the operating plane's measured anchor (they
  are downstream passive planes), so one `anchor_curve` inverts them all — the script
  needs no plane model.
- A v1 artifact (no `anchor_curve`) loads as today: `delta(f)=0`, `ceiling=max_gain_db`,
  behaviour byte-identical to v1. **`PowerMap` stays backward compatible.**
- calkit and the agent resolver agree at every frequency by construction (a test
  cross-checks `PowerMap` against `ResolvedCalibration` across frequencies).

---

## 6. Safety under a moving frequency

The one thing to get right: does making dB frequency-dependent weaken the ceiling? No —
because of *where* the safety-critical limit sits.

- **Amp protection is on a measured plane.** The mandatory limit
  (`sdr_output ≤ −2.5 dBm`, the amp's P1dB input) inverts through the **measured**
  `sdr_output` curve, which is **not** frequency-dependent. So the amp-protection
  ceiling is a fixed gain cap regardless of frequency — the most safety-critical clamp
  stays rock-steady. The agent folds it into `gain_ceiling_db` once, as today.
- **Only a downstream regulatory cap moves.** A limit on a passive-derived plane (e.g. a
  30 dBm EIRP cap at `antenna_eirp`) depends on the antenna gain, so its gain cap varies
  with `f`. These ride in `freq_dependent_limits`; `PowerMap` computes
  `ceiling(f) = min(gain_ceiling_db, min over freq_dependent_limits of
  gain_for_power_on(max_dbm, plane, f))`. Still "tightest wins", just evaluated at `f`.
- **Table edges clamp and warn.** Evaluating a component table outside its measured
  frequency span clamps to the end value (never extrapolates) and raises a soft warning
  the banner surfaces ("cable table only spans 1.1–1.6 GHz; evaluated at edge"). It
  never silently invents a slope.

The v1 fail-safe table (v1 §8) carries over; add: *component reference missing from the
catalog → refuse (broken chain)*, and *operating/limit plane passes through a passive
hop but the script declares no `CAL_FREQ_PARAM` → refuse* (we won't guess a frequency for
a frequency-dependent chain).

---

## 7. Resolution, merge, and validate-on-upload

- **Merge order (unchanged spine).** `chain = type_defaults.chain ⊕ unit.chain`, then
  passive `component` refs are resolved against the **deployed catalog**. A component id
  with no catalog entry is a hard error.
- **Resolve (`resolve`, agent).** As v1, plus: when building a `derived` plane, take its
  dB from `component`'s table (kept as a table on the resolved `_Derived`, or a scalar if
  inline). Producing the artifact = emit the anchor curve + the ordered passive hop
  tables + the split ceilings (§5.2). At a representative frequency (from
  `CAL_FREQ_PARAM`'s configured value) the agent can still report single `min/max_power`
  numbers for the UI, labelled "at f".
- **Validate-on-upload (`validate_document`).** Runs without any runtime frequency, so it
  checks *structure*, resolving each signal as today: measured curves monotonic/invertible;
  every `from` / `component` / limit plane resolves; graph acyclic; anchor measured;
  a ceiling derivable. Passive tables are validated for shape (≥1 point, strictly
  increasing freq). Where a single representative frequency is available it additionally
  resolves end-to-end at the **endpoints of each passive table's span**, catching a chain
  that only breaks at some frequencies.
- **Partial measured stages (`_build_planes`).** A chain may carry more than one measured
  stage (e.g. `sdr_output` then `amplifier_output`), but you needn't measure every signal
  at every one. When a signal has **no curve for a measured stage that isn't the first**,
  that stage is resolved as a *transparent +0 dB hop* from the stage before it, so the
  signal inherits the nearest upstream measured curve instead of failing — you can add a
  downstream measured plane for a signal or two without re-measuring all thirty. The
  **first** stage has nothing to inherit, so a missing source curve stays a hard error. The
  synthetic hop contributes 0 dB and is omitted from the artifact's `passive_hops`; the
  amp-protection ceiling still binds on its own measured plane, so the fallback never
  loosens safety.

---

## 8. Editor / UX — the redesigned Calibration tab

The current tab mixes plane topology (JSON-only) with per-signal curve grids, which is
what feels off. v2 reorganises around the new nouns:

1. **Component library** (a sub-panel, or its own place next to the shared Library):
   characterize a cable/antenna once. Paste a VNA sweep → a frequency table (reuse the
   spreadsheet-paste curve grid from the calibration curve tables) with a loss/gain-vs-f
   sparkline. This is fleet-wide and deployed.
2. **Per-unit hardware chain** — a builder that reads left-to-right
   `SDR → amp → … → antenna`:
   - **measured** stages (SDR output, amplifier output) show their per-signal curve grids
     (today's editor, unchanged);
   - **passive** stages show a **component picker** (a dropdown of catalog entries of the
     right kind) — *this* is "drop in the cable you used"; a preview shows its dB at the
     signals' representative frequencies;
   - pick the operating plane; edit limits.
3. **Signals** shrink to what's genuinely per-signal: the measured curves + amplitude
   (the frequency is no longer a signal property — it comes from the task at runtime).

So the field flow becomes: characterize cables/antennas once (library) → per unit, choose
components from dropdowns → deploy. Changing a cable is one dropdown.

The JSON view remains the escape hatch / source of truth for topology, as today.

---

## 9. Staging

Each stage is shippable and leaves the system working (v1 docs valid throughout):

- **Stage 1 — agent core.** Catalog file + loader; `component` refs in the resolver;
  artifact v2 (`passive_hops`, split ceilings); `validate_document` updates; keep v1
  scalar `delta_db` working. Pure-resolver unit tests (frequency interpolation, clamping,
  ceiling split, catalog merge, back-compat).
- **Stage 2 — script/runtime.** `CAL_FREQ_PARAM` declaration + agent read-through;
  `PowerMap` v2 (fold at `f`, live-tune safe, v1-artifact compatible); banner shows the
  operating quantity and the evaluated frequency. Port M-code first.
- **Stage 3 — client.** Component-library editor + deploy plumbing; the per-unit
  chain-builder with component pickers; slim the signals editor. JSON view stays.

---

## 10. Decisions taken

- Catalog is a **fleet-level shared file** (`configs/components.yaml`, its own file —
  components are hardware reusable across *all* unit types, whereas
  `calibration_defaults.yaml` is keyed *by* unit type), deployed to units.
- The **script declares its frequency parameter** (`CAL_FREQ_PARAM`); the agent reads
  it, the script applies it at runtime. *(Stage 2.)*
- **Only passive components are frequency-aware**; measured planes (SDR, amp) stay
  measured per signal (no VNA on an SDR).
- A component's table stores **signed `delta_db`** (`delta_db_by_freq: [[hz, db], …]`,
  negative = loss, positive = gain) — a VNA reading is already signed, and it keeps a
  single code path.
- **Linear** interpolation over frequency (matches the gain-curve interp); a monotone
  spline stays a future option that needs no schema change.
- A per-signal **`center_freq_hz`** is the *representative* frequency: it both drives the
  editor's bounds preview **and** is the frequency the agent folds the v1-compat artifact
  `curve` / scalar ceiling at. It is **optional** even on a frequency-dependent chain — the
  real transmit frequency is a runtime quantity the task supplies via `--freq` /
  `CAL_FREQ_PARAM` (`SDR_CAL_FREQ_HZ`), and a v2 consumer re-folds at that live frequency.
  When it is absent the resolver derives a representative one (agent ≥ 1.7.1): the
  **tightest-ceiling breakpoint** when the chain carries a frequency-dependent *safety
  limit* (so a v1 script that folds no frequency of its own can never exceed a per-frequency
  limit), else the midpoint of the chain's frequency breakpoints. Setting it pins the
  preview / v1 fold to one frequency. It is ignored for constant chains. (A ≤ 1.7.0 agent
  still *requires* it on a frequency-dependent chain; the client gates the blank field on
  the `calibration-freq-optional-center` capability.)

## 11. Implementation status

- **Stage 1 (agent core) — done.** `agent/calibration.py`: derived planes carry a
  `delta_db`-vs-frequency table resolved from an inline `delta_db` (1-point → constant)
  or a catalog `component`; the ceiling is split into a frequency-independent part and
  frequency-dependent limits; `resolve` / `validate_document` take the catalog and an
  optional representative frequency; `load_components` reads `configs/components.yaml`;
  the artifact keeps `schema_version: 1` and stays v1-consumable (always emits `curve`),
  adding the v2 fields (`anchor_curve`, `passive_hops`, `gain_ceiling_db`,
  `freq_dependent_limits`, `center_freq_hz`) **additively** — a v2 consumer detects them
  by the presence of `passive_hops`, so no version gate is needed anywhere. Wired into
  `config.CALIBRATION_COMPONENTS`, `process_manager` injection, and the `/calibration`
  validate/dry-run endpoints. All v1 documents resolve byte-identically. **Partial
  measured stages** (§7): a signal missing the curve for a non-first measured stage
  inherits the nearest upstream measured curve via a synthetic transparent hop. Covered by
  `tests/test_calibration_v2.py`.
- **Stage 2 (script/runtime) — done.** `paramkit/calkit.py`: `PowerMap` folds the
  `passive_hops` at the frequency the script passes to `gain_for_power` / `power_for_gain`
  (defaulting to `center_freq_hz`), and tightens the ceiling per frequency from
  `freq_dependent_limits` — inverting them through the shared `anchor_curve`. A v1
  artifact still loads byte-identically. The artifact's `freq_dependent_limits` now
  carry a summed `delta_db_by_freq` so the consumer needs no plane model. The agent
  honours the task's `SDR_CAL_FREQ_HZ` env as the representative fold frequency
  (`config.CAL_FREQ_HZ_ENV`, threaded through `resolve_public`). Covered by
  `tests/test_calkit.py`, including a cross-check that `PowerMap` agrees with the agent
  resolver at every frequency.
  - **Script adoption — done for the frequency-swept signals.** `fm_chirp_tx.py` and
    `cw_tx.py` declare `CAL_FREQ_PARAM = "freq"` and pass their transmit frequency to
    `PowerMap.gain_for_power` / `power_for_gain`. The chirp's `--freq` is live, so a
    retune re-maps the held target `--power` at the new frequency (a raw `--gain`
    override drops the held target so it isn't re-applied). The static extractor surfaces
    the declared param as `calibration_freq_param` in `/scripts/{name}/params` (agent
    1.7.2). Other frequency-fixed signals can adopt the same two-line pattern when their
    chain becomes frequency-dependent.
- **Stage 3 (client) — mostly done.** `sdr-client`: a `ComponentCatalog` (the client's
  canonical library; VNA-sweep paste, validation, the `components.yaml` wire format), a
  **Component library** editor dialog, and the Calibration tab's chain now offers a
  **component picker** on each derived plane (or a constant Δ dB) plus a per-signal
  `center_freq_hz` field. On Save/Validate the catalog is uploaded to the unit first so
  references resolve; on open the unit's catalog is merged back so a fresh client learns
  the deployed parts. The catalog rides the existing `/files` store as the reserved,
  validated `components.yaml` (agent `CALIBRATION_COMPONENTS` → `DATA_DIR/components.yaml`,
  validated on upload) — so "deploy the catalog" reuses the calibration.json path rather
  than a new fleet-config channel.
  The Calibration tab was since rebuilt as the mockup's **chain-flow builder**: a
  left-to-right flow of stage cards (drag-to-reorder via the grip, or the ◀▶ handles), a
  per-stage detail pane (a frequency-response plot for a passive stage, the per-signal
  curve grids for a measured one), the resolved Signals table, and the Component library
  grid. The source stage is pinned (never removable); a signal can be removed from its
  expanded editor; a measured stage left unmeasured for a signal shows that it inherits
  the previous stage (the partial-measured-stage fallback above).
  - **Form re-fold — done.** The Run… dialog and the sequence step editor re-fold a
    signal's `--power` / `--gain` range at the frequency the operator enters, so the
    displayed range is the range at THAT frequency (a frequency-dependent chain's range
    moves with frequency). The `/calibration` view's per-signal summary carries the full
    resolved `artifact` (agent 1.7.2), and `state/power_fold.py` (`PowerFold`) re-folds it
    client-side — a deliberate mirror of `calkit.PowerMap`, so the form shows exactly what
    the script will map. Wiring: the form reads the script's `calibration_freq_param`,
    folds on a committed frequency change (preset pick / Enter / focus-out, never per
    keystroke), and degrades to the resolved representative range against an older agent
    that doesn't embed the artifact. Covered by `tests/test_power_fold.py` and
    `tests/test_param_form_freq_refold.py`.
  - **Clamp warning across a sequence — done.** A tune step can move the frequency to a
    point where the running `--power` can't be delivered (the runtime clamps it safely, but
    then delivers less than the number says). The sequence step editor carries the
    effective `--freq` / `--power` forward from the task's deployed args through the earlier
    same-task steps (`timeline_model.sequence_effective_values`), folds the step's `--power`
    range at that effective frequency (even when the step doesn't set `--freq` itself), and
    shows a **warning — never a block** (`power_fold.clamp_warning`) when the effective power
    exceeds the achievable range there. The live-tune dialog re-folds and warns the same way
    as you tune a running task. Covered by `tests/test_sequence_effective_values.py` and the
    fold tests above.
  - *Remaining:* the client setting `SDR_CAL_FREQ_HZ` on a task from the script's
    `CAL_FREQ_PARAM` (task-creation wiring) — with the script now reading its own `--freq`,
    this only pins the agent's representative fold for the v1-compat scalar read-outs.


## 12. Active components — a task-controlled gain/attenuation stage

v2's derived planes are **passive**: a fixed Δ dB(f) baseline. An **active component**
extends a derived plane with a `control` block so the stage can *dynamically* apply
gain/attenuation through a controllable parameter of a task (e.g. a step attenuator's
`attenuation`). It keeps the passive baseline (its behaviour at 0 dB applied) and layers a
programmable offset on top — extending the achievable power range and changing the achievable
*resolution* across it (attenuator-only at the bottom, SDR-only at the top, combined in the
middle). Example: an SDR that delivers −40..0 dBm plus a 0..95 dB / 0.25 dB step attenuator
gives an effective −135..0 dBm, and the operator asks for any power using only `--power`.

### 12.1 Wire format — a `control` block on a derived plane

Backward compatible: a derived plane is *active* iff it carries `control`; without it, it's
passive exactly as before.

```jsonc
"attenuator_out": {
  "type": "derived", "from": "sdr_output", "delta_db": 0.0,   // (or a component baseline)
  "control": {
    "task": "atten_set", "param": "attenuation",  // the task + the ONE param that drives it
    "sense": "attenuation",                        // "attenuation" (param subtracts) | "gain" (adds)
    "min_db": 0.0, "max_db": 95.0, "step_db": 0.25, // the param's own range + resolution
    "engage_pct": 0.0,                             // % of the SDR's dynamic range below which it engages
    "consts": { "port": "/dev/ttyACM0" }           // other params of the SAME task, sent on every set
  }
}
```

`param` is the single parameter driven from the requested power. A control task usually needs
**other** params too that don't vary with power — a step attenuator's serial `port`, a channel
select, a fixed mode. Those go in **`consts`** (`{dest: value}`) and are passed verbatim on every
set alongside the driven param; the one-shot would otherwise run with the driven param only and
the script's other required args missing. The editor's parameter form lists every param of the
set-task: you pick which numeric one drives the attenuation and give the rest constant values.

Validation (`agent/calibration.py:_parse_control`, mirrored in the client's
`_control_issues`): `task`/`param` non-empty, `sense ∈ {attenuation, gain}`, `min_db < max_db`,
`step_db > 0`, `0 ≤ engage_pct ≤ 100`, `consts` an object that does **not** re-list the driving
`param`. A defective block raises `CalibrationError` (refuse), never a silent fall-back.

**Baseline vs. programmable range — the plane's baseline is the part that's always there.**
An active plane's baseline is its behaviour at **0 dB applied** — i.e. a real attenuator's
**fixed insertion loss** — and it belongs to the component itself. It comes from exactly one
of: `delta_db` (a flat constant), `delta_db_by_freq` (the component's **own inline Δ dB(f)
table**, for a frequency-dependent insertion loss), or `component` (a shared catalog part, when
you deliberately want to reuse one). The `control` range (`min_db..max_db`) is the
*programmable* attenuation on top. The resolver folds the baseline into the range (so it shifts
the whole achievable range down — e.g. a 5 dB insertion loss caps the top at 5 dB below the
SDR's own maximum, and a frequency-dependent loss shifts it per frequency) and into the
SDR-first split, then commands the device with the **programmable part only** (its insertion
loss is inherent, never commanded). So an attenuator with 5 dB insertion loss set to
`--attenuation 1` delivers 6 dB of loss, and `"delta_db_by_freq": [[1e9,-5],[6e9,-8]]` makes
the maths — and the presented `--power` range — track that across the band. Whichever form the
baseline takes, it rides the artifact's `passive_hops` so the script and client fold it too.

### 12.2 The achievable-level resolver (shared, pure)

`paramkit/achievable.py` (`AchievableGrid` + `Active`) is imported by BOTH the agent resolver
and the transmit script, and mirrored **byte-identically** in the client
(`sdr-client/state/achievable.py`). Model: `P = P_base(g) − R`, where `P_base(g)` is the
delivered power with the SDR at grid gain `g` and every active component at rest (max applied
gain), and `R ≥ 0` is a reduction the components spend on their own discrete grids.

- **SDR-first.** The SDR carries the signal down to an engagement threshold
  `T = S_lo + engage_pct/100 · (S_hi − S_lo)`; below `T` the SDR pins and the components fill
  the rest (keeps the SDR out of its unstable low-gain region). `engage_pct = 0` ⇒ the SDR
  uses its full range and the attenuator only extends below `S_lo`.
- **Universal achievable snapping.** Because a fine component trims the fraction between the
  SDR's coarse grid points, the achievable resolution is the finest device step across the
  whole range. `snap` / `quantize_up` / `quantize_down` return only realizable levels — and
  this holds for a plain SDR-only chain too (it snaps to the real gain grid).
- **realize(power)** returns `{power_dbm, sdr_gain_db, settings}` where `settings` names each
  component's `task`, `param` and the value to command on it. Multiple components with
  different steps combine via a greedy chain-order realizer (no exponential enumeration).

`ResolvedCalibration` exposes `has_active`, `realize`, `snap_power`, `quantize_*`, and emits an
`active_components` list in the artifact; `min/max_power_dbm` come from the extended range.

### 12.3 Auto-command both — the SDR *and* the component

Requesting a calibrated `--power` drives BOTH the SDR (the transmit script maps its own SDR
gain via the active-aware `PowerMap`) and each linked component. The component is set to the
realization's value **first** — and awaited — so the SDR never briefly transmits with the
attenuator still at rest.

The firing is **centralized in the agent**, at the single chokepoint every transmit launch
passes through, so the operator never has to think about the attenuator and it works
uniformly for Run, quick-play, sequences, ramps, and any direct API caller. The client just
sends `--power` as it always has.

- **One-shot, not a running task.** A component is set by firing its control task as a
  fire-and-exit **one-shot** (`atten_set --attenuation N`, exits — the hardware holds the
  setting). Nothing has to be started or kept running; this is the "set and forget" model —
  the achievable `--power` range behaves like the SDR's own, and the attenuator is invisible.
- **The chokepoint.** `ProcessManager._precommand_active` runs at the top of `start`,
  `restart`, `run_oneshot` and `set_params`: it reads the absolute `--power` from the launch
  command (or the tuned `power` value), resolves the active components
  (`ProcessManager.active_settings` → the SDR-first realization), resolves each control
  `param` to its CLI flag from the control task's argspec (cached), and awaits a one-shot
  `atten_set <flag> <value> [<const-flag> <const-value>…]` **before** the transmit process
  starts / retunes — each `consts` entry resolved to its flag the same way and appended, so
  the attenuator's `--port` (and any other fixed param) rides along. Because
  sequence steps and ramps go through these same methods (`start` / `run_oneshot` /
  `set_params`), they are covered with no sequence-specific code.
- **Safety / failure.** A one-shot that fails or times out (`_ACTIVE_SET_TIMEOUT_S`) is logged
  and the transmit still proceeds — the script clamps its own SDR gain to a safe range
  regardless. (A future option could make an attenuator-set failure refuse the transmit.)

### 12.4 Editor / capability

The Calibration tab's chain builder adds an **Active component** stage (an ACTIVE badge, a
set-task / set-param picker fed from the unit's tasks and each task's `/scripts/{name}/params`,
a sense selector, and min/max/step/engage fields). Its **baseline** (the fixed insertion loss)
is chosen in the editor as either a **constant Δ dB** (flat) or the component's **own inline
Δ dB(f) table** — entered as a grid on the active stage and plotted inline (it belongs to the
component, not the shared library) — so a frequency-dependent insertion loss folds into the
range and the SDR/attenuation split at each signal's frequency. Saving an
active-component document is gated on the agent's `calibration-active-components` capability
(agent ≥ 1.8.0), mirroring the existing component gate.

### 12.5 Status — done

Agent core + resolver (`agent/calibration.py`, `paramkit/achievable.py`), transmit-script
consumer (`paramkit/calkit.py`), client fold + universal achievable slider
(`state/power_fold.py`, `state/achievable.py`, `ui/param_form.py`), agent-centralized one-shot
auto-commanding (`agent/process_manager.py`), and the calibration-panel active-stage editor.
Covered across `test_calibration_active`, `test_power_fold`, `test_achievable_levels` (client)
and `test_calibration_active`, `test_active_autocommand` (agent).


## 13. Reported & limiting power — quantity BRIDGES

Some signals are naturally measured in one power quantity but must be *reported* in another,
and *safety-limited* in a third. A chirp is measured as a spectral **density** (dBm/MHz); the
operator wants to set its **full-bandwidth (total) power** (dBm), and the amplifier must be
protected against that same total power. A comb is measured as **total** power but reported as
**per-tooth peak**. A PRN's main-lobe peak converts to total by a code-type constant.

A calibration node is measured **once**. Two readings derive from that measurement by a
**bridge**:

- **REPORTED** — what `--power` means to the operator (the operating quantity);
- **LIMITING** — what a safety ceiling is gauged against.

Each bridge is one of:

- `same` — the reading IS the measured quantity, up to a constant `k` dB (a pure denominator
  restatement, e.g. dBm/Hz → dBm/MHz is +60). Default ⇒ every v1 document is byte-identical.
- `law` — a signal-declared conversion; the ONLY bridge that changes the quantity/unit family.
- `own` — the reading is measured independently (its own curve).

### 13.1 The law shape (affine in log₁₀)

A law is closed and declarative — not an expression evaluator — because the SAME law is
evaluated by three code paths (the agent resolver for the UI bounds, the transmit script at
the live parameter value, and the client fold for the form read-out); a parity bug would mean
wrong emitted power. Every RF power-quantity conversion is of the form:

```
out = in + k + Σ  coeffᵢ · log10( paramᵢ / refᵢ )
```

`k` alone is a constant (a PRN peak→total ratio, a denominator restatement); one log term is a
density↔total (`+10·log10(bw/1 MHz)`) or per-tooth↔total (`−10·log10(N)`) conversion; several
terms handle a multi-parameter law. `in`/`out` fix the unit **family** of each side (`abs` =
dBm, `density` = dBm per bandwidth); a law is only applicable when the measurement's family
matches its `in`, and within `density` the denominator is a free display choice. A term may
carry an optional `rep` — a representative parameter value for the scalar read-outs — while
`ref` defines the formula. The shared math lives in `paramkit/power_law.py` (`Law`, `Bridge`,
`parse_law`/`parse_bridge`), mirrored byte-identically in the client (`state/power_law.py`).

### 13.2 Where a law is declared, where a bridge lives

A signal script declares the laws it offers as a module constant, surfaced statically like
`CAL_SIGNAL_ID` / `CAL_FREQ_PARAM`:

```python
CAL_POWER_LAWS = [
    {"id": "fbw_power", "name": "Full-bandwidth power", "in": "density", "out": "abs",
     "param": "bw", "coeff": 10.0, "ref": 1.0, "rep": 20.0},   # bw in MHz
]
```

The agent's `argspec` surfaces these raw as `calibration_power_laws` in `/scripts/{name}/params`
so the calibration editor can offer them in a "declared by this signal" picker. When the
operator picks one, the FULL law is **embedded** into the unit's calibration doc — so the
resolver, the artifact, and the transmit script never read the script metadata; the document is
self-contained.

A bridge lives on the **operating node**, resolved **per signal first** (a signal entry's
`reported`/`limiting` block) with the **operating-plane spec as the shared default** — so a
single-signal-type unit sets it once on the plane, and a mixed unit overrides per signal:

```jsonc
"antenna_eirp": {
  "type": "derived", "from": "cable_output",
  "reported": { "kind": "law", "unit": "dBm", "quantity": "full-bandwidth power",
                "law": { …embedded fbw_power… } },
  "limiting": { "kind": "same", "max_dbm": 30.0 }   // optional ceiling on the limiting reading
}
```

### 13.3 Resolve, artifact, runtime

- **Resolve (`agent/calibration.py`).** The REPORTED delta is a pure additive offset on the
  operator power axis — `gain_for_power`/`power_for_gain`, `realize`, `snap`/`quantize`,
  `min`/`max_power` and the v1-compat `curve` all shift by it — so an old (v1) script still
  shows the reported number. Param-keyed laws bake at a representative value for the scalar
  read-outs (like `center_freq_hz` does for frequency). The operating quantity/unit and the
  banner reflect the reported reading.
- **Artifact.** A `readings` block carries the reported + limiting bridges (embedded laws), the
  limiting cap, and the representative deltas the v1 fields bake in, plus `operating_unit`. The
  anchor curve is emitted whenever a bridge is present, so a bridge-aware consumer folds the
  MEASURED anchor and applies the bridge itself; the flat `curve` stays REPORTED for v1 scripts.
  Additive — a v1 consumer ignores it.
- **Runtime (`calkit` / client `power_fold`).** The transmit script passes its live keyed-param
  values (e.g. `{"bw": …}`) alongside the frequency; the reported bridge converts `--power` to
  the operating quantity before inverting the curve, and the limiting bridge + cap tighten the
  ceiling — all re-evaluated at the LIVE parameter value, so a chirp's `--power` and its
  total-power safety cap track the sweep bandwidth as it is tuned. The client form mirrors this:
  the `--power` field is labelled in the reported unit and its range/snapping re-fold on a
  keyed-parameter change, exactly as they already re-fold on a frequency change.

### 13.4 Status — done

Shared law/bridge math (`paramkit/power_law.py` ↔ `state/power_law.py`), the resolver
(reported axis shift, per-signal reading, `readings` artifact), the transmit-script consumer
(`paramkit/calkit.py`, live-parameter re-fold), the client fold (`state/power_fold.py`), the
operator-form unit + parameter re-fold (`ui/param_form.py`), the `CAL_POWER_LAWS` surfacing
(`agent/argspec.py` ↔ `api/argspec.py`), a chirp adopting it (`fm_chirp_tx.py`), and the
calibration-panel reported/limiting bridge editor. Covered by `test_power_law`,
`test_calibration_bridges`, `test_calkit_bridges` (agent) and `test_power_fold_bridges`,
`test_param_form_bridge`, `test_calibration_reading_editor` (client).


## 14. Measurement DE-EMBED — remove the analyzer-cable loss from a measured stage

You measure a stage's gain→power on a spectrum analyzer connected by a **measurement cable**.
That cable's loss `L(f)` (signed, negative) sits between the plane and the analyzer, so the
analyzer **under-reads** and the loss is baked into the measured curve. It is a **bench
artifact** — it is NOT in the transmit path, so it must be *removed* from the measurement, not
folded into the delivered-power chain.

```
P_true(g) = P_measured(g) − L_cable(f)      # L is signed (e.g. −1 dB); subtracting it adds the loss back
```

Because the measured curve is taken at one frequency (the signal's `center_freq_hz`), the
correction is a constant per signal. A measured plane names the cable:

```jsonc
"sdr_output": { "type": "measured", "quantity": "power",
                "measurement_deembed": "sa_cable_lmr240_1m" }   // a components.yaml id, or an inline table
```

- **It is not a chain stage.** The chain is the transmit path (`SDR → amp → cable → antenna`),
  where a stage *adds* loss. The measurement cable is on the bench (`SDR → analyzer`); it
  attaches to the measured stage as a de-embed, is applied in **reverse** (removed), and never
  appears in the chain, the artifact, or anything the transmit script sees.
- **Resolve.** `resolve()` folds `−L_cable(center_freq_hz)` into the measured plane's
  `offset_db` — recovering true plane power — **before** the ceiling/limit inversion, so every
  safety limit (e.g. the amp P1dB cap) gauges true power. A constant-loss cable needs no
  frequency; a frequency-dependent one uses the signal's measured-at frequency (lowest-frequency
  fallback when unknown).
- **Swappable.** The stored curve stays the **raw** analyzer reading; the correction comes from
  the referenced component. Change the physical cable → re-measure and re-select (the raw
  reading drops by the new cable's extra loss, and the de-embed adds exactly that back, so the
  resolved true curve is cable-independent). Re-characterize the *same* cable (a better VNA
  sweep) → update its library entry and the existing raw readings re-correct, no re-measuring.
- **Gate.** A ≤1.10.0 agent ignores `measurement_deembed` and leaves the cable loss in (wrong
  absolute power and a mis-placed ceiling), so the client gates saving on
  `calibration-measurement-deembed` (agent ≥ 1.11.0). Covered by `test_calibration_deembed`.

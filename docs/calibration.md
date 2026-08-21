# Per-unit power calibration — design spec

**Status:** design / spec. For review. No code yet.
**Scope:** a per-unit, per-signal calibration format + resolver so the `--power`
(dBm) knob on the transmit scripts is accurate and safe across a growing fleet of
units with different amplifiers, cables, and antennas. Also lays the groundwork
for a general per-unit data store (§9).

---

## 1. The problem

Today each transmit script bakes its calibration into constants:

```python
OUTPUT_POWER_DBM = -20.0    # max output at GAIN_AT_MAX_DB and AMPLITUDE
GAIN_AT_MAX_DB   = 89.75
CABLE_LOSS_DB    = 0.0
AMPLIFIER_GAIN_DB = 0.0
```

The scripts are **shared** across the fleet, but these numbers are a property of
the **physical unit** — its amplifier's compression point, its cable loss, its
antenna. Per-unit data baked into shared code. Every scaling pain (a new unit
invalidates the constants; a second signal needs different numbers) follows from
that one misplacement. The fix is to move the data out of the code and let each
unit carry its own, resolved at runtime.

Three facts about the real measurement drove the design:

1. **The gain→power transfer is a curve, not a line.** A single anchor point with
   an assumed 1 dB/dB slope is exact only at the anchor; error grows as you dial
   away from it. The fix is to store measured points and interpolate.
2. **Calibration is per `(unit × signal)`.** Occupied bandwidth, and the gain that
   reaches the amplifier's compression point, differ by signal (M-code vs CW vs a
   chirp). One flat per-unit number silently over/under-drives the second signal.
3. **The chain has several measurement planes.** Safety is measured at the SDR
   output with the amplifier *disconnected*; the operating power is what comes out
   the far end of the chain (after amp, cable, antenna). Those are different points
   in the RF cascade, measured in different passes.

---

## 2. Core concepts

- **Unit** — one broadcaster. Owns exactly one calibration document.
- **Signal** — a stable slug (`gps_l1_mcode`, `cw`, `fm_chirp`, …) the script
  declares as `CAL_SIGNAL_ID`. The calibration is keyed by it.
- **Plane** — a named reference point in the RF chain, carrying a `gain → power`
  relationship. Two kinds (§3).
- **Quantity** — a free-text *label* on each plane ("total in-band power",
  "main-lobe power", "EIRP"). **The runtime never interprets it** — the
  interpolator only ever sees `gain → dBm` numbers; the quantity is human context,
  echoed in the banner/UI. Because it's only a label it can be anything and can
  never break the math.
- **Ceiling** — a hard cap on commanded gain, in **gain-space**, that protects the
  amplifier (and can enforce a regulatory limit). Mandatory: no derivable ceiling →
  refuse to transmit.
- **Operating plane** — the plane the `--power` knob refers to. The delivered/
  radiated figure the operator actually cares about.

---

## 3. Planes: measured vs derived

The stages after the amplifier — cable, antenna — are **linear and
gain-independent**: a cable's loss and an antenna's gain don't change with drive
level. So they aren't curves; they're constant dB offsets. The amplifier, by
contrast, is nonlinear near compression and **must** be measured. Hence two plane
types:

- **`measured`** — carries a `gain → power` curve (`points`). Use where the transfer
  is nonlinear or otherwise has to be measured (`sdr_output`, `amplifier_output`).
  Optional `offset_db` is a within-plane fine-trim.
- **`derived`** — `from: <parent plane>` + a constant `delta_db` (negative = loss,
  positive = gain). No measurement, no curve.

A well-backed-off linear amplifier *may* be modelled as a derived plane (`+G_amp`)
instead of measured — the model supports either; measure it when you don't trust it
linear near your operating point.

### The cascade

```
sdr_output ──[amp: measured]──► amplifier_output ──[cable −1.8 dB]──► cable_output ──[antenna +6.0 dB]──► antenna_eirp
 (safety)                        (measured)             (derived)                        (derived, OPERATING)
```

### Why this stays correct

- **Inversion walks back to the nearest measured ancestor.** To hit a requested
  EIRP, subtract the downstream derived deltas to land on `amplifier_output`
  (`target = EIRP − 6.0 − (−1.8) = EIRP − 4.2`), invert *that* curve to a gain,
  clamp. Only measured planes can be inverted — they own the gain relationship;
  derived hops are add/subtract constants.
- **The ceiling stays upstream.** Passive planes are gain-independent, so they can't
  drive the amp and never touch the ceiling. Safety is derived on `sdr_output`; the
  downstream planes only relabel the resulting power.

---

## 4. Safety: calibration vs operation are different passes

The two measurement passes are deliberately distinct:

- **Safety pass — amplifier DISCONNECTED.** Measure total in-band power at the SDR
  port vs gain. You cannot tell from the amplifier *output* alone whether its input
  is above or below compression, so you cap the SDR drive directly. The gain where
  SDR output reaches the drive limit (e.g. −2.5 dBm total in-band, just below the
  amp's P1dB input) is the max safe gain. → the `sdr_output` measured plane + a
  limit.
- **Operation pass — amplifier CONNECTED.** Measure the delivered/radiated power at
  the plane you care about (`amplifier_output`, or a derived plane past cable and
  antenna). This is what `--power` maps to.

Because the ceiling is in gain-space, it's derived on the safety plane but applied
to a knob that reads on the operating plane — **gain is the shared axis**.

### Limits on any plane (generalized ceiling)

A limit is *a plane + a max power*, resolved to a gain cap via that plane's
transfer (walking back to a measured ancestor if the plane is derived). This makes
amp-protection and a regulatory EIRP cap the same mechanism:

```jsonc
"limits": [
  { "plane": "sdr_output",   "max_dbm": -2.5, "reason": "amp P1dB input" },
  { "plane": "antenna_eirp", "max_dbm": 30.0, "reason": "regulatory EIRP cap" }
]
```

Resolved ceiling = **min** gain over all limits, capped by the hardware max. The
tightest wins.

---

## 5. The format (annotated)

One document per unit, e.g. `calibration.json` in the unit's data area (§9).

```jsonc
{
  "schema_version": 1,
  "unit_id": "unit_9841f459",              // detects a misplaced file
  "meta": {
    "measured_by": "magnus",
    "measured_at": "2026-08-21T14:00:00Z",
    "instrument": "Rigol RSA5065N",
    "notes": "B206-mini + ZVE-8G amp"
  },

  "defaults": {                            // unit-wide, per-signal may override
    "hw_gain_limits": { "min_gain_db": 0.0, "max_gain_db": 89.75 }
  },

  "signals": {
    "gps_l1_mcode": {
      "amplitude": 0.8,                    // baseband amplitude the curves were measured at
      "occupied_bw_hz": 40920000,          // integration BW (metadata)
      "gain_limits": { "min_gain_db": 0.0, "max_gain_db": 89.75 },

      "limits": [
        { "plane": "sdr_output", "max_dbm": -2.5, "reason": "amp P1dB input" }
      ],

      "operating_plane": "antenna_eirp",   // what --power means; use "sdr_output" pre-amp-pass

      "planes": {
        "sdr_output": {
          "type": "measured",
          "quantity": "total in-band power",
          "description": "Amp DISCONNECTED. Integrated over the 40.92 MHz band at the SDR port.",
          "interp": "linear",
          "offset_db": 0.0,
          "points": [
            { "gain_db": 74.0, "power_dbm":  -2.5 },
            { "gain_db": 70.0, "power_dbm":  -6.3 },
            { "gain_db": 60.0, "power_dbm": -16.1 },
            { "gain_db": 50.0, "power_dbm": -26.0 },
            { "gain_db": 40.0, "power_dbm": -36.0 }
          ]
        },
        "amplifier_output": {
          "type": "measured",
          "quantity": "main-lobe power",
          "description": "Amp CONNECTED. Peak main-lobe power at the amplifier output.",
          "interp": "linear",
          "offset_db": 0.0,
          "points": []                     // filled by the operation pass
        },
        "cable_output": {
          "type": "derived",
          "from": "amplifier_output",
          "delta_db": -1.8,
          "description": "3 m LMR-240 to the antenna"
        },
        "antenna_eirp": {
          "type": "derived",
          "from": "cable_output",
          "delta_db": 6.0,
          "quantity": "EIRP",
          "description": "6 dBi patch"
        }
      }
    }
  }
}
```

A single-point measured curve is legal: with one point the reader falls back to a
slope-1 assumption, so this is a strict superset of today's constants and migration
never breaks.

---

## 6. Formal schema (JSON Schema, draft 2020-12, trimmed)

```jsonc
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["schema_version", "signals"],
  "additionalProperties": false,
  "properties": {
    "schema_version": { "const": 1 },
    "unit_id": { "type": "string" },
    "meta": { "type": "object" },
    "defaults": {
      "type": "object",
      "properties": {
        "hw_gain_limits": { "$ref": "#/$defs/gain_limits" }
      }
    },
    "signals": {
      "type": "object",
      "minProperties": 1,
      "additionalProperties": { "$ref": "#/$defs/signal" }
    }
  },

  "$defs": {
    "gain_limits": {
      "type": "object",
      "properties": {
        "min_gain_db": { "type": "number", "minimum": 0 },
        "max_gain_db": { "type": "number", "minimum": 0 }
      }
    },
    "point": {
      "type": "object",
      "required": ["gain_db", "power_dbm"],
      "additionalProperties": false,
      "properties": {
        "gain_db":   { "type": "number", "minimum": 0 },
        "power_dbm": { "type": "number" }
      }
    },
    "plane": {
      "oneOf": [
        {
          "type": "object",
          "required": ["type", "points"],
          "additionalProperties": false,
          "properties": {
            "type":        { "const": "measured" },
            "quantity":    { "type": "string" },
            "description": { "type": "string" },
            "interp":      { "enum": ["linear"] },
            "offset_db":   { "type": "number" },
            "points":      { "type": "array", "minItems": 1, "items": { "$ref": "#/$defs/point" } }
          }
        },
        {
          "type": "object",
          "required": ["type", "from", "delta_db"],
          "additionalProperties": false,
          "properties": {
            "type":        { "const": "derived" },
            "from":        { "type": "string" },
            "delta_db":    { "type": "number" },
            "quantity":    { "type": "string" },
            "description": { "type": "string" }
          }
        }
      ]
    },
    "limit": {
      "type": "object",
      "required": ["plane", "max_dbm"],
      "additionalProperties": false,
      "properties": {
        "plane":   { "type": "string" },
        "max_dbm": { "type": "number" },
        "reason":  { "type": "string" }
      }
    },
    "signal": {
      "type": "object",
      "required": ["amplitude", "operating_plane", "planes"],
      "additionalProperties": false,
      "properties": {
        "amplitude":       { "type": "number", "exclusiveMinimum": 0, "maximum": 1 },
        "occupied_bw_hz":  { "type": "number", "exclusiveMinimum": 0 },
        "gain_limits":     { "$ref": "#/$defs/gain_limits" },
        "limits":          { "type": "array", "items": { "$ref": "#/$defs/limit" } },
        "operating_plane": { "type": "string" },
        "planes": {
          "type": "object",
          "minProperties": 1,
          "additionalProperties": { "$ref": "#/$defs/plane" }
        }
      }
    }
  }
}
```

**Load-time validations JSON Schema can't express (all hard errors):**

- every measured plane's `points` are strictly increasing in **both** `gain_db` and
  `power_dbm` (monotonic → invertible);
- every `from` and every `limits[].plane` / `operating_plane` names a plane that
  exists; the `from` graph is acyclic and every derived chain ends at a measured
  plane;
- at least one ceiling is derivable (`limits` non-empty **or** an explicit
  `max_gain_db`);
- `operating_plane` resolves to a plane with a usable transfer (a measured plane, or
  a derived chain ending in one with ≥1 point).

---

## 7. Resolution & runtime

### 7.1 Layered merge (most specific wins)

The document the resolver evaluates is the merge of, in increasing precedence:

```
script baked-in defaults      (safe, conservative — today's constants)
  └─ unit-type defaults        (all Broadcasters share amp model X)
       └─ per-unit overrides    (this box's measured curves)
            └─ per-(unit×signal) (the empirical measurement)
```

Merge is per-key deep-merge; a more specific layer replaces scalars and whole
`points` arrays (curves are never element-wise merged).

### 7.2 Ceiling

```python
def resolve_max_gain(sig, planes, hw):
    candidates = []
    if sig.gain_limits.max_gain_db is not None:
        candidates.append(sig.gain_limits.max_gain_db)
    for lim in sig.limits:                       # each limit -> a gain
        candidates.append(gain_for_power_on(lim.max_dbm, lim.plane, planes))
    if not candidates:
        refuse("no safety ceiling — refusing to transmit")   # MANDATORY
    return min(min(candidates), hw.max_gain_db)
```

### 7.3 Plane power & inversion

Power at any plane, walking derived hops down to a measured curve:

```python
def power_on(plane_name, gain, planes):
    p = planes[plane_name]
    if p.type == "measured":
        return float(np.interp(gain, p.gains, p.powers)) + p.offset_db
    return power_on(p.from, gain, planes) + p.delta_db     # derived: constant hop

def gain_for_power_on(power, plane_name, planes):
    # accumulate derived deltas down to the anchor measured plane, then invert once
    delta, p = 0.0, planes[plane_name]
    while p.type == "derived":
        delta += p.delta_db
        p = planes[p.from]
    target = power - delta - p.offset_db
    if target >= p.powers[-1]:  return p.gains[-1]     # clamp up (never past ceiling)
    if target <= p.powers[0]:   return p.gains[0]      # clamp down to min measured gain
    return float(np.interp(target, p.powers, p.gains)) # powers monotonic -> unambiguous
```

### 7.4 The two script-facing functions

```python
gmin = sig.gain_limits.min_gain_db          # can't command below this
gmax = resolve_max_gain(sig, planes, hw)    # safety ceiling
op   = sig.operating_plane

def power_for_gain(g):                       # readout for the banner/report
    return power_on(op, min(max(g, gmin), gmax), planes)

def gain_for_power(p_req):                   # --power -> commanded gain
    g = gain_for_power_on(p_req, op, planes)
    return min(max(g, gmin), gmax)
```

`np.interp` is already available (lazy numpy import) — linear interpolation with
endpoint clamping, no scipy. The **only** inviolable rule: upward is always clamped
to the ceiling, never extrapolated.

---

## 8. Fail-safe semantics

| Situation | Behavior |
|---|---|
| No calibration file at all | Script's baked-in conservative defaults. Normal. |
| File present, this `signal` id missing | Baked defaults **+ loud banner warning**. |
| **No ceiling derivable** (no `limits`, no `max_gain_db`) | **Refuse to transmit.** Never run wide open. |
| `operating_plane` has no usable transfer (e.g. amp pass not done yet) | **Refuse** — "measure it, or set `operating_plane` to a measured plane". |
| Malformed / non-monotonic curve / broken `from` chain | **Refuse** — not invertible. |
| Requested power above/below the operating curve's range | Clamp to max/min gain; report the actual delivered power. |

The distinction that matters: **"no file" is normal; "broken file" is an error you
stop on** — an operator who uploaded calibration clearly intended it, and a silent
fallback could over- or under-drive.

---

## 9. Delivery to the script & the per-unit store

### 9.1 Script side

- Each script declares `CAL_SIGNAL_ID = "gps_l1_mcode"` — a stable slug, independent
  of filename or task name.
- The agent resolves the merged calibration for `(this unit, this signal)` and
  injects it, e.g. `SDR_CALIBRATION_FILE=/…/resolved.json` in the task env (the
  agent already injects env into tasks).
- The calibration block becomes: read that file → validate → build interpolators;
  if absent, fall back to the baked constants.
- The banner echoes the **operating plane's `quantity`** so the number is never
  ambiguous, e.g. `power: -12.0 dBm (EIRP, at antenna_eirp)`; a nonzero `offset_db`
  on any active plane is flagged.

### 9.2 The store (stub — to design next)

The per-unit data area is deliberately general — calibration is the first tenant,
not the only one. Proposed shape:

- A per-unit directory the agent owns (sits beside the unit's other state, distinct
  from the shared scripts/config), e.g. `<unit-data>/calibration.json` plus room for
  future files.
- An agent endpoint to **upload / fetch / list** files in that area, surfaced in
  FleetView as a per-unit "Files / calibration" panel.
- **Discipline that keeps it from rotting:** the area accepts arbitrary files, but
  every *kind* of data earns a small schema when it's introduced (calibration is the
  first). No executable uploads — data only; a computed calibration is expressed as
  a table, never as code.
- Validation on upload for known kinds (run the §6 schema + §6 hard-checks against
  `calibration.json`) so a broken file is rejected at upload, not at transmit.

---

## 10. Open items

- **Curve interpolation** is `linear` only for now; the `interp` enum leaves room
  for a monotone spline later if linear segments prove too coarse between points.
- **Unit-type layer** (§7.1) needs a home — likely a shared config keyed by unit
  type, merged under the per-unit file.
- **EIRP quantity/units** — `power_dbm` assumes dBm throughout; if EIRP is ever
  wanted in dBW or as ERP, that's a per-plane unit tag, not a structural change.
- **FleetView editor** — the file/upload store (§9.2) is the foundation; a
  point-and-click curve editor can layer on top later.

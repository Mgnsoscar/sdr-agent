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
  { "plane": "amplifier_output", "side": "input",  "max_dbm": -2.5, "reason": "amp P1dB input" },
  { "plane": "antenna_eirp",     "side": "output", "max_dbm": 50.0, "reason": "licence EIRP cap" }
]
```

Resolved ceiling = **min** gain over all limits, capped by the hardware max. The
tightest wins.

**Which stage boundary a limit applies at (`side`).** A plane is a *node* between
stages: it is the **output** of the stage before it and the **input** of the stage
after it. `side` picks which boundary a limit protects:

- `"output"` (the default, and what a limit with no `side` means) — the cap is on the
  named plane itself, i.e. the output of that stage. A regulatory EIRP cap is an
  output limit on `antenna_eirp`.
- `"input"` — the cap is on the plane **feeding** the named stage, one hop upstream in
  the cascade (a derived plane's `from`; a measured plane's predecessor by cascade
  order). An amplifier's max input power is an input limit on `amplifier_output`.

The point of `"input"` is that the limit **follows its stage**. An amp that must never
see more than −2.5 dBm at its input is `{ "plane": "amplifier_output", "side": "input" }`.
If you later insert a pad or a longer cable *in front of* the amp (a new plane between
the SDR and the amp), the amp's input plane changes — but the limit re-resolves to the
new upstream plane automatically. You never restate −2.5 dBm on the newly inserted
component. (An input-side limit on the very first plane has nothing upstream and is
refused.)

> The default is `"output"`, so every existing `{ plane, max_dbm }` limit keeps
> resolving exactly as before. `side` is advertised as the `calibration-limit-side`
> capability (agent ≥ 1.5.0); an older agent ignores the key and would apply the cap at
> the *output*, so the client refuses to push a `side`-using document to it.

### 4.1 Measured-plane roles: what a curve counts toward (`role` / `of`)

A safety limit is a threshold in a *specific quantity*, and it is only meaningful when
inverted through a curve measured in that **same** quantity. That becomes explicit when
one physical node is measured two ways — e.g. a source measured as **full-band integrated
power** (what an amplifier's input P1dB spec refers to) *and* as **main-lobe power** (the
region of interest the operator actually wants to read). Gauging the amp's full-band limit
through the main-lobe curve silently over-drives it: main-lobe reads lower, so the ceiling
lands too high.

`role` keeps the two apart:

- **`"limiting"`** (the default — every v1 plane is this) — the curve safety limits invert
  through, and a valid operating/reporting anchor.
- **`"reported"`** — a *re-measurement of the same node in a different quantity*. It is the
  number `--power` shows and it propagates downstream like any anchor, but it is
  **invisible to limit inversion**: the limit walk punches straight through it (0 dB, same
  node) to the `"limiting"` curve named by **`of`**. So `--power` reports the region of
  interest while every limit is still gauged in its own quantity.

A `"reported"` plane **must** set `of` to a `"limiting"` plane; that requirement makes the
source/root plane impossible to mark `"reported"` (it has no limiting basis to point at).
Downstream stages derive from the reported plane, so observed power flows in the
region-of-interest quantity, while a limit anywhere below it still resolves to the limiting
curve — and, via `side: "input"`, still **follows its stage**: drop a pad in front of the
amp and the source may climb by the pad while the amp stays protected on full-band.

```jsonc
"planes": {
  "source":    { "type": "measured", "role": "limiting",
                 "quantity": "total in-band power" },       // amp-protection basis
  "main_lobe": { "type": "measured", "role": "reported", "of": "source",
                 "quantity": "main-lobe power" },           // what --power shows
  "amplifier": { "type": "derived", "from": "main_lobe", "delta_db": 20.0 }
}
// limit { plane: "amplifier", side: "input", max_dbm: -2.5 } gauges on `source` (full-band),
// while operating_plane "main_lobe" reports main-lobe power.
```

The validate-on-save summary reports, per limit, the plane and quantity it resolved to
gauge on (`amp P1dB input → gauged on 'source' (total in-band power)`), so a quantity
mismatch is visible at save time. `role`/`of` are advertised as the
`calibration-plane-roles` capability (agent ≥ 1.6.0); a `≤1.5.2` agent doesn't understand
them and would treat a reported plane as an ordinary limiting one (mis-gauging the
ceiling), so the client refuses to push a role-using document to it.

---

## 5. The format (annotated)

The calibration is split into two parts that live at different scopes, because they
change at different rates:

- **`chain`** — the RF cascade: plane topology, derived planes (cable loss, antenna
  gain), quantities, `operating_plane`, gain limits, and the limit *thresholds*.
  These are **unit hardware**, so they're stated **once per unit** (merged over the
  unit-type defaults, §7.1). The amplifier/cable/antenna are never restated per
  signal.
- **`signals[…].curves`** — the measured `points` that populate the measured planes,
  plus `amplitude` and `occupied_bw_hz`. These are the only genuinely per-signal
  facts (the curve shape depends on the signal's spectrum, crest factor, and
  amplitude).

The per-signal ceiling is *derived*, not stated: a unit-level limit like "SDR-port
total-in-band ≤ −2.5 dBm" (the amp's P1dB input, constant across signals) resolves
to a different gain cap for each signal by inverting *that signal's* `sdr_output`
curve against the shared threshold.

### 5.1 Per-unit document (`calibration.json`)

```jsonc
{
  "schema_version": 1,
  "unit_id": "unit_9841f459",              // detects a misplaced file
  "unit_type": "broadcaster",              // selects the type-defaults layer (§7.1)
  "meta": {
    "measured_by": "magnus",
    "measured_at": "2026-08-21T14:00:00Z",
    "instrument": "Rigol RSA5065N",
    "notes": "B206-mini + ZVE-8G amp"
  },

  // ── RF chain: this unit's HARDWARE, stated once (merges over type defaults) ──
  "chain": {
    "gain_limits": { "min_gain_db": 0.0, "max_gain_db": 89.75 },
    "operating_plane": "antenna_eirp",     // what --power means; "sdr_output" pre-amp-pass
    "limits": [
      // amp-input protection: follows the amp if a component is inserted upstream
      { "plane": "amplifier_output", "side": "input", "max_dbm": -2.5, "reason": "amp P1dB input" }
    ],
    "planes": {
      "sdr_output": {
        "type": "measured", "quantity": "total in-band power",
        "description": "Amp DISCONNECTED; integrated at the SDR port."
        // NOTE: no points here — the curve is per-signal (§5.2)
      },
      "amplifier_output": {
        "type": "measured", "quantity": "main-lobe power",
        "description": "Amp CONNECTED; at the amplifier output."
      },
      "cable_output": {
        "type": "derived", "from": "amplifier_output", "delta_db": -1.8,
        "description": "3 m LMR-240 to the antenna"
      },
      "antenna_eirp": {
        "type": "derived", "from": "cable_output", "delta_db": 6.0,
        "quantity": "EIRP", "description": "6 dBi patch"
      }
    }
  },

  // ── per-signal: ONLY the measurements that fill the measured planes ──
  "defaults": { "amplitude": 0.8 },        // unit-wide; a signal may override
  "signals": {
    "gps_l1_mcode": {
      "amplitude": 0.8,                    // amplitude the curves were measured at
      "occupied_bw_hz": 40920000,          // integration BW (metadata)
      "curves": {
        "sdr_output": {
          "interp": "linear", "offset_db": 0.0,
          "points": [
            { "gain_db": 74.0, "power_dbm":  -2.5 },
            { "gain_db": 70.0, "power_dbm":  -6.3 },
            { "gain_db": 60.0, "power_dbm": -16.1 },
            { "gain_db": 50.0, "power_dbm": -26.0 },
            { "gain_db": 40.0, "power_dbm": -36.0 }
          ]
        },
        "amplifier_output": {
          "interp": "linear", "offset_db": 0.0,
          "points": []                     // filled by the amp-connected pass
        }
      }
    }
  }
}
```

`curves` keys must name **measured** planes from `chain.planes`; derived planes take
no curve. A single-point measured curve is legal — with one point the reader falls
back to a slope-1 assumption, so this is a strict superset of today's constants and
migration never breaks.

### 5.2 Shared unit-type defaults (`calibration_defaults.yaml`, keyed by type)

Lives in the shared configs dir beside `tasks.yaml`; every unit reads it and picks
its own type. Adding a type is a **data** change — no agent code (§7.1).

```jsonc
{
  "schema_version": 1,
  "types": {
    "broadcaster": {
      "chain": {
        "gain_limits": { "min_gain_db": 0.0, "max_gain_db": 89.75 },
        "operating_plane": "sdr_output",   // conservative until the amp pass is done
        "limits": [
          { "plane": "sdr_output", "max_dbm": -2.5, "reason": "amp P1dB input (nominal)" }
        ],
        "planes": {
          "sdr_output":       { "type": "measured", "quantity": "total in-band power" },
          "amplifier_output": { "type": "measured", "quantity": "main-lobe power" }
        }
      },
      "defaults": { "amplitude": 0.8 }
    }
    // "sensor": { ... }   ← added later; the agent code never changes
  }
}
```

A unit inherits this skeleton and overrides only what differs — its real cable/
antenna planes, its measured limit, its `operating_plane` once the amp is measured.

---

## 6. Formal schema (JSON Schema, draft 2020-12, trimmed)

This is the schema for a **per-unit document**; the shared type-defaults file is the
same `chain` / `defaults` shapes wrapped in `{ "types": { <type>: {…} } }`, all
fields optional (it only supplies defaults). Note that in the per-unit file the
`chain` planes carry **no** `points` — the curve is supplied per signal under
`signals[…].curves` and merged into the measured planes at resolve time (§7).

```jsonc
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["schema_version", "chain", "signals"],
  "additionalProperties": false,
  "properties": {
    "schema_version": { "const": 1 },
    "unit_id":   { "type": "string" },
    "unit_type": { "type": "string" },
    "meta":      { "type": "object" },
    "defaults":  { "type": "object", "properties": { "amplitude": { "$ref": "#/$defs/amplitude" } } },
    "chain":     { "$ref": "#/$defs/chain" },
    "signals": {
      "type": "object",
      "minProperties": 1,
      "additionalProperties": { "$ref": "#/$defs/signal" }
    }
  },

  "$defs": {
    "amplitude":   { "type": "number", "exclusiveMinimum": 0, "maximum": 1 },
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
    "limit": {
      "type": "object",
      "required": ["plane", "max_dbm"],
      "additionalProperties": false,
      "properties": {
        "plane":   { "type": "string" },
        "side":    { "enum": ["input", "output"] },   // default "output" (the plane itself)
        "max_dbm": { "type": "number" },
        "reason":  { "type": "string" }
      }
    },

    "plane": {                                   // chain topology — no points here
      "oneOf": [
        {
          "type": "object",
          "required": ["type"],
          "additionalProperties": false,
          "properties": {
            "type":        { "const": "measured" },
            "role":        { "enum": ["limiting", "reported"] },  // default "limiting" (§4.1)
            "of":          { "type": "string" },   // reported only → the limiting plane it re-measures
            "quantity":    { "type": "string" },
            "description": { "type": "string" }
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
    "chain": {
      "type": "object",
      "required": ["planes", "operating_plane"],
      "additionalProperties": false,
      "properties": {
        "gain_limits":     { "$ref": "#/$defs/gain_limits" },
        "operating_plane": { "type": "string" },
        "limits":          { "type": "array", "items": { "$ref": "#/$defs/limit" } },
        "planes": {
          "type": "object",
          "minProperties": 1,
          "additionalProperties": { "$ref": "#/$defs/plane" }
        }
      }
    },

    "curve": {                                   // per-signal measurement for one measured plane
      "type": "object",
      "required": ["points"],
      "additionalProperties": false,
      "properties": {
        "interp":    { "enum": ["linear"] },
        "offset_db": { "type": "number" },
        "points":    { "type": "array", "minItems": 1, "items": { "$ref": "#/$defs/point" } }
      }
    },
    "signal": {
      "type": "object",
      "required": ["curves"],
      "additionalProperties": false,
      "properties": {
        "amplitude":      { "$ref": "#/$defs/amplitude" },
        "occupied_bw_hz": { "type": "number", "exclusiveMinimum": 0 },
        "curves": {
          "type": "object",
          "minProperties": 1,
          "additionalProperties": { "$ref": "#/$defs/curve" }   // key = a measured plane name
        }
      }
    }
  }
}
```

**Load-time validations JSON Schema can't express (all hard errors), checked on the
*assembled* `(chain ⊕ curves)` doc:**

- every `curves` key names a **measured** plane declared in `chain.planes`;
- every measured plane's supplied `points` are strictly increasing in **both**
  `gain_db` and `power_dbm` (monotonic → invertible);
- every `from` and every `limits[].plane` / `operating_plane` names a plane that
  exists; the `from` graph is acyclic and every derived chain ends at a measured
  plane;
- at least one ceiling is derivable (`chain.limits` non-empty **or** an explicit
  `max_gain_db`);
- `operating_plane` resolves to a plane with a usable transfer for this signal (a
  measured plane the signal supplied ≥1 point for, or a derived chain ending in
  one).

---

## 7. Resolution & runtime

### 7.1 Layered merge (most specific wins)

Resolution has two axes. First the **chain** is merged across scopes; then the
signal's **curves** are attached to the merged chain's measured planes.

**Chain merge**, in increasing precedence:

```
script baked-in defaults          (safe, conservative — today's constants)
  └─ unit-type defaults            (calibration_defaults.yaml → types[unit_type].chain)
       └─ per-unit chain           (this box's real cable/antenna/limits/operating_plane)
```

- The **unit-type layer is data, not code.** The agent reads the shared
  `calibration_defaults.yaml`, selects `types[self.unit_type]`, and merges the
  per-unit file's `chain` on top. A new unit type is a new section in that file —
  **the agent binary is identical for every type**, so there's never a per-type PR.
- Merge is per-key deep-merge; a more specific layer replaces scalars and whole
  arrays (a `points` array or a `limits` list is never element-wise merged).

**Curve attachment (per signal):** for the requested `signal`, each entry in
`signals[sig].curves` is written into the matching measured plane of the merged
chain, producing the fully-assembled doc the runtime evaluates. `amplitude` resolves
as `signals[sig].amplitude` ?? `defaults.amplitude`. The passive planes
(amp/cable/antenna topology) are stated once in `chain` and reused by every signal;
only the measured curves vary — so adding a signal never restates hardware.

A measured plane with no curve for this signal stays *latent*: fine unless the
`operating_plane` or a `limits[].plane` needs it, in which case resolution refuses
(§8).

### 7.2 Ceiling

`chain`, `planes`, and the signal's populated curves come from the assembled doc
(§7.1). Limits and gain bounds live on the **chain** (unit hardware), but each
limit's gain cap is computed against *this signal's* curves:

```python
def resolve_max_gain(chain, planes):
    candidates = []
    if chain.gain_limits.max_gain_db is not None:
        candidates.append(chain.gain_limits.max_gain_db)
    for lim in chain.limits:                      # each limit -> a gain, via this signal's curve
        plane = upstream_plane(lim.plane) if lim.side == "input" else lim.plane
        candidates.append(gain_for_power_on(lim.max_dbm, plane, planes))
    if not candidates:
        refuse("no safety ceiling — refusing to transmit")   # MANDATORY
    return min(candidates)                         # hardware max already folded into gain_limits
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
gmin = chain.gain_limits.min_gain_db        # can't command below this
gmax = resolve_max_gain(chain, planes)      # safety ceiling (uses this signal's curves)
op   = chain.operating_plane

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

### 9.1 Script side *(implemented)*

The agent is the single source of truth for resolution; the script consumes a
**flat artifact** and does trivial interpolation — no plane model script-side, no
resolver duplicated across repos.

- Each script declares `CAL_SIGNAL_ID = "gps_l1_mcode"` — a stable slug, independent
  of filename or task name.
- A task **opts in** by setting `SDR_CAL_SIGNAL_ID` to that slug. At task start the
  agent (`process_manager._inject_calibration`) resolves `(this unit, this signal)`,
  writes the flattened artifact (`ResolvedCalibration.to_public_dict`) to
  `CAL_RUN_DIR/<task>.json`, and points the task at it via `SDR_CALIBRATION_FILE`.
  A hard/unsafe calibration error **aborts the start**; a soft miss (no doc, signal
  absent) falls back to the script's baked constants.
- The artifact is the operating-plane curve pre-flattened (derived hops folded in):
  `{ curve: [[gain, power], …], min_gain_db, max_gain_db, amplitude, quantity,
  operating_plane, … }`.
- The script uses `PowerMap` from **paramkit** (`from paramkit import PowerMap`;
  the calibration consumer lives in `paramkit/calkit.py` so it ships with paramkit on
  `PYTHONPATH=/opt/sdr-agent` and is importable on every unit, no extra deploy step):
  `PowerMap.load(baked)` returns the artifact-backed map when `SDR_CALIBRATION_FILE`
  is set, else a `from_linear` map built from the baked constants that is byte-
  identical to the old single-anchor behaviour. `--power` ↔ gain both route through
  it, as does the flowgraph amplitude.
- **Amplitude gate.** A script transmits at a **fixed baseband amplitude** (its baked
  `AMPLITUDE`, not a task parameter — the operator never sets it), and its calibration
  is only valid at the amplitude the curve was measured at. `PowerMap.load` compares the
  two: on a match it uses the calibration; on a **mismatch** the calibrated power scale
  no longer describes this script, so it **falls back to the baked (uncalibrated) map and
  warns loudly** (a `warning` on the map, a logged WARNING, and a `⚠ CALIBRATION` banner
  line) — never a silent switch to a wrong scale. Re-running calibration at the script's
  amplitude restores it. (The Pi broadcaster scripts fix this at `AMPLITUDE = 0.5`.)
- The banner echoes the map's **source** and the operating plane's `quantity`, so the
  number is never ambiguous, e.g. `power: -12.0 dBm (EIRP, at antenna_eirp)` +
  `calibration: calibration file`.
- Cross-repo consistency is covered by construction: the agent resolver and
  `calkit.PowerMap` produce identical gain↔power over the whole chain.

Not yet done: the client auto-populating `SDR_CAL_SIGNAL_ID` on task creation from
the script's `--describe-params` (today it's set by hand), and porting the remaining
scripts beyond M-code.

### 9.2 The store *(implemented)*

The per-unit data area is deliberately general — calibration is the first tenant,
not the only one.

- A per-unit directory the agent owns — `config.DATA_DIR` (`$SDR_DATA_DIR`, default
  `STATE_DIR/data`), distinct from `configs/` (fleet state) and `scripts/` (code).
  `calibration.json` is the reserved, validated name in it; other files sit beside it.
- Agent endpoints (all behind the API key):
  - `POST /files` (multipart) — upload a file. Validated per known kind *before* it
    is written; **executable kinds are refused** and nothing in the store is ever
    executed; 5 MiB cap.
  - `GET /files` — list (name, size, modified); `GET /files/{name}` — fetch;
    `DELETE /files/{name}` — remove. All reject path traversal.
  - `GET /calibration` — convenience view for a FleetView panel: the stored document
    plus what it resolves to per signal (operating plane, quantity, gain/power
    bounds), or `{valid:false, error}` if the stored doc is broken.
- **Validate-on-upload:** uploading `calibration.json` runs
  `calibration.validate_document` — the full §6 hard-checks for **every signal**, as
  it would resolve at runtime (merged with this unit's type defaults) — so a broken
  or unsafe document is rejected at upload with a specific reason, never at transmit.
  The success response returns the per-signal resolved summary.
  - **A signal-less document is accepted** (onboarding: a unit is wired up and its
    chain + safety ceiling characterised *before* any signal is measured). It is
    validated by the **curve-independent** subset of the checks
    (`validate_chain_structure`): plane topology, each derived hop (inline `delta_db`
    or a catalog `component` and its frequency table), the operating plane's
    existence, limit plane references, and that a ceiling is *declared*. Curve-based
    checks (operating-plane usability, a *derivable* ceiling) simply have nothing to
    run on yet, and nothing can transmit until a signal is added — `resolve` raises
    `SignalNotCalibrated` for an absent signal — so persisting the skeleton is safe.
    The success response's per-signal summary is then empty (`{}`).
- **Discipline that keeps it from rotting:** the area accepts arbitrary *data* files,
  but every *kind* earns a small schema/validator when it's introduced (calibration
  is the first). No executable uploads; a computed calibration is expressed as a
  table, never as code.

FleetView has a Calibration sub-tab over these endpoints: a resolved per-signal
summary plus a two-view editor — an **Editor** tab (forms for gain limits, the
limits list, and per-(signal × measured-plane) curve grids) and a **JSON** tab (the
raw document, source of truth for plane topology). Save validates through the agent
and surfaces any rejection reason. Still form-only for values — editing the plane
chain (adding measured/derived planes) is done in the JSON view.

---

## 10. Open items

- **Curve interpolation** is `linear` only for now; the `interp` enum leaves room
  for a monotone spline later if linear segments prove too coarse between points.
- **`broadcaster` type defaults** (§5.2) are the only section defined initially;
  other unit types are added to `calibration_defaults.yaml` as they're calibrated —
  no agent change (§7.1).
- **EIRP quantity/units** — `power_dbm` assumes dBm throughout; if EIRP is ever
  wanted in dBW or as ERP, that's a per-plane unit tag, not a structural change.
- **FleetView editor** — the file/upload store (§9.2) is the foundation; a
  point-and-click curve editor can layer on top later.

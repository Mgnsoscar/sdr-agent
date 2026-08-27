"""
Loads agent configuration from environment variables and tasks.yaml.
"""
from __future__ import annotations

import os
import yaml
import logging
from pathlib import Path
from typing import Dict

from .models import TaskConfig

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────

BASE_DIR   = Path(os.environ.get("SDR_AGENT_BASE", "/opt/sdr-agent"))
# State (configs, logs, runtime) lives in STATE_DIR, decoupled from the code so an
# OTA update — which replaces the code dir — never touches tasks/sequences/plans/
# logs. Defaults to BASE_DIR, so a classic single-dir install is unchanged; a
# versioned OTA install sets SDR_STATE_DIR to a shared dir outside the release.
STATE_DIR  = Path(os.environ.get("SDR_STATE_DIR", BASE_DIR))
TASKS_YAML = Path(os.environ.get("SDR_TASKS_FILE", STATE_DIR / "configs" / "tasks.yaml"))
LOG_DIR    = Path(os.environ.get("SDR_LOG_DIR",   STATE_DIR / "logs"))
EVENTS_FILE = Path(os.environ.get("SDR_EVENTS_FILE", STATE_DIR / "configs" / "events.json"))
SEQUENCES_FILE = Path(os.environ.get("SDR_SEQUENCES_FILE", STATE_DIR / "configs" / "sequences.json"))
SEQUENCE_RUNS_FILE = Path(os.environ.get("SDR_SEQUENCE_RUNS_FILE", STATE_DIR / "configs" / "sequence_runs.json"))
PLANS_FILE = Path(os.environ.get("SDR_PLANS_FILE", STATE_DIR / "configs" / "plans.json"))
SCHEDULE_FILE = Path(os.environ.get("SDR_SCHEDULE_FILE", STATE_DIR / "configs" / "schedule.json"))
# Per-run control sockets for live-parameter tuning (paramkit.live). Kept short —
# AF_UNIX paths are capped at ~108 bytes — and outside configs/ since they're
# ephemeral runtime state, not saved config.
CTRL_DIR   = Path(os.environ.get("SDR_CTRL_DIR", STATE_DIR / "run" / "ctl"))

# ── Per-unit data store ───────────────────────────────────────────────────────
# A per-unit area for arbitrary unit-specific files (calibration is the first
# tenant; see docs/calibration.md §9.2). Distinct from configs/ (fleet-managed
# state) and scripts/ (code). Uploaded via the /files API, validated per known kind.
DATA_DIR   = Path(os.environ.get("SDR_DATA_DIR", STATE_DIR / "data"))

# ── Power calibration (see docs/calibration.md) ───────────────────────────────
# The per-unit calibration document (this box's measured curves) lives in the data
# store; the shared, type-keyed defaults it merges over live in configs/ (they're
# fleet state, not per-unit). CAL_RUN_DIR holds the ephemeral per-task RESOLVED
# artifact the agent injects.
CALIBRATION_NAME     = "calibration.json"      # reserved, validated name in DATA_DIR
CALIBRATION_DOC      = Path(os.environ.get("SDR_CALIBRATION_DOC",
                                           DATA_DIR / CALIBRATION_NAME))
CALIBRATION_DEFAULTS = Path(os.environ.get("SDR_CALIBRATION_DEFAULTS",
                                           STATE_DIR / "configs" / "calibration_defaults.yaml"))
# The component catalog (cables / antennas / pads characterized once as a
# loss-vs-frequency table) a per-unit chain references. Authored on the client and
# uploaded to each unit's data store alongside calibration.json (a reserved, validated
# name), so it travels through the same /files path. Absent → no catalog (only inline
# delta_db hops resolve). See docs/calibration-v2.md.
CALIBRATION_COMPONENTS_NAME = "components.yaml"      # reserved, validated name in DATA_DIR
CALIBRATION_COMPONENTS = Path(os.environ.get("SDR_CALIBRATION_COMPONENTS",
                                             DATA_DIR / CALIBRATION_COMPONENTS_NAME))
CAL_RUN_DIR          = Path(os.environ.get("SDR_CAL_RUN_DIR", STATE_DIR / "run" / "cal"))
# A task opts into calibration by setting this env to its script's CAL_SIGNAL_ID.
# When present (and a calibration doc exists) the agent resolves it and points the
# task at the resolved artifact via CALIBRATION_FILE_ENV; the script reads that and
# maps --power (dBm) → gain, falling back to its baked defaults if the var is absent.
CAL_SIGNAL_ID_ENV    = "SDR_CAL_SIGNAL_ID"
CALIBRATION_FILE_ENV = "SDR_CALIBRATION_FILE"
# Optional: the task's transmit centre frequency in Hz. When set (the client sources
# it from the script's CAL_FREQ_PARAM), the agent folds the artifact's v1-compat curve
# and scalar bounds at this frequency; a frequency-aware script still re-folds per its
# live frequency from the artifact's passive_hops. See docs/calibration-v2.md.
CAL_FREQ_HZ_ENV      = "SDR_CAL_FREQ_HZ"

# ── OTA update layout ─────────────────────────────────────────────────────────
# Release dirs live under RELEASES_DIR as <version>/ (the code), and BASE_DIR is a
# symlink to the active one that the updater flips atomically. OTA markers live in
# RELEASES_DIR/.markers (never inside a replaced release). All three are outside
# the code dir, so they survive an update. A classic install leaves BASE_DIR a
# plain dir and simply never uses these.
RELEASES_DIR = Path(os.environ.get("SDR_RELEASES_DIR", str(BASE_DIR) + "-releases"))
CURRENT_LINK = Path(os.environ.get("SDR_CURRENT_LINK", BASE_DIR))
# The systemd unit the agent restarts to load a freshly-activated release.
SERVICE_NAME = os.environ.get("SDR_SERVICE_NAME", "sdr-agent")
# The agent marks a freshly-activated release healthy after serving this long…
UPDATE_CONFIRM_DELAY_S = float(os.environ.get("SDR_UPDATE_CONFIRM_DELAY_S", "30"))
# …and the external confirm timer rolls it back if it's still unconfirmed after
# this long (larger than the confirm delay, so a healthy agent always wins the race).
UPDATE_HEALTH_GRACE_S = float(os.environ.get("SDR_UPDATE_HEALTH_GRACE_S", "90"))

# ── Agent identity ────────────────────────────────────────────────────────────

import socket
HOSTNAME   = socket.gethostname()
UNIT_ID    = os.environ.get("SDR_UNIT_ID", HOSTNAME)
# This unit's kind (e.g. "broadcaster"). Selects the calibration type-defaults
# layer (docs/calibration.md §7.1). Empty = no type layer; the per-unit doc stands
# alone. The agent itself is type-agnostic — this is just data it reads.
UNIT_TYPE  = os.environ.get("SDR_UNIT_TYPE", "")


def machine_id() -> str:
    """A stable, unique identifier for this physical machine, from
    /etc/machine-id (generated once at OS install; survives hostname changes and
    reboots). Empty string if it can't be read. This is the client's reliable
    fingerprint for 'the same Pi', independent of hostname/IP/label."""
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            v = Path(p).read_text(encoding="utf-8").strip()
            if v:
                return v
        except OSError:
            continue
    return ""


MACHINE_ID = machine_id()

# ── HTTP server ───────────────────────────────────────────────────────────────

AGENT_HOST    = os.environ.get("SDR_AGENT_HOST", "0.0.0.0")
AGENT_PORT    = int(os.environ.get("SDR_AGENT_PORT", "8765"))
# Bump on any change that alters the agent's HTTP surface, so the OTA updater and the
# client's "Update agent…" flow (which compare version strings) can tell builds apart
# and actually install the new code. 1.1.0 adds the per-unit file store + /calibration;
# 1.1.1 surfaces a script's CAL_SIGNAL_ID in /scripts/{name}/params; 1.1.2 aligns
# calibration upload-validation and the /calibration view with transmit-time
# unit_type resolution, and hardens the upload size cap; 1.1.3 rejects a
# duration+hold ramp too short to hold two levels (a single step, not a ramp);
# 1.1.4 advertises capabilities in /info so the client can feature-gate explicitly;
# 1.1.5 confirms a freshly-activated OTA release before the slow startup steps (so a
# slow boot can't get a good update rolled back) and exposes /admin/update-status;
# 1.1.6 lets a task name contain '/' (routes use {name:path}; log/socket paths are
# sanitised + hash-disambiguated and can no longer traverse); 1.1.7 rejects arming a
# sequence when one of its tasks is already running, or when its on-air window overlaps
# a run already armed/running on this unit (one TX channel — was a device-busy crash);
# 1.1.8 moves the read-only task sub-resources (logs, history, live-params, log stream)
# to their own /task-* prefixes with the name as the terminal segment, so a task name
# containing '/' can never be misrouted to a shorter name's sub-resource;
# 1.1.9 adds POST /calibration/validate — a dry-run that validates a document without
# storing it, so the editor can preview what it resolves to before Save;
# 1.2.0 adds calibration v2 (docs/calibration-v2.md): a derived plane may reference a
# shared component catalog (components.yaml — a reserved, validated file in the /files
# store) whose cable/antenna loss is a Δ dB-vs-frequency table, resolved at the transmit
# frequency; the resolved artifact carries the passive-hop tables + a frequency-split
# ceiling so the script folds --power at its live frequency. Inline delta_db and every
# v1 document keep resolving unchanged.
# 1.3.0 adds partial measured stages: a signal missing the curve for a non-first measured
# stage inherits the nearest upstream measured curve (a transparent +0 dB hop) instead of
# being rejected — so a downstream measured plane can be added for a signal or two without
# re-measuring the rest. Such a document RESOLVES here but is REJECTED by ≤1.2.0 agents,
# so the client gates on the calibration-partial-stages capability below.
# 1.4.0 accepts a signal-less calibration document (upload/validate/view): a unit's chain
# and safety ceiling can be saved during onboarding, before any signal is measured. The
# curve-independent structure is validated; a broken chain is still rejected; nothing can
# transmit until a signal is added. Such a document is REJECTED by ≤1.3.0 agents ("document
# has no signals"), so the client gates on the calibration-no-signals capability below.
# 1.5.0 lets a safety limit choose the stage boundary it applies at: `side: "input"` caps
# the plane feeding the named stage (one hop upstream), `side: "output"` (default) the plane
# itself. An input-protection limit (e.g. an amp's max input power) then follows its stage
# when a component is inserted upstream, instead of naming a fixed plane that detaches. A
# ≤1.4.0 agent IGNORES `side` and would mis-apply the cap at the output, so the client gates
# on the calibration-limit-side capability below (a safety gate, not just a feature gate).
# 1.5.1 (bundle-only, no HTTP-surface change) — calkit now enforces an amplitude gate: a
# transmit script drives a FIXED baseband amplitude, and its injected calibration is honoured
# only when measured at that same amplitude; a mismatch falls back to uncalibrated (baked)
# levels with a loud warning instead of transmitting on an invalid power scale. calkit ships
# in the OTA bundle, so the version bumps to propagate it to units.
# 1.5.2 (bundle-only, no HTTP-surface change) — calkit drops the baked dBm fallback entirely:
# with no valid calibration it returns an "uncalibrated" map that refuses --power (NoAbsoluteScale)
# rather than inventing levels, so a script maps absolute power only on a real measured curve and
# otherwise runs on a relative gain. Also ships in the OTA bundle, so bump to propagate it.
# 1.6.0 gives a measured plane a `role`: `limiting` (default — safety limits invert through it)
# or `reported` (a re-measurement of the same node in a different quantity, e.g. main-lobe vs
# full-band, that `of:` names). A reported plane is what --power shows the operator but is
# INVISIBLE to limit inversion — the limit walk punches through it to the limiting curve — so a
# limit is always gauged in its own quantity while the operator sees the region of interest. A
# ≤1.5.2 agent doesn't understand `role`/`of` and would treat a reported plane as an ordinary
# limiting one (mis-gauging the ceiling), so the client gates on the calibration-plane-roles
# capability below (a safety gate). The validate summary now also reports each limit's resolved
# gauge plane + quantity.
# 1.6.1 extends the partial-measured-stage fallback to reported stages: a signal not measured
# at a reported stage passes straight through to the upstream (limiting) curve — it reports the
# upstream quantity for that signal instead of the save being rejected — while safety limits are
# unaffected (they already gauge on that upstream curve). No new capability; reported stages are
# already gated on calibration-plane-roles.
# 1.7.0 adds chain.gain_limits.gain_step_db: the SDR settles on a discrete gain grid, so the
# commanded gain is snapped to the nearest step on that grid (never above the safety ceiling —
# it floors there), and the reported power reflects the snapped gain. Both the resolver and the
# script-side calkit (in the OTA bundle) snap, so they agree. A ≤1.6.1 agent ignores the field
# and would command an off-grid gain the SDR silently rounds, so the client gates on the
# calibration-gain-step capability below.
# 1.7.1 makes a signal's center_freq_hz OPTIONAL on a frequency-dependent chain: the transmit
# frequency is a runtime quantity (the task's --freq / CAL_FREQ_PARAM), so instead of rejecting
# a document with no center_freq_hz the resolver derives a representative one — the tightest-
# ceiling breakpoint when a frequency-dependent SAFETY limit exists (so a v1 script folding no
# frequency of its own still can't exceed a per-frequency limit), else the midpoint of the
# chain's breakpoints. A ≤1.7.0 agent still rejects such a document at validate, so the client
# gates on the calibration-freq-optional-center capability below before allowing an empty
# center-frequency field on a frequency-dependent chain.
# 1.7.2 carries the full resolved artifact (v1 curve + v2 anchor/passive-hops/limits) in the
# /calibration view's per-signal summary, and surfaces a script's CAL_FREQ_PARAM in
# /scripts/{name}/params as calibration_freq_param. Together these let the client re-fold a
# signal's --power range at the frequency the operator picks in the Run/sequence form — the
# same fold calkit does at transmit — instead of showing only the representative-frequency
# range. Purely additive; a client gates the re-fold on the calibration-summary-artifact
# capability below.
# 1.7.3 emits the v2 artifact fields (anchor_curve / passive_hops / freq_dependent_limits /
# gain_ceiling_db / center_freq_hz) whenever the operating point moves with frequency — not
# only when the operating plane sits behind passive hops, but also when a frequency-dependent
# safety LIMIT tightens the ceiling per frequency while the operating plane is MEASURED (no
# hops). Without this the artifact was v1-only in that topology and a consumer (the client's
# form re-fold, or a v2 script) couldn't track the max power as the frequency changed, even
# though it really moves. Purely additive; no new capability.
# 1.7.4 supports a frequency-dependent limit whose OPERATING plane is 'reported' (its observed
# curve differs from the limiting curve the limit gauges on — same physical node, two
# quantities, e.g. main-lobe operating point + full-band amp-output limit). Previously refused
# ("frequency-dependent limits combined with a 'reported' operating plane aren't supported
# yet"). Now the resolver accepts it and the artifact publishes that limit's own limiting
# curve (per-limit anchor_curve in freq_dependent_limits) so a consumer inverts the limit
# against the right curve; calkit (OTA bundle) and the client's PowerFold both honour it. The
# scalar bounds were always correct; only the save-time refusal and the v2 publish changed.
AGENT_VERSION = "1.7.4"

# Feature flags this agent's HTTP surface supports, reported by GET /info so the
# client can light features up (or say "needs a newer agent") from an explicit list
# instead of probing each endpoint and inferring support from a 404. Add a flag when
# a new capability ships; never remove or rename one (the client matches by string).
AGENT_CAPABILITIES = [
    "calibration",        # per-unit power calibration: /files store + /calibration view
    "script-cal-signal",  # /scripts/{name}/params reports a script's CAL_SIGNAL_ID
    "ota-status",         # /admin/update-status reports the OTA confirm/rollback state
    "cal-validate",       # POST /calibration/validate dry-runs a document without storing
    "calibration-components",  # v2: a derived plane may reference a components.yaml entry
                               # (Δ dB-vs-frequency); resolved per transmit frequency
    "calibration-partial-stages",  # a signal may omit the curve for a non-first measured
                                   # stage and inherit the nearest upstream measured curve
    "calibration-no-signals",      # a signal-less document (chain + ceiling only) is
                                   # accepted, for onboarding before any signal is measured
    "calibration-limit-side",      # a limit may set side: input/output to apply at a
                                   # stage's input (one hop upstream) vs its output plane
    "calibration-plane-roles",     # a measured plane may set role: limiting/reported; a
                                   # reported plane (of: a limiting plane) is shown to the
                                   # operator but invisible to limits (they punch through it)
    "calibration-gain-step",       # chain.gain_limits.gain_step_db snaps the commanded gain
                                   # to the SDR's discrete gain grid (never above the ceiling)
    "calibration-freq-optional-center",  # center_freq_hz is optional on a frequency-dependent
                                         # chain: the resolver derives a representative (worst-
                                         # case) frequency when it's absent, since --freq supplies
                                         # the real transmit frequency at runtime
    "calibration-summary-artifact",      # the /calibration view's per-signal summary carries the
                                         # full resolved artifact, so the client re-folds the
                                         # --power range at the operator's chosen frequency
    "calibration-freq-limit-reported",   # a frequency-dependent limit is allowed with a
                                         # 'reported' operating plane: the limit inverts against
                                         # its own published limiting curve (per-limit anchor)
]

# The interpreter tasks should launch with, reported to the client so it pre-fills
# task defaults. "python3" (the default) resolves via PATH at launch — on the X410
# that's the system python3 with UHD, distinct from the agent's own bundled python.
# Override with SDR_TASK_INTERPRETER only if tasks need a specific interpreter path.
TASK_INTERPRETER = os.environ.get("SDR_TASK_INTERPRETER", "python3")

# ── Auth (optional shared secret) ────────────────────────────────────────────
# Set SDR_API_KEY on both the Pi and your client. Leave empty to disable auth.
#
# TRUST MODEL: the agent speaks plain HTTP and is designed for a TRUSTED LAN (a lab
# bench / isolated network). The API key is compared in constant time (see
# main.verify_key), but because traffic is not encrypted it travels in cleartext — so
# the key guards against casual access on the LAN, NOT against an attacker who can
# sniff the wire. Do not expose the agent to an untrusted network or the public
# internet; put it behind a VPN or a TLS-terminating reverse proxy if you must.

API_KEY = os.environ.get("SDR_API_KEY", "")


# ── Task registry ─────────────────────────────────────────────────────────────

def _env_str(v) -> str:
    """Coerce a YAML-parsed env value back to the string env always is. Bools and
    null get conventional forms rather than Python's 'True'/'None'."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return ""
    return str(v)


def load_tasks() -> Dict[str, TaskConfig]:
    """Parse tasks.yaml and return a dict keyed by task name."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if not TASKS_YAML.exists():
        logger.warning("tasks.yaml not found at %s — no tasks registered", TASKS_YAML)
        return {}

    try:
        with TASKS_YAML.open() as fh:
            raw = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        logger.error("tasks.yaml is not valid YAML (%s) — registering no tasks", exc)
        return {}

    tasks: Dict[str, TaskConfig] = {}
    # `raw.get("tasks", [])` is NOT enough: a file with a bare `tasks:` line (no
    # entries) parses that key as None, and `.get` only returns the default when the
    # key is ABSENT — so None slips through and iterating it crashes agent startup
    # (a crash-loop on a freshly-seeded unit). `or []` treats null/empty as "no tasks".
    for entry in (raw.get("tasks") or []):
        try:
            if isinstance(entry, dict) and isinstance(entry.get("env"), dict):
                # Env is all strings, but YAML may have parsed a value like `on` or
                # `8080` as a bool/int (e.g. a hand-edited or legacy file). Coerce
                # so a stray type never drops the whole task.
                entry = {**entry, "env": {str(k): _env_str(v)
                                          for k, v in entry["env"].items()}}
            task = TaskConfig(**entry)
            tasks[task.name] = task
        except Exception as exc:
            logger.error("Skipping malformed task entry %s: %s", entry, exc)

    logger.info("Loaded %d task(s) from %s", len(tasks), TASKS_YAML)
    return tasks
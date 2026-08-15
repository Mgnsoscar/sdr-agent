# Provisioning & OTA updates — design spec

> **Phase 1 (OTA update of known units) is implemented** — agent side in
> [§11](#11-phase-1--implemented-agent-side); the client "Update agent…" button ships
> too. **Phase 2 (SSH bootstrap provisioning) is implemented** — see
> [§12](#12-phase-2--implemented). Phase 3 (base image) is still design-only.

**Status:** design / spec. For review.
**Scope decisions (from discussion):** support both Bookworm (NetworkManager) and
Bullseye (dhcpcd) by detecting the stack at provision time; provisioning access is
by **SSH user + password**; blank‑SD‑card imaging stays in Raspberry Pi Imager.

---

## 1. Goal

Two related capabilities, driven from the FleetView client:

1. **OTA update** — push a new agent build to a unit that is already running the
   agent and reachable (known IP/mDNS). One click per unit; safe against bricking.
2. **Provision a new Pi by IP** — take a fresh Pi that is on the network (SSH on,
   no agent yet), install the agent, and configure it for the fleet:
   - hostname `broadcaster-<N>`,
   - **static** IPs on Ethernet **and** WiFi derived from the unit number `N`,
   - unit id / API key wired into the service,
   - enable + start the agent, reboot, and register it in the client.

These share a "bundle" (the agent payload) but are otherwise independent; see the
phasing in §9. **Recommendation: build Feature 1 first** — highest value, lowest
risk, no SSH.

---

## 2. What already exists (the foundation)

- **Versioning:** `agent/config.py:AGENT_VERSION` is reported in `GET /info`
  (`AgentInfo.agent_version`) and advertised over mDNS. The client already learns
  it, so "current vs available" is basically free.
- **systemd:** `sdr-agent.service` runs `uvicorn agent.main:app` from
  `/opt/sdr-agent`, `Restart=on-failure`, `RestartSec=5`. So an update just has to
  swap files and restart the unit; systemd re-launches it.
- **Installer:** `install.sh` already encodes the whole install (copy
  `agent/ scripts/ configs/ paramkit/ requirements.txt` → `/opt/sdr-agent`, apt +
  pip deps, install+enable+restart the service). Both features reuse this logic.
- **Config via env:** `SDR_AGENT_BASE`, `SDR_UNIT_ID`, `SDR_API_KEY`,
  `SDR_TASKS_FILE`, … all read from the environment; `HOSTNAME = socket.gethostname()`.
  Provisioning just writes the service env + OS config.
- **Identity:** units carry a permanent `unit_id`, a display `label`
  (e.g. "Broadcaster 1"), and resolve at `broadcaster-<N>.local`. "Unit number" `N`
  is the natural key for hostname + IP derivation.

The important gap for updates: **configs must survive an update.** `install.sh`
currently does `rm -rf /opt/sdr-agent` (a wipe). The OTA path must preserve
`configs/` and `logs/` (tasks.yaml, sequences, plans, schedule, events). See §3.4.

---

## 3. Feature 1 — OTA update of a known unit

### 3.1 Flow

```
Client                                  Agent (running, /opt/sdr-agent)
  │  POST /admin/update  (bundle)         │
  │─────────────────────────────────────▶│  stage → verify → install deps
  │  202 Accepted {staged_version}        │  into /opt/sdr-agent-<ver>/
  │◀─────────────────────────────────────│
  │                                       │  flip `current` symlink, schedule
  │                                       │  systemd restart (after replying)
  │  poll GET /info until                 │  ── agent restarts ──
  │  agent_version == staged_version      │  new version boots, health OK
  │◀───────────── /info (new version) ────│
  │  (or: timeout → agent auto-rolled     │  health-check fails → revert
  │       back → /info shows old version) │  symlink → restart old version
```

### 3.2 Layout change (versioned installs + a `current` symlink)

Move from a single `/opt/sdr-agent` dir to versioned dirs behind a symlink so a
swap is atomic and reversible:

```
/opt/sdr-agent            → symlink to the active release
/opt/sdr-agent-1.0.0/     agent/ scripts/ paramkit/ requirements.txt   (code only)
/opt/sdr-agent-1.1.0/     …
/opt/sdr-agent-shared/    configs/  logs/  run/     (state — never in a release dir)
```

- The systemd `WorkingDirectory`/`PYTHONPATH` stay `/opt/sdr-agent` (the symlink).
- `SDR_*` file paths point at `…-shared/` (via service env) so state is decoupled
  from code and untouched by updates. This is the key change that makes updates
  non‑destructive.
- Rollback = flip the symlink back to the previous release dir and restart. Keep
  the **last 2–3** releases; prune older.

> This is a one‑time migration of the on‑disk layout. `install.sh` gets a
> `--migrate` path (or the first OTA does it): create `…-shared/`, move existing
> `configs/`+`logs/` into it, lay the current code down as `…-<AGENT_VERSION>/`,
> point the symlink at it.

### 3.3 Agent endpoints

- `POST /admin/update` (auth: `SDR_API_KEY`) — body is the bundle (see §5).
  1. Write to a temp dir; verify checksum + that it contains `agent/` and a
     `VERSION`/version string; refuse if malformed.
  2. `unpack → /opt/sdr-agent-<ver>-staging/`, run dep install
     (`pip … -r requirements.txt`), non‑destructive.
  3. Record `previous = readlink(current)`, flip `current → …-<ver>`.
  4. Reply `202 {ok, from_version, to_version}` **then** trigger restart
     (a background task / `systemd-run --on-active=2s systemctl restart sdr-agent`,
     so the HTTP response flushes first).
- `POST /admin/rollback` (auth) — flip to `previous`, restart. Manual escape hatch.
- `GET /admin/releases` (auth) — list installed release dirs + which is `current`.
- Extend `GET /info` with `previous_version` and maybe `updated_at`.

### 3.4 Safety — the part that matters most

Remote code replacement can brick a headless unit, so:

- **Non‑destructive:** never `rm -rf` the live dir; state lives in `…-shared/`.
- **Health‑check + auto‑rollback:** after the swap+restart, a tiny **supervisor**
  decides if the new version is healthy. Two viable designs:
  - *systemd‑native:* `Restart=on-failure` + a `ExecStartPost` health probe that
    curls `/info`; if uvicorn crashes on the new code, systemd restarts, and after
    `StartLimit` failures a `OnFailure=` unit flips the symlink back. Robust, no
    extra daemon.
  - *marker file:* the newly‑booted agent writes `…-shared/health.ok` once it has
    served `/info` cleanly for ~30 s. A oneshot `sdr-agent-confirm.timer` checks:
    if the marker for `current` is missing N seconds after a swap, revert. Simpler
    to reason about; one extra timer unit.
  I lean **marker file** — it survives a hard uvicorn hang (not just a crash),
  which the systemd‑only approach misses.
- **Version gate:** refuse to "update" to an identical or older version unless
  `force=true`.
- **Single‑flight:** reject a second update while one is in progress.
- **Disk guard:** check free space before staging (each release is small, but be safe).

### 3.5 Client side

- A **bundle** is produced from the agent repo (`make bundle` / a script that tars
  the release files + a `VERSION` + a manifest checksum). The client ships with, or
  is pointed at, a bundle file.
- Unit card: show `agent_version`; if it differs from the bundled version, offer
  **Update**. Confirm dialog → `POST /admin/update` → progress → poll `/info` until
  the version bumps (success) or a timeout with the old version still showing
  (rolled back → show the failure).
- **Fleet update:** "Update all" iterates units **one at a time** (never brick the
  whole fleet at once), stopping on the first failure.

---

## 4. Feature 2 — provision a new Pi by IP (SSH bootstrap)

For a Pi that is on the network with SSH enabled but no agent. Chosen access model:
**SSH username + password** (prompted in the client; optionally remembered per
session, not written to disk in plaintext).

### 4.1 Inputs (a "Provision unit" dialog)

- IP address (or `broadcaster-N.local`).
- Unit number `N` → derives hostname + IPs.
- SSH user + password (+ sudo password if different).
- WiFi SSID + PSK (for the wlan static profile / to keep WiFi up).
- API key for the fleet.
- The IP scheme (defaults below; editable, stored in client config).

### 4.2 Steps (over SSH; paramiko — pure‑Python, cross‑platform)

1. **Connect & sanity‑check:** SSH in; confirm it's a Pi (`/etc/rpi-issue` or
   `uname -m`), sudo works, python3 present.
2. **Copy the bundle:** `scp`/SFTP the same bundle from §3.5 to `/tmp`.
3. **Install the agent:** run `install.sh` (adjusted for the versioned layout in
   §3.2) under sudo. Writes `SDR_UNIT_ID`/`SDR_API_KEY` into the service env drop‑in
   (`/etc/systemd/system/sdr-agent.service.d/override.conf`), not the unit file.
4. **Hostname:** `hostnamectl set-hostname broadcaster-<N>` + fix `/etc/hosts`.
5. **Static IPs (detect the stack):**
   - If `nmcli` exists and NetworkManager is active → write connection profiles
     with `nmcli con mod … ipv4.addresses … ipv4.method manual` for eth0 and wlan0.
   - Else if `/etc/dhcpcd.conf` is in use → append `interface eth0 / static ip_address=…`
     blocks (and the wlan0 equivalent), keep a backup.
   - Detection is a probe run at the start of provisioning; both writers are small.
6. **Apply last + reboot:** network changes go **last**, then `reboot`. See §4.4.
7. **Verify:** after the reboot window, the client reconnects at the **new** static
   IP, polls `/info`, and registers the unit (its `unit_id`/`label`).

### 4.3 Unit‑number → hostname/IP scheme (deterministic, client‑side)

Default (all editable in client config):

| Field        | Formula                | Example (N=2)     |
|--------------|------------------------|-------------------|
| hostname     | `broadcaster-<N>`      | `broadcaster-2`   |
| Ethernet IP  | `10.0.0.<N>/24`        | `10.0.0.2`        |
| WiFi IP      | `10.0.1.<N>/24`        | `10.0.1.2`        |
| gateway/DNS  | fixed per subnet       | `10.0.0.1` / …    |

The client computes these from `N` and shows them for confirmation before writing.
`N` also seeds `SDR_UNIT_ID` (e.g. `broadcaster-2`) and the display label.

### 4.4 The re‑IP gotcha (must design around)

Changing an interface's IP **drops the SSH session you're using**. Rules:

- Apply hostname + agent install first (safe), network config **last**, then reboot
  and reconnect at the new IP — don't try to keep the live session across the change.
- Prefer to run provisioning over a **different** path than the interface being
  re‑IP'd (e.g. provision over WiFi while setting eth0's static, or vice‑versa). If
  only one path exists, accept that the SSH session ends at reboot and resume by
  reconnecting to the computed static IP.
- Keep a **backup** of the prior network config on the Pi and a short
  "if `/info` isn't reachable at the new IP within T, the operator can re‑image"
  fallback (a headless Pi can't self‑heal a bad static IP without a keyboard).

### 4.5 Blank Pi (out of app scope, documented)

Don't build SD‑card imaging into the PyQt app (raw disk access, per‑OS disk
enumeration, admin elevation — Raspberry Pi Imager already does this well).
Instead document a **base image**: flash Raspberry Pi OS with Imager, pre‑seed
SSH‑on + user/password (+ WiFi so it joins the network). Then Feature 2 does the
rest over SSH. Optionally ship a `custom.toml`/`firstrun` snippet the operator
pastes into Imager's advanced options.

---

## 5. The bundle (shared artifact)

- A `.tar.gz` of `agent/ scripts/ paramkit/ requirements.txt` + a top‑level
  `VERSION` + a `MANIFEST` (sha256 of each file). Small (KBs–low MBs).
- Built from the agent repo by a script (`scripts/build_bundle.sh`) so a release is
  reproducible and checksummed. The client verifies the checksum before/at upload;
  the agent verifies again before staging.
- Same bundle feeds both the OTA endpoint (§3) and the SSH bootstrap (§4).

---

## 6. Security

- `/admin/*` endpoints require `SDR_API_KEY` (already the fleet auth); reject
  unauthenticated calls. This is remote code execution by design — treat it that way.
- Bundle integrity: checksum + shape validation; optionally sign the bundle and pin
  a public key on the agent for defence‑in‑depth.
- SSH password handling: prompt, hold in memory for the provisioning run, never
  persist in plaintext; offer to switch the unit to key‑based auth as a follow‑up.
- Provisioning writes network + service config as root; log every step and keep
  backups so a failed run is diagnosable.

---

## 7. Data‑model / config additions

- **Agent:** `AgentInfo.previous_version` (+ maybe `updated_at`); `/admin/update`,
  `/admin/rollback`, `/admin/releases`; the versioned‑layout migration.
- **Client config:** the IP scheme (subnets/gateway/base), default SSH user, WiFi
  SSID, bundle path/location; per‑unit `N`.
- **Client state:** provisioning is a one‑shot action, not persistent state, but the
  learned unit (uid/label/addresses) is registered as today.

---

## 8. Risks & mitigations (summary)

| Risk | Mitigation |
|------|-----------|
| Update bricks a headless unit | Versioned installs + `current` symlink + marker‑file health‑check + auto‑rollback; keep N‑1. |
| Update wipes tasks/plans/logs | Move state to `…-shared/`; updates touch code dirs only. |
| Re‑IP drops the provisioning session | Network change last → reboot → reconnect at computed IP; provision over the other interface when possible. |
| Bad static IP strands a headless Pi | Config backup on device; documented re‑image fallback; confirm computed IPs before writing. |
| Mixed OS (NM vs dhcpcd) | Detect the stack at provision time; two small writers. |
| Unauthorized code push | `SDR_API_KEY` on `/admin/*`; bundle checksum (optional signing). |
| Fleet‑wide bad push | Update one unit at a time; stop on first failure. |

---

## 9. Suggested phasing

1. **Phase 1 — OTA update (known units).** Versioned‑layout migration + `…-shared/`
   state + `/admin/update` / `/admin/rollback` / `/admin/releases` + marker‑file
   rollback; bundle builder; client Update button + version display + one‑at‑a‑time
   "Update all". *No SSH. Biggest value, lowest risk.*
2. **Phase 2 — SSH bootstrap provisioning (by IP).** Provision dialog, paramiko
   flow, hostname + stack‑detecting static‑IP writers, unit‑number scheme, re‑IP
   ordering, post‑reboot verify + register.
3. **Phase 3 — base‑image + docs.** A documented base image + Imager pre‑seed so a
   blank Pi is "flash base → Provision in the UI."

---

## 10. Open questions

- Subnets/gateway/DNS for the eth + wlan static scheme (defaults in §4.3 are
  placeholders).
- Do units have internet on‑device? If yes, the OTA bundle could be a git ref /
  release URL the agent pulls, instead of an upload. (Upload works either way and is
  network‑independent — I'd keep upload as the default.)
- Is systemd guaranteed on every unit (it is today)? The rollback design assumes it.
- Should provisioning also push the current library (tasks/sequences/plans) to a
  freshly‑provisioned unit, or leave that to the existing sync? (Probably the latter.)

---

## 11. Phase 1 — implemented (agent side)

What shipped in the agent for OTA update of a known unit:

**Code**
- `agent/updater.py` — `Updater`: `stage` (safe-extract + dep install), `activate`
  (atomic symlink flip + record previous + pending marker), `confirm_healthy`,
  `needs_rollback`, `rollback`, `prune`, `apply`. Pure/testable; the two side
  effects (dep install, service restart) are injectable.
- `agent/main.py` — `POST /admin/update` (upload a bundle → `apply`, off the event
  loop), `POST /admin/rollback`, `GET /admin/releases`; `/info` now reports
  `previous_version`; on boot the agent confirms a freshly-activated release healthy
  after `SDR_UPDATE_CONFIRM_DELAY_S`.
- `agent/config.py` — `STATE_DIR` (state decoupled from code), OTA layout constants
  (`RELEASES_DIR`, `CURRENT_LINK`, `SERVICE_NAME`, confirm/grace timings).
- `agent/models.py` — `UpdateResult`, `AgentRelease`, `AgentInfo.previous_version`.

**Tests** — `tests/test_updater.py` (staging, safety, activate, confirm, rollback,
prune, apply) and `tests/test_update_endpoint.py` (the handlers with an injected
updater). All green.

**Deploy artifacts (`deploy/`)**
- `build_bundle.sh` — builds `dist/sdr-agent-<version>.tar.gz` (+ `.sha256`).
- `sdr-agent.service` — the versioned-layout unit (symlinked `WorkingDirectory`,
  `SDR_STATE_DIR` state).
- `sdr-agent-confirm.sh` + `.service` + `.timer` — the external, self-contained
  rollback watchdog (mirrors `Updater.rollback`; keep them in sync).
- `migrate_layout.sh` — one-time classic → versioned migration, run once per Pi.

**Deploying it**
1. On the build machine: `bash deploy/build_bundle.sh` → a bundle in `dist/`.
2. On each Pi (once): `sudo deploy/migrate_layout.sh` — moves state to
   `/opt/sdr-agent-shared`, lays the current code down as a release under
   `/opt/sdr-agent-releases/<version>/`, points `/opt/sdr-agent` at it, and installs
   the OTA service + confirm timer.
3. Thereafter: `POST /admin/update` the bundle (the client's Update button, once
   built) → the unit stages, swaps, restarts, and self-rolls-back if unhealthy.

**Backward compatible:** a classic single-dir install still works — `STATE_DIR`
defaults to `BASE_DIR`, and the `/admin/*` queries just return empty/None until the
unit is migrated to the versioned layout.

**Client side (shipped):** `AgentClient.update_agent`/`rollback_agent`/`agent_releases`
(with a per-call timeout for the long upload), a frozen-path bundle locator
(`state/agent_bundle.py`), and the per-unit **"Update agent…"** button + version
display + one-click roll back (`ui/agent_update_dialog.py`). Still open: a
one-at-a-time fleet **"Update all"**.

---

## 12. Phase 2 — implemented

SSH bootstrap provisioning of a fresh Pi, driven from the client. No agent code
changes — the agent is installed *by* this flow.

**Agent repo (`deploy/`)**
- `provision_install.sh` — from-scratch versioned-layout install straight from an
  unpacked bundle (the counterpart to `migrate_layout.sh`, which converts a classic
  install). Lays code down as a release, symlinks `current`, seeds shared state
  (non-destructively), installs deps + the OTA service + confirm timer, and writes
  the unit's `SDR_UNIT_ID`/`SDR_API_KEY` into a 0600 service drop-in.
- `provision_network.sh` — sets the hostname and STATIC IPs, then reboots. Detects
  the stack (NetworkManager `nmcli` vs `dhcpcd`) and writes whichever applies, with a
  timestamped backup of the prior config. Runs **last** (it drops the SSH session at
  the IP change / reboot — the re-IP gotcha in §4.4). If **cloud-init** is present
  (Ubuntu Server for Pi, some RPi OS images) it drops
  `/etc/cloud/cloud.cfg.d/99-sdr-provision.cfg` with `preserve_hostname: true` +
  `network: {config: disabled}` — otherwise cloud-init re-applies the old hostname
  and network from its datasource on every boot (the classic "hostname resets after
  reboot").
- `build_bundle.sh` now embeds `configs/` (seed state) and `deploy/` (these scripts)
  in the bundle, so the single artifact serves OTA *and* provisioning. For OTA the
  extra dirs land unused in the release dir (state is read from `SDR_STATE_DIR`).

**Client repo**
- `config.py` — `ProvisionScheme` (hostname prefix, eth/wlan subnets, prefix, gateway,
  DNS, SSH user, WiFi SSID), persisted in `units.yaml` under `provision:`. Computes a
  unit's hostname + static IPs from its number `N`.
- `state/provisioner.py` — `Provisioner` (paramiko): connect → sanity-check
  (Pi + sudo + python3) → upload+unpack bundle → `provision_install.sh` → verify the
  agent is active → `provision_network.sh` → reboot. Secrets (passwords, PSK, API key)
  go over stdin / root-only env files, never on a command line. Progress via an
  `on_step` callback; no Qt.
- `ui/provision_dialog.py` — **"Provision new Pi…"** dialog (Units tab): target
  address + SSH login + unit number, editable addressing scheme, optional WiFi, a live
  computed hostname/IP preview, a streamed step log, then registers the unit at its new
  addresses and waits for it to come back after the reboot.
- `requirements.txt` — adds `paramiko`.
- `tests/test_provisioner.py` — 9 tests over a fake SSH transport (ordered steps,
  sudo-from-bundle, secrets-never-on-a-command-line, env-file identity, auth/sudo/
  agent-down/missing-bundle failures).

**Using it**
1. Flash a Pi with Raspberry Pi Imager, pre-seeding SSH-on + a user/password (and
   WiFi so it joins the network) — the Phase 3 base-image story, done by hand for now.
2. In the client: Units → **Provision new Pi…**, enter its current IP + login + a unit
   number, confirm the computed hostname/IPs, **Provision**.

**Still to do:** Phase 3 (a documented base image + Imager pre-seed snippet) and the
fleet "Update all".

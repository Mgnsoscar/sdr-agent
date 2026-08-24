# Porting the SDR agent to the Ettus/NI X410

Scope: run the **same agent**, achieving the **same behaviour**, on a stationary
Ettus USRP X410 (RFSoC ZU28DR, quad Cortex-A53, aarch64) as on the mobile
Raspberry Pi broadcaster units. This is the working checklist we build against —
it is grounded in the current agent code, not guesses. Fill in the `TODO(recon)`
blanks against the hardware.

The client already treats a heterogeneous fleet correctly: a unit has a `type`
(`broadcaster` | `x410`) and the canonical library is scoped per type, so an
X410 only ever receives X410-scoped (or shared) scripts/tasks/sequences. This
document is only about the **agent + its install** on the X410; the SDR content
(the IQ-replay scripts/tasks) is a library concern, authored on the PC.

---

## 1. What already ports unchanged

The agent is almost entirely OS-agnostic Python. A grep of `agent/` for
Pi/Debian coupling turns up only two spots (both already fall back gracefully):
the thermal path + `vcgencmd` in `system.py`, and one `pip` flag in
`updater.py`. Everything below needs **no change**:

| Component | Why it's portable |
|---|---|
| **FastAPI app** (`agent/main.py`) — every endpoint: `/info`, `/health`, `/system`, `/sdr`, tasks, sequences, events, plans/schedule, `/library`, `/admin/update\|rollback\|releases`, `/panic`, SSE `/events/stream`, WS log stream | Pure Python/HTTP |
| **ProcessManager, SequenceRunner, Scheduler, log_manager, recovery, client_state, paramkit** | Launch subprocesses + manage files; no OS assumptions |
| **SDR probe** (`system.py` → `uhd_find_devices`) | UHD is **native** on the X410 — probing works better there than on a Pi |
| **Health** (`system.py`, `psutil`) | Cross-platform; temperature/throttle already degrade to `None` when unreadable |
| **Config** (`agent/config.py`) | Every path/name is an env var: `SDR_AGENT_BASE`, `SDR_STATE_DIR`, `SDR_RELEASES_DIR`, `SDR_CURRENT_LINK`, `SDR_SERVICE_NAME`, `SDR_TASKS_FILE`, `SDR_LOG_DIR`, … — relocating everything is a service-env change, not a code change |
| **Identity** (`/etc/machine-id`, `socket.gethostname()`) | Present on any systemd/Linux host |
| **mDNS** (`agent/mdns.py`, `zeroconf`) | Pure Python; enumerates interfaces via `psutil`, so it advertises whatever IPs exist |
| **OTA** (`agent/updater.py` + `sdr-agent-confirm.{service,timer,sh}`) | Symlink flip + `systemctl restart` + a self-contained shell rollback timer — **the X410 runs systemd**, so the whole mechanism applies |

**Implication:** there is no "X410 agent". There is one agent, one codebase, and
an **X410 install profile** (different install script + service env + dependency
delivery). Keep it that way — do not fork.

---

## 2. The gaps (all narrow, all in the install/env layer)

### G1 — Dependency delivery (pip is the path; wheelhouse only if offline)
There is no apt/dpkg. OpenEmbedded's own package manager (**opkg**, `.ipk`) may
be present, but it does **not** carry our app stack (FastAPI/pydantic/uvicorn/
zeroconf at our pinned versions aren't in any NI/OE feed). So the delivery path
for the Python deps is **pip, regardless of opkg**. Two ways to feed pip:

- **Online** — if the X410 reaches the internet, `pip install -r requirements.txt`
  just works: every dep publishes prebuilt **aarch64 manylinux** wheels on PyPI,
  so pip downloads them, no compiling. **No wheelhouse needed in this case.**
- **Offline / deterministic** — the on-device wheelhouse (below), for a unit with
  no internet or when a repeatable pinned install is wanted.

opkg would matter only as a source of **build prerequisites** (compiler/headers)
*iff* a dep had no aarch64 wheel and had to build from sdist — unlikely for our
pinned set (all ship aarch64 wheels). `psutil` is pip-installed here (the Pi used
apt only to avoid a pip-uninstall conflict that doesn't exist on the X410).

Compiled deps whose wheels must match the device: `pydantic-core` (Rust),
`uvloop`/`httptools`/`websockets` (C), `psutil`, `inotify-simple` (C).

**Wheelhouse certainty rule:** if we go offline, the only way to be *sure* the
wheels fit is to build the wheelhouse **on the X410 itself** (or a byte-identical
Yocto SDK sysroot) so the wheels carry that interpreter's real ABI/arch/libc
tags — `pip download --platform …` from a laptop is fragile for exactly these
manylinux cases. `deploy/x410/build_wheelhouse.sh` runs on-device, produces
`wheels/`, and prints the platform tags to verify before trusting them.

Also note `updater.py`'s pip args (`--break-system-packages`) may be rejected on
Yocto — see G5.

`TODO(recon)`: internet reachability (decides online vs wheelhouse); exact
`python3 --version` and platform tag (`cp311-cp311-…`).

### G2 — Update-surviving storage (Mender A/B)
An NI OS update swaps the whole rootfs (A/B slots), so anything under `/opt` on
the rootfs is lost on the next OS update. Put the install on the **persistent
data partition**:

- `SDR_AGENT_BASE`, `SDR_STATE_DIR`, `SDR_RELEASES_DIR`, `SDR_CURRENT_LINK` →
  persistent mount (e.g. `/data/sdr-agent*` — confirm the real path).
- The **systemd unit files** live on the rootfs too. Either keep them on the
  persistent partition and symlink into `/etc/systemd/system/`, or accept that
  an NI OS update requires a re-provision (fine for our use). Decide per how
  often the borrowed unit will be re-imaged (expected: never, during the loan).

No code change — this is purely which paths the service env points at.

**Script-path parity (implemented):** the real install lives on `/data`, but
`install.sh` symlinks `/opt/sdr-agent → /data/sdr-agent` and points the service's
`SDR_AGENT_BASE` at the symlink, so the agent reports and bakes the Pi-identical
`/opt/sdr-agent/scripts` path into task commands. That's what lets a *shared*
library task (scoped to both unit types) run on a Pi and an X410 unchanged. The
symlink is rootfs state, so an OS image update wipes it — the same re-run of
`install.sh` that re-establishes the unit file recreates it.

`TODO(recon)`: the writable path that survives reboot **and** an OS update.

### G3 — X410 install profile (no apt, persistent paths)
`deploy/x410/install.sh` = the counterpart to `deploy/provision_install.sh`,
minus apt, plus: install from `wheels/`, write the persistent `SDR_*_DIR` into
the service drop-in, install the systemd units, enable + start. The client's
paramiko provisioning transport (SSH `root@host`, upload tar, unpack, run script)
works unchanged — it just runs this script instead.

### G4 — Networking: same goals as the Pi, X410-native mechanism
We *may* set eth0 IP + hostname on the X410 (worst case: revert by hand at
hand-back). `deploy/provision_network.sh` (Pi) is Debian **netplan +
NetworkManager + cloud-init** and does **not** apply on Yocto — but its *goals*
carry over one-for-one. The X410 stack is most likely **systemd-networkd**
(possibly ConnMan or an NI-managed config — recon confirms).

The Pi's default (DHCP) mode achieves three goals; static mode adds a fourth.
Mapped to the X410:

| Pi goal | Pi mechanism | X410 mechanism |
|---|---|---|
| Persistent hostname (survives reboot) | `hostnamectl` + cloud-init `preserve_hostname` pin + `/etc/hosts` | `hostnamectl set-hostname` (persistent; **no cloud-init pin needed** — the "resets after reboot" cause doesn't exist here) + avahi so `<hostname>.local` follows |
| Stay reachable at `<hostname>.local`, no reboot | avahi + agent restart | restart avahi + `sdr-agent` to re-announce |
| Direct-cable `169.254.1.N/16` on eth0, no DHCP server | netplan drop-in + `optional:true` (defeats NM teardown loop) | **simpler:** a `.network` with `Address=169.254.1.N/16` + `DHCP=yes`. networkd has no NM teardown loop, and `LinkLocalAddressing=ipv4` is a real knob (it was a no-op under netplan/NM) |
| Static site IP (opt-in) + gateway/DNS, reboot | nmcli / dhcpcd writers | a `.network` with `Address=`/`Gateway=`/`DNS=`, `networkctl reload` (often no reboot) |

Always **snapshot the original config first** so revert is a one-liner
(borrowed unit). Until the stack is confirmed this stays a stub; the agent works
fine on the factory addressing + mDNS meanwhile.

`TODO(recon)`: which stack manages eth0 (`networkctl` / `connmanctl`); the
factory eth0 address + hostname to snapshot.

### G5 — `updater.py` pip flag
`_default_deps_install` hardcodes `--break-system-packages`. Make the pip base
args configurable (e.g. `SDR_PIP_ARGS`, default keeps today's Pi behaviour) so
OTA dep-installs work on Yocto too. Small, unit-testable.

### G6 — Thermal source (cosmetic)
`system.py` reads `/sys/class/thermal/thermal_zone0/temp` (Pi) and `vcgencmd`
(Pi). On the ZU28DR the thermal path differs and `vcgencmd` is absent (already
returns `None`). Add a Zynq UltraScale thermal read (or accept
`cpu_temp_c = None`). Health is unaffected either way.

`TODO(recon)`: `ls /sys/class/thermal/` and which zone is the SoC.

### G8 — Don't advertise the internal `int0` NIC
The X410 has an internal management interface `int0` (`169.254.0.1`, RFSoC/MPM
side) that a client must never touch or reach. The agent's mDNS advertiser
enumerates every non-loopback IPv4, so it would otherwise announce `int0`'s
address as a unit address. Fixed with `SDR_MDNS_EXCLUDE_IFACES` (comma-separated
interface names; empty by default so the Pis are unaffected) — the X410 profile
sets it to `int0`. This is read-only: the agent never configures `int0`, only
declines to advertise it.

### G9 — UHD tasks need `$HOME` and the system Python
Two runtime facts for the SDR tasks the agent launches on the X410:
- **`HOME` must be set.** UHD resolves `~/.config/uhd` and aborts with
  `get_xdg_config_home(): Unable to find $HOME or $XDG_CONFIG_HOME` if it's unset —
  and systemd does not set `HOME` for services. The X410 service sets
  `Environment=HOME=/root`, which every spawned task inherits.
- **Tasks run under the system Python, not the bundled one.** `uhd` + `numpy` live
  in the system `python3.7` (that's what UHD ships with). The agent runs on the
  isolated `/data/python` 3.11, but an SDR task's command must use
  `/usr/bin/python3` so it can `import uhd`. (The agent now resolves a bare
  `python3` via PATH too — needed because uvloop/libuv, unlike plain asyncio,
  doesn't search PATH for argv[0].)

### G7 — Revert guarantee (borrowed unit)
Before touching anything, snapshot the current state; provide a clean
`deploy/x410/uninstall.sh` and a documented footprint so hand-back is
deterministic. Footprint is only: the persistent install dir(s), a few systemd
unit files (+ drop-in), and — if G4 is used — the network/hostname change (with
its recorded original).

---

## 3. Decisions

1. **Single codebase + X410 install profile.** Confirmed: the agent is portable;
   only the install/env differs. Content differences (which scripts/tasks) are
   handled by the library scoping already built.
2. **Wheelhouse built on-device** for deterministic, offline, ABI-correct deps.
3. **Persistent storage path** — deferred to recon (owner: revisit on hardware).

---

## 4. Recon checklist (Phase 0 — run on the X410)

Answers unblock G1/G2/G4/G6. `deploy/x410/recon.sh` collects these in one pass:

- [ ] `python3 --version`; `python3 -c "import sysconfig,platform;print(sysconfig.get_platform(),platform.machine())"`
- [ ] `pip3 --version` (and whether `--break-system-packages` is accepted/needed)
- [ ] `which uhd_find_devices` and a sample `uhd_find_devices` run
- [ ] `systemctl --version`; confirm `systemctl enable` persists across reboot
- [ ] `cat /etc/machine-id` (stable, non-empty?)
- [ ] a **writable path that survives reboot + OS update** (candidate: `/data`?)
- [ ] the mDNS/hostname it answers to (NI advertises `ni-x4xx-<serial>.local`)
- [ ] is **GNU Radio** present? (`gnuradio-config-info --version`) — for IQ replay
- [ ] internet reachability (`curl -sSI https://pypi.org` timeout) — offline-first?
- [ ] `ls /sys/class/thermal/*/type` — the SoC thermal zone
- [ ] which service manages eth0 (`networkctl`, `connmanctl`, or NI config)
- [ ] snapshot: current hostname + eth0 address/config (for revert)

---

## 5. Build plan (phased)

- **P0 — Recon.** Run `deploy/x410/recon.sh` on the unit; fill the blanks above.
- **P1 — Install profile.** `deploy/x410/build_wheelhouse.sh` (on-device) →
  `deploy/x410/install.sh` (no apt, wheelhouse, persistent `SDR_*_DIR`) + service
  drop-in. Verify the agent boots, `/info` + `/sdr` respond, mDNS advertises.
- **P2 — `system.py` portability.** Conditional thermal source; keep every
  fallback. Add a tiny test.
- **P3 — `updater.py`.** Configurable pip args (`SDR_PIP_ARGS`); test the offline
  path. Confirm OTA update + rollback on the X410.
- **P4 — Revert.** `deploy/x410/uninstall.sh` + footprint doc; dry-run it.
- **P5 — (optional) Network provisioning.** X410-native hostname + static eth0,
  recording the original first. Only if we choose to change addressing.
- **P6 — Library content.** Author the X410-scoped IQ-replay script + task on the
  PC (uses the scoping feature); deploy; run a sequence end-to-end.

---

## 6. Open questions

- Does an NI OS update wipe `/etc/systemd/system/` (→ symlink units from
  persistent storage, or accept re-provision)?
- Is the borrowed unit ever going to be re-imaged during the loan? (If never,
  G2's systemd-unit concern is moot — everything can live in one persistent dir.)
- IQ replay path: GNU Radio flowgraph vs a UHD replay utility
  (`uhd_tx_samples_from_file` / RFNoC replay block)? Determines what the
  X410-scoped task's `command` is — but that's library content, not agent code.

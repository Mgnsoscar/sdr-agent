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

### G1 — Dependency delivery without `apt` (the wheelhouse)
Yocto/OpenEmbedded has no apt/dpkg, and pip on Yocto may not accept Debian's
`--break-system-packages`. Most of the stack has **compiled** extensions that
must match the device exactly:

- `pydantic 2.8.2` → **`pydantic-core`** (compiled Rust)
- `uvicorn[standard]` → **`uvloop`, `httptools`, `websockets`** (compiled C)
- `psutil`, `inotify-simple` (compiled C)
- pure-Python: `fastapi`, `PyYAML`, `python-multipart`, `zeroconf`, `ruamel.yaml`

**Certainty rule:** the only way to be *sure* the wheels fit is to build the
wheelhouse **on the X410 itself** (or a byte-identical Yocto SDK sysroot), so the
wheels carry that interpreter's real ABI/arch/libc tags. `pip download
--platform …` from a laptop is fragile for exactly these manylibc/manylinux
cases. `deploy/x410/build_wheelhouse.sh` runs on-device, produces `wheels/`, and
prints the platform tags so we can verify before trusting them.

`TODO(recon)`: exact `python3 --version` and platform tag (`cp311-cp311-…`).

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

`TODO(recon)`: the writable path that survives reboot **and** an OS update.

### G3 — X410 install profile (no apt, persistent paths)
`deploy/x410/install.sh` = the counterpart to `deploy/provision_install.sh`,
minus apt, plus: install from `wheels/`, write the persistent `SDR_*_DIR` into
the service drop-in, install the systemd units, enable + start. The client's
paramiko provisioning transport (SSH `root@host`, upload tar, unpack, run script)
works unchanged — it just runs this script instead.

### G4 — Networking: in scope, but via the X410's own stack
We *may* set eth0 IP + hostname on the X410 (worst case: revert by hand at
hand-back). But `deploy/provision_network.sh` is Debian **netplan + NetworkManager
+ cloud-init** and does **not** apply on Yocto. The X410 uses a different stack
(systemd-networkd / ConnMan / NI's own config — confirm). So we need an
X410-specific `provision_network` that:
- sets the hostname the X410 way (persisting across reboot), and
- sets a static eth0 address the X410 way,
- **records the original config first** so revert is a one-liner.

Until confirmed on hardware this stays a stub; the agent works fine with the
factory addressing + mDNS in the meantime.

`TODO(recon)`: which network stack manages eth0; how hostname persists; the
factory eth0 address to snapshot.

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

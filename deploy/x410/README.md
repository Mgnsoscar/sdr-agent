# deploy/x410 — X410 install profile (skeleton)

Bring the SDR agent up on an Ettus/NI X410. Same agent, same behaviour as the Pi
units; only the install/env layer differs. Full rationale + gap analysis:
[`docs/x410-agent-port.md`](../../docs/x410-agent-port.md).

**These are scaffolds.** `recon.sh` and `build_wheelhouse.sh` are ready to run;
`install.sh`, `uninstall.sh`, and `provision_network.sh` carry `TODO(recon)`
blanks that the hardware fills in. Nothing here has been run against a real X410
yet — treat first use as a supervised bring-up on the borrowed unit.

## Order of operations

1. **`recon.sh`** (read-only) — run on the X410, paste output back. Answers:
   Python/ABI tags, persistent-storage path, network stack, thermal zone, UHD,
   internet reach. Nothing is changed.
2. **`build_wheelhouse.sh`** — run **on the X410** to build ABI-correct wheels
   into `wheels/`. This is the only way to be certain the compiled deps
   (`pydantic-core`, `uvloop`, `httptools`, `websockets`, `psutil`,
   `inotify-simple`) match the device. Bundle the resulting `wheels/` with the
   agent tarball.
3. **`install.sh`** — no apt, installs from `wheels/`, lays the OTA versioned
   layout on the **persistent partition**, writes the service drop-in (persistent
   `SDR_*` paths + `WorkingDirectory`/`PYTHONPATH`), enables the service +
   rollback timer. Set `PERSIST_ROOT` once recon confirms it.
4. **`provision_network.sh`** *(optional)* — set hostname + static eth0 the X410
   way. Snapshots the original first for hand-back. Stub until the stack is
   confirmed.
5. **`uninstall.sh`** — revert everything install.sh created (borrowed-unit
   hand-back). `KEEP_STATE=1` preserves configs/logs.

## The blanks recon fills (`TODO(recon)`)

- `PERSIST_ROOT` — writable path surviving reboot **and** an NI OS update.
- Whether pip needs `--break-system-packages` here.
- Whether `/etc/systemd/system` survives an OS update (else symlink units from
  persistent storage, or accept re-provision).
- Which stack manages eth0 (systemd-networkd / connman / NI) for
  `provision_network.sh`.
- The SoC thermal zone (for the `system.py` port, gap G6 — cosmetic).

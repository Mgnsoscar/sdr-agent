# deploy/x410 — provision an Ettus/NI X410

Bring the SDR agent up on an X410. Same agent, same behaviour as the Pi units;
only the install differs (a bundled Python + offline wheels + systemd-networkd).
Full rationale/gap analysis: [`docs/x410-agent-port.md`](../../docs/x410-agent-port.md).

This flow is **validated** on NI Alchemy/Zeus (aarch64, Python 3.7 system,
systemd 243, UHD 4.1, GNU Radio 3.8). The X410's system Python is too old for the
agent's stack, so the agent runs on a self-contained Python 3.11 under `/data` —
the system Python (and UHD/GNU Radio) is never touched.

## One-time bundle build (on a PC with internet)

The X410 is typically offline, so assemble a bundle on your PC and copy it over.

1. **Standalone Python** — from
   <https://github.com/astral-sh/python-build-standalone/releases/latest> download
   `cpython-3.11.*-aarch64-unknown-linux-gnu-install_only.tar.gz`, save it into the
   agent repo as `python-aarch64.tar.gz`.
2. **Wheels** (aarch64/cp311). Note `uvloop` must be listed **explicitly**: it's
   gated `sys_platform != "win32"`, so a `pip download` run on Windows skips it —
   and the agent needs it on Linux.
   ```
   python -m pip download --only-binary=:all: --platform manylinux2014_aarch64 \
       --python-version 3.11 --implementation cp --abi cp311 -d wheels \
       -r requirements.txt psutil uvloop
   ```
3. **Bundle it** (repo root already has `agent/ scripts/ paramkit/ configs/
   requirements.txt deploy/`):
   ```
   tar --exclude .git -czf sdr-x410-bundle.tar.gz \
       python-aarch64.tar.gz wheels agent scripts paramkit configs requirements.txt deploy
   scp sdr-x410-bundle.tar.gz root@<x410>:/data/
   ```

## Install (on the X410, as root)

```sh
cd /data && tar -xzf sdr-x410-bundle.tar.gz
SDR_UNIT_ID=x410-1 deploy/x410/install.sh
```
`install.sh` extracts the Python to `/data/python`, lays the agent in
`/data/sdr-agent`, installs the wheels offline into the bundle, writes the systemd
service (bundled Python + persistent paths + `HOME=/root` for UHD + `int0`
excluded from mDNS), enables it, and prints `/health`.

## Networking (on the X410, as root)

```sh
PROV_HOSTNAME=x410-1 deploy/x410/provision_network.sh              # DHCP + direct-cable link-local
# or a static site IP:
PROV_HOSTNAME=x410-1 PROV_STATIC=1 PROV_ETH_IP=10.0.0.5 PROV_PREFIX=24 \
    PROV_GATEWAY=10.0.0.254 PROV_DNS="10.0.0.254 1.1.1.1" deploy/x410/provision_network.sh
```
Default mode gives eth0 a stable `169.254.1.N/16` link-local (N from the hostname)
so a **direct cable** to a PC works with no PC IP change (Windows auto-APIPA). It
snapshots the original config to `/data/sdr-netsnapshot` first.

## In FleetView
Add the unit at its eth0 address (e.g. `169.254.1.1`). New tasks auto-fill the
right script dir + interpreter from the unit (needs the client's matching build),
so you don't re-type `/data/sdr-agent/scripts` or the system `python3`.

## Revert (hand-back)
```sh
deploy/x410/uninstall.sh        # removes /data/python, /data/sdr-agent, state, the unit file
```
Then revert the network from `/data/sdr-netsnapshot` (`rm
/etc/systemd/network/10-sdr-eth0.network ; systemctl restart systemd-networkd`).
Footprint is only: the `/data` dirs, one systemd unit, and one networkd file.

## Files
- `recon.sh` — read-only fact-finder (run first on any new/unknown image).
- `build_wheelhouse.sh` — build the wheelhouse **on-device** instead of cross-
  downloading (use if the PC cross-download ever misses a wheel).
- `install.sh` / `uninstall.sh` / `provision_network.sh` — the flow above.

## Caveats
- A full NI OS *image* update (Mender A/B) replaces the rootfs, wiping
  `/etc/systemd/system` and the networkd file — re-run `install.sh` +
  `provision_network.sh` after such an update. Everything under `/data` persists.
- SDR tasks run under the **system** `python3` (has `uhd`/`numpy`); the agent runs
  under the bundled `/data/python`. The client leaves the task interpreter as
  `python3`, which resolves via PATH to the system one.

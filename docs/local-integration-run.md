# Local integration run — agent + client together, headless (no hardware)

Run the real on-unit agent (`sdr-agent`) and the real PyQt6 client (`sdr-client`) on
one machine, with **no SDR/Pi hardware and no monitor**, so the two halves can be
exercised together (discovery, `/info`, the SSE event stream, the poller, authoring
against a live unit). This is a **dev/test harness only** — it is not how real units are
deployed (see `deploy/provision_install.sh` / `install.sh` for that).

The three repos are sibling checkouts (`sdr-agent/`, `sdr-client/`, `sdr-scripts/`).
Two committed helpers do the whole thing:

| helper | what it does |
| --- | --- |
| `sdr-agent/deploy/run_local.sh` | stage a flat scripts dir from `sdr-scripts`, start `uvicorn agent.main:app` on `0.0.0.0:8765` |
| `sdr-client/tools/run_local.sh` | write a `units.yaml` pointing at the agent, launch `main.py` headless (Qt offscreen) |
| `sdr-client/tools/screenshot.py` | build a throwaway client, connect to the agent, save a PNG of a chosen tab |

## Quickstart

```bash
# 1. Agent — start it BACKGROUNDED (it blocks while serving).
bash sdr-agent/deploy/run_local.sh          # → http://<this-host-ip>:8765

# 2a. Client — long-running, headless:
bash sdr-client/tools/run_local.sh

# 2b. …or just a screenshot (self-contained: seeds its own units.yaml):
python3 sdr-client/tools/screenshot.py --tab units --out /tmp/units.png
```

A Claude session should launch the agent with the Bash tool's `run_in_background: true`
(never a foreground blocking server), then drive the client / screenshot separately.

Verify the agent by hand:

```bash
curl -s http://127.0.0.1:8765/info | python3 -m json.tool     # agent_version, capabilities
curl -s http://127.0.0.1:8765/scripts                         # staged transmit scripts
```

A connected client shows **"clocks: synced ✓"** (top bar) and the unit card as
**online / 1/1 online** on the Units tab.

## Env overrides (both scripts)

- `SDR_LOCAL_RUN_DIR` — scratch root for staged code + client state (default `/tmp/sdr-local`).
  Agent code/state under `<root>/agent-base` + `<root>/agent-state`; client state under
  `<root>/client-data`.
- `SDR_SCRIPTS_REPO` — path to the `sdr-scripts` checkout (agent script default: sibling `../sdr-scripts`).
- `SDR_AGENT_HOST` — address written into the client's `units.yaml` (client default: this host's first IP).
- `SDR_AGENT_PORT` — agent listen port (default `8765`).
- `QT_QPA_PLATFORM` — client Qt platform (default `offscreen`; set `xcb`/empty if you have a display).

## The three gotchas that make this non-obvious

1. **The agent serves a FLAT `<base>/scripts/` dir**, but `sdr-scripts` nests scripts under
   platform folders (`Raspberry pi + b206 mini-i/PRN GPS/…`). `run_local.sh` flattens the
   `Raspberry pi + b206 mini-i` tree into `<base>/scripts/`. The scripts are only READ
   statically (`argspec`) for `/scripts/{name}/params` — they are never imported unless a task
   runs — so staging every script is safe even though most need UHD/GNU Radio at runtime.
2. **The client drops loopback / colon addresses on load** (`config._parse_unit`:
   `":" not in a and not a.startswith("127.")`). So a unit's `units.yaml` address must be a
   **bare host with no port and not `127.*`** — use the container's real IP (`hostname -I`,
   which is what the helpers do). The port is fixed at `8765` by `AgentClient` (bare host only).
3. **`SDR_CLIENT_DATA_DIR` is captured at import time** (`config.DEFAULT_UNITS_FILE =
   data_file("units.yaml")`). `screenshot.py` sets it (to `<run-dir>/client-data`) BEFORE
   importing `paths`/`config`, so its seed + load agree and the repo root is never touched.
   From source with no override, `paths.data_dir()` is the repo root — do not point a test run
   there or it reads/writes the tracked dev `units.yaml`.

## What is (correctly) absent with no hardware

`sdr: none`, `temp —`, `clock —` on the unit card, and `/sdr` reporting no device are
**expected** — the agent honestly reports no radio. Everything above the hardware layer
(discovery, calibration authoring, sequences/plans, arm/proceed/hold, the event stream)
works end to end.

## `screenshot.py` notes

- Builds its OWN client instance — it does not attach to a separately running `run_local.sh`
  client. Run the agent, then the screenshot; a running client is not required.
- `--tab timeline|units|library`, `--out <png>`, `--settle-ms` (poller warm-up, default 7000),
  `--size WxH` (default `1500x950`). It switches tabs via `MainWindow._select_tab`, waits for
  the tab to fetch its data, grabs the window with `QWidget.grab()` (renders at the widget
  size regardless of the offscreen virtual screen), then quits.

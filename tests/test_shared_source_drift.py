"""Drift guard for logic that is duplicated between this agent and the client.

ramp.py (ramp expansion) and argspec.py (static script-parameter extraction) MUST be
byte-identical in both repos: the client previews/validates exactly what the agent
executes/serves. This test fails if they diverge.

It compares against a sibling sdr-client checkout when one is present (a dev tree or a
CI job that checks out both repos); it skips when the client repo isn't on disk, so an
agent-only checkout still runs green.
"""
from pathlib import Path

import pytest

# (this-repo path, client-repo path) for each shared file, relative to each repo root.
SHARED = [("agent/ramp.py", "api/ramp.py"),
          ("agent/argspec.py", "api/argspec.py")]

_AGENT_ROOT = Path(__file__).resolve().parents[1]


def _client_root():
    for cand in (_AGENT_ROOT.parent / "sdr-client",          # siblings (dev/CI)
                 _AGENT_ROOT.parent.parent / "sdr-client"):
        if (cand / "api").is_dir():
            return cand
    return None


@pytest.mark.parametrize("agent_rel,client_rel", SHARED)
def test_shared_file_matches_client(agent_rel, client_rel):
    client_root = _client_root()
    if client_root is None:
        pytest.skip("sibling sdr-client checkout not found — drift check is a no-op here")
    ours = (_AGENT_ROOT / agent_rel).read_text(encoding="utf-8")
    theirs = (client_root / client_rel).read_text(encoding="utf-8")
    assert ours == theirs, (
        f"{agent_rel} has drifted from {client_rel} — these must stay byte-identical. "
        f"Sync the change into both repos.")

"""GET /info capability advertisement and the API-key gate (constant-time compare)."""
import asyncio

import pytest

pytest.importorskip("fastapi")
HTTPException = pytest.importorskip("fastapi").HTTPException

from agent import main
from agent import config as cfg
from agent.models import AgentInfo


def test_capabilities_advertised_and_stable():
    # The client feature-gates on these exact strings — they must be present.
    assert "calibration" in cfg.AGENT_CAPABILITIES
    assert "script-cal-signal" in cfg.AGENT_CAPABILITIES
    assert "cal-validate" in cfg.AGENT_CAPABILITIES
    assert "calibration-components" in cfg.AGENT_CAPABILITIES     # calibration v2


def test_agent_info_carries_capabilities():
    info = AgentInfo(hostname="h", unit_id="u", agent_version="1.1.4",
                     python_version="3.11", tasks=[], capabilities=["calibration"])
    assert info.capabilities == ["calibration"]
    # Defaulted, so an agent/response omitting it still parses (skew tolerance).
    bare = AgentInfo(hostname="h", unit_id="u", agent_version="1.0.0",
                     python_version="3.11", tasks=[])
    assert bare.capabilities == []


def test_verify_key_disabled_when_unset(monkeypatch):
    monkeypatch.setattr(cfg, "API_KEY", "")
    asyncio.run(main.verify_key(key=None))          # no key required → no raise
    asyncio.run(main.verify_key(key="anything"))


def test_verify_key_enforced_when_set(monkeypatch):
    monkeypatch.setattr(cfg, "API_KEY", "s3cret")
    asyncio.run(main.verify_key(key="s3cret"))      # correct → ok
    for bad in (None, "", "wrong"):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(main.verify_key(key=bad))
        assert exc.value.status_code == 401

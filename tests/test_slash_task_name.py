"""A task name may contain '/' (e.g. "GPS/L1"). The /tasks/{name:path} routes must
still dispatch correctly — the greedy catch-all status route must not swallow the
/logs, /history, /start, … sub-routes — and the per-task log dir / control socket must
stay unique and safe (no traversal, no collision between names that sanitise alike)."""
import pytest

pytest.importorskip("fastapi")
from starlette.routing import Match

from agent.main import app
from agent.log_manager import _safe_dirname
from agent.process_manager import _ctrl_sock_path


def _match(method: str, path: str):
    scope = {"type": "http", "method": method, "path": path}
    for route in app.router.routes:
        matched, _ = route.matches(scope)
        if matched == Match.FULL:
            return route.endpoint.__name__
    return None


def test_slash_name_dispatches_to_the_right_handler():
    assert _match("GET",  "/tasks/GPS/L1") == "task_status"
    assert _match("GET",  "/tasks/GPS/L1/logs") == "get_logs"
    assert _match("GET",  "/tasks/GPS/L1/history") == "task_history"
    assert _match("POST", "/tasks/GPS/L1/start") == "start_task"
    assert _match("POST", "/tasks/GPS/L1/stop") == "stop_task"
    assert _match("POST", "/tasks/GPS/L1/restart") == "restart_task"
    # A plain name still works as before.
    assert _match("GET",  "/tasks/mocktask") == "task_status"
    assert _match("GET",  "/tasks/mocktask/logs") == "get_logs"


def test_log_dir_safe_and_unique():
    # Plain names keep a stable, readable dir (log history survives).
    assert _safe_dirname("mocktask") == "mocktask"
    # A '/' name is sanitised (no nesting/traversal) and disambiguated from a name
    # that would sanitise to the same string.
    assert "/" not in _safe_dirname("a/b")
    assert _safe_dirname("a/b") != _safe_dirname("a_b")
    # A traversal attempt yields a single, safe component (no '/', not '.'/'..'),
    # so `log_root / result` can never escape the log root.
    for evil in ("../../etc", "..", ".", "a/../b"):
        d = _safe_dirname(evil)
        assert "/" not in d and d not in (".", "..")


def test_ctrl_socket_unique_and_bounded():
    assert _ctrl_sock_path("a/b") != _ctrl_sock_path("a_b")
    long = "x" * 200
    # Must stay under the AF_UNIX ~108-byte limit.
    assert len(_ctrl_sock_path(long)) < 108

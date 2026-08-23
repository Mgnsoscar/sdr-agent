"""A task name may contain '/' (e.g. "GPS/L1"). Routing must dispatch correctly for
every name, INCLUDING a pathological name that ends in an API word ("Site/logs",
"x/history"): the read-only sub-resources live under their own /task-* prefixes (name
as the terminal segment), so the bare /tasks/{name} status route can never be swallowed.
The per-task log dir / control socket must also stay unique and safe (no traversal, no
collision between names that sanitise alike)."""
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


def _ws_match(path: str):
    scope = {"type": "websocket", "path": path}
    for route in app.router.routes:
        matched, _ = route.matches(scope)
        if matched == Match.FULL:
            return route.endpoint.__name__
    return None


def test_slash_name_dispatches_to_the_right_handler():
    assert _match("GET",  "/tasks/GPS/L1") == "task_status"
    assert _match("GET",  "/task-logs/GPS/L1") == "get_logs"
    assert _match("GET",  "/task-history/GPS/L1") == "task_history"
    assert _match("GET",  "/task-live-params/GPS/L1") == "get_live_params"
    assert _ws_match("/task-log-stream/GPS/L1") == "stream_logs"
    assert _match("POST", "/tasks/GPS/L1/start") == "start_task"
    assert _match("POST", "/tasks/GPS/L1/stop") == "stop_task"
    assert _match("POST", "/tasks/GPS/L1/restart") == "restart_task"
    assert _match("POST", "/tasks/GPS/L1/params") == "set_live_params"
    # A plain name still works as before.
    assert _match("GET",  "/tasks/mocktask") == "task_status"
    assert _match("GET",  "/task-logs/mocktask") == "get_logs"


def test_status_of_a_name_ending_in_an_api_word_is_not_swallowed():
    # These are the names the old /tasks/{name}/<sub> layout misrouted: a status GET
    # for "Site/logs" used to hit get_logs("Site"). Now the bare route always wins.
    assert _match("GET", "/tasks/Site/logs") == "task_status"       # name = "Site/logs"
    assert _match("GET", "/tasks/x/history") == "task_status"       # name = "x/history"
    assert _match("GET", "/tasks/a/params/live") == "task_status"   # name = "a/params/live"
    # And the actual sub-resource of such a task is still reachable (name is terminal).
    assert _match("GET", "/task-logs/Site/logs") == "get_logs"      # name = "Site/logs"
    assert _match("GET", "/task-history/x/history") == "task_history"


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

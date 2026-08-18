"""A task's env survives the tasks.yaml write/read round-trip — including values
(and keys) that are YAML-1.1 booleans like on/off/yes/no.

Regression: tasks.yaml is written with ruamel (YAML 1.2, which leaves on/off/yes/no
bare) but read with PyYAML (YAML 1.1, which parses them as booleans). An env like
LIVE=on then failed TaskConfig's Dict[str, str] and the whole task was silently
dropped — "the task won't deploy to the unit"."""
from agent import config as cfg
from agent import main
from agent.models import TaskConfig


def _point_at(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "TASKS_YAML", tmp_path / "tasks.yaml")
    monkeypatch.setattr(cfg, "LOG_DIR", tmp_path / "logs")


def test_env_with_yaml_bool_tokens_survives_deploy(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    (tmp_path / "tasks.yaml").write_text("tasks: []\n")

    env = {"PORT": "8080", "LIVE": "on", "STANDBY": "off", "YES": "yes",
           "NO": "no", "MODE": "live", "EMPTY": "", "PATH": r"C:\sdr\bin"}
    spec = TaskConfig(name="rx", command=["python3", "rx.py"], env=env)

    doc = main._load_tasks_doc()
    doc["tasks"] = [main._spec_to_entry(spec)]
    main._save_tasks_doc(doc)

    loaded = cfg.load_tasks()
    assert "rx" in loaded, "task with env was dropped on load"
    assert loaded["rx"].env == env      # every key and value faithful


def test_load_tasks_coerces_stray_env_types(tmp_path, monkeypatch):
    """A hand-edited / legacy tasks.yaml with an unquoted on/8080 still loads
    (defensive coercion) rather than dropping the task."""
    _point_at(tmp_path, monkeypatch)
    (tmp_path / "tasks.yaml").write_text(
        "tasks:\n"
        "  - name: rx\n"
        "    command: [python3, rx.py]\n"
        "    env:\n"
        "      LIVE: on\n"        # YAML 1.1 -> bool True
        "      PORT: 8080\n"      # -> int
        "      QUIET: off\n"      # -> bool False
    )
    loaded = cfg.load_tasks()
    assert "rx" in loaded
    assert loaded["rx"].env == {"LIVE": "true", "PORT": "8080", "QUIET": "false"}


def test_load_tasks_survives_empty_tasks_key(tmp_path, monkeypatch):
    """A bare `tasks:` line parses that key as None. `.get("tasks", [])` returns the
    default only when the key is ABSENT, so None used to slip through and crash agent
    startup (a crash-loop on a freshly-seeded unit). It must load as no tasks."""
    _point_at(tmp_path, monkeypatch)
    (tmp_path / "tasks.yaml").write_text("tasks:\n")     # key present, value null
    assert cfg.load_tasks() == {}


def test_load_tasks_empty_file_and_absent_key(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    (tmp_path / "tasks.yaml").write_text("")             # empty file
    assert cfg.load_tasks() == {}
    (tmp_path / "tasks.yaml").write_text("# only comments\n")
    assert cfg.load_tasks() == {}


def test_load_tasks_reads_real_entries(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    (tmp_path / "tasks.yaml").write_text(
        "tasks:\n  - name: t1\n    command: [echo, hi]\n")
    loaded = cfg.load_tasks()
    assert set(loaded) == {"t1"}

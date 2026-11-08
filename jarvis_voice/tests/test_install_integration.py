"""Tests for the companion integration installer (no Home Assistant needed)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install_integration as inst  # noqa: E402


def make_source(base: Path, version="0.5.0", code="VALUE = 1\n") -> Path:
    src = base / "src" / "jarvis_q1"
    (src / "translations").mkdir(parents=True, exist_ok=True)
    (src / "manifest.json").write_text(
        json.dumps({"domain": "jarvis_q1", "version": version}), encoding="utf-8")
    (src / "__init__.py").write_text(code, encoding="utf-8")
    (src / "translations" / "en.json").write_text("{}", encoding="utf-8")
    return src


@pytest.fixture
def env(tmp_path):
    config = tmp_path / "homeassistant"
    config.mkdir()
    root = config / "custom_components"
    options = tmp_path / "options.json"
    options.write_text(json.dumps({"auto_install_integration": True}), encoding="utf-8")
    return tmp_path, root, options


def run(options, src, root):
    return inst.main(options_path=options, source=src, root=root)


def test_fresh_install(env):
    base, root, options = env
    src = make_source(base)
    assert run(options, src, root) == "installed"
    dest = root / "jarvis_q1"
    assert (dest / inst.MARKER).is_file()
    assert (dest / "translations" / "en.json").is_file()
    assert inst.tree_hash(dest) == inst.tree_hash(src)
    assert not (root / inst.STAGE_NAME).exists()
    assert not (root / inst.BACKUP_NAME).exists()


def test_noop_when_identical(env):
    base, root, options = env
    src = make_source(base)
    assert run(options, src, root) == "installed"
    stamp = (root / "jarvis_q1" / "__init__.py").stat().st_mtime_ns
    assert run(options, src, root) == "unchanged"
    assert (root / "jarvis_q1" / "__init__.py").stat().st_mtime_ns == stamp


def test_pycache_does_not_trigger_update(env):
    base, root, options = env
    src = make_source(base)
    run(options, src, root)
    cache = root / "jarvis_q1" / "__pycache__"
    cache.mkdir()
    (cache / "x.cpython-311.pyc").write_bytes(b"junk")
    assert run(options, src, root) == "unchanged"


def test_update_when_code_changes_but_manifest_same(env):
    base, root, options = env
    src = make_source(base)
    run(options, src, root)
    (src / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert run(options, src, root) == "installed"
    assert (root / "jarvis_q1" / "__init__.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (root / "jarvis_q1" / inst.MARKER).is_file()
    assert not (root / inst.BACKUP_NAME).exists()


def test_removed_source_file_is_removed(env):
    base, root, options = env
    src = make_source(base)
    (src / "old.py").write_text("x", encoding="utf-8")
    run(options, src, root)
    (src / "old.py").unlink()
    assert run(options, src, root) == "installed"
    assert not (root / "jarvis_q1" / "old.py").exists()


def test_recovery_from_missing_dest_with_backup(env):
    base, root, options = env
    src = make_source(base)
    run(options, src, root)
    # Simulate a crash after DEST -> backup rename, before stage -> DEST.
    (root / "jarvis_q1").rename(root / inst.BACKUP_NAME)
    assert inst.recover(root) is True
    assert (root / "jarvis_q1" / inst.MARKER).is_file()
    assert not (root / inst.BACKUP_NAME).exists()
    # Full run afterwards sees an identical install.
    assert run(options, src, root) == "unchanged"


def test_stale_backup_removed_when_dest_present(env):
    base, root, options = env
    src = make_source(base)
    run(options, src, root)
    backup = root / inst.BACKUP_NAME
    backup.mkdir()
    (backup / inst.MARKER).write_text("x", encoding="utf-8")
    assert run(options, src, root) == "unchanged"
    assert not backup.exists()


def test_stale_stage_cleanup(env):
    base, root, options = env
    src = make_source(base)
    root.mkdir()
    stage = root / inst.STAGE_NAME
    stage.mkdir()
    (stage / inst.MARKER).write_text("x", encoding="utf-8")
    (stage / "partial.py").write_text("half", encoding="utf-8")
    assert run(options, src, root) == "installed"
    assert not stage.exists()
    assert not (root / "jarvis_q1" / "partial.py").exists()


def test_empty_stage_cleanup(env):
    base, root, options = env
    src = make_source(base)
    root.mkdir()
    (root / inst.STAGE_NAME).mkdir()
    assert run(options, src, root) == "installed"
    assert not (root / inst.STAGE_NAME).exists()


def test_foreign_stage_is_left_alone(env):
    base, root, options = env
    src = make_source(base)
    root.mkdir()
    stage = root / inst.STAGE_NAME
    stage.mkdir()
    (stage / "user_file.txt").write_text("keep", encoding="utf-8")
    assert run(options, src, root) == "unsafe_state"
    assert (stage / "user_file.txt").is_file()
    assert not (root / "jarvis_q1").exists()


def test_unmanaged_dest_not_overwritten(env):
    base, root, options = env
    src = make_source(base)
    dest = root / "jarvis_q1"
    dest.mkdir(parents=True)
    (dest / "mine.py").write_text("user", encoding="utf-8")
    assert run(options, src, root) == "unmanaged"
    assert (dest / "mine.py").is_file()


def test_failure_restores_previous_and_removes_stage(env, monkeypatch):
    base, root, options = env
    src = make_source(base)
    run(options, src, root)
    (src / "__init__.py").write_text("VALUE = 3\n", encoding="utf-8")
    real_rename = Path.rename

    def flaky_rename(self, target):
        if self.name == inst.STAGE_NAME:
            raise OSError("disk full")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)
    with pytest.raises(OSError):
        run(options, src, root)
    monkeypatch.undo()
    assert (root / "jarvis_q1" / "__init__.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (root / inst.STAGE_NAME).exists()
    assert not (root / inst.BACKUP_NAME).exists()


def test_disabled_option(env):
    base, root, options = env
    options.write_text(json.dumps({"auto_install_integration": False}), encoding="utf-8")
    assert run(options, make_source(base), root) == "disabled"
    assert not root.exists()


def test_missing_options_logged(env, caplog):
    base, root, _ = env
    caplog.set_level("WARNING", logger="jarvis.integration_install")
    assert run(base / "nope.json", make_source(base), root) == "no_options"
    assert "not found" in caplog.text


@pytest.mark.parametrize("content", ["{broken", "[1, 2]"])
def test_invalid_options_logged(env, caplog, content):
    base, root, options = env
    options.write_text(content, encoding="utf-8")
    caplog.set_level("ERROR", logger="jarvis.integration_install")
    assert run(options, make_source(base), root) == "no_options"
    assert "options" in caplog.text.lower()


def test_missing_source(env):
    base, root, options = env
    assert run(options, base / "missing", root) == "no_source"


def test_env_overrides(env, monkeypatch):
    base, root, options = env
    src = make_source(base)
    monkeypatch.setenv("OPTIONS_PATH", str(options))
    monkeypatch.setenv("JARVIS_INTEGRATION_SOURCE", str(src))
    monkeypatch.setenv("JARVIS_CUSTOM_COMPONENTS", str(root))
    assert inst.main() == "installed"

"""Install bundled companion integration into HA config, only with explicit App option.

This app owns only /homeassistant/custom_components/jarvis_q1. Never modify other
integrations or HA configuration.yaml. A Home Assistant Core restart is required
after the initial installation/update; user controls that restart.

Paths can be overridden for tests via function parameters or the environment
variables OPTIONS_PATH, JARVIS_INTEGRATION_SOURCE and JARVIS_CUSTOM_COMPONENTS.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import shutil

LOG = logging.getLogger("jarvis.integration_install")
logging.basicConfig(level=logging.INFO)

SOURCE = Path("/opt/jarvis_q1_integration/jarvis_q1")
ROOT = Path("/homeassistant/custom_components")
DEST_NAME = "jarvis_q1"
DEST = ROOT / DEST_NAME
OPTIONS_PATH = Path("/data/options.json")
MARKER = ".managed_by_jarvis_voice"
STAGE_NAME = ".jarvis_q1_new"
BACKUP_NAME = ".jarvis_q1_backup"

# Files that differ between source and installed trees without being "our code".
_IGNORED_DIRS = {"__pycache__"}
_IGNORED_FILES = {MARKER}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


def tree_hash(path: Path) -> str:
    """sha256 over relative paths and contents of every file in ``path``."""
    path = Path(path)
    digest = hashlib.sha256()
    files = []
    for item in path.rglob("*"):
        rel = item.relative_to(path)
        if any(part in _IGNORED_DIRS for part in rel.parts):
            continue
        if item.is_dir() or item.name in _IGNORED_FILES or item.suffix in _IGNORED_SUFFIXES:
            continue
        files.append((rel.as_posix(), item))
    for rel, item in sorted(files):
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_options(options_path: Path) -> dict | None:
    """Read App options; log (not raise) when missing or invalid."""
    options_path = Path(options_path)
    if not options_path.is_file():
        LOG.warning("App options not found at %s; skipping companion install", options_path)
        return None
    try:
        options = json.loads(options_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        LOG.error("Cannot read App options %s: %s; skipping companion install", options_path, exc)
        return None
    if not isinstance(options, dict):
        LOG.error("App options %s are not a JSON object; skipping companion install", options_path)
        return None
    return options


def _is_ours(path: Path) -> bool:
    """A real directory created by this installer (marker, or our manifest)."""
    if path.is_symlink() or not path.is_dir():
        return False
    if (path / MARKER).is_file():
        return True
    try:
        doc = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and doc.get("domain") == DEST_NAME


def recover(root: Path) -> bool:
    """Repair state left by an interrupted install. Return False if unsafe to continue."""
    dest, stage, backup = root / DEST_NAME, root / STAGE_NAME, root / BACKUP_NAME
    if backup.exists() or backup.is_symlink():
        if not _is_ours(backup):
            LOG.error("Unexpected %s is not managed by this App; not modifying config", backup)
            return False
        if not dest.exists() and not dest.is_symlink():
            LOG.warning("Restoring %s from interrupted-install backup", dest)
            backup.rename(dest)
        else:
            LOG.warning("Removing stale backup %s from a completed install", backup)
            shutil.rmtree(backup)
    if stage.exists() or stage.is_symlink():
        if stage.is_symlink() or not stage.is_dir():
            LOG.error("Unexpected %s is not a directory; not modifying config", stage)
            return False
        # An empty stage can only come from our mkdir() before the marker was written.
        if not _is_ours(stage) and any(stage.iterdir()):
            LOG.error("Unexpected %s is not managed by this App; not modifying config", stage)
            return False
        LOG.warning("Removing leftover install stage %s", stage)
        shutil.rmtree(stage)
    return True


def install(source: Path = SOURCE, root: Path = ROOT) -> str:
    """Install/update ``source`` into ``root``/jarvis_q1. Return a status string."""
    source, root = Path(source), Path(root)
    dest, stage, backup = root / DEST_NAME, root / STAGE_NAME, root / BACKUP_NAME
    if not source.is_dir() or not (source / "manifest.json").is_file():
        LOG.warning("Bundled integration not found at %s; skipping", source)
        return "no_source"
    if not root.parent.is_dir() or not os.access(root.parent, os.W_OK):
        LOG.warning("Home Assistant config is not writable/mounted; skipping companion install")
        return "not_writable"
    root.mkdir(parents=True, exist_ok=True)
    if not recover(root):
        return "unsafe_state"
    if dest.is_symlink():
        LOG.error("Refusing to replace symlink: %s", dest)
        return "symlink"
    if dest.exists() and not (dest / MARKER).is_file():
        LOG.warning("%s exists and is not managed by this App. Will not overwrite", dest)
        return "unmanaged"
    if dest.is_dir() and tree_hash(dest) == tree_hash(source):
        LOG.info("JARVIS Q1 integration already installed, no changes")
        return "unchanged"
    try:
        # Marker first, so an interrupted copy is recognised as ours on next start.
        stage.mkdir()
        (stage / MARKER).write_text("Created by JARVIS Voice App\n", encoding="utf-8")
        shutil.copytree(source, stage, symlinks=False, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
        if dest.exists():
            dest.rename(backup)
        stage.rename(dest)
    except Exception:
        if backup.exists() and not dest.exists():
            backup.rename(dest)
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        LOG.exception("Companion integration install failed; previous version kept")
        raise
    if backup.exists():
        shutil.rmtree(backup)
    LOG.warning("Installed/updated jarvis_q1 companion integration; restart Home Assistant Core once to load this version")
    return "installed"


def main(options_path: Path | None = None, source: Path | None = None, root: Path | None = None) -> str:
    options_path = Path(options_path or os.environ.get("OPTIONS_PATH", OPTIONS_PATH))
    source = Path(source or os.environ.get("JARVIS_INTEGRATION_SOURCE", SOURCE))
    root = Path(root or os.environ.get("JARVIS_CUSTOM_COMPONENTS", ROOT))
    options = load_options(options_path)
    if options is None:
        return "no_options"
    if not options.get("auto_install_integration", False):
        LOG.info("Companion integration auto-install disabled")
        return "disabled"
    return install(source, root)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - never block App startup on companion install
        LOG.exception("Companion integration install aborted")

"""Install bundled companion integration into HA config, only with explicit App option.

This app owns only /homeassistant/custom_components/jarvis_q1. Never modify other
integrations or HA configuration.yaml. A Home Assistant Core restart is required
after the initial installation/update; user controls that restart.
"""
import json
import logging
import os
from pathlib import Path
import shutil

LOG = logging.getLogger("jarvis.integration_install")
logging.basicConfig(level=logging.INFO)

SOURCE = Path("/opt/jarvis_q1_integration/jarvis_q1")
ROOT = Path("/homeassistant/custom_components")
DEST = ROOT / "jarvis_q1"
MARKER = ".managed_by_jarvis_voice"

def main():
    options_path = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))
    if not options_path.is_file():
        return
    options = json.loads(options_path.read_text(encoding="utf-8"))
    if not options.get("auto_install_integration", False):
        LOG.info("Companion integration auto-install disabled")
        return
    if not SOURCE.is_dir() or not (SOURCE / "manifest.json").is_file():
        LOG.warning("Bundled integration not found; skipping")
        return
    if not ROOT.parent.is_dir() or not os.access(ROOT.parent, os.W_OK):
        LOG.warning("Home Assistant config is not writable/mounted; skipping companion install")
        return
    ROOT.mkdir(parents=True, exist_ok=True)
    if DEST.is_symlink():
        LOG.error("Refusing to replace symlink: %s", DEST)
        return
    if DEST.exists() and not (DEST / MARKER).is_file():
        LOG.warning("%s exists and is not managed by this App. Will not overwrite", DEST)
        return
    desired = (SOURCE / "manifest.json").read_bytes()
    if DEST.exists() and (DEST / "manifest.json").is_file() and (DEST / "manifest.json").read_bytes() == desired:
        LOG.info("JARVIS Q1 integration already installed, no changes")
        return
    stage = ROOT / ".jarvis_q1_new"
    backup = ROOT / ".jarvis_q1_backup"
    if stage.exists() or backup.exists():
        LOG.error("Found interrupted install stage or backup; not modifying config. Inspect %s", ROOT)
        return
    shutil.copytree(SOURCE, stage, symlinks=False)
    (stage / MARKER).write_text("Created by JARVIS Voice App\n", encoding="utf-8")
    try:
        if DEST.exists():
            DEST.rename(backup)
        stage.rename(DEST)
    except Exception:
        if backup.exists() and not DEST.exists():
            backup.rename(DEST)
        raise
    if backup.exists():
        shutil.rmtree(backup)
    LOG.warning("Installed/updated jarvis_q1 companion integration; restart Home Assistant Core once to load this version")

if __name__ == "__main__":
    main()

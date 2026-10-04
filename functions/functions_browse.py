"""A small folder browser so the app knows the real path of a chosen picture.
(A web page cannot learn the path of a file picked with the normal file chooser, so it
could never delete the original.) Lists folders and picture files only."""

from __future__ import annotations

import os
import string
import sys
from pathlib import Path

from . import functions_config as cfg

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
MAX_ENTRIES = 500


def _places() -> list[dict]:
    home = Path.home()
    places = [{"name": "This folder", "path": str(cfg.LAUNCH_DIR),
               "note": "The folder Time Lock was started from"},
              {"name": "Home", "path": str(home)}]
    for name in ("Pictures", "Downloads", "Desktop", "Documents"):
        if (home / name).is_dir():
            places.append({"name": name, "path": str(home / name)})
    if sys.platform == "win32":
        for letter in string.ascii_uppercase:
            if os.path.exists(f"{letter}:\\"):
                places.append({"name": f"Drive {letter}:", "path": f"{letter}:\\"})
    else:
        for extra in ("/media", "/mnt"):
            if os.path.isdir(extra):
                places.append({"name": extra, "path": extra})
    return places


def browse(path: str | None) -> dict:
    target = Path(path).expanduser() if path else Path.home()
    try:
        target = target.resolve()
        if not target.is_dir():
            raise NotADirectoryError
        with os.scandir(target) as handle:
            entries = sorted(handle, key=lambda e: e.name.lower())
    except (OSError, NotADirectoryError):
        raise FileNotFoundError("That folder cannot be opened.")
    dirs, files = [], []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if entry.is_dir():
                dirs.append(entry.name)
            elif entry.is_file() and Path(entry.name).suffix.lower() in IMAGE_EXTENSIONS:
                files.append({"name": entry.name, "size": entry.stat().st_size})
        except OSError:
            continue
        if len(dirs) + len(files) >= MAX_ENTRIES:
            break
    parent = str(target.parent) if target.parent != target else None
    return {"path": str(target), "parent": parent, "dirs": dirs, "files": files, "places": _places()}

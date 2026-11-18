"""User storage for ALOUD and its integrations."""

import os
from pathlib import Path

_DATA_DIR = os.environ.get("ALOUD_DATA_DIR")
SUPPORT = (Path(_DATA_DIR).expanduser().resolve() if _DATA_DIR else
           Path.home() / "Library" / "Application Support" / "ALOUD")
CACHE = SUPPORT / "cache" if _DATA_DIR else Path.home() / "Library" / "Caches" / "ALOUD"
NOTES = SUPPORT / "notes" if _DATA_DIR else Path.home() / "Documents" / "ALOUD Notes"

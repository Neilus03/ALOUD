"""Create a local Finder launcher for the installed ALOUD package."""

from importlib.metadata import version
from importlib.resources import files
from pathlib import Path
import plistlib
import shlex
import sys


def main():
    bundle_id = "org.aloud-reader.app"
    app = Path(sys.argv[1]).expanduser().resolve() / "ALOUD.app"
    contents = app / "Contents"
    manifest = contents / "Info.plist"
    if app.exists():
        try:
            with manifest.open("rb") as f:
                existing = plistlib.load(f).get("CFBundleIdentifier")
        except (OSError, ValueError, plistlib.InvalidFileException):
            existing = None
        if existing != bundle_id:
            raise SystemExit(f"An app named ALOUD already exists at {app}. Choose a different ALOUD_APPS_DIR.")
    launcher = contents / "MacOS" / "ALOUD"
    icon = contents / "Resources" / "ALOUD.icns"
    for folder in (launcher.parent, icon.parent):
        folder.mkdir(parents=True, exist_ok=True)
    release = version("aloud-macos")
    manifest.write_bytes(plistlib.dumps({
        "CFBundleIdentifier": bundle_id,
        "CFBundleExecutable": launcher.name,
        "CFBundleIconFile": icon.name,
        "CFBundleName": "ALOUD",
        "CFBundleDisplayName": "ALOUD",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": release,
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
    }))
    icon.write_bytes((files("aloud") / "assets" / "ALOUD.icns").read_bytes())
    launcher.write_text(
        '#!/bin/sh\n'
        'mkdir -p "$HOME/Library/Logs"\n'
        f'exec {shlex.quote(sys.executable)} -m aloud "$@" >>"$HOME/Library/Logs/ALOUD.log" 2>&1\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    print(f"Installed {app}")


if __name__ == "__main__":
    main()

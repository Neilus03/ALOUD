"""Launch the ALOUD desktop reader."""

import argparse
from importlib.metadata import version
from pathlib import Path
import platform
import sys


def main():
    parser = argparse.ArgumentParser(prog="aloud", description="Read along with ALOUD.")
    parser.add_argument("document", nargs="?", type=Path, help="document to open")
    parser.add_argument("--version", action="version", version=f"ALOUD {version('aloud-macos')}")
    args = parser.parse_args()
    if sys.platform != "darwin" or platform.machine() != "arm64":
        parser.error("ALOUD requires macOS on Apple silicon.")
    if args.document is not None and not args.document.expanduser().is_file():
        parser.error(f"document not found: {args.document}")
    from .app import main as open_reader
    open_reader(args.document)


if __name__ == "__main__":
    main()

#!/bin/sh
set -eu

if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
    printf '%s\n' 'ALOUD requires macOS on Apple silicon.' >&2
    exit 1
fi

macos_version=$(sw_vers -productVersion)
if [ "${macos_version%%.*}" -lt 14 ]; then
    printf '%s\n' 'ALOUD requires macOS 14 or later.' >&2
    exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
    printf '%s\n' 'Install uv first. https://docs.astral.sh/uv/getting-started/installation/' >&2
    exit 1
fi

project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
install_dir="${ALOUD_INSTALL_DIR:-$HOME/.local/share/aloud-macos}"
apps_dir="${ALOUD_APPS_DIR:-$HOME/Applications}"
install_python="$install_dir/venv/bin/python"

if [ ! -x "$install_python" ]; then
    uv venv --python 3.12 "$install_dir/venv"
fi
uv pip sync --python "$install_python" "$project_dir/requirements-macos-arm64.txt"
uv pip install --python "$install_python" --no-deps --no-build-isolation --reinstall "$project_dir"
"$install_python" "$project_dir/scripts/make-app.py" "$apps_dir"

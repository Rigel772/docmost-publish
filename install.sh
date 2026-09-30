#!/usr/bin/env bash
# Install for the current Linux user without touching system Python packages.
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
  printf '%s\n' 'Run this installer as your normal user, not with sudo.' >&2
  exit 1
fi
command -v python3 >/dev/null || { printf '%s\n' 'python3 is required.' >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
  printf '%s\n' 'Python 3.11 or newer is required.' >&2; exit 1;
}
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/docmost-publisher"
VENV="$APP_DIR/venv"
BIN_DIR="$HOME/.local/bin"
LINK="$BIN_DIR/docmost-publish"
EXPECTED="$VENV/bin/docmost-publish"
if [[ -e "$LINK" || -L "$LINK" ]]; then
  if [[ ! -L "$LINK" || "$(readlink -- "$LINK")" != "$EXPECTED" ]]; then
    printf 'An existing command would be overwritten: %s\nMove/remove it explicitly before installing.\n' "$LINK" >&2
    exit 1
  fi
fi
if [[ -e "$VENV" && ! -f "$APP_DIR/.installed-by-docmost-publisher" ]]; then
  printf 'Existing unrecognized virtual environment: %s\n' "$VENV" >&2
  exit 1
fi
mkdir -p -- "$APP_DIR" "$BIN_DIR"
: > "$APP_DIR/.installed-by-docmost-publisher"
if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV" || {
    printf '%s\n' 'Creating a Python virtual environment failed. Install/repair Python and pip for your distribution, then rerun.' >&2
    exit 1
  }
fi
"$VENV/bin/python" -m pip install --disable-pip-version-check "$SOURCE_DIR"
ln -sfn -- "$EXPECTED" "$LINK"
printf '\nInstalled: %s\n\n' "$LINK"
printf '%s\n' 'Bash/zsh, when ~/.local/bin is not already on PATH:' '  export PATH="$HOME/.local/bin:$PATH"' \
  '' 'Fish:' '  fish_add_path ~/.local/bin' '' 'Check:' '  docmost-publish --help'

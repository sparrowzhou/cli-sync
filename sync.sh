#!/usr/bin/env bash
# Set up and run Cli Sync on macOS or Linux.
# Use --setup for setup only, --menu for the wizard, or --help for task commands.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${DIR}/.venv/bin/python"
OS="$(uname)"
PIP_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"   # Fallback mirror for package downloads.

say() { printf '%s\n' "$1"; }
die() { printf '%s\n' "❌ $1" >&2; exit 1; }

# Use sudo for installation only when needed.
as_root() {
  if [ "$(id -u)" = "0" ]; then "$@"
  elif command -v sudo >/dev/null; then sudo "$@"
  else die "Administrator rights are needed to install: $* (no sudo; run once as root)"
  fi
}

py_ok() {  # Require Python 3.10 or newer.
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null
}

find_python() {
  local c
  for c in python3 python3.14 python3.13 python3.12 python3.11 python3.10 \
           /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$c" >/dev/null 2>&1 && py_ok "$c"; then command -v "$c"; return 0; fi
  done
  return 1
}

install_python() {
  if [ "${OS}" = "Darwin" ]; then
    local brew=""
    for b in brew /opt/homebrew/bin/brew /usr/local/bin/brew; do
      command -v "$b" >/dev/null 2>&1 && { brew="$(command -v "$b")"; break; }
    done
    if [ -n "${brew}" ]; then
      say "Python 3.10+ not found; installing it with Homebrew (a few minutes)…"
      "${brew}" install python || die "Homebrew failed to install Python"
    else
      say "This Mac has neither Python 3.10+ nor Homebrew."
      say "Opened the python.org download page: install the macOS package, then run this again."
      open "https://www.python.org/downloads/macos/" 2>/dev/null
      exit 1
    fi
  elif command -v apt-get >/dev/null; then
    say "Python 3.10+ not found; installing it (you may be asked for your login password)…"
    as_root apt-get update -qq && as_root apt-get install -y python3 python3-venv \
      || die "Installing Python failed; run: sudo apt install python3 python3-venv"
  elif command -v dnf >/dev/null; then
    say "Python 3.10+ not found; installing it (you may be asked for your login password)…"
    as_root dnf install -y python3 || die "Installing Python failed; run: sudo dnf install python3"
  else
    die "Python 3.10+ not found; install python3 with your package manager first"
  fi
}

make_venv() {
  local base="$1"
  rm -rf "${DIR}/.venv"
  "${base}" -m venv "${DIR}/.venv" >/dev/null 2>&1 && return 0
  # Debian and Ubuntu may package the venv module separately.
  if [ "${OS}" = "Linux" ] && command -v apt-get >/dev/null; then
    local ver
    ver="$("${base}" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
    say "Installing python${ver}-venv (you may be asked for your login password)…"
    as_root apt-get update -qq
    as_root apt-get install -y python3-venv "python${ver}-venv" >/dev/null 2>&1 \
      || as_root apt-get install -y python3-venv \
      || die "Installing python3-venv failed; run: sudo apt install python3-venv"
    rm -rf "${DIR}/.venv"
    "${base}" -m venv "${DIR}/.venv" && return 0
  fi
  die "Creating the virtual environment failed (${base} -m venv)"
}

pip_install() {
  local pip=("${PY}" -m pip install -q --disable-pip-version-check -r "${DIR}/requirements.txt")
  "${pip[@]}" 2>/dev/null && return 0
  say "Download from PyPI failed; retrying with a mirror…"
  "${pip[@]}" -i "${PIP_MIRROR}" && return 0
  die "Installing dependencies failed; check the network (pypi.org or ${PIP_MIRROR} must be reachable)"
}

setup() {
  # Reuse the existing environment when it works.
  if [ -x "${PY}" ] && "${PY}" -c "import paramiko" 2>/dev/null; then return 0; fi
  say "Preparing the runtime environment (only once)…"
  local base
  base="$(find_python)" || { install_python; base="$(find_python)" || die "Python 3.10+ still not found after installing"; }
  say "Using $("${base}" --version 2>&1) (${base})"
  make_venv "${base}"
  pip_install
  "${PY}" -c "import paramiko" 2>/dev/null || die "Dependencies still unusable after installing; send the output above to your maintainer"
  say "✅ Runtime environment is ready"
}

setup
if [ "${1:-}" = "--setup" ]; then
  [ -n "${SFTP_SYNC_QUIET_SETUP:-}" ] && exit 0
  pv="$("${PY}" --version 2>&1)"; mv="$("${PY}" -c 'import paramiko; print(paramiko.__version__)')"
  say "Environment OK: ${pv}, paramiko ${mv}"
  exit 0
fi

case "${1:-}" in
  --menu) exec "${PY}" "${DIR}/sftp_sync.py" --menu ;;
esac
# Resolve the task in Python or forward the configured file location.
if [ -n "${SFTP_SYNC_CONFIG:-}" ]; then
  exec "${PY}" "${DIR}/sftp_sync.py" -c "${SFTP_SYNC_CONFIG}" "$@"
fi
exec "${PY}" "${DIR}/sftp_sync.py" "$@"

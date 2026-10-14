#!/usr/bin/env bash
# decomp.wtf installer for Linux (Windows: install.ps1; macOS support is coming later).
#
#   curl -fsSL https://decomp.wtf/install.sh | bash
#
# What it does, and nothing else:
#   1. installs uv, Astral's Python tool manager, if you don't have it (asks first)
#   2. installs decomp.wtf (open source, MIT) with uv, in its own environment
#   3. downloads the free N64 compilers and the m2c draft decompiler (pinned and checksum-verified)
#   4. adds decomp.wtf to your apps menu and opens it
#
# Your projects go to ~/Decomp. Nothing is sent to decomp.wtf.
# Uninstall: uv tool uninstall decompnet   (and delete ~/Decomp if you want your projects gone too)
# Options: --no-start (install only)
set -euo pipefail

VERSION="0.9.6"
WHEEL="${DECOMP_WHEEL:-https://decomp.wtf/dl/decompnet-${VERSION}-py3-none-any.whl}"
START=1
for arg in "$@"; do
  case "$arg" in
    --no-start) START=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

has_tty() { (exec </dev/tty) 2>/dev/null; }  # a terminal we can read, even when piped from curl
bold() { printf '\033[1m%s\033[0m\n' "$*"; }
step() { printf '\033[32m▸\033[0m %s\n' "$*"; }
fail() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
ask() {
  local reply
  has_tty || return 0
  printf '%s [Y/n] ' "$1" > /dev/tty
  read -r reply < /dev/tty || true
  [[ -z "$reply" || "$reply" =~ ^[Yy] ]]
}

bold "decomp.wtf setup"
case "$(uname -s)" in
  Linux) ;;
  Darwin)
    # not tested on a Mac yet, so not offered there: better than a half-working install
    [ "${DECOMP_ALLOW_MACOS:-}" = 1 ] || fail "decomp.wtf supports Linux and Windows for now; macOS support is coming later (https://decomp.wtf)" ;;
  *) fail "this installer is for Linux; on Windows run in PowerShell: irm https://decomp.wtf/install.ps1 | iex" ;;
esac
command -v curl >/dev/null || fail "curl is required"

for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do [ -x "$d/uv" ] && export PATH="$d:$PATH"; done
if ! command -v uv >/dev/null; then
  ask "decomp.wtf is installed with uv (Astral's Python tool manager). Install uv now?" || fail "uv is needed: https://docs.astral.sh/uv/"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null || fail "uv installed, but it isn't on PATH yet; open a new terminal and run this again"
fi
step "uv $(uv --version | cut -d' ' -f2)"

step "installing decomp.wtf ${VERSION}"
uv tool install --force --quiet --python 3.12 "decompnet @ ${WHEEL}"
BIN="$(uv tool dir --bin)"
uv tool update-shell >/dev/null 2>&1 || true

step "downloading the compilers and tools"
"$BIN/decomp" setup

# an older decomp.wtf still running from before this upgrade: stop it, so the new version takes over
"$BIN/decomp" stop >/dev/null 2>&1 || true

"$BIN/decomp" shortcut >/dev/null 2>&1 && step "added decomp.wtf to your apps" || true

# Your AI agents with decomp.wtf's tools: only when someone is there to say yes (it changes their configs)
if has_tty && "$BIN/decomp" connect --list 2>/dev/null | grep -q "not connected"; then
  if ask "Give your AI agents (Claude Code, Codex, Antigravity CLI, opencode) decomp.wtf's tools, so you can just ask them to decompile a game?"; then
    "$BIN/decomp" connect | sed 's/^/  /'
  fi
fi

bold "Done."
echo "  Start it any time from your apps menu, or with: decomp"
echo "  From your AI agent: run 'decomp connect' once, then ask it to decompile your game."
if [ "$START" = 1 ]; then
  if [ "$(uname -s)" = Darwin ] || [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    nohup "$BIN/decomp" >/dev/null 2>&1 &
    echo "  Opening decomp.wtf in your browser…"
  else
    echo "  No desktop session found (SSH?). Run 'decomp' on the machine itself."
  fi
fi

#!/usr/bin/env bash
# decomp.wtf installer for Linux and macOS (Windows: install.ps1, in PowerShell).
#
#   curl -fsSL https://decomp.wtf/install.sh | bash
#
# Linux: decomp.wtf installs on this computer, with uv:
#   1. installs uv, Astral's Python tool manager, if you don't have it (asks first)
#   2. installs decomp.wtf (open source, MIT) with uv, in its own environment
#   3. downloads the free compilers and the m2c draft decompiler (pinned and checksum-verified)
#   4. adds decomp.wtf to your apps menu and opens it
# macOS: decomp.wtf runs in Docker (Docker Desktop, OrbStack or Colima): the compilers are Linux programs, so it
#   gets its own small Linux there, the same one it has on Windows. This script builds it, starts it, and puts a
#   `decomp` command and a decomp.wtf app on your Mac.
#
# Your projects go to ~/Decomp (macOS: inside Docker's decomp-wtf-home volume). Nothing is sent to decomp.wtf.
# Uninstall: uv tool uninstall decompnet   (macOS: decomp uninstall)
#
# Options:
#   --no-start          install only
#   --docker            Linux: run decomp.wtf in Docker, as on macOS
#   --source <what>     where decomp.wtf comes from: "release" (default), "branch:<name>" (a branch on GitHub, for
#                       testing; its updates follow that branch) or a folder with decomp.wtf's source. Run from a
#                       git checkout, the checkout is used (and updates follow its branch).
# The same through the environment: DECOMP_SOURCE=..., DECOMP_BRANCH=<name>.
set -euo pipefail

VERSION="0.9.8"
REPO="i12bp8/decomp.wtf"
START=1
DOCKER=0
SOURCE="${DECOMP_SOURCE:-}"
[ -z "$SOURCE" ] && [ -n "${DECOMP_BRANCH:-}" ] && SOURCE="branch:${DECOMP_BRANCH}"
while [ $# -gt 0 ]; do
  case "$1" in
    --no-start) START=0 ;;
    --docker) DOCKER=1 ;;
    --source) shift; SOURCE="${1:-}" ;;
    --source=*) SOURCE="${1#--source=}" ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
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

# Run from a checkout (site/public/install.sh in the repository): install that checkout
SCRIPT_DIR=""
CHECKOUT=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ -f "$SCRIPT_DIR/../../pyproject.toml" ] && grep -q '^name = "decompnet"' "$SCRIPT_DIR/../../pyproject.toml"; then
    CHECKOUT="$(cd "$SCRIPT_DIR/../.." && pwd)"
  fi
fi
[ -n "$SOURCE" ] || SOURCE="${CHECKOUT:-release}"

# decomp.wtf's own Linux (WSL on Windows, Docker): made by linux-base.sh; then this installs for its user only
IN_BOX=""
[ -f /etc/decomp-wtf-runtime ] && IN_BOX="$(cat /etc/decomp-wtf-runtime)"

bold "decomp.wtf setup"
OS="$(uname -s)"
case "$OS" in
  Linux) ;;
  Darwin) DOCKER=1 ;;
  *) fail "this installer is for Linux and macOS; on Windows run in PowerShell: irm https://decomp.wtf/install.ps1 | iex" ;;
esac
[ -n "$IN_BOX" ] && DOCKER=0
command -v curl >/dev/null || fail "curl is required"

# ------------------------------------------------------------------------------------------- where it comes from
# CHANNEL/BRANCH/COMMIT are remembered (decomp install-info): the app's updates come from the same place.
CHANNEL=release BRANCH="" COMMIT="" SPEC="" SRC_DIR=""
case "$SOURCE" in
  release)
    SPEC="decompnet @ ${DECOMP_WHEEL:-https://decomp.wtf/dl/decompnet-${VERSION}-py3-none-any.whl}" ;;
  branch:*)
    CHANNEL=branch BRANCH="${SOURCE#branch:}"
    [ -n "$BRANCH" ] || fail "--source branch:<name> needs a branch name" ;;
  *)
    SRC_DIR="$(cd "$SOURCE" 2>/dev/null && pwd)" || fail "no folder $SOURCE (--source is release, branch:<name> or a folder)"
    [ -f "$SRC_DIR/pyproject.toml" ] && [ -d "$SRC_DIR/src/decompnet" ] || fail "$SRC_DIR isn't decomp.wtf's source"
    if command -v git >/dev/null; then  # a checkout: updates follow its branch on GitHub
      BRANCH="$(git -c safe.directory='*' -C "$SRC_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
      COMMIT="$(git -c safe.directory='*' -C "$SRC_DIR" rev-parse HEAD 2>/dev/null || true)"
      [ "$BRANCH" = HEAD ] && BRANCH=""
      [ -n "$BRANCH" ] && CHANNEL=branch
    fi ;;
esac

resolve_branch() {  # the branch's newest commit, installed from GitHub's archive of it
  local api
  api="$(curl -fsSL -H 'Accept: application/vnd.github+json' "https://api.github.com/repos/$REPO/commits/$BRANCH" 2>/dev/null || true)"
  COMMIT="$(printf '%s' "$api" | grep -m1 '"sha"' | sed 's/.*"sha": *"\([0-9a-f]*\)".*/\1/' || true)"
  if [ -n "$COMMIT" ]; then
    SPEC="decompnet @ https://github.com/$REPO/archive/$COMMIT.tar.gz"
  else  # GitHub's API said no (rate limit): the branch as it is now
    SPEC="decompnet @ https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz"
  fi
}

# ------------------------------------------------------------------------------------------- Docker (macOS)
docker_install() {
  local ctx port host_os log f base id i url follow
  local -a mounts
  command -v docker >/dev/null || fail "decomp.wtf runs in Docker on macOS. Install Docker Desktop (https://www.docker.com/products/docker-desktop/), OrbStack (https://orbstack.dev) or Colima (brew install colima docker && colima start), then run this again."
  docker info >/dev/null 2>&1 || fail "Docker isn't running: start Docker Desktop (or OrbStack, or: colima start), then run this again."
  host_os=linux; [ "$OS" = Darwin ] && host_os=macos

  ctx="$(mktemp -d)"
  base="https://decomp.wtf"
  [ "$CHANNEL" = branch ] && [ -z "$SRC_DIR" ] && base="https://raw.githubusercontent.com/$REPO/$BRANCH/site/public"
  for f in Dockerfile linux-base.sh install.sh; do
    if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/$f" ]; then cp "$SCRIPT_DIR/$f" "$ctx/$f"
    else curl -fsSL "$base/$f" -o "$ctx/$f" || fail "couldn't download $base/$f"; fi
  done

  step "building decomp.wtf's Linux for Docker (a few minutes the first time)"
  log="$ctx/build.log"
  if ! docker build --platform linux/amd64 -t decomp-wtf:latest "$ctx" >"$log" 2>&1; then
    tail -n 25 "$log" >&2
    fail "building the image failed (above)"
  fi

  # the port an earlier install used (the browser remembers it), else the first free one from 6464
  port="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' decomp-wtf 2>/dev/null | sed -n 's/^DECOMP_PORT=//p' | head -n1 || true)"
  if [ -z "$port" ]; then
    port=6464
    while (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; do port=$((port + 1)); done
  fi
  docker rm -f decomp-wtf >/dev/null 2>&1 || true  # the volume with the projects stays

  # your home, read-only, at the same path: games you name by path (or your agent does) are found as they are
  mounts=(-v decomp-wtf-home:/home/decomp -v "$HOME:$HOME:ro")
  if [ -n "$SRC_DIR" ]; then
    case "$SRC_DIR/" in "$HOME"/*) ;; *) mounts+=(-v "$SRC_DIR:$SRC_DIR:ro") ;; esac
  fi
  id="$(date +%s)-$$"
  step "starting decomp.wtf in Docker (port $port)"
  docker run -d --name decomp-wtf --platform linux/amd64 --init --restart on-failure:5 \
    -p "127.0.0.1:$port:$port" -e DECOMP_PORT="$port" -e DECOMP_SOURCE="$SOURCE" -e DECOMP_INSTALL_ID="$id" \
    -e DECOMP_HOST_HOME="$HOME" -e DECOMP_HOST_OS="$host_os" "${mounts[@]}" decomp-wtf:latest >/dev/null

  step "installing decomp.wtf inside (a few minutes the first time)"
  docker logs -f decomp-wtf 2>&1 &  # what the installer inside says, while it works
  follow=$!
  i=0
  until docker exec decomp-wtf curl -fsS "http://127.0.0.1:$port/health" >/dev/null 2>&1; do
    if [ "$(docker inspect -f '{{.State.Running}}' decomp-wtf 2>/dev/null)" != true ]; then
      kill "$follow" 2>/dev/null || true
      fail "decomp.wtf stopped while installing (its output is above; again with: docker logs decomp-wtf)"
    fi
    i=$((i + 1)); [ "$i" -lt 1800 ] || { kill "$follow" 2>/dev/null; fail "decomp.wtf didn't start in 30 minutes (docker logs decomp-wtf)"; }
    sleep 1
  done
  kill "$follow" 2>/dev/null || true
  wait "$follow" 2>/dev/null || true

  install_host_command
  bold "Done."
  echo "  decomp.wtf runs in Docker (container decomp-wtf; your projects are in its volume decomp-wtf-home)."
  echo "  Open it any time: decomp.wtf in your Applications, or type: decomp"
  echo "  A terminal inside it (to sign in the agents it runs for you): decomp shell"
  if has_tty && { command -v claude || command -v codex || command -v agy || command -v opencode; } >/dev/null 2>&1; then
    if ask "Give your AI agents (Claude Code, Codex, Antigravity CLI, opencode) decomp.wtf's tools, so you can just ask them to decompile a game?"; then
      "$HOME/.local/bin/decomp" connect | sed 's/^/  /'
    fi
  fi
  if [ "$START" = 1 ]; then
    url="$(docker exec decomp-wtf decomp url 2>/dev/null || true)"
    if [ -n "$url" ]; then
      echo "  Opening decomp.wtf in your browser…"
      if [ "$OS" = Darwin ]; then open "$url"; elif command -v xdg-open >/dev/null; then xdg-open "$url" >/dev/null 2>&1 || echo "  $url"; else echo "  $url"; fi
    fi
  fi
}

install_host_command() {
  local bin="$HOME/.local/bin" rc
  mkdir -p "$bin"
  cat > "$bin/decomp" <<'WRAPPER'
#!/bin/sh
# decomp.wtf runs in Docker on this computer (container decomp-wtf, made by https://decomp.wtf/install.sh).
# This is its `decomp` command here: it starts the container when needed and runs decomp.wtf's commands inside.
#   decomp            open decomp.wtf in your browser        decomp shell     a terminal inside
#   decomp connect    give your AI agents its tools          decomp stop      stop it
#   decomp update     install the newest decomp.wtf          decomp uninstall remove it (asks about projects)
C=decomp-wtf
up() {
  docker info >/dev/null 2>&1 || { echo "Docker isn't running: start Docker Desktop (or OrbStack, or: colima start) first" >&2; exit 1; }
  if [ "$(docker inspect -f '{{.State.Running}}' "$C" 2>/dev/null)" != true ]; then
    docker start "$C" >/dev/null 2>&1 || { echo "decomp.wtf's container is gone: install again with: curl -fsSL https://decomp.wtf/install.sh | bash" >&2; exit 1; }
  fi
  i=0
  until docker exec "$C" sh -c 'curl -fsS "http://127.0.0.1:$DECOMP_PORT/health"' >/dev/null 2>&1; do
    i=$((i + 1))
    [ "$i" -lt 900 ] || { echo "decomp.wtf didn't start; see: docker logs $C" >&2; exit 1; }
    sleep 1
  done
}
open_url() {
  if [ "$(uname -s)" = Darwin ]; then open "$1"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1 || echo "$1"
  else echo "$1"; fi
}
connect() {  # the agent CLIs on this computer start `decomp mcp` here, which reaches decomp.wtf inside
  me="$HOME/.local/bin/decomp" n=0
  if command -v claude >/dev/null 2>&1; then
    claude mcp remove -s user decomp >/dev/null 2>&1 || true
    claude mcp add -s user decomp -e DECOMP_AGENT_NAME=claude-code -- "$me" mcp >/dev/null && echo "Claude Code: connected"; n=1
  fi
  if command -v codex >/dev/null 2>&1; then
    codex mcp remove decomp >/dev/null 2>&1 || true
    codex mcp add --env DECOMP_AGENT_NAME=codex decomp -- "$me" mcp >/dev/null && echo "Codex: connected"; n=1
  fi
  if command -v agy >/dev/null 2>&1; then
    agy mcp add -e DECOMP_AGENT_NAME=antigravity decomp -- "$me" mcp >/dev/null && echo "Antigravity CLI: connected"; n=1
  fi
  if command -v opencode >/dev/null 2>&1; then
    echo "opencode: add this to ~/.config/opencode/opencode.json:"
    echo "  \"mcp\": {\"decomp\": {\"type\": \"local\", \"command\": [\"$me\", \"mcp\"], \"environment\": {\"DECOMP_AGENT_NAME\": \"opencode\"}}}"
    n=1
  fi
  [ "$n" = 1 ] || { echo "No agent CLI found (Claude Code, Codex, Antigravity CLI, opencode). Install one, then run: decomp connect"; return 1; }
  echo 'Now ask your agent, for example: "decompile my game at ~/roms/game.z64"'
}
case "${1:-}" in
  ""|open) up; url="$(docker exec "$C" decomp url)" && open_url "$url" ;;
  --no-browser) up ;;
  url) up; exec docker exec "$C" decomp url ;;
  mcp) shift; up >&2; exec docker exec -i -e DECOMP_AGENT_NAME "$C" /usr/local/bin/decomp mcp "$@" ;;
  shell) up; exec docker exec -it "$C" bash -l ;;
  stop) docker stop "$C" >/dev/null 2>&1; echo "decomp.wtf stopped" ;;
  logs) exec docker logs -f "$C" ;;
  connect) shift; connect "$@" ;;
  uninstall)
    docker rm -f "$C" >/dev/null 2>&1; docker image rm decomp-wtf:latest >/dev/null 2>&1
    rm -rf "$HOME/Applications/decomp.wtf.app"
    printf 'Delete your projects too (Docker volume decomp-wtf-home)? [y/N] '; read -r a
    case "$a" in y|Y|yes) docker volume rm decomp-wtf-home >/dev/null && echo "projects deleted" ;; *) echo "kept: docker volume decomp-wtf-home" ;; esac
    rm -f "$0"; echo "decomp.wtf removed" ;;
  *) up
     if [ -t 0 ] && [ -t 1 ]; then exec docker exec -it "$C" /usr/local/bin/decomp "$@"; fi
     exec docker exec -i "$C" /usr/local/bin/decomp "$@" ;;
esac
WRAPPER
  chmod 755 "$bin/decomp"
  case ":$PATH:" in
    *":$bin:"*) ;;
    *) rc="$HOME/.zshrc"; [ "${SHELL##*/}" = bash ] && rc="$HOME/.bashrc"
       grep -qs '.local/bin' "$rc" || printf '\nexport PATH="$HOME/.local/bin:$PATH"  # decomp.wtf\n' >> "$rc"
       echo "  (added ~/.local/bin to your PATH in $rc: open a new terminal for the decomp command)" ;;
  esac
  if [ "$OS" = Darwin ]; then  # an app that opens it, for Launchpad, Spotlight and the Dock
    local app="$HOME/Applications/decomp.wtf.app"
    mkdir -p "$app/Contents/MacOS"
    cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>decomp.wtf</string>
<key>CFBundleIdentifier</key><string>wtf.decomp.app</string>
<key>CFBundleExecutable</key><string>decomp-wtf</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSUIElement</key><true/>
</dict></plist>
PLIST
    printf '#!/bin/sh\nPATH="/usr/local/bin:/opt/homebrew/bin:$HOME/.orbstack/bin:$PATH" exec "%s/decomp" >/dev/null 2>&1\n' "$bin" > "$app/Contents/MacOS/decomp-wtf"
    chmod 755 "$app/Contents/MacOS/decomp-wtf"
    step "added decomp.wtf to ~/Applications"
  fi
}

if [ "$DOCKER" = 1 ]; then
  docker_install
  exit 0
fi

# ------------------------------------------------------------------------------------------- on this Linux
for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do [ -x "$d/uv" ] && export PATH="$d:$PATH"; done
if ! command -v uv >/dev/null; then
  if [ -z "$IN_BOX" ]; then
    ask "decomp.wtf is installed with uv (Astral's Python tool manager). Install uv now?" || fail "uv is needed: https://docs.astral.sh/uv/"
  fi
  if [ -n "$IN_BOX" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
  else
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null || fail "uv installed, but it isn't on PATH yet; open a new terminal and run this again"
fi
step "uv $(uv --version | cut -d' ' -f2)"

if [ -n "$SRC_DIR" ]; then
  # a copy of the source (not the checkout itself: building leaves nothing behind in it, and it may be read-only)
  TMP_SRC="$(mktemp -d)"
  cp -R "$SRC_DIR/pyproject.toml" "$SRC_DIR/README.md" "$SRC_DIR/LICENSE" "$SRC_DIR/src" "$TMP_SRC/"
  find "$TMP_SRC" -name __pycache__ -type d -prune -exec rm -rf {} +
  SPEC="$TMP_SRC"
  step "installing decomp.wtf from $SRC_DIR${BRANCH:+ ($BRANCH @ ${COMMIT:0:7})}"
elif [ "$CHANNEL" = branch ]; then
  resolve_branch
  step "installing decomp.wtf from the $BRANCH branch${COMMIT:+ @ ${COMMIT:0:7}}"
else
  step "installing decomp.wtf ${VERSION}"
fi
uv tool install --force --quiet --python 3.12 "$SPEC"
[ -n "${TMP_SRC:-}" ] && rm -rf "$TMP_SRC"
BIN="$(uv tool dir --bin)"
[ -n "$IN_BOX" ] || uv tool update-shell >/dev/null 2>&1 || true
"$BIN/decomp" install-info --channel "$CHANNEL" --branch "$BRANCH" --commit "$COMMIT" --repo "$REPO" >/dev/null

step "downloading the compilers and tools"
"$BIN/decomp" setup

# an older decomp.wtf still running from before this upgrade: stop it, so the new version takes over
"$BIN/decomp" stop >/dev/null 2>&1 || true

if [ -n "$IN_BOX" ]; then  # decomp.wtf's own Linux: the installer on Windows/macOS does the rest
  step "decomp.wtf is installed"
  exit 0
fi

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
  if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    nohup "$BIN/decomp" >/dev/null 2>&1 &
    echo "  Opening decomp.wtf in your browser…"
  else
    echo "  No desktop session found (SSH?). Run 'decomp' on the machine itself."
  fi
fi

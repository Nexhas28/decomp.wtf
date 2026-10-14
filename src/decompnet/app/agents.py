"""Running the user's AI agents on a ROM project.

CLI agents (Claude Code, Codex, Gemini CLI, opencode) run headless in a private working folder, connected
to this app's MCP endpoint and allowed to use only its tools. Each session works through a few functions
and exits; the runner starts a fresh one until the user stops it or nothing is left. Models reached with
an API key (DeepSeek, OpenRouter, a local Ollama, any OpenAI-compatible endpoint) run in a small built-in
tool loop instead. Everything the agents print is parsed into a readable live feed.
"""

from __future__ import annotations

import collections
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import tools
from .engine import RomNetwork

WIN = sys.platform == "win32"


@dataclass
class Kind:
    id: str
    name: str
    command: str  # executable, "" for the built-in agent
    vendor: str
    install: dict[str, str]  # os -> command
    signin: str
    docs: str


KINDS = {
    "claude": Kind("claude", "Claude Code", "claude", "Anthropic",
                   {"unix": "curl -fsSL https://claude.ai/install.sh | bash",
                    "windows": "irm https://claude.ai/install.ps1 | iex"},
                   "Open a terminal, run `claude` and sign in with /login.", "https://docs.claude.com/en/docs/claude-code"),
    "codex": Kind("codex", "Codex CLI", "codex", "OpenAI",
                  {"unix": "npm install -g @openai/codex", "windows": "npm install -g @openai/codex"},
                  "Run `codex login` once in a terminal.", "https://github.com/openai/codex"),
    "gemini": Kind("gemini", "Gemini CLI", "gemini", "Google",
                   {"unix": "npm install -g @google/gemini-cli", "windows": "npm install -g @google/gemini-cli"},
                   "Open a terminal and run `gemini` once to sign in.", "https://github.com/google-gemini/gemini-cli"),
    "opencode": Kind("opencode", "opencode", "opencode", "any provider (DeepSeek, Qwen, Kimi, ...)",
                     {"unix": "curl -fsSL https://opencode.ai/install | bash", "windows": "npm install -g opencode-ai"},
                     "Run `opencode auth login` once to add a provider (DeepSeek, OpenRouter, ...).", "https://opencode.ai"),
    "api": Kind("api", "API key", "", "DeepSeek, OpenRouter, Ollama, any OpenAI-compatible API", {}, "",
                "https://api-docs.deepseek.com"),
}

API_PRESETS = {
    "deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "key": True},
    "openrouter": {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "model": "deepseek/deepseek-chat", "key": True},
    "ollama": {"name": "Ollama (local)", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen3-coder", "key": False},
    "lmstudio": {"name": "LM Studio (local)", "base_url": "http://127.0.0.1:1234/v1", "model": "local-model", "key": False},
    "openai": {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-5", "key": True},
    "custom": {"name": "Custom", "base_url": "", "model": "", "key": True},
}

# what an agent CLI says when nobody signed in to it (Claude Code: "Not logged in · Please run /login")
_SIGNED_OUT = re.compile(r"not logged in|run /login|invalid api key|not authenticated|please (?:log|sign) ?in|"
                         r"auth(?:entication)? method", re.I)

_versions: dict[str, tuple[float, str | None]] = {}
_models: dict[str, tuple[float, list[dict]]] = {}

# Claude Code's aliases always point at Anthropic's latest model of each kind.
CLAUDE_MODELS = [("fable", "Fable", "Anthropic's most capable model"), ("opus", "Opus", "very capable"),
                 ("sonnet", "Sonnet", "fast and capable, cheaper"), ("haiku", "Haiku", "fastest, cheapest")]


def _config_model(kind: str) -> str | None:
    """The model the user set as default in the agent's own settings, if any."""
    home = Path.home()
    try:
        if kind == "claude":
            return json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8")).get("model") or None
        if kind == "codex":
            import tomllib
            return tomllib.loads((home / ".codex" / "config.toml").read_text(encoding="utf-8")).get("model") or None
        if kind == "gemini":
            m = json.loads((home / ".gemini" / "settings.json").read_text(encoding="utf-8")).get("model")
            return (m.get("name") if isinstance(m, dict) else m) or None
        if kind == "opencode":
            base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
            return json.loads((base / "opencode" / "opencode.json").read_text(encoding="utf-8")).get("model") or None
    except (OSError, ValueError, AttributeError):
        return None
    return None


def models(kind: str, path: str | None) -> list[dict]:
    """Models to offer for this agent: [{"id", "name", "note"}]. Claude Code's aliases; opencode's own list
    (whatever providers the user signed in to); for the others the user's default plus any model name they type."""
    if kind == "claude":
        return [{"id": i, "name": n, "note": note} for i, n, note in CLAUDE_MODELS]
    if kind == "opencode" and path:
        cached = _models.get(kind)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        try:
            res = subprocess.run(argv([path, "models"]), capture_output=True, text=True,
                                 encoding="utf-8", errors="replace",
                                 timeout=30)
            rows = [line.strip() for line in res.stdout.splitlines() if re.fullmatch(r"[\w.-]+/[\w.:/-]+", line.strip())]
        except (OSError, subprocess.TimeoutExpired):
            rows = []
        out = [{"id": r, "name": r.split("/", 1)[1], "note": r.split("/", 1)[0]} for r in rows[:200]]
        _models[kind] = (time.time(), out)
        return out
    return []


def search_path() -> str | None:
    """Where to look for agents and npm (None: this process's PATH). On Windows also the PATH set for the user right
    now, read from the registry: an agent or Node.js installed while the app runs is found without restarting it."""
    if not WIN:
        return None
    import winreg

    dirs = os.environ.get("PATH", "").split(os.pathsep)
    for root, key in ((winreg.HKEY_CURRENT_USER, "Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, key) as k:
                dirs += [os.path.expandvars(d) for d in str(winreg.QueryValueEx(k, "Path")[0]).split(";")]
        except OSError:
            pass
    return os.pathsep.join(dict.fromkeys(d for d in dirs if d))


def _which(cmd: str) -> str | None:
    found = shutil.which(cmd, path=search_path())
    if found:
        return found
    home = Path.home()
    dirs = [home / ".local" / "bin", home / ".claude" / "local", home / ".opencode" / "bin", home / ".npm-global" / "bin"]
    if WIN and os.environ.get("APPDATA"):
        dirs.append(Path(os.environ["APPDATA"]) / "npm")  # npm's global installs on Windows
    # on Windows the file without an extension next to npm's foo.cmd is a shell script Windows can't run
    names = (f"{cmd}.exe", f"{cmd}.cmd") if WIN else (cmd, f"{cmd}.cmd", f"{cmd}.exe")
    for d in dirs:
        for name in names:
            if (d / name).exists():
                return str(d / name)
    return None


# npm's Windows launcher (cmd-shim) ends with: "%_prog%" [node options] "%dp0%\node_modules\...\cli.js" %*
_NPM_SHIM = re.compile(r'"%_prog%"\s+(?P<opts>[^"\r\n]*?)\s*"%~?dp0%?\\(?P<script>[^"\r\n]+)"\s+%\*', re.I)


def argv(cmd: list[str]) -> list[str]:
    """The command to start, as Windows needs it. npm installs agents there as .cmd scripts, which run through cmd.exe:
    it cuts an argument at its first line break (a prompt loses everything after its first line, and the flags after
    it with it) and treats & | < > in it as commands. Start node with the agent's script instead, as the .cmd would."""
    if not WIN or Path(cmd[0]).suffix.lower() not in (".cmd", ".bat"):
        return cmd
    shim = Path(cmd[0])
    try:
        m = _NPM_SHIM.search(shim.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        m = None
    if not m:
        return cmd
    script = shim.parent / m["script"]
    node = shim.parent / "node.exe"
    node = str(node) if node.exists() else shutil.which("node", path=search_path())
    if not node or not script.exists():
        return cmd
    return [node, *m["opts"].split(), str(script), *cmd[1:]]


def detect() -> list[dict]:
    out = []
    for k in KINDS.values():
        row = {"id": k.id, "name": k.name, "vendor": k.vendor, "signin": k.signin, "docs": k.docs,
               "install": k.install.get("windows" if WIN else "unix", "")}
        if not k.command:
            row.update(installed=True, version=None, path=None)
        else:
            path = _which(k.command)
            cached = _versions.get(k.id)
            if path and (cached is None or time.time() - cached[0] > 300):
                try:
                    res = subprocess.run(argv([path, "--version"]), capture_output=True, text=True, encoding="utf-8",
                                         errors="replace", timeout=15)
                    version = (res.stdout or res.stderr).strip().splitlines()[0][:60] if (res.stdout or res.stderr).strip() else None
                except (OSError, subprocess.TimeoutExpired):
                    version = None
                _versions[k.id] = (time.time(), version)
            row.update(installed=path is not None, path=path, version=_versions.get(k.id, (0, None))[1] if path else None)
            if path:
                row.update(models=models(k.id, path), default_model=_config_model(k.id))
        if row["install"].startswith("npm ") and not row["installed"] and not shutil.which("npm", path=search_path()):
            row["needs"] = "Node.js (https://nodejs.org) for npm"
        elif k.id == "claude" and WIN and not shutil.which("git", path=search_path()):
            row["needs"] = "Git for Windows (https://git-scm.com/downloads/win): Claude Code needs its Git Bash"
        out.append(row)
    return out


# ---------------------------------------------------------------------------------- output parsing

def _tool_name(name: str) -> str:
    for prefix in ("mcp__decomp__", "decomp__", "decomp_", "decomp."):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _args_brief(args) -> str:
    if not isinstance(args, dict):
        return ""
    parts = []
    for k, v in args.items():
        if k == "source":
            parts.append(f"{len(str(v).splitlines())} lines of C")
        elif isinstance(v, str):
            parts.append(v if len(v) < 40 else v[:37] + "…")
        elif v not in (None, "", {}):
            parts.append(str(v))
    return ", ".join(parts)


def parse_line(kind: str, line: str) -> list[tuple[str, str]]:
    """(type, text) items for the live feed from one line of agent output: text, think, tool, info, error."""
    line = line.strip()
    if not line:
        return []
    try:
        obj = json.loads(line)
    except ValueError:
        return [("info", line[:400])]
    if not isinstance(obj, dict):
        return []
    t = obj.get("type")
    out: list[tuple[str, str]] = []
    if t == "assistant" and isinstance(obj.get("message"), dict):  # Claude Code
        for c in obj["message"].get("content", []):
            if c.get("type") == "text" and c.get("text", "").strip():
                out.append(("text", c["text"].strip()))
            elif c.get("type") == "thinking" and c.get("thinking", "").strip():
                out.append(("think", c["thinking"].strip()))
            elif c.get("type") == "tool_use" and c.get("name") != "ToolSearch":  # ToolSearch: Claude Code loading tools
                out.append(("tool", f"{_tool_name(c.get('name', ''))}({_args_brief(c.get('input'))})"))
    elif t == "result":  # Claude Code / Gemini CLI end of session
        if obj.get("is_error") or obj.get("status") == "error":
            out.append(("error", str(obj.get("result") or obj.get("error") or "session failed")[:400]))
        else:
            cost = obj.get("total_cost_usd")
            turns = obj.get("num_turns")
            bits = [f"{turns} turns" if turns else "", f"${cost:.2f}" if isinstance(cost, (int, float)) else ""]
            out.append(("info", "session finished" + (f" ({', '.join(b for b in bits if b)})" if any(bits) else "")))
            if isinstance(cost, (int, float)):
                out.append(("usage", json.dumps({"usd": cost})))
            stats = obj.get("stats") if isinstance(obj.get("stats"), dict) else {}
            if stats.get("total_tokens"):
                out.append(("usage", json.dumps({"tokens": stats["total_tokens"]})))
    elif t in ("item.started", "item.completed") and isinstance(obj.get("item"), dict):  # Codex
        item = obj["item"]
        it = item.get("type") or item.get("item_type")
        if it == "agent_message" and t == "item.completed":
            out.append(("text", str(item.get("text", "")).strip()))
        elif it == "reasoning" and t == "item.completed" and item.get("text"):
            out.append(("think", str(item["text"]).strip()))
        elif it == "mcp_tool_call" and t == "item.started":
            out.append(("tool", f"{_tool_name(item.get('tool', ''))}({_args_brief(item.get('arguments'))})"))
        elif it == "command_execution" and t == "item.started":
            out.append(("tool", f"shell: {str(item.get('command', ''))[:80]}"))
        elif it == "error":
            out.append(("error", str(item.get("message", ""))[:400]))
    elif t == "turn.completed":
        out.append(("info", "turn finished"))
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
        tokens = sum(v for k, v in usage.items() if k in ("input_tokens", "output_tokens") and isinstance(v, int))
        if tokens:
            out.append(("usage", json.dumps({"tokens": tokens})))
    elif t in ("error", "turn.failed"):
        err = obj.get("error")
        msg = err.get("message") if isinstance(err, dict) else (obj.get("message") or err)
        out.append(("error", str(msg)[:400]))
    elif t == "message" and obj.get("role") == "assistant":  # Gemini CLI
        content = obj.get("content")
        if isinstance(content, str) and content.strip():
            out.append(("text", content))
    elif t == "tool_use":  # Gemini CLI / opencode
        part = obj.get("part") if isinstance(obj.get("part"), dict) else obj
        name = part.get("tool_name") or part.get("tool") or part.get("name") or ""
        args = part.get("parameters") or (part.get("state") or {}).get("input") or part.get("input")
        out.append(("tool", f"{_tool_name(name)}({_args_brief(args)})"))
    elif t == "text" and isinstance(obj.get("part"), dict):  # opencode
        text = obj["part"].get("text", "").strip()
        if text:
            out.append(("text", text))
    elif t in ("step_finish", "step-finish") and isinstance(obj.get("part"), dict):  # opencode: cost per step
        part = obj["part"]
        if isinstance(part.get("cost"), (int, float)) and part["cost"]:
            out.append(("usage", json.dumps({"usd": part["cost"]})))
    return [(k, v) for k, v in out if v]


# ---------------------------------------------------------------------------------- sessions

@dataclass
class Session:
    id: str
    kind: str
    label: str
    slug: str
    agent_name: str
    options: dict
    state: str = "starting"  # starting | working | waiting | stopped | error
    started_at: float = field(default_factory=time.time)
    runs: int = 0
    error: str = ""
    feed: collections.deque = field(default_factory=lambda: collections.deque(maxlen=400))
    seq: int = 0
    stop: threading.Event = field(default_factory=threading.Event)
    proc: subprocess.Popen | None = None
    usd: float = 0.0  # what the agent's provider charged, as the agent reports it
    tokens: int = 0
    signed_out: bool = False  # the agent CLI isn't signed in: trying again won't help

    def push(self, kind: str, text: str) -> None:
        self.seq += 1
        self.feed.append({"seq": self.seq, "t": time.time(), "type": kind, "text": text[:4000]})

    def snapshot(self, since: int = 0) -> dict:
        return {"id": self.id, "kind": self.kind, "label": self.label, "slug": self.slug, "agent": self.agent_name,
                "state": self.state, "runs": self.runs, "error": self.error, "started_at": self.started_at,
                "model": self.options.get("model") or "", "usd": round(self.usd, 4), "tokens": self.tokens,
                "feed": [f for f in self.feed if f["seq"] > since]}


class Manager:
    def __init__(self, net: RomNetwork, home: Path, mcp_url: str):
        self.net, self.home, self.mcp_url = net, home, mcp_url
        self.sessions: dict[str, Session] = {}
        self.assignments: dict[int, str] = {}  # agent id -> project slug
        self._lock = threading.Lock()
        self._prep: dict[str, threading.Event] = {}  # slug -> giving its code source files before agents claim
        self.on_stop = lambda: None  # set by the server: close tunnels when the app quits
        self.persist = lambda running: None  # set by the server: remember helpers to resume after a restart

    # -- control

    def start(self, kind: str, slug: str, count: int = 1, options: dict | None = None) -> list[str]:
        if kind not in KINDS:
            raise ValueError(f"unknown agent kind {kind!r}")
        options = options or {}
        ids = []
        self._prepare(slug)
        with self._lock:
            for _ in range(max(1, min(int(count), 8))):
                n = 1 + sum(1 for s in self.sessions.values() if s.kind == kind)
                sid = f"{kind}-{n}"
                while sid in self.sessions:
                    n += 1
                    sid = f"{kind}-{n}"
                label = f"{KINDS[kind].name} #{n}" if kind != "api" else f"{options.get('provider_name') or 'API'} #{n}"
                s = Session(sid, kind, label, slug, sid, dict(options))
                self.sessions[sid] = s
                threading.Thread(target=self._run, args=(s,), daemon=True, name=f"agent-{sid}").start()
                ids.append(sid)
        self.persist(self.running())
        return ids

    def _prepare(self, slug: str) -> None:
        """Agents on a GameCube/Wii decomp where code has no source file yet: give it files first (start_files,
        checked against the game's checksum), or they'd all share the few hard functions that are left."""
        with self._lock:
            if slug in self._prep:
                return
            try:
                if not self.net.fork_slug(slug):
                    return
                _, fp, cfg = self.net.forks.get(slug)
                if not (self.net.forks.uncovered(slug) or {}).get("functions") or not fp.linkable():
                    return
            except Exception:
                return
            done = self._prep[slug] = threading.Event()

        def run() -> None:
            try:
                self.net.forks.start_files(slug, log=lambda *_: None)
            except Exception:  # the agents still work on what has files
                pass
            finally:
                done.set()
                with self._lock:
                    self._prep.pop(slug, None)

        threading.Thread(target=run, daemon=True, name=f"start-files-{slug}").start()

    def running(self) -> list[dict]:
        """The helpers that should keep working: resumed by the server after the app restarts."""
        return [{"kind": s.kind, "slug": s.slug, "options": {k: v for k, v in s.options.items() if k != "api_key"}}
                for s in self.sessions.values() if s.state in ("starting", "working", "waiting") and not s.stop.is_set()]

    def stop(self, sid: str | None = None) -> None:
        """Stop one helper (for good) or all of them (the app is quitting: they resume next time)."""
        for s in list(self.sessions.values()):
            if sid in (None, s.id):
                s.stop.set()
                if s.proc and s.proc.poll() is None:
                    _terminate(s.proc)
                if s.state not in ("error",):
                    s.state = "stopped"
        if sid is not None:
            self.persist(self.running())

    def remove(self, sid: str) -> None:
        self.stop(sid)
        self.sessions.pop(sid, None)

    def snapshot(self, since: dict | None = None) -> list[dict]:
        since = since or {}
        return [s.snapshot(int(since.get(s.id, 0))) for s in self.sessions.values()]

    def project_of(self, agent_id: int) -> str | None:
        return self.assignments.get(agent_id)

    # -- loop

    def _progress(self, agent_id: int) -> int:
        """Compiles + claims + notes by this agent: a session that adds none of these made no progress."""
        row = self.net.store.one(
            "SELECT (SELECT COUNT(*) FROM attempts WHERE agent_id=?) + (SELECT COUNT(*) FROM events WHERE agent_id=?) AS n",
            agent_id, agent_id)
        return row["n"]

    def _left(self, slug: str) -> int:
        proj = self.net.project_row(slug)
        row = self.net.store.one("SELECT COUNT(*) AS n FROM functions WHERE project_id=? AND status='open'", proj["id"])
        return row["n"]

    def _account(self, s: Session, usage: dict) -> None:
        """Add what a session spent to the session and to the project (kept in its config across restarts)."""
        usd, tokens = float(usage.get("usd") or 0), int(usage.get("tokens") or 0)
        s.usd += usd
        s.tokens += tokens
        self.net.add_spend(s.slug, usd, tokens)

    def over_budget(self, slug: str) -> float | None:
        """The budget (USD) when the project's agents have spent it, else None."""
        cap = self.budget()
        spent = (self.net.spend(slug) or {}).get("usd", 0)
        return cap if cap and spent >= cap else None

    def budget(self) -> float:
        return 0.0  # set by the server from the settings

    def _run(self, s: Session) -> None:
        agent, token = self.net.local_agent(s.agent_name)
        self.assignments[agent.id] = s.slug
        workdir = self.home / "agents" / s.id
        workdir.mkdir(parents=True, exist_ok=True)
        failures = 0
        s.push("info", f"{s.label} starting on {s.slug}")
        if (prep := self._prep.get(s.slug)) is not None:
            s.push("info", "first giving the game's code without a source file one (about a minute)")
            prep.wait(900)
        while not s.stop.is_set():
            if self._left(s.slug) == 0:
                s.push("info", "nothing left to decompile here 🎉")
                break
            if (cap := self.over_budget(s.slug)) is not None:
                s.push("info", f"stopping: the agents on this project have spent the ${cap:.2f} budget (Setup)")
                break
            link = self.net.links.get(s.slug)
            if link is not None and (link.error or time.time() - link.last_ok > 30):
                # in a room whose host is away (or that hasn't let us in yet): wait, don't fail
                if s.state != "waiting" or not s.feed or "waiting for the room" not in s.feed[-1]["text"]:
                    s.push("info", f"waiting for the room's host: {link.error or 'not reachable right now'}")
                s.state = "waiting"
                try:
                    link.sync()
                except Exception:
                    pass
                s.stop.wait(10)
                continue
            s.state = "working"
            s.runs += 1
            t0 = time.time()
            before = self._progress(agent.id)
            try:
                ok = self._run_api(s, agent) if s.kind == "api" else self._run_cli(s, token, workdir)
            except Exception as e:  # keep the runner alive; show what happened
                s.push("error", f"{type(e).__name__}: {e}")
                ok = False
            if ok and not s.stop.is_set() and self._progress(agent.id) == before:
                s.error = "the session ended without compiling anything (check the agent's output)"
                s.push("error", s.error)
                ok = False
            if s.stop.is_set():
                break
            if s.signed_out:
                s.state = "error"
                break
            failures = 0 if ok else failures + 1
            if failures >= 3:
                s.state = "error"
                s.error = s.error or "the agent failed three times in a row"
                s.push("error", f"stopping: {s.error}. {KINDS[s.kind].signin}".strip())
                break
            s.state = "waiting"
            s.stop.wait(3 if ok else min(60, 5 * failures + max(0, 10 - (time.time() - t0))))
        if s.state != "error":
            s.state = "stopped"
        if not s.stop.is_set():  # finished by itself (done, budget, errors): no need to resume it
            s.stop.set()
            self.persist(self.running())
        held = self.net.store.one("SELECT f.name FROM claims c JOIN functions f ON f.id=c.function_id WHERE c.agent_id=?", agent.id)
        link = self.net.links.get(s.slug)
        if held and link is not None:  # in a room, the host holds the real claim
            try:
                link.call("/collab/release", agent, function=held["name"], notes="")
            except Exception:
                pass
        with self.net.store.tx() as db:  # release any claim the agent still holds so others can take it
            db.execute("DELETE FROM claims WHERE agent_id=?", (agent.id,))

    def prompt(self, s: Session) -> str:
        text = self._prompt(s)
        # the know-how every session reads first, given up front (saves each session its first tool calls)
        ov = self.net.overview(s.slug)
        if ov.get("kind") == "fork":
            topics = ["fork", "objdiff"]
        else:
            topics = ["workflow", {"psx": "psx", "ps2": "ps2", "gba": "agbcc"}.get(ov.get("platform")) or (
                "ido" if ov["compiler"]["toolchain"].startswith("ido") else "gcc-kmc")]
        text += "\nKnow-how you'd otherwise read with guide() (more topics: guide()):\n\n" + "\n\n".join(
            tools._guide(t) for t in topics)
        if (wish := self.net.instructions(s.slug)):
            text += f"\nThe user's instructions for this project (they come first):\n{wish}\n"
        return text

    def _prompt(self, s: Session) -> str:
        ov = self.net.overview(s.slug)
        if ov.get("kind") == "fork":
            guides = ["fork", "objdiff", *tools.PLATFORM_GUIDES.get(ov.get("platform") or "", [])]
            return tools.FORK_PROMPT.format(title=ov["title"], platform=ov.get("platform_name") or "its platform",
                                            count=s.options.get("per_session", 4),
                                            guides=", ".join(f'guide("{g}")' for g in guides))
        guide = {"psx": "psx", "ps2": "ps2", "gba": "agbcc"}.get(ov.get("platform")) or (
            "ido" if ov["compiler"]["toolchain"].startswith("ido") else "gcc-kmc")
        return tools.WORK_PROMPT.format(title=ov["title"], count=s.options.get("per_session", 4), compiler_guide=guide)

    def _command(self, s: Session, token: str, workdir: Path) -> tuple[list[str], dict]:
        k = KINDS[s.kind]
        exe = _which(k.command)
        if not exe:
            raise RuntimeError(f"{k.name} is not installed")
        env = dict(os.environ)
        if WIN:
            env["PATH"] = search_path()  # the agent finds node (and what it needs) even when installed after the app
        prompt = self.prompt(s)
        model = s.options.get("model") or ""
        if s.kind == "claude":
            cfg = workdir / "mcp.json"
            cfg.write_text(json.dumps({"mcpServers": {"decomp": {"type": "http", "url": self.mcp_url,
                                                                  "headers": {"Authorization": f"Bearer {token}"}}}}),
                           encoding="utf-8")
            cmd = [exe, "-p", prompt, "--output-format", "stream-json", "--verbose", "--mcp-config", str(cfg),
                   "--strict-mcp-config", "--allowedTools", "mcp__decomp", "--disallowedTools",
                   "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch"]
            if model:
                cmd += ["--model", model]
        elif s.kind == "codex":
            env["DECOMP_TOKEN"] = token
            cmd = [exe, "exec", "--json", "--skip-git-repo-check", "--ephemeral", "-C", str(workdir), "-s", "read-only",
                   "-c", f'mcp_servers.decomp.url="{self.mcp_url}"',
                   "-c", 'mcp_servers.decomp.bearer_token_env_var="DECOMP_TOKEN"',
                   "-c", 'mcp_servers.decomp.default_tools_approval_mode="approve"',
                   "-c", "mcp_servers.decomp.tool_timeout_sec=180",
                   "-c", 'approval_policy="never"']
            if model:
                cmd += ["-m", model]
            cmd.append(prompt)
        elif s.kind == "gemini":
            (workdir / ".gemini").mkdir(exist_ok=True)
            (workdir / ".gemini" / "settings.json").write_text(json.dumps({
                "mcpServers": {"decomp": {"httpUrl": self.mcp_url, "headers": {"Authorization": f"Bearer {token}"},
                                          "trust": True}},
                "tools": {"exclude": ["run_shell_command", "write_file", "replace", "web_fetch", "google_web_search"]},
            }), encoding="utf-8")
            cmd = [exe, "-p", prompt, "-o", "stream-json", "--skip-trust", "--allowed-mcp-server-names", "decomp"]
            if model:
                cmd += ["-m", model]
        elif s.kind == "opencode":
            (workdir / "opencode.json").write_text(json.dumps({
                "$schema": "https://opencode.ai/config.json",
                "mcp": {"decomp": {"type": "remote", "url": self.mcp_url, "enabled": True,
                                   "headers": {"Authorization": f"Bearer {token}"}}},
                "permission": {"edit": "deny", "bash": "deny", "webfetch": "deny"},
            }), encoding="utf-8")
            cmd = [exe, "run", "--format", "json", "--dir", str(workdir)]
            if model:
                cmd += ["-m", model]
            cmd.append(prompt)
        else:
            raise RuntimeError(s.kind)
        cmd = argv(cmd)
        if WIN and len(subprocess.list2cmdline(cmd)) > 32000:  # Windows' limit for a whole command line
            raise RuntimeError("the prompt is too long for Windows' command line: shorten this project's instructions")
        return cmd, env

    def _run_cli(self, s: Session, token: str, workdir: Path) -> bool:
        cmd, env = self._command(s, token, workdir)
        s.push("info", f"session {s.runs}: {KINDS[s.kind].name} started")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if WIN else 0
        proc = subprocess.Popen(cmd, cwd=workdir, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
                                creationflags=flags, start_new_session=not WIN)
        s.proc = proc
        errors: collections.deque = collections.deque(maxlen=20)

        def drain_err():
            for line in proc.stderr:
                if line.strip():
                    errors.append(line.strip())
        threading.Thread(target=drain_err, daemon=True).start()
        saw_tool = False
        said: collections.deque = collections.deque(maxlen=20)
        for line in proc.stdout:
            for kind, text in parse_line(s.kind, line):
                if kind == "usage":
                    self._account(s, json.loads(text))
                    continue
                saw_tool |= kind == "tool"
                said.append(text)
                s.push(kind, text)
            if s.stop.is_set():
                _terminate(proc)
                break
        code = proc.wait()
        s.proc = None
        if code != 0 and not s.stop.is_set() and _SIGNED_OUT.search(" ".join([*said, *errors])):
            s.signed_out = True
            s.error = f"{KINDS[s.kind].name} isn't signed in on this computer. {KINDS[s.kind].signin}"
            s.push("error", s.error)
            return False
        if code != 0 and not s.stop.is_set():
            tail = " · ".join(list(errors)[-3:]) or f"exited with code {code}"
            s.error = tail[:300]
            s.push("error", tail[:600])
            return False
        if not saw_tool and not s.stop.is_set():
            s.error = "the agent never used the decomp tools" + (f": {list(errors)[-1]}" if errors else "")
            s.push("error", s.error)
            return False
        s.error = ""
        return True

    # -- built-in agent for OpenAI-compatible APIs

    def _run_api(self, s: Session, agent) -> bool:
        base = (s.options.get("base_url") or "").rstrip("/")
        model = s.options.get("model") or ""
        if not base or not model:
            raise RuntimeError("set the API address and model first")
        headers = {"Content-Type": "application/json"}
        if s.options.get("api_key"):
            headers["Authorization"] = f"Bearer {s.options['api_key']}"
        schema = api_tool_schemas(fork=bool(self.net.fork_slug(s.slug)))
        messages = [{"role": "system", "content": tools.INSTRUCTIONS},
                    {"role": "user", "content": self.prompt(s)}]
        s.push("info", f"session {s.runs}: {model} via {base}")
        used_tool = False
        with httpx.Client(timeout=httpx.Timeout(300, connect=15)) as client:
            for _step in range(int(s.options.get("max_steps", 120))):
                if s.stop.is_set():
                    return True
                r = client.post(f"{base}/chat/completions", headers=headers,
                                json={"model": model, "messages": messages, "tools": schema, "tool_choice": "auto"})
                if r.status_code != 200:
                    s.error = f"API error {r.status_code}: {r.text[:300]}"
                    s.push("error", s.error)
                    return False
                reply = r.json()
                msg = reply["choices"][0]["message"]
                usage = reply.get("usage") if isinstance(reply.get("usage"), dict) else {}
                if usage.get("total_tokens"):
                    self._account(s, {"tokens": usage["total_tokens"]})
                if msg.get("reasoning_content"):
                    s.push("think", msg["reasoning_content"][:2000])
                if msg.get("content"):
                    s.push("text", msg["content"])
                calls = msg.get("tool_calls") or []
                messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")} | {"role": "assistant"})
                if not calls:
                    return used_tool
                for c in calls:
                    name = c["function"]["name"]
                    try:
                        args = json.loads(c["function"].get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    used_tool = True
                    s.push("tool", f"{name}({_args_brief(args)})")
                    result = tools.call(self.net, agent, s.slug, name, args)
                    messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
                if len(messages) > 80:  # keep the conversation small: summarize by restarting fresh
                    return True
        return used_tool


def api_tool_schemas(fork: bool = False) -> list[dict]:
    """OpenAI-style tool definitions for the built-in agent, from the same tools CLI agents get."""
    s = lambda **props: {"type": "object", "properties": props, "required": [k for k, v in props.items() if v.pop("req", False)]}
    st = lambda desc, req=False: {"type": "string", "description": desc, "req": req}
    it = lambda desc: {"type": "integer", "description": desc}
    defs = {
        "project_info": ("Which game, compiler and progress.", s()),
        "claim_function": ("Claim the next function (or one by name/address). Returns assembly, data, best attempt, "
                           "notes and a draft.", s(function=st("optional name or address"),
                                                     strategy=st("finish | easiest | closest | hardest | random"))),
        "get_function": ("Show any function by name or address.", s(function=st("name or address", True))),
        "compile_and_diff": ("Compile C for the function, link it at its address and diff against the ROM. "
                             "Score 0 = matched.", s(function=st("function name", True), source=st("C source", True))),
        "release_function": ("Give up a function with notes for the next agent.",
                             s(function=st("function", True), notes=st("what you tried, what is left", True))),
        "get_context": ("The shared header included in every compile.", s()),
        "edit_context": ("Replace `old` (exactly once) with `new` in the shared header, or append when old is empty.",
                         s(old=st("text to replace, or empty", True), new=st("replacement", True),
                           message=st("why", True))),
        "rename_symbol": ("Give a function or data address a real name.",
                          s(old=st("current name", True), new=st("new name", True), message=st("why"))),
        "split_function": ("Split a merged function at an address.",
                           s(function=st("function", True), at=st("address like 0x80001234", True), new_name=st("name"))),
        "read_data": ("Hex dump of ROM data at an address or symbol.", s(what=st("address or symbol", True),
                                                                         length=it("bytes, up to 512"))),
        "list_functions": ("List functions by status.", s(status=st("open | matched | all"), limit=it("max rows"))),
        "add_note": ("Leave a note for other agents.", s(text=st("note", True), function=st("function"))),
        "search_notes": ("Search notes.", s(query=st("text", True))),
        "find_similar": ("Functions that look like this one, matched ones first, with the best one's source.",
                         s(function=st("function", True), limit=it("how many"))),
        "guide": ("Decompilation know-how; no topic lists topics.", s(topic=st("topic"))),
        "read_file": ("Read a project file with line numbers.", s(path=st("path from the project root", True),
                                                                 start=it("first line"), count=it("lines"))),
        "search_code": ("Regex search over the project's C/C++ sources and headers.",
                        s(pattern=st("regular expression", True), glob=st("optional file glob"))),
        "edit_file": ("Replace `old` (exactly once) with `new` in a source/header file, or append when old is empty. "
                      "Undone if any match breaks.", s(path=st("file", True), old=st("text to replace, or empty", True),
                                                       new=st("replacement", True), message=st("why", True))),
        "file_status": ("How far a source file (or a function's file) is from being linked: functions left, its data "
                        "next to the original's, why the last link failed.", s(path=st("file or function", True))),
        "split_file": ("Divide a source file at a function: it and the rest of the file's code (and data from the given "
                       "offsets) go to a new file. Kept only if the game still builds identically.",
                       s(path=st("file", True), at=st("function name or .text offset", True),
                         new_name=st("new file path (optional)"), data=st("e.g. .rodata:0x88 .data:0x28 (optional)"))),
    }
    if fork:
        defs["compile_and_diff"] = ("Write one complete function definition into its source file, build it and diff "
                                    "with objdiff. 100% / score 0 = matched.",
                                    s(function=st("function name", True), source=st("the function definition", True)))
    names = set(tools.tool_names(fork))
    return [{"type": "function", "function": {"name": n, "description": d, "parameters": p}}
            for n, (d, p) in defs.items() if n in names]


def _terminate(proc: subprocess.Popen) -> None:
    try:
        if WIN:
            # the agent with everything it started (node starts opencode.exe, ...): terminate() would only end the
            # first process and leave the rest working, and spending, in the background
            res = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30)
            if res.returncode != 0 and proc.poll() is None:
                proc.terminate()
        else:
            os.killpg(proc.pid, 15)
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        pass

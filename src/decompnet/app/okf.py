"""The project's knowledge base: the `.okf/` folder at the checkout's root, an Open Knowledge Format bundle (markdown
files with YAML front matter, okf_version 0.2). Every helper can search and read it; researcher helpers (and the
agent the user talks to) add sourced know-how they found on the web under research/.

Everything stays inside the bundle: paths are checked, and a symbolic link is never followed, in or out."""

from __future__ import annotations

import datetime
import json
import os
import re
import threading
from pathlib import Path

from ..network import NetworkError
from . import localtools

MAX_READ = 20_000  # characters read_knowledge returns
MAX_TEXT = 20_000  # characters of body add_knowledge accepts
MAX_HITS = 10
RESEARCH = "research"
RESERVED = ("index.md", "log.md")
_write = threading.Lock()


def folder() -> Path | None:
    """DECOMP_OKF when set (made on the first write), else this checkout's .okf/, else the main checkout's."""
    if (given := os.environ.get("DECOMP_OKF")):
        return Path(given).expanduser()
    return next((c for c in localtools.checkout_folders(".okf", "DECOMP_OKF") if c.is_dir()), None)


def _root() -> Path:
    root = folder()
    if root is None or not root.is_dir():
        raise NetworkError("this decomp.wtf has no knowledge base (.okf/ at the checkout's root)")
    return root


def _inside(root: Path, rel: str) -> Path:
    """`rel` as a path in the bundle, or an error: no absolute paths, no '..', no symbolic link on the way."""
    rel = (rel or "").strip().replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if rel.startswith(("/", "~")) or ".." in parts or re.match(r"^[A-Za-z]:", rel) or "\0" in rel:
        raise NetworkError("paths are inside the knowledge base, like \"index.md\" or \"tools/armips.md\"")
    path = root
    for p in parts:
        path = path / p
        if path.is_symlink():
            raise NetworkError(f"{rel} is a link: only the knowledge base's own files can be read")
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        raise NetworkError("paths are inside the knowledge base") from None
    return path


def _docs(root: Path):
    """(bundle path, text) of every concept file, symbolic links left out."""
    for here, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (Path(here) / d).is_symlink())
        for name in sorted(files):
            p = Path(here) / name
            if name.endswith(".md") and name not in RESERVED and not p.is_symlink():
                try:
                    yield p.relative_to(root).as_posix(), p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue


def _field(text: str, key: str) -> str:
    """A one-line front-matter value (title, description), unquoted; "" when absent."""
    head = text.split("\n---", 2)[0] if text.startswith("---") else ""
    m = re.search(rf"^{key}:\s*(.+)$", head, re.M)
    if not m:
        return ""
    value = m.group(1).strip()
    if value.startswith('"'):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value.strip("'\"")


def search(query: str) -> str:
    """Concepts whose front matter or body has every word of `query` (any case), most mentions first."""
    root = _root()
    words = [w for w in re.split(r"\s+", (query or "").lower()) if w]
    if not words:
        raise NetworkError("search_knowledge needs a word or two, like \"IDO\" or \"GBA agbcc\"")
    hits = []
    for path, text in _docs(root):
        low = text.lower()
        if all(w in low for w in words):
            hits.append((-sum(low.count(w) for w in words), path, text, low))
    if not hits:
        return f"Nothing in the knowledge base mentions {query!r}. read_knowledge(\"index.md\") lists what is there."
    out = []
    for _, path, text, low in sorted(hits)[:MAX_HITS]:
        body = text.split("\n---", 2)[-1] if text.startswith("---") else text
        at = max(0, body.lower().find(words[0]))
        snippet = re.sub(r"\s+", " ", body[max(0, at - 80):at + 160]).strip()
        out.append(f"- {path}: {_field(text, 'title') or path}. {_field(text, 'description')}\n  …{snippet}…")
    more = f"\n({len(hits) - MAX_HITS} more: narrow the search)" if len(hits) > MAX_HITS else ""
    return "\n".join(out) + more + "\nread_knowledge(path) reads one."


def read(path: str = "index.md") -> str:
    """A file of the bundle (a folder: its index.md), cut at MAX_READ characters."""
    root = _root()
    p = _inside(root, path or "index.md")
    if p.is_dir():
        p = _inside(root, (p.relative_to(root) / "index.md").as_posix())
    if p.suffix != ".md" or not p.is_file():
        raise NetworkError(f"no {path!r} in the knowledge base: read_knowledge(\"index.md\") lists what is there")
    text = p.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_READ:
        text = text[:MAX_READ] + f"\n[... cut at {MAX_READ} characters]"
    return text


def slug(title: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", title.lower())).strip("-")[:60].strip("-")


def _q(value: str) -> str:
    """A YAML scalar: a JSON string is a valid YAML double-quoted one."""
    return json.dumps(str(value))


def _sources(sources) -> list[dict]:
    """[{id, resource, title}] from URLs or {url|resource, title} entries; every one an http(s) URL."""
    if isinstance(sources, str):
        sources = [s for s in re.split(r"[\s,]+", sources) if s]
    out = []
    for i, s in enumerate(sources or [], start=1):
        url, title = (s, "") if isinstance(s, str) else (str(s.get("url") or s.get("resource") or ""),
                                                          str(s.get("title") or "")) if isinstance(s, dict) else ("", "")
        url = url.strip()
        if not re.fullmatch(r"https?://[^\s<>\"]+", url):
            raise NetworkError(f"sources are the web pages it came from (http or https URLs); {url or s!r} isn't one")
        out.append({"id": f"s{i}", "resource": url, "title": title.strip()[:200] or url})
    if not out:
        raise NetworkError("add_knowledge needs at least one source: the URL of the page you read it on")
    return out[:12]


def add(title: str, description: str, text: str, sources, tags=None, type: str = "Concept", by: str = "") -> str:
    """A new concept, research/<slug>.md, listed in research/index.md (and research/ in the root index.md), with a
    line in log.md. An existing entry is never overwritten: a new finding gets its own title."""
    title, description, text = (title or "").strip(), re.sub(r"\s+", " ", description or "").strip(), (text or "").strip()
    if not title or not description or not text:
        raise NetworkError("add_knowledge needs a title, a one-sentence description and the text")
    if len(text) > MAX_TEXT:
        raise NetworkError(f"keep an entry under {MAX_TEXT} characters: split it into several topics")
    srcs = _sources(sources)
    if isinstance(tags, str):
        tags = [t for t in re.split(r"[\s,]+", tags) if t]
    tags = [re.sub(r"[^a-z0-9+._-]+", "-", str(t).lower()).strip("-")[:40] for t in (tags or [])]
    tags = [t for t in dict.fromkeys(tags) if t][:12]
    name = slug(title)
    if not name:
        raise NetworkError("give it a title with letters or numbers in it")
    root = folder()
    if root is None:
        raise NetworkError("this decomp.wtf has no knowledge base (.okf/ at the checkout's root)")
    now = datetime.datetime.now(datetime.timezone.utc)
    at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    kind = re.sub(r"\s+", " ", type or "").strip()[:60] or "Concept"
    head = ["---", f"type: {_q(kind)}",
            f"title: {_q(title[:200])}", f"description: {_q(description[:400])}"]
    if tags:
        head.append("tags: [" + ", ".join(_q(t) for t in tags) + "]")
    head.append(f"generated: {{by: {_q(by or 'decomp.wtf/researcher')}, at: {_q(at)}}}")
    head.append("sources:")
    for s in srcs:
        head += [f"  - id: {s['id']}", f"    resource: {_q(s['resource'])}", f"    title: {_q(s['title'])}"]
    head.append("---")
    cites = " ".join(f"[^{s['id']}]" for s in srcs)
    body = f"# {title}\n\n{text}\n\nSources: {cites}\n\n" + "\n".join(
        f"[^{s['id']}]: " + (s["resource"] if s["title"] == s["resource"] else f"{s['title']} ({s['resource']})")
        for s in srcs)
    with _write:
        res = _inside(root, RESEARCH) if root.is_dir() else root / RESEARCH
        if res.exists() and not res.is_dir():
            raise NetworkError("research/ in the knowledge base isn't a folder")
        res.mkdir(parents=True, exist_ok=True)
        path = _inside(root, f"{RESEARCH}/{name}.md")
        try:
            with open(path, "x", encoding="utf-8") as f:
                f.write("\n".join(head) + "\n\n" + body + "\n")
        except FileExistsError:
            raise NetworkError(f"{RESEARCH}/{name}.md exists already: read_knowledge(\"{RESEARCH}/{name}.md\"), and add "
                               "what's new under a title of its own") from None
        _list(root, name, title, description)
        _log(root, now.strftime("%Y-%m-%d"), f"* **Creation**: Added [{title}]({RESEARCH}/{name}.md) "
                                             f"({len(srcs)} source{'s' if len(srcs) != 1 else ''}, by {by or 'a researcher'}).")
    return (f"Saved as {RESEARCH}/{name}.md and listed in {RESEARCH}/index.md. Every helper can find it with "
            "search_knowledge.")


def _save(path: Path, text: str) -> None:
    if path.is_symlink():
        raise NetworkError(f"{path.name} in the knowledge base is a link: not written")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _list(root: Path, name: str, title: str, description: str) -> None:
    """The entry in research/index.md, and research/ in the bundle's root index.md when it isn't there yet."""
    index = root / RESEARCH / "index.md"
    text = index.read_text(encoding="utf-8") if index.is_file() else (
        "# Research\n\nCompiler, platform and matching know-how that researcher helpers found on the web, each entry "
        "with the pages it came from.\n")
    gap = "\n" if "\n- [" in text else "\n\n"  # the first entry starts the list after the intro
    _save(index, text.rstrip("\n") + f"{gap}- [{title}](./{name}.md) - {description}\n")
    top = root / "index.md"
    text = top.read_text(encoding="utf-8") if top.is_file() else '---\nokf_version: "0.2"\n---\n\n# Knowledge base\n'
    if f"./{RESEARCH}/" not in text:
        _save(top, text.rstrip("\n") + f"\n- [Research](./{RESEARCH}/) - compiler and platform know-how researcher "
                                         f"helpers found on the web (see {RESEARCH}/index.md)\n")


def _log(root: Path, day: str, entry: str) -> None:
    """log.md, newest first: under today's heading when it's the first one, else a new heading on top."""
    log = root / "log.md"
    lines = (log.read_text(encoding="utf-8") if log.is_file() else "# Update Log\n").rstrip("\n").split("\n")
    first = next((i for i, line in enumerate(lines) if line.startswith("## ")), None)
    if first is not None and lines[first].strip() == f"## {day}":
        lines.insert(first + 1, entry)
    else:
        at = first if first is not None else len(lines)
        lines[at:at] = [f"## {day}", entry, ""] if first is not None else ["", f"## {day}", entry]
    _save(log, "\n".join(lines) + "\n")

"""The decomp.dev catalog: public matching decompilation projects with their progress."""

from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

URL = "https://decomp.dev/projects.json"
PLATFORMS = {
    "n64": "Nintendo 64", "gc": "GameCube", "wii": "Wii", "ps": "PlayStation", "ps2": "PlayStation 2",
    "psp": "PSP", "gba": "Game Boy Advance", "nds": "Nintendo DS", "3ds": "Nintendo 3DS", "switch": "Switch",
    "win32": "Windows PC", "xbox": "Xbox", "xbox360": "Xbox 360",
}


# How decomp.wtf uses decomp.dev's list: each install is a client of its own (its own
# user agent, so the automatic rate limit doesn't count every decomp.wtf as one user), asks at most once a day and
# only when someone opens Public decomps, and waits a day after a refusal, an hour after an error. One request at a
# time per app. The page credits decomp.dev and links every project to its page there.
MAX_AGE = 24 * 3600
AFTER_REFUSAL = 24 * 3600
AFTER_ERROR = 3600
_LOCK = threading.Lock()


def install_id(folder: Path) -> str:
    """This install's random id (made once, kept next to the cached list): nothing about the user or the computer."""
    path = folder / "install-id"
    try:
        found = path.read_text(encoding="utf-8").strip()
        if len(found) == 12 and all(c in "0123456789abcdef" for c in found):
            return found
    except OSError:
        pass
    made = secrets.token_hex(6)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(made, encoding="utf-8")
    except OSError:
        pass
    return made


def user_agent(folder: Path) -> str:
    """decompwtf-<install id>/<version>. ("decomp.wtf" written as such, in any part of the agent, falls under the
    rate limit that tripped on every install sharing one agent; this names the app all the same.)"""
    from .. import __version__

    return f"decompwtf-{install_id(folder)}/{__version__}"


def _fetch(agent: str = "decompwtf") -> dict:
    req = urllib.request.Request(URL, headers={"User-Agent": agent, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def normalize(raw: dict) -> list[dict]:
    out = []
    for p in raw.get("projects", []):
        m = p.get("measures") or {}
        out.append({
            "id": f"{p['owner']}/{p['repo']}", "name": p.get("name") or p["repo"], "platform": p.get("platform", ""),
            "platform_name": PLATFORMS.get(p.get("platform", ""), (p.get("platform") or "").upper()),
            "repo_url": p.get("repo_url") or f"https://github.com/{p['owner']}/{p['repo']}",
            "version": p.get("default_version"), "matched": round(m.get("matched_code_percent", 0.0), 2),
            "functions": m.get("total_functions", 0), "matched_functions": m.get("matched_functions", 0),
            "updated": (p.get("commit") or {}).get("timestamp"),
        })
    out.sort(key=lambda p: p["updated"] or "", reverse=True)
    return out


class CatalogError(RuntimeError):
    """decomp.dev's list couldn't be had, and there's no earlier copy: the reason, in words."""


def _cached(cache: Path) -> list[dict] | None:
    try:
        return normalize(json.loads(cache.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError):
        return None


def catalog(cache: Path, max_age: float = MAX_AGE, fetch: bool = True) -> list[dict]:
    """Projects from decomp.dev, cached on disk for a day. When it can't be had, the last copy (however old) is used;
    with none, CatalogError says why instead of an empty list that looks like "no projects". A refusal is remembered
    (next to the cache) and not asked again for a day; another failure for an hour. `fetch`
    False: only what's cached (the home page's count never makes a request)."""
    if not fetch:
        return _cached(cache) or []
    with _LOCK:  # the page and the home page at once: one request
        return _catalog(cache, max_age)


def _catalog(cache: Path, max_age: float) -> list[dict]:
    if cache.exists() and time.time() - cache.stat().st_mtime < max_age and (fresh := _cached(cache)) is not None:
        return fresh
    failed = cache.with_suffix(".failed.json")
    try:
        last = json.loads(failed.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        last = None
    wait = (AFTER_REFUSAL if last and last.get("code") in (401, 403, 429) else AFTER_ERROR) if last else 0
    if last and time.time() - last.get("at", 0) < wait:
        why = last.get("why") or "decomp.dev's project list isn't available"
    else:
        try:
            raw = _fetch(user_agent(cache.parent))
            normalized = normalize(raw)
        except urllib.error.HTTPError as e:
            code = e.code
            why = ("decomp.dev's rate limit is refusing decomp.wtf's requests for its project list for now"
                   if code in (401, 403, 429) else f"decomp.dev answered {code} to the request for its project list")
        except (OSError, ValueError, KeyError) as e:
            code, why = None, f"decomp.dev couldn't be reached ({getattr(e, 'reason', None) or e})"
        else:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(raw), encoding="utf-8")
            failed.unlink(missing_ok=True)
            return normalized
        try:
            failed.parent.mkdir(parents=True, exist_ok=True)
            failed.write_text(json.dumps({"at": time.time(), "code": code, "why": why}), encoding="utf-8")
        except OSError:
            pass
    old = _cached(cache)
    if old is not None:
        return old
    raise CatalogError(why)

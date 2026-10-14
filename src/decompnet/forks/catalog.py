"""The decomp.dev catalog: public matching decompilation projects with their progress."""

from __future__ import annotations

import json
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


# How often the list is asked for: once a day at most per install, also after decomp.dev refuses (its automatic rate
# limit counts every decomp.wtf as one client, so many installs asking every few hours tripped it). Every request
# says what it is: decomp.dev decides whether it serves decomp.wtf.
MAX_AGE = 24 * 3600
AFTER_REFUSAL = 24 * 3600
AFTER_ERROR = 3600


def _user_agent() -> str:
    from .. import __version__

    return f"decomp.wtf/{__version__} (+https://decomp.wtf)"


def _fetch() -> dict:
    req = urllib.request.Request(URL, headers={"User-Agent": _user_agent(), "Accept": "application/json"})
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


def catalog(cache: Path, max_age: float = MAX_AGE) -> list[dict]:
    """Projects from decomp.dev, cached on disk for a day. When it can't be had, the last copy (however old) is used;
    with none, CatalogError says why instead of an empty list that looks like "no projects". A refusal is remembered
    (next to the cache) and not asked again for a day; another failure for an hour."""
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
            raw = _fetch()
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

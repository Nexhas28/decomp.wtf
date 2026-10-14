"""The decomp.dev catalog: public matching decompilation projects with their progress."""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

URL = "https://decomp.dev/projects.json"
PLATFORMS = {
    "n64": "Nintendo 64", "gc": "GameCube", "wii": "Wii", "ps": "PlayStation", "ps2": "PlayStation 2",
    "psp": "PSP", "gba": "Game Boy Advance", "nds": "Nintendo DS", "3ds": "Nintendo 3DS", "switch": "Switch",
    "win32": "Windows PC", "xbox": "Xbox", "xbox360": "Xbox 360",
}


def _fetch() -> dict:
    req = urllib.request.Request(URL, headers={"User-Agent": "decomp.wtf", "Accept": "application/json"})
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


def catalog(cache: Path, max_age: float = 6 * 3600) -> list[dict]:
    """Projects from decomp.dev, cached on disk; falls back to the cache when offline."""
    try:
        if cache.exists() and time.time() - cache.stat().st_mtime < max_age:
            return normalize(json.loads(cache.read_text(encoding="utf-8")))
        raw = _fetch()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(raw), encoding="utf-8")
        return normalize(raw)
    except (OSError, ValueError):
        if cache.exists():
            return normalize(json.loads(cache.read_text(encoding="utf-8")))
        return []

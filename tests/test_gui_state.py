"""GUI state wiring for the truthful-status slices: P11.7 (A.2) renders the verification scope the
server already exposes, and P2.8 (A.4) renders the compile cache's diagnostics through /api/cache.

Three things are pinned here, deliberately cheaply:
  * /api/cache answers with the cache's real numbers, and answers "off" when the cache is off;
  * app.js's render path consumes verification.receipts / origins / stalls and the cache numbers,
    so the wiring can't silently regress to a green percentage that overclaims;
  * status_json() carries verification / origins / stalls for a stub project (the server side of A.2).
"""

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from decompnet import cache as ccache
from decompnet.app.engine import RomNetwork
from decompnet.app.server import build
from decompnet.network import Network

from .test_permute import make_project

PORT, SECRET = 6501, "s3cret-for-tests"
STATIC = Path(__file__).resolve().parents[1] / "src" / "decompnet" / "app" / "static"


@pytest.fixture
def app(tmp_path):
    """The GUI app over its test client, the same shape tests/test_app_server.py uses."""
    home = tmp_path / "home"
    (home / ".decomp").mkdir(parents=True)
    net = RomNetwork(home / ".decomp" / "decomp.db", home)
    mcp, manager, security = build(net, home / ".decomp", PORT, SECRET)
    client = TestClient(mcp.streamable_http_app(transport_security=security, host="127.0.0.1"),
                        base_url=f"http://127.0.0.1:{PORT}")
    client.cookies.set("decomp_live", SECRET)
    return client, net


# --------------------------------------------------------------------------- A.4: /api/cache


def test_api_cache_reports_the_real_cache(app, tmp_path, monkeypatch):
    """The payload is the cache's own accounting, and it follows real activity (P2.8)."""
    client, _ = app
    root = tmp_path / "cache"
    monkeypatch.setenv("DECOMPNET_CACHE_DIR", str(root))
    monkeypatch.delenv("DECOMPNET_COMPILE_CACHE", raising=False)

    body = client.get("/api/cache").json()
    assert body["enabled"] is True
    assert Path(body["dir"]) == root.resolve()
    for key in ("entries", "bytes", "hit_rate", "hits", "misses", "puts", "coalesced",
                "quarantined", "evicted"):
        assert key in body, key
    assert body["entries"] == 0 and body["bytes"] == 0
    assert body["hit_rate"] is None  # no hits and no misses yet: no rate to claim

    # one real entry through the cache's own API: the payload must follow it
    cache = ccache.cache_for_dir(root)
    assert cache is not None
    payload = b"\x7fELF a compiled object"
    cache.put("k" * 64, ok=True, outcome=ccache.OUTCOME_SUCCESS, payload=payload, log="ok")
    assert cache.get("k" * 64) is not None

    body = client.get("/api/cache").json()
    assert body["enabled"] is True
    assert body["entries"] == 1
    assert body["bytes"] >= len(payload)  # 512 bytes of manifest overhead plus payload and log
    assert body["puts"] == 1 and body["hits"] == 1 and body["misses"] == 0
    assert body["hit_rate"] == 1.0
    assert body["quarantined"] == 0 and body["evicted"] == 0
    assert "1 entries" in body["diagnose"]


def test_api_cache_says_off_when_the_compile_cache_is_disabled(app, monkeypatch):
    """DECOMPNET_COMPILE_CACHE=0: the GUI gets a truthful "off", never stale numbers."""
    client, _ = app
    monkeypatch.setenv("DECOMPNET_COMPILE_CACHE", "0")
    body = client.get("/api/cache").json()
    assert body["enabled"] is False
    assert "off" in body["reason"].lower()


def test_api_cache_says_off_when_its_directory_cannot_be_created(app, tmp_path, monkeypatch):
    """A path that is a file can't hold a cache: the payload says so instead of raising."""
    client, _ = app
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("in the way")
    monkeypatch.setenv("DECOMPNET_CACHE_DIR", str(blocker))
    monkeypatch.delenv("DECOMPNET_COMPILE_CACHE", raising=False)
    body = client.get("/api/cache").json()
    assert body["enabled"] is False
    assert Path(body["dir"]).resolve() == blocker.resolve()
    assert "cannot" in body["reason"].lower()


# --------------------------------------------------------------------------- A.2/A.4: app.js wiring


def _function_body(src: str, name: str) -> str:
    """The source of one top-level `function name(...)` up to the next one."""
    start = src.index(f"function {name}(")
    end = src.find("\nfunction ", start + 1)
    return src[start:] if end == -1 else src[start:end]


def _render_block(src: str) -> str:
    """The A.2 + A.4 render functions, wherever they sit between their neighbours."""
    start = src.index("function renderTrust(")
    end = src.index("function renderAgentsKpi(")
    return src[start:end]


def test_app_js_renders_the_truthful_verification_scope():
    src = (STATIC / "app.js").read_text(encoding="utf-8")
    block = _render_block(src)
    assert ".verification" in block
    assert "v.receipts" in block          # receipt coverage
    assert "v.matched_epoch" in block     # the verified epoch
    assert "v.origins" in block           # candidate origin breakdown
    assert "v.stalls" in block            # wall/stall reasons
    # the overview render path actually reaches it (and the live poll carries it forward)
    assert "renderTrust(" in _function_body(src, "renderOverview")
    assert "verification" in _function_body(src, "poll")


def test_app_js_renders_the_cache_diagnostics():
    src = (STATIC / "app.js").read_text(encoding="utf-8")
    cache_fn = _function_body(src, "renderCache")
    assert "enabled" in cache_fn          # the explicit "cache off" state
    assert "entries" in cache_fn and "bytes" in cache_fn and "hit_rate" in cache_fn
    assert "puts" in cache_fn and "coalesced" in cache_fn
    assert "quarantined" in cache_fn and "evicted" in cache_fn
    assert 'api("/api/cache")' in src
    assert "renderCache(" in _function_body(src, "renderOverview")


# --------------------------------------------------------------------------- A.2: status_json


def test_status_json_carries_the_truthful_scope_fields(tmp_path):
    """A stub project still reports the three fields, as empty-but-present facts."""
    net = Network(tmp_path / "stub.db")
    make_project(net, 3)
    project = net.status_json()["projects"][0]
    assert set(project["verification"]) == {"receipts", "matched_epoch"}
    assert project["verification"]["receipts"] == 0
    assert project["verification"]["matched_epoch"] == 0
    assert project["origins"] == {}
    assert project["stalls"] == {}

    # and they are live counts, not constants
    with net.store.tx() as db:
        db.execute("INSERT INTO candidates(function_id, origin, source, source_digest, created_at) "
                   "VALUES(1, 'retrieval', 'int func_001(void) { return 1; }', 'd', 0)")
        db.execute("INSERT INTO search_budgets(function_id, reset_reason) VALUES(1, 'exhausted')")
        db.execute("INSERT INTO verification_receipts(function_id, snapshot_id, source_digest, origin, "
                   "function_link_match, acceptance_policy, verified_at_utc) "
                   "VALUES(1, 'snap', 'd', 'retrieval', 'match', 'snapshot', '2024-01-01T00:00:00Z')")
        db.execute("UPDATE projects SET matched_epoch=4 WHERE id=1")
    project = net.status_json()["projects"][0]
    assert project["verification"] == {"receipts": 1, "matched_epoch": 4}
    assert project["origins"] == {"retrieval": 1}
    assert project["stalls"] == {"exhausted": 1}
    # the honest fields survive JSON, which is what the GUI actually receives
    assert json.loads(json.dumps(project["verification"]))["receipts"] == 1


def test_drawer_panes_never_collapse_to_zero_height():
    """Regression: the drawer body is a definite-height grid whose auto rows can collapse to 0px when a
    pane's automatic minimum is 0. The code pane then painted 580px of overflow while xref/notes were
    laid out on top of it, covering the Compile and compare button (measured live: rows 117/39/0/46/995,
    code pane height 0 while its children were 580 tall). The panes must keep max-content height."""
    css = (Path(__file__).resolve().parent.parent / "src" / "decompnet" / "app" / "static" / "app.css").read_text(encoding="utf-8")
    assert ".code-cols {" in css
    code_cols = css.split(".code-cols {", 1)[1].split("}", 1)[0]
    assert "min-height: max-content" in code_cols, "the code pane must not collapse to 0 height"
    assert "min-height: 0" not in code_cols
    assert "#fnDiffView { min-height: max-content; }" in css, "the diff pane needs the same guard"
    # the panes' height alone is not enough: the auto ROW must not shrink either, or the item grows
    # while its row stays 0px and the next rows land on top of it anyway (measured live).
    body = css.split(".drawer-body {", 1)[1].split("}", 1)[0]
    assert "grid-auto-rows: max-content" in body, "drawer rows must size to their content"

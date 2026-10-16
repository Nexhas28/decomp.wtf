"""`decomp mcp` identities: each bridge process has its own stable client id, and a bridge whose identity
another session took over (it sat idle) gets a new one instead of failing every call with
"invalid API token". No demo ROM needed (unlike test_bridge)."""

import io
import json
import time
import types

import pytest

from decompnet.app import bridge
from decompnet.app.engine import RomNetwork
from decompnet.network import NetworkError


def test_each_bridge_process_sends_its_own_stable_client_id(monkeypatch):
    sent = []

    def urlopen(req, timeout=0):
        sent.append(json.loads(req.data))
        return io.BytesIO(json.dumps({"agent": "chat", "token": "dn_x"}).encode())

    monkeypatch.setattr(bridge.urllib.request, "urlopen", urlopen)
    info = {"port": 1, "secret": "s"}
    bridge.mint(info, "chat")
    bridge.mint(info, "chat")  # again after the app restarted: the same id, so the same identity comes back
    assert bridge.CLIENT_ID and sent[0]["client"] == sent[1]["client"] == bridge.CLIENT_ID


def test_an_explicit_bridge_id_still_wins(monkeypatch):
    import importlib

    monkeypatch.setenv("DECOMP_BRIDGE_ID", "my-bridge")
    try:
        assert importlib.reload(bridge).CLIENT_ID == "my-bridge"
    finally:
        monkeypatch.delenv("DECOMP_BRIDGE_ID")
        importlib.reload(bridge)
    assert bridge.CLIENT_ID.startswith("bridge-")


def test_a_rejected_token_is_recognised_in_errors_and_error_results():
    text = types.SimpleNamespace(text="invalid API token")
    assert bridge._rejected("Error: invalid API token")
    assert bridge._rejected(bridge._error_text(types.SimpleNamespace(is_error=True, content=[text])))
    assert not bridge._error_text(types.SimpleNamespace(is_error=False, content=[text]))  # a normal result
    assert not bridge._rejected("f1 is claimed by bob")


@pytest.fixture
def net(tmp_path):
    return RomNetwork(tmp_path / "decomp.db", tmp_path / "work")


def test_a_bridge_whose_idle_identity_was_taken_over_gets_a_working_one_again(net):
    a, token_a = net.chat_agent("claude", client="bridge-a")
    with net.store.tx() as db:  # A sits idle for more than ten minutes...
        db.execute("UPDATE agents SET last_seen=? WHERE id=?", (time.time() - 3600, a.id))
    b, token_b = net.chat_agent("claude", client="bridge-b")  # ...and a second session takes the name over
    assert b.id == a.id
    with pytest.raises(NetworkError, match="invalid API token") as e:  # what A's next call gets
        net.authenticate(token_a)
    assert bridge._rejected(str(e.value))
    again, token_again = net.chat_agent("claude", client="bridge-a")  # A connects again (bridge re-mint)
    assert net.authenticate(token_again).id == again.id != b.id
    assert net.authenticate(token_b).id == b.id  # and B keeps working
    # and a bridge that comes back after the app restarted gets its own row back, not a suffixed one
    assert net.chat_agent("claude", client="bridge-a")[0].id == again.id


def test_the_bridge_gets_a_new_identity_and_repeats_the_call_when_its_token_is_rejected(monkeypatch):
    """The whole proxy path, with the app replaced: the first call answers "invalid API token" (as the app does,
    an error result rather than an exception); the bridge mints again and the repeated call works."""
    import asyncio
    import contextlib

    import mcp
    import mcp.client.streamable_http as streamable
    import mcp.server.lowlevel.server as lowlevel
    import mcp.server.stdio as stdio

    calls, minted, out = [], [], {}

    class FakeClient:
        def __init__(self, transport, read_timeout_seconds=None):
            self.token = transport.headers["Authorization"].removeprefix("Bearer ")
            self.instructions = ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call_tool(self, name, arguments):
            calls.append(self.token)
            ok = self.token != "dn_old"
            return types.SimpleNamespace(is_error=not ok,
                                         content=[types.SimpleNamespace(text="ok" if ok else "invalid API token")])

    class FakeServer:
        def __init__(self, *args, on_call_tool=None, **kwargs):
            self.on_call_tool = on_call_tool

        def create_initialization_options(self):
            return None

        async def run(self, read, write, options):
            out["result"] = await self.on_call_tool(None, types.SimpleNamespace(name="progress", arguments={}))

    @contextlib.asynccontextmanager
    async def fake_stdio():
        yield None, None

    monkeypatch.setattr(mcp, "Client", FakeClient)
    monkeypatch.setattr(streamable, "streamable_http_client", lambda url, http_client=None: http_client)
    monkeypatch.setattr(lowlevel, "Server", FakeServer)
    monkeypatch.setattr(stdio, "stdio_server", fake_stdio)
    info = {"port": 1, "secret": "s"}
    monkeypatch.setattr(bridge, "ensure_app", lambda home: info)  # the same app: only the token went stale
    monkeypatch.setattr(bridge, "_alive", lambda port: True)
    monkeypatch.setattr(bridge, "mint", lambda i, name: minted.append(name) or "dn_new")
    asyncio.run(asyncio.wait_for(bridge.proxy(info, "dn_old", "claude"), 10))
    assert minted == ["claude"] and calls == ["dn_old", "dn_new"]
    assert not out["result"].is_error and out["result"].content[0].text == "ok"

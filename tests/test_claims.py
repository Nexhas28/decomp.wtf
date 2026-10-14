"""Which function an agent gets next in a fork: what nobody gave up on, small first, and among functions of about
the same size the one in the file closest to done. A crowd of agents must not spend its time passing the hardest
leftovers around (Mario Party 6: 16 agents, one match, on 326 big functions released again and again)."""

import json

from decompnet.app.engine import RomNetwork


def test_fresh_small_functions_first_and_files_close_to_done_among_equals(tmp_path):
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                         ("game", "Game", "", "ppc", 1, str(tmp_path), json.dumps({"kind": "rom"}))).lastrowid
        rows = [("leftover", "a", 12, 3), ("big_fresh", "b", 400, 0), ("small_in_big_file", "c", 20, 0),
                ("small_in_done_file", "d", 22, 0), ("tiny", "c", 4, 0)]
        rows += [(f"other_{i}", "c", 300, 0) for i in range(5)]  # file c has lots left; file d only one
        for name, unit, n, abandons in rows:
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,status,unit,abandons) VALUES(?,?,?,?,?,?,?)",
                       (pid, name, n * 4, n, "open", unit, abandons))
    order = []
    for k in range(len(rows)):
        agent, _ = net.local_agent(f"a{k}")
        order.append(net.claim_row(agent, "game", None, "finish")["name"])
    # 4 and 22 instructions are about the same size: the file with one function left goes first
    assert order[:3] == ["small_in_done_file", "tiny", "small_in_big_file"], order
    assert order[-1] == "leftover" and order.index("big_fresh") < order.index("leftover"), order

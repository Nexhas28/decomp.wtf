"""Function statuses stay short words (store.py: heal_statuses)."""

import pytest


def test_a_garbled_status_is_put_right_and_refused(tmp_path):
    """A PS1 project's database had functions whose status was their assembly: they showed as "to do" but no claim
    ever picked them. Opening the database puts them right; a garbled status isn't stored again."""
    import sqlite3

    from decompnet.store import Store

    Store(tmp_path / "x.db")
    db = sqlite3.connect(tmp_path / "x.db")
    db.execute("DROP TRIGGER functions_status_insert")
    db.execute("DROP TRIGGER functions_status_update")
    db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES('p','p','','mips',0,'','{}')")
    for name, status, source in (("a", "glabel a\n/* 1 */  jr $ra\n", None), ("b", "glabel b\n", "void b(void) {}"),
                                 ("c", "open", None), ("d", "nosource", None)):
        db.execute("INSERT INTO functions(project_id,name,status,matched_source) VALUES(1,?,?,?)", (name, status, source))
    db.commit()
    db.close()
    store = Store(tmp_path / "x.db")
    assert {r["name"]: r["status"] for r in store.all("SELECT name, status FROM functions")} == {
        "a": "open", "b": "matched", "c": "open", "d": "nosource"}
    with pytest.raises(sqlite3.IntegrityError, match="short word"):
        store.conn.execute("UPDATE functions SET status=? WHERE name='a'", ("glabel a\n",))
    store.conn.execute("UPDATE functions SET status='asm' WHERE name='a'")  # a real status still goes in

"""Editing a dtk-template project's file layout like a contributor: splits.txt round trips, configure.py gets new
files in the right library with the settings its neighbours use, or a new library made like a sibling module's."""

from decompnet.forks import dtksplits

SPLITS = """Sections:
\t.text       type:code align:4
\t.rodata     type:rodata align:32
\t.data       type:data align:8

REL/m656DLL/prolog.c:
\t.text       start:0x00000000 end:0x000000A0

REL/m656DLL/game.c:
\t.text       start:0x000000A0 end:0x0000961C
\t.rodata     start:0x00000000 end:0x00000220
\t.data       start:0x00000000 end:0x000006CA
"""

CONFIGURE = '''\
def Rel(lib_name, objects):
    return {"lib": lib_name, "mw_version": "GC/1.3.2", "objects": objects}


config.libs = [
    {
        "lib": "Runtime.PPCEABI.H",
        "objects": [
            Object(Matching, "Runtime.PPCEABI.H/__va_arg.c"),
            Object(NonMatching, "Runtime.PPCEABI.H/ptmf.c"),
        ],
    },
    Rel(
        "m656DLL",
        objects={
            Object(Matching, "REL/m656DLL/prolog.c", extra_cflags=["-pooldata off"]),
            Object(
                NonMatching,
                "REL/m656DLL/game.c",
                extra_cflags=["-pooldata off"],
            ),
            Object(Matching, "REL/m656DLL/runtime.c", extra_cflags=["-DRUNTIME=1", "-pooldata off"]),
        },
    ),
]
'''


def test_splits_round_trip_and_owner():
    sp = dtksplits.Splits.parse(SPLITS)
    assert sp.render() == SPLITS
    assert [f.name for f in sp.files] == ["REL/m656DLL/prolog.c", "REL/m656DLL/game.c"]
    assert sp.owner(".text", 0x100).name == "REL/m656DLL/game.c" and sp.owner(".rodata", 0x300) is None


def test_libraries_and_their_files():
    libs = dtksplits.libs(CONFIGURE)
    assert [lib.name for lib in libs] == ["Runtime.PPCEABI.H", "m656DLL"]
    assert [p for p, _, _ in libs[1].objects] == ["REL/m656DLL/prolog.c", "REL/m656DLL/game.c", "REL/m656DLL/runtime.c"]


def test_a_new_file_goes_next_to_its_neighbours_with_their_settings():
    text = dtksplits.add_objects(CONFIGURE, "m656DLL", ["REL/m656DLL/text_00001000.c"])
    assert 'Object(NonMatching, "REL/m656DLL/text_00001000.c", extra_cflags=["-pooldata off"])' in text
    assert "RUNTIME=1\", \"-pooldata off\"]),\n            Object(NonMatching" in text
    assert dtksplits.add_objects(text, "m656DLL", ["REL/m656DLL/text_00001000.c"]) == text  # listed already
    compile(text, "configure.py", "exec")


def test_a_module_without_a_library_gets_one_like_its_sibling():
    text = dtksplits.add_objects(CONFIGURE, "m657DLL", ["REL/m657DLL/text_00000000.cpp"])
    libs = dtksplits.libs(text)
    assert [lib.name for lib in libs] == ["Runtime.PPCEABI.H", "m656DLL", "m657DLL"]
    assert [p for p, _, _ in libs[2].objects] == ["REL/m657DLL/text_00000000.cpp"]
    assert 'Rel(\n        "m657DLL",\n        objects={\n            Object(NonMatching, "REL/m657DLL/text_00000000.cpp", ' \
           'extra_cflags=["-pooldata off"]),\n        },\n    ),' in text
    compile(text, "configure.py", "exec")

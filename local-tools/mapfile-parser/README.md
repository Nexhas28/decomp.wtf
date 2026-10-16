# mapfile-parser

Python library and CLI from the Decompollaborate project (repo ethteck/mapfile_parser, which redirects to Decompollaborate/mapfile_parser) for reading linker map files. It parses GNU ld and clang lld maps into sections and symbols with addresses and sizes, and ships utilities such as `jsonify`, `progress`, `objdiff_report`, `symbol_sizes_csv` and `sym_info`.

Why it helps decomp.wtf: byte-matching projects lean on the linker map to know where each function and data symbol landed, so this is the usual way to turn a built map into progress numbers and JSON for other tools.

How to run (from this folder):

    .venv/bin/python -m mapfile_parser --help
    .venv/bin/python -m mapfile_parser jsonify path/to/game.map -o out.json

Installed from source at tag 2.9.0 (commit dd01f8c1cb3250c715d0b7f4f93076531c28dc00) with `uv venv .venv` and `uv pip install <checkout>`. It is a Rust extension built by maturin, so the venv holds a compiled wheel built locally; no binary was downloaded. The `.venv/` folder is a local install and can be rebuilt with the same commands.

Test: `bash test.sh` builds a tiny GNU ld map and checks the Python API and the `jsonify` CLI both find the expected symbols. Last run: pass.

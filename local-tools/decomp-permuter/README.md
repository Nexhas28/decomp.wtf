# decomp-permuter

Randomised source-code permuter from simonlindholm/decomp-permuter (MIT). It takes a C function that almost matches a target object file, applies many small rewrites (reordering, equivalent expressions, temporaries), compiles each variant and keeps the one whose assembly scores closest to the target. This is how byte-matching decompilation finds the exact compiler-style source for stubborn functions.

Installed from upstream main at commit 8556c81d80d1c1af98a858c8f4dc951357f29139 (2026-10-06). The upstream repo has no release tags. The Python code is copied into `app/`, and the entry point is `run`, which uses the local `.venv/` Python 3.12.

Run it from a folder that holds `base.c`, `compile.sh`, `settings.toml` and `target.o`:

    local-tools/decomp-permuter/run <folder> --debug
    local-tools/decomp-permuter/run <folder> --stop-on-zero -j1

Smoke test: `bash local-tools/decomp-permuter/test.sh`. It builds a small MIPS function with the IDO 7.1 compiler and checks that a mismatching target gives a non-zero base score, and that the matching target reaches score 0 and stops. Last run: PASS.

Needs the IDO compiler under `~/Decomp/tools/vendor/compilers/` and the MIPS binutils cache under `~/Library/Application Support/decompnet/toolchains/binutils-mips`.

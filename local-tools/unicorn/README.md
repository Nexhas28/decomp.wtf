# unicorn

Unicorn is a CPU emulator framework (built on QEMU) with Python bindings. For decomp work it can run a small function or data routine from a ROM or binary and show the registers and memory it produces, which helps check what a rewritten function actually computes.

Repo: https://github.com/unicorn-engine/unicorn

Install: `uv venv` plus `uv pip install unicorn==2.1.4` (PyPI wheel, release tag 2.1.4) into `local-tools/unicorn/.venv/`.

Run: `local-tools/unicorn/.venv/bin/python -I your_script.py`. Import with `from unicorn import *` and the arch constants modules (`unicorn.mips_const`, `unicorn.arm_const`, `unicorn.ppc_const`).

Test: `bash local-tools/unicorn/test.sh` builds three tiny snippets (MIPS32 big-endian, ARM Thumb, PPC32 big-endian), runs them and checks the result registers. Last result: pass (prints `ok:` for each arch, then `PASS`).

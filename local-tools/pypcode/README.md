# pypcode

Python bindings for Ghidra's SLEIGH processor specifications (angr/pypcode). It disassembles raw machine code and lifts it to P-code, Ghidra's intermediate language, from Python.

Why it helps decomp.wtf: it gives scripts a quick way to check what a byte sequence means for MIPS (N64/PSX/PS2), ARM (GBA/NDS) and PowerPC (GameCube/Wii), without starting Ghidra.

How to run: `.venv/bin/python -I your_script.py`, then `from pypcode import Context` and `Context("MIPS:BE:32:default").disassemble(bytes, addr)`.

Install: PyPI package `pypcode` version 4.0.1 (matches upstream tag v4.0.1 of https://github.com/angr/pypcode), installed with `uv pip install pypcode==4.0.1` into `.venv/` using Python 3.12 (arm64).

Test: `bash test.sh` disassembles MIPS BE, ARM (Thumb-free `bx lr`) and PowerPC BE bytes, checks the expected mnemonics, and checks that MIPS `addiu` lifts to a P-code `INT_ADD`. Result on 2026-10-16: PASS.

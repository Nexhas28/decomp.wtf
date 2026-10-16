# ghidriff

Ghidra-based binary diffing CLI (https://github.com/clearbluejar/ghidriff). It imports two builds of a binary into Ghidra headless, auto-analyzes them, matches functions (Version Tracking engine) and writes a markdown/JSON report of added, deleted and modified functions with decompiled-code diffs.

Why for decomp.wtf: compare two builds of the same program (e.g. your recompiled output vs the original, or two compiler flag sets) and see which functions differ, with pseudocode diffs, without opening the Ghidra GUI.

Run: `local-tools/ghidriff/bin/ghidriff old.elf new.elf -o out_dir` (wrapper sets JAVA_HOME to JDK 21 and GHIDRA_INSTALL_DIR to the Ghidra 12.1.4 install; the install is only read, never modified).

Installed: `uv venv .venv` + `uv pip install ghidriff==1.0.0` (PyPI release v1.0.0; pulls pyghidra 3.1.0, which matches the pyghidra shipped with Ghidra 12.1.4).

Test: `bash local-tools/ghidriff/test.sh` builds two tiny x86-64 ELFs (clang + lld@21, only foo() differs), diffs them, and checks foo is reported modified and bar is not. Result: PASS (about 20 s).

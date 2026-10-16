# keystone

Keystone is a multi-architecture assembler library (Capstone's counterpart). It turns assembly text into machine-code bytes, which is what decomp.wtf needs when it checks that a rewritten function assembles back to the original bytes. This folder has the `kstool` command-line tool and the Python binding.

- Repo: https://github.com/keystone-engine/keystone (release 0.9.2, commit dc7932ef2b2c4a793836caec6ecab485005139d6)
- Installed: Python package `keystone-engine` 0.9.2 in `.venv/` (built from the PyPI sdist, sha256 2f7af62d...), `bin/kstool` and `lib/libkeystone.0.dylib` built for macOS arm64.
- Run the CLI: `./run mipsbe 'jr $ra'` prints the bytes (`03 e0 00 08`). Supported names include mipsbe, thumb, armbe, ppc32be.
- Python: `.venv/bin/python -c 'import keystone'`.
- Test: `bash test.sh` assembles MIPS, Thumb, ARM and PPC snippets through the CLI and the Python binding and compares the bytes to known values. Last result: PASS (2026-10-16).

# mkpsxiso (dumpsxiso)

mkpsxiso builds PlayStation 1 disc images (BIN/CUE) from an XML project file, and its companion dumpsxiso extracts files and the layout back out of a disc image. Both come from the Lameguy64/mkpsxiso repo[^repo].

Why it helps decomp.wtf: PS1 decomp projects need to rebuild a disc around the recompiled executable and extract the original disc to get the assets and the SYSTEM.CNF/executable layout. These two tools cover both directions.

How to run:
- `local-tools/mkpsxiso/bin/mkpsxiso -y disc.xml` builds `test.bin` and `test.cue` from an XML project.
- `local-tools/mkpsxiso/bin/dumpsxiso -x outdir -s out.xml image.bin` extracts a disc image into `outdir` and writes an XML description.

Install: built from the upstream git tag v2.30 (commit 54fb1644ed8741223583e2dcda358b75a205e214) with CMake (Release) on macOS arm64 and copied into `bin/`. Git submodules were initialised for the build. Runs natively on macOS arm64.

Test: `bash local-tools/mkpsxiso/test.sh` builds a small disc with a file and a subdirectory, extracts it with dumpsxiso, and compares the files byte for byte. Last run: PASS (exit 0).

[^repo]: https://github.com/Lameguy64/mkpsxiso README

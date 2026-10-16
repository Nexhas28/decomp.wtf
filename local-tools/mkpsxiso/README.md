# mkpsxiso (dumpsxiso)

mkpsxiso builds PlayStation 1 disc images (BIN/CUE) from an XML project file, and its companion dumpsxiso extracts files and the layout back out of a disc image. Both come from the Lameguy64/mkpsxiso repo[^repo].

Why it helps decomp.wtf: PS1 decomp projects need to rebuild a disc around the recompiled executable and extract the original disc to get the assets and the SYSTEM.CNF/executable layout. These two tools cover both directions.

How to run:
- `local-tools/mkpsxiso/bin/mkpsxiso -y disc.xml` builds `test.bin` and `test.cue` from an XML project.
- `local-tools/mkpsxiso/bin/dumpsxiso -x outdir -s out.xml image.bin` extracts a disc image into `outdir` and writes an XML description.

Install: the prebuilt binaries from the upstream GitHub release v2.30 (`mkpsxiso-2.30-Darwin.zip`, a universal Intel + Apple Silicon build, sha256 a64d4d57d78ce57cd810a6a044602ce3cf1b552ca7c7e8675518f554206e0b49), copied into `bin/`. No build needed; the earlier CMake build is kept aside in `bin.local-build/`.

Test: `bash local-tools/mkpsxiso/test.sh` builds a small disc with a file and a subdirectory, extracts it with dumpsxiso, and compares the files byte for byte. Last run: PASS (exit 0).

[^repo]: https://github.com/Lameguy64/mkpsxiso README

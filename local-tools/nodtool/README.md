# nodtool

Command-line tool from encounter/nod (Apache-2.0) for reading GameCube and Wii disc images. It shows disc info, lists and extracts the file system (including `sys/main.dol` and the `files/` tree), converts images to ISO, and converts to and from RVZ compressed format.

For decomp.wtf this is how a GameCube or Wii project gets its `main.dol` and assets out of a disc image before splitting or matching them. Its RVZ round trip also lets us check that a conversion is lossless.

Run it: `local-tools/nodtool/bin/nodtool info game.iso`, or `nodtool extract game.iso out/` to unpack the disc.

Install: built from the encounter/nod release tag `v2.0.0-alpha.12` (commit `ebd80cac99b48a323200d84365e07a58cc27412d`). `nodtool -V` reports that same commit. The exact build command was not recorded in this folder, so the binary is not reproducible from notes alone.

Test: `bash local-tools/nodtool/test.sh` hand-builds a tiny GCM disc image with Python, then checks `info` (game ID and title), `extract` (file contents and `main.dol` bytes), `convert` to RVZ and back, and that the round trip is byte-identical. Result: PASS on 2026-10-16.

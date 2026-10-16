# pret-gba-tools

Command-line tools from the pret GBA decompilation project (gbagfx, preproc, scaninc, gbafix, mid2agb, mapjson, jsonproc, bin2c, ramscrgen, rsfont, wav2agb). They convert GBA graphics, sound and maps, expand `.string` charmap text, and find include dependencies, all of which the pret build depends on.

Why it helps decomp.wtf: byte-matching GBA projects need these exact converters to rebuild assets identically, so the outputs can be checked against the original ROM.

How to run: `local-tools/pret-gba-tools/bin/<tool>` (for example `bin/gbagfx in.png out.4bpp`). Running a tool with no arguments prints its usage.

Install: built with clang on macOS arm64 from the `tools/` folder of pret/pokeemerald at commit 731ad5bfd6e6f265508d0efcca0ba42f9dcf5881 (commit 731ad5b). Sources were extracted with `git archive` into a fresh scratch dir and compiled with the flags from the pret build logs. Only the binaries are kept here.

Test: `bash local-tools/pret-gba-tools/test.sh`. It round-trips 4bpp graphics through PNG and LZ77, checks preproc charmap output, checks scaninc transitive includes and checks gbafix header patching. Last run: PASS.

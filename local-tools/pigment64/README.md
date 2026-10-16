# pigment64

pigment64 is a Rust library and command-line tool from the decompals project that converts N64 image data (RGBA16, RGBA32, I/IA, CI formats) to and from PNG.

Why it helps decomp.wtf: when matching N64 textures, you often need to turn a PNG into the exact binary texture bytes the game uses, or turn a dumped texture back into a PNG to look at it.

Run it:

```bash
local-tools/pigment64/bin/pigment64 to-bin -f rgba16 -o out.bin in.png
local-tools/pigment64/bin/pigment64 to-png --width 4 --height 2 -f rgba16 -o back.png out.bin
```

Install: the prebuilt Apple Silicon binary from the upstream GitHub release 0.6.3 (`pigment64-aarch64-apple-darwin.tar.gz`, sha256 b848a38be03958f4eeff71fb81ebf17472d76a17f1089b9734242688acb42a5c), copied into `bin/`. No build needed.

Test: `bash local-tools/pigment64/test.sh` prints `PASS` (last run: exit 0). It checks exact RGBA16 bytes for a tiny generated PNG, a PNG round trip, and an I8 output size.

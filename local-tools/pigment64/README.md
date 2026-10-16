# pigment64

pigment64 is a Rust library and command-line tool from the decompals project that converts N64 image data (RGBA16, RGBA32, I/IA, CI formats) to and from PNG.

Why it helps decomp.wtf: when matching N64 textures, you often need to turn a PNG into the exact binary texture bytes the game uses, or turn a dumped texture back into a PNG to look at it.

Run it:

```bash
local-tools/pigment64/bin/pigment64 to-bin -f rgba16 -o out.bin in.png
local-tools/pigment64/bin/pigment64 to-png --width 4 --height 2 -f rgba16 -o back.png out.bin
```

Install: built from the upstream git tag 0.6.3 (commit 0fa6b5cf41bb8ec0cf6b82687076931a2e5552c5) with `cargo build --release --locked` (rustc 1.98.1, Apple Silicon, native). The release binary was copied into `bin/`.

Test: `bash local-tools/pigment64/test.sh` prints `PASS` (last run: exit 0). It checks exact RGBA16 bytes for a tiny generated PNG, a PNG round trip, and an I8 output size.

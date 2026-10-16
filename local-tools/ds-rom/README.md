# ds-rom

Rust command-line tool `dsrom` from AetiasHax/ds-rom (MIT). It dumps, extracts and rebuilds Nintendo DS ROMs, which is what a decomp.wtf DS project needs to split a ROM into its ARM9/ARM7 binaries, overlays, and assets and to rebuild it for byte-matching.

Run it: `local-tools/ds-rom/bin/dsrom --help`. Example: `dsrom extract --rom game.nds --path out/` then `dsrom build --config out/config.yaml --rom rebuilt.nds`.

Install: `cargo install --root local-tools/ds-rom --locked` from the v0.8.0 source (crate ds-rom-cli 0.8.0). Built on macOS arm64 with rustc 1.98.1. The source snapshot had no git metadata, so the exact commit is not recorded.

Test: `bash local-tools/ds-rom/test.sh` builds a synthetic DS ROM (no copyrighted data) with arm-none-eabi-gcc and the sibling ndstool, then checks dump, extract, build and a stable second round trip. Result: PASS (2026-10-16).

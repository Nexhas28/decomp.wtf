# binwalk (v3)

binwalk v3 is ReFirmLabs' Rust rewrite of the classic firmware and binary carving tool. It scans a file for embedded signatures (compressed streams, filesystems, executables) and can extract them. For decomp.wtf it helps find compressed or packed data inside ROM and firmware dumps before any disassembly.

Run it with `local-tools/binwalk/bin/binwalk`. For example, `bin/binwalk -e -C out some.bin` scans and extracts into `out/`.

Installed with `cargo install --locked binwalk@3.1.0` into this folder (crates.io release 3.1.0, whose Cargo.toml points at https://github.com/ReFirmLabs/binwalk). Result: Mach-O arm64 binary, `binwalk 3.1.0`.

Test: `bash local-tools/binwalk/test.sh` builds a small hand-made blob (gzip at 0x400, xz at 0x800, plus a zlib file), runs binwalk with extraction, and checks the signatures and extracted payloads. Status: pass (2026-10-16).

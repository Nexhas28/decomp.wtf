# binwalk (v3)

binwalk v3 is ReFirmLabs' Rust rewrite of the classic firmware and binary carving tool. It scans a file for embedded signatures (compressed streams, filesystems, executables) and can extract them. For decomp.wtf it helps find compressed or packed data inside ROM and firmware dumps before any disassembly.

Run it with `local-tools/binwalk/bin/binwalk`. For example, `bin/binwalk -e -C out some.bin` scans and extracts into `out/`.

Installed from Homebrew (`brew install binwalk`, Apple Silicon bottle 3.1.0): `bin/binwalk` links to `/opt/homebrew/bin/binwalk`. Upstream https://github.com/ReFirmLabs/binwalk publishes no macOS binaries itself. The earlier cargo build is kept aside in `bin.local-build/`.

Test: `bash local-tools/binwalk/test.sh` builds a small hand-made blob (gzip at 0x400, xz at 0x800, plus a zlib file), runs binwalk with extraction, and checks the signatures and extracted payloads. Status: pass (2026-10-16).

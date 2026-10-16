# crunch64

crunch64 is a command-line tool from the decompals project for compressing and decompressing the N64 asset formats Yay0, Yaz0, MIO0 and gzip (raw deflate). Decomp work on N64 games needs to re-create the exact compressed bytes a game ships with, so having a matching compressor is useful for decomp.wtf.

Run it from the tool folder:

    local-tools/crunch64/bin/crunch64 compress yay0 in.bin out.yay0
    local-tools/crunch64/bin/crunch64 decompress yay0 out.yay0 back.bin

Installation: `cargo install --root local-tools/crunch64 --locked --git https://github.com/decompals/crunch64 --tag 0.6.2 crunch64-cli`. Upstream commit is 35bb166ae26ee5143af7a62844aa084d4ecf33ce, built with rustc 1.98.1 on macOS arm64.

Test: `bash local-tools/crunch64/test.sh` round-trips a generated file through all formats and checks magic bytes. Last run on 2026-10-16: PASS (exit 0).

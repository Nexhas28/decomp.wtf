---
type: Tool
title: crunch64
description: Command-line compressor and decompressor for N64 asset formats Yay0, Yaz0, MIO0 and gzip.
resource: https://github.com/decompals/crunch64
tags: [n64, compression]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:02:54Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:02:54Z }
sources:
  - id: repo
    resource: https://github.com/decompals/crunch64
    title: crunch64 README
---

# crunch64

crunch64 compresses and decompresses the N64 formats Yay0, Yaz0, MIO0 and gzip (raw deflate) [^repo]. Byte-matching decompilation of N64 games often depends on recreating the exact compressed blobs the game ships with, so a matching compressor is needed. It also provides C and Python bindings upstream [^repo].

## Local install

Path `local-tools/crunch64/`, version 0.6.2 (tag, commit 35bb166ae26ee5143af7a62844aa084d4ecf33ce). Entry point `bin/crunch64`. Runs natively on macOS arm64.

## Examples

```bash
local-tools/crunch64/bin/crunch64 compress yay0 in.bin out.yay0
local-tools/crunch64/bin/crunch64 decompress yay0 out.yay0 back.bin
```

## Test result

PASS, 2026-10-16. `test.sh` round-trips a generated file through yay0, yaz0 and mio0, inflates the gzip output with Python zlib, checks the format magic bytes, and checks that yaz0 output is smaller than the input.

## Related

- [splat](/project/toolchains.md)
- objdiff (not in local-tools)

[^repo]: crunch64 README

---
type: Tool
title: pigment64
description: Command-line and Rust library that converts N64 texture formats to and from PNG.
resource: https://github.com/decompals/pigment64
tags: [n64, texture-converter]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:04:28Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:04:28Z }
sources:
  - id: repo
    resource: https://github.com/decompals/pigment64
    title: pigment64 README
---

# pigment64

pigment64 converts N64 image data between native texture formats (RGBA16, RGBA32, I1/I4/I8, IA4/IA8/IA16, CI4/CI8) and PNG files. It is a Rust library with a CLI and Python bindings.[^repo] For byte-matching decompilation of N64 textures, it lets you produce the exact binary texture a game uses from a PNG, and view a dumped texture as a PNG.

## Local install

Path `local-tools/pigment64/`, version 0.6.3 (commit 0fa6b5cf41bb8ec0cf6b82687076931a2e5552c5). Entry point `bin/pigment64`. Runs natively on macOS arm64.

## Examples

```bash
local-tools/pigment64/bin/pigment64 to-bin -f rgba16 -o out.bin in.png
```

## Test result

Pass, 2026-10-16. `test.sh` generates a 4x2 RGBA PNG, checks that RGBA16 output bytes are exactly `f80107c1003ffffef80107c1003ffffe`, converts back to PNG and to bin again (round trip must be identical), and checks that I8 output is 8 bytes.

## Related

Related texture and asset tools may be listed under [/tools/](/tools/) in this bundle.

[^repo]: pigment64 README

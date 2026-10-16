---
type: Platform Support
title: Host environment (macOS app, compiler container)
description: decomp.wtf is a macOS app. The native Mac app runs the Python backend on the Mac, and only the Linux-only period compilers run in a small Docker container.
tags: [macos, docker, linuxbox, mac-app, updates]
generated:
  by: claude-code/claude-opus-5-5
  at: '2026-10-16T11:51:12Z'
sources:
  - id: macreadme
    resource: macos/README.md
    title: Mac app developer guide
  - id: backend
    resource: macos/DecompWTF/Backend.swift
    title: How the Mac app finds and starts decomp.wtf
  - id: updater
    resource: macos/DecompWTF/Updater.swift
    title: Mac app updates from GitHub Releases
  - id: release
    resource: macos/release.sh
    title: Building and publishing a Mac release
  - id: linuxbox
    resource: src/decompnet/linuxbox.py
    title: macOS compiler container
  - id: dockerfile
    resource: src/decompnet/linuxbox.Dockerfile
    title: Compiler container image
  - id: toolchains
    resource: src/decompnet/toolchains.py
    title: via_linux
  - id: readme
    resource: README.md
    title: Install and Linux-only compilers sections
---

# Host environment (macOS app, compiler container)

decomp.wtf runs on macOS 14 or newer. Linux and Windows builds, their installers, WSL and the whole-app Docker image were removed[^readme].

## The Mac app (`macos/`)

The Mac app is a native Swift/SwiftUI app that shows decomp.wtf in its own window. Its sidebar and project overview are native screens; the other pages are decomp.wtf's own web pages shown in the window[^macreadme]. It starts decomp.wtf with `--no-browser` and stops it on quit, unless decomp.wtf was already running[^backend].

It finds decomp.wtf in this order[^backend]:

1. the folder chosen on its setup screen (remembered)
2. the checkout it was built from (`macos/build/...` inside a checkout), run with `uv run --project <checkout> decomp`
3. an installed `decomp` command on its search path (`~/.local/bin`, Homebrew)

A setup screen appears when uv or decomp.wtf is missing. A bar appears when Docker Desktop is missing or not running[^backend]. Projects live in `~/Decomp`, or `DECOMP_HOME`[^backend].

**Build.** XcodeGen generates the project from `project.yml`, and `xcodebuild` builds it. The app is not sandboxed, is signed ad hoc, and isn't notarized. A downloaded copy therefore needs **Open Anyway** in Privacy & Security once[^macreadme].

**Updates.** The app checks GitHub Releases of `Nexhas28/decomp.wtf` on launch (at most every 6 hours) and on request. It looks for a published release tagged `mac-v<version>` with a `decomp.wtf-mac-<version>.zip` asset, then swaps itself in place[^updater]. `macos/release.sh <version> [--publish]` builds that zip and publishes the release with `gh`[^release]. `DECOMP_MAC_UPDATE_FEED` points the check at another feed[^updater]. This updates the app only. The Python backend is the checkout it runs, updated with `git pull`[^readme].

## The compiler container (`linuxbox.py`)

The backend runs natively on the Mac. Toolchains with a macOS build run natively, and so do the Metrowerks compilers (Windows programs) through the macOS build of `wibo`. On Apple Silicon the Intel programs need Rosetta 2[^readme].

Linux-only compilers run in one long-lived container, `decomp-wtf-compiler`[^linuxbox]:

- KMC GCC (N64)
- the PlayStation GCCs
- EE-GCC (PS2)
- agbcc (GBA)

`toolchains.via_linux` routes a toolchain there when it has a Linux build and no build for the Mac[^toolchains].

- **Image.** Bare Debian (`linux/amd64`) with the 32-bit C library and nothing of decomp.wtf, built from `linuxbox.Dockerfile` the first time a Linux-only compiler is installed[^dockerfile].
- **Mounts.** The toolchain folder is mounted read-only and the compile folders read-write, both at the same absolute paths as on the Mac[^linuxbox].
- **Limits.** No network, limited memory and processes[^linuxbox].
- **Needs Docker Desktop running.** Without it, installing or compiling with a Linux-only compiler says what to start[^linuxbox].

`DECOMP_LINUXBOX` points tests at a stand-in for `docker`[^linuxbox].

## Hosting a room

`decomp host ... --service` runs decomp.wtf in the background as a launchd agent on the Mac, so a team room can be hosted from an always-on Mac[^readme]. See [collaboration rooms](/project/collaboration-rooms.md).

[^macreadme]: macos/README.md
[^backend]: macos/DecompWTF/Backend.swift
[^updater]: macos/DecompWTF/Updater.swift
[^release]: macos/release.sh
[^linuxbox]: src/decompnet/linuxbox.py
[^dockerfile]: src/decompnet/linuxbox.Dockerfile
[^toolchains]: src/decompnet/toolchains.py
[^readme]: README.md

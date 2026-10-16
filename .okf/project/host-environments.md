---
type: Platform Support
title: Host environments (Linux, Windows/WSL, macOS, Docker)
description: How decomp.wtf runs on Linux, Windows through its own WSL 2 distribution, macOS with a compiler-only Docker container, and fully in Docker.
tags: [windows, wsl, macos, docker, linux, hostenv]
generated:
  by: claude-code/claude-opus-5-5
  at: '2026-10-16T11:51:12Z'
sources:
  - id: hostenv
    resource: src/decompnet/hostenv.py
    title: Runtime detection
  - id: wsl
    resource: src/decompnet/wsl.py
    title: WSL 2 compiler lane
  - id: linuxbox
    resource: src/decompnet/linuxbox.py
    title: macOS compiler container
  - id: dockerfile
    resource: src/decompnet/linuxbox.Dockerfile
    title: Compiler container image
  - id: limits
    resource: src/decompnet/limits.py
    title: Memory caps
  - id: toolchains
    resource: src/decompnet/toolchains.py
    title: via_linux
  - id: readme
    resource: README.md
    title: Install, Windows, macOS, Docker sections
---

# Host environments (Linux, Windows/WSL, macOS, Docker)

## Runtime detection (`hostenv.py`)

decomp.wtf is "a Linux program everywhere"[^hostenv]. `hostenv.runtime()` returns one of:

- `native`: Linux itself
- `wsl`: its own WSL 2 distribution on Windows
- `docker`: a container

Detection order[^hostenv]:

1. `DECOMP_RUNTIME`
2. `/etc/decomp-wtf-runtime`, written by `site/public/linux-base.sh`
3. `WSL_DISTRO_NAME` or WSLInterop
4. `/.dockerenv`

Only a few things cross to the user's computer: opening the browser or a folder, and file paths the user types[^hostenv].

## Windows: WSL 2

The installer (`install.ps1`) enables WSL 2 and builds a `decomp-wtf` distribution from Debian's WSL image. Projects are at `\\wsl.localhost\decomp-wtf\home\decomp\Decomp`[^readme].

Separately, `wsl.py` runs the Linux-only period compilers when `sys.platform == "win32"` (or when `DECOMP_WSL` names a stand-in). It runs them inside WSL 2, reading the toolchain folder through `/mnt/<drive>`[^wsl]:

- KMC GCC
- the PlayStation GCCs
- EE-GCC
- agbcc

WSL 1 can't run them, because they are 32-bit. Commands are sent as a small shell script in the compile folder[^wsl]. A registry part for `wsl-x86_64` (for example the Debian `libc6-i386` runtime) replaces the Linux part there[^toolchains].

## macOS (experimental): compiler container

The app runs natively (`uv run decomp`). Linux-only compilers run in one long-lived container, `decomp-wtf-compiler`[^linuxbox][^readme]:

- **Image.** Bare Debian with 32-bit libc, built from `linuxbox.Dockerfile`[^dockerfile].
- **Mounts.** The toolchain folder is mounted read-only and the compile folders read-write, both at the same absolute paths[^linuxbox].
- **Limits.** No network and limited resources[^linuxbox].

`DECOMP_LINUXBOX` points tests at a stand-in for `docker`[^linuxbox]. Metrowerks compilers run natively through the macOS build of `wibo`. The one-line installer doesn't support macOS[^readme].

## The whole app in Docker

`docker compose up -d` builds `site/public/Dockerfile`. The app then listens on `127.0.0.1:6464`, with state in the `decomp-wtf-home` volume at `/home/decomp`[^readme]. Inside Docker the app binds 0.0.0.0 and, when `DECOMP_PORT` is set, keeps that exact port[^readme]. `toolchains.via_linux` decides per toolchain whether it goes through WSL or the container[^toolchains].

## Resource caps

On Linux, `limits.py` runs heavy commands in their own systemd scope, with a memory cap and no swap. Only that command is killed if it runs away[^limits]. Compiles also go through `prlimit`. See [matching and scoring](/project/matching-and-scoring.md).

[^hostenv]: src/decompnet/hostenv.py
[^wsl]: src/decompnet/wsl.py
[^linuxbox]: src/decompnet/linuxbox.py
[^dockerfile]: src/decompnet/linuxbox.Dockerfile
[^limits]: src/decompnet/limits.py
[^toolchains]: src/decompnet/toolchains.py
[^readme]: README.md

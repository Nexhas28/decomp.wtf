# Workspace-local Java runtime for decomp.wtf real analysis lanes

Provisioned 2026-10-16 (decomp.wtf Workstream C) — everything under this directory; nothing touched
outside the workspace.

## Artifact (full JDK — required by the REA Ghidra bridge, which rejects runtime-only JREs)

- **Source URL (Adoptium API):** https://api.adoptium.net/v3/binary/latest/21/ga/mac/aarch64/jdk/hotspot/normal/eclipse
- **Resolved GitHub release:** https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.12.1%2B1/OpenJDK21U-jdk_aarch64_mac_hotspot_21.0.12.1_1.tar.gz
- **Vendor / release:** Eclipse Temurin, JDK 21.0.12.1+1 (macOS aarch64, hotspot, normal)
- **Archive name:** `OpenJDK21U-jdk_aarch64_mac_hotspot_21.0.12.1_1.tar.gz`
- **SHA-256:** `3623232f33a9c3baadf304480b2535f9a3cba8a58d42ecbb438ba267315d9998`
  (cross-checked against the official Adoptium assets API response for image_type=jdk, os=mac,
  architecture=aarch64, vendor=eclipse — the API's `binary.package.checksum` matches exactly)
- **Extracted to:** `jdk-21.0.12.1+1/`
- **java binary:** `jdk-21.0.12.1+1/Contents/Home/bin/java` (full JDK: javac/jar/javap included)

Note: a JRE-only Temurin 21 archive (sha256 `dec50fc6f9fcd4fe3ae8cabf5a5fa68f6afc48841f7698e468e9aa5d54beed84`,
verified against the same API for image_type=jre) was provisioned first; it satisfied Ghidra's own
launcher but REA's Ghidra doctor rejected it ("runtime only; accepted a 64-bit full JDK 21 or newer" —
see REAL-LANE-RESULTS.md), so it was replaced by this full JDK.

```
$ ./jdk-21.0.12.1+1/Contents/Home/bin/java -version
openjdk version "21.0.12.1" 2026-08-18 LTS
OpenJDK Runtime Environment Temurin-21.0.12.1+1 (build 21.0.12.1+1-LTS)
```

## Discovery order (see src/decompnet/forks/ghidra.py `java_home()`)

1. `DECOMP_JAVA_HOME` environment variable (a JAVA_HOME-style root containing `bin/java`,
   or the JDK's `Contents/Home` directly),
2. this workspace directory: `~/Decomp/java/*/Contents/Home/bin/java`,
3. `PATH` (`shutil.which("java")`).

The same discovery is exported for the REA adapter (`decompnet.analysis.rea`): the REA sidecar's
Ghidra bridge receives `JAVA_HOME` from this discovery and `GHIDRA_INSTALL_DIR` from the pinned Ghidra
root (see REAL-LANE-RESULTS.md).

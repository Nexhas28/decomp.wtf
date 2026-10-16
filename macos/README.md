# decomp.wtf for Mac

The developer guide for the Mac app: a native macOS app (Swift, SwiftUI) that runs decomp.wtf in its own window: no
terminal, no browser tab. For what decomp.wtf does and how to use it, see the [main README](../README.md).

It starts decomp.wtf in the background, shows the app in the window and stops it again when you quit. The sidebar
(your projects with how much of each is matched, and the other screens) and a project's overview (matched %, functions
matched, the autopilot with its Run/Stop button, recent activity) are native Mac screens; the rest are decomp.wtf's own
pages, shown beside the native sidebar. Both read the same running decomp.wtf, so the numbers always agree. Plus:

- **File menu:** New Project (⌘N), Open Game… (⌘O, with the Mac's file picker; the game is read where it is, so big
  discs work), Open the Demo Game, Continue a Public Decomp, Join a Team.
- **decomp.wtf menu:** Agents & Models, Compilers & Tools, Settings (⌘,), Check for Updates…, Show Projects Folder
  in Finder, Show Log, Restart decomp.wtf.
- **View menu:** Back (⌘[), Forward (⌘]), Reload Page (⌘R).
- A setup screen when something is missing (uv, or where decomp.wtf is), and a bar when Docker Desktop isn't
  installed or running (the Linux-only compilers, KMC GCC, EE-GCC, the PlayStation GCCs and agbcc, run in Docker on
  a Mac; the rest run natively).
- Links to the web open in your browser; downloads go to Downloads.

## Requirements

- macOS 14 or newer (Rosetta 2 on Apple Silicon, for the compilers that are Intel programs).
- To build: Xcode and [XcodeGen](https://github.com/yonaskolb/XcodeGen) (`brew install xcodegen`).
- To run: [uv](https://docs.astral.sh/uv/) and a checkout of this repository. [Docker
  Desktop](https://docs.docker.com/desktop/setup/install/mac-install/), running, for the Linux-only compilers.
- To publish a release: the [GitHub CLI](https://cli.github.com/) (`gh`), logged in.

## Build

```
cd macos
xcodegen generate
xcodebuild -project DecompWTF.xcodeproj -scheme DecompWTF -configuration Release \
  -derivedDataPath build/DerivedData SYMROOT="$PWD/build" build
open build/Release/decomp.wtf.app
```

`project.yml` is the source of the Xcode project; edit it, not the `.xcodeproj`. The app isn't sandboxed (it starts
uv, Python and Docker and reads `~/Decomp`) and is signed ad hoc, for running on your own Mac. It isn't notarized, so
a copy downloaded from GitHub is blocked the first time it opens: **System Settings ▸ Privacy & Security ▸ Open
Anyway** lets it run (once).

## Where it finds decomp.wtf

1. The folder you chose on its setup screen (remembered).
2. The checkout it was built from, when the app is inside one (`macos/build/...`): it runs `uv run decomp` there.
3. An installed `decomp` command (`~/.local/bin`, Homebrew).

Projects live where `decomp` keeps them: `~/Decomp`, or `DECOMP_HOME`. If decomp.wtf is already running, the app
opens that one and leaves it running when you quit.

## Updates

The app updates itself from [GitHub Releases](https://github.com/Nexhas28/decomp.wtf/releases): on launch (at most
every 6 hours) and from **decomp.wtf ▸ Check for Updates…** it looks for the newest published release tagged
`mac-v<version>` with a `decomp.wtf-mac-<version>.zip` attached. When that's newer than the app, it asks; on yes it
downloads it, checks it's this app at that version, quits (stopping decomp.wtf), and a small helper swaps the app
and opens it again. If the swap fails the old app is put back. Drafts and pre-releases are ignored, and so are
releases without the Mac zip. `DECOMP_MAC_UPDATE_FEED` points the check at another releases list (for testing).

This updates the app only. The decomp.wtf it runs is the checkout you chose: `git pull` there updates it.

## Making a release

```
cd macos
./release.sh 0.11.0                                  # build and zip only: build/release/decomp.wtf-mac-0.11.0.zip
NOTES="What's new" ./release.sh 0.11.0 --publish     # ...and publish it with your gh login
```

`--publish` creates the release `mac-v<version>` on `Nexhas28/decomp.wtf` (`REPO` overrides it) with the zip
attached, which is what the updater looks for. Commit and push first: it refuses when `macos/` has changes that
aren't committed or the commit isn't on GitHub yet.

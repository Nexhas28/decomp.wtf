# decomp.wtf for Mac

A native macOS app (Swift, SwiftUI) that runs decomp.wtf in its own window: no terminal, no browser tab.

It starts decomp.wtf in the background, shows the app in the window and stops it again when you quit. The screens
inside are decomp.wtf's own, so everything the app can do works here, plus:

- **File menu:** New Project (⌘N), Open Game… (⌘O, with the Mac's file picker; the game is read where it is, so big
  discs work), Open the Demo Game, Continue a Public Decomp, Join a Team.
- **decomp.wtf menu:** Agents & Models, Compilers & Tools, Settings (⌘,), Show Projects Folder in Finder, Show Log,
  Restart decomp.wtf.
- **View menu:** Back (⌘[), Forward (⌘]), Reload Page (⌘R).
- A setup screen when something is missing (uv, or where decomp.wtf is), and a bar when Docker Desktop isn't
  installed or running (most compilers run in Docker on a Mac).
- Links to the web open in your browser; downloads go to Downloads.

## Updates

The app updates itself from [GitHub Releases](https://github.com/Nexhas28/decomp.wtf/releases): on launch (at most
every 6 hours) and from **decomp.wtf ▸ Check for Updates…** it looks for the newest published release tagged
`mac-v<version>` with a `decomp.wtf-mac-<version>.zip` attached. When that's newer than the app, it asks; on yes it
downloads it, checks it's this app at that version, quits (stopping decomp.wtf), and a small helper swaps the app
and opens it again. If the swap fails the old app is put back. Drafts and pre-releases are ignored, and so are
releases without the Mac zip (decomp.wtf's own updater, for the Python side, doesn't use GitHub Releases).
`DECOMP_MAC_UPDATE_FEED` points the check at another releases list (for testing).

## Making a release

```
cd macos
./release.sh 0.11.0                                  # build and zip only: build/release/decomp.wtf-mac-0.11.0.zip
NOTES="What's new" ./release.sh 0.11.0 --publish     # ...and publish it with your gh login
```

## Where it finds decomp.wtf

1. The folder you chose on its setup screen (remembered).
2. The checkout it was built from, when the app is inside one (`macos/build/...`): it runs `uv run decomp` there.
3. An installed `decomp` command (`~/.local/bin`, Homebrew).

Projects live where `decomp` keeps them: `~/Decomp`, or `DECOMP_HOME`. If decomp.wtf is already running, the app
opens that one and leaves it running when you quit.

## Build

Needs Xcode and [XcodeGen](https://github.com/yonaskolb/XcodeGen) (`brew install xcodegen`):

```
cd macos
xcodegen generate
xcodebuild -project DecompWTF.xcodeproj -scheme DecompWTF -configuration Release \
  -derivedDataPath build/DerivedData SYMROOT="$PWD/build" build
open build/Release/decomp.wtf.app
```

`project.yml` is the source of the Xcode project; edit it, not the `.xcodeproj`. The app isn't sandboxed (it starts
uv, Python and Docker and reads `~/Decomp`) and is signed ad hoc, for running on your own Mac.

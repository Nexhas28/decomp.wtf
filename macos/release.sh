#!/bin/bash
# Builds decomp.wtf for Mac at a version and zips it for a GitHub release; with --publish it also creates the
# release (tag mac-v<version>, asset decomp.wtf-mac-<version>.zip), which is what the app's updater looks for.
#
#   ./release.sh 0.11.0                 build and zip only (build/release/)
#   ./release.sh 0.11.0 --publish       ...and publish it on GitHub with your gh login
#
# Release notes for --publish come from NOTES (the updater shows them), e.g. NOTES="Faster startup" ./release.sh ...
set -euo pipefail

VERSION="${1:?usage: release.sh <version> [--publish]}"
REPO="${REPO:-Nexhas28/decomp.wtf}"
cd "$(dirname "$0")"

if [[ ! "$VERSION" =~ ^[0-9]+(\.[0-9]+){1,2}$ ]]; then
  echo "the version should look like 0.11.0" >&2
  exit 1
fi
if [ -z "${DEVELOPER_DIR:-}" ] && [ -d /Applications/Xcode.app ]; then
  export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer  # works even when xcode-select points elsewhere
fi

xcodegen generate --quiet
rm -rf build/Release build/release
xcodebuild -project DecompWTF.xcodeproj -scheme DecompWTF -configuration Release \
  -derivedDataPath build/DerivedData SYMROOT="$PWD/build" \
  MARKETING_VERSION="$VERSION" CURRENT_PROJECT_VERSION="$VERSION" build -quiet

APP=build/Release/decomp.wtf.app
BUILT=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$APP/Contents/Info.plist")
[ "$BUILT" = "$VERSION" ] || { echo "built version $BUILT, expected $VERSION" >&2; exit 1; }

mkdir -p build/release
ZIP="build/release/decomp.wtf-mac-$VERSION.zip"
ditto -c -k --keepParent "$APP" "$ZIP"
echo "built $ZIP"

if [ "${2:-}" = "--publish" ]; then
  # The release's tag has to point at the code the app was built from, on GitHub.
  if [ -n "$(git status --porcelain -- .)" ]; then
    echo "macos/ has changes that aren't committed: commit them first, then publish" >&2
    exit 1
  fi
  git fetch --quiet origin
  if [ -z "$(git branch -r --contains HEAD)" ]; then
    echo "this commit isn't on GitHub yet: push it first, then publish" >&2
    exit 1
  fi
  gh release create "mac-v$VERSION" "$ZIP" --repo "$REPO" --target "$(git rev-parse HEAD)" \
    --title "decomp.wtf for Mac $VERSION" \
    --notes "${NOTES:-decomp.wtf for Mac $VERSION. Download the zip, unzip it and move decomp.wtf.app to Applications; after that it updates itself.}"
  echo "published mac-v$VERSION on $REPO"
fi

#!/bin/zsh
# Builds a self-contained "Juniper.app" (its own Python, packages, models and helpers) and a
# DMG to send to people. Each person sets up their own Claude Code login and ElevenLabs key in the
# first-run setup.
#   packaging/package.sh            → packaging/dist/Juniper.dmg
#
# With a "Developer ID Application" certificate in the Keychain the app is signed with it and
# notarized by Apple (credentials stored once with `xcrun notarytool store-credentials juniper`; NOTARY_PROFILE names another),
# so it opens like any download. Without one it's signed ad hoc, and the first open needs
# System Settings › Privacy & Security › Open Anyway. REQUIRE_NOTARIZED=1 turns that into an error.
set -euo pipefail
setopt null_glob          # an rm pattern that matches nothing is fine
ROOT="${0:A:h:h}"
PK="$ROOT/packaging"
BUILD="$PK/build"
DIST="$PK/dist"
CACHE="$PK/cache"
VERSION="${VERSION:-1.0.0}"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.11.18%2B20261026-aarch64-apple-darwin-install_only_stripped.tar.gz"
APP="$BUILD/Juniper.app"
RES="$APP/Contents/Resources"

echo "› clean"
# Every removal below names a path that must be set (${VAR:?} stops the build if it isn't).
rm -rf "${BUILD:?}" && mkdir -p "$BUILD" "$DIST" "$CACHE" "$APP/Contents/MacOS" "$RES/app"

echo "› Python"
[ -f "$CACHE/python.tar.gz" ] || curl -fsSL -o "$CACHE/python.tar.gz" "$PY_URL"
tar -xzf "$CACHE/python.tar.gz" -C "$RES"           # → $RES/python
PY="$RES/python/bin/python3"

echo "› packages"
"$PY" -m pip install --quiet --disable-pip-version-check --no-cache-dir -r "$ROOT/requirements.txt"

echo "› the app"
cp "$ROOT"/{boot.py,juniper.py,ui.py,onboarding.py,connections.py,agents.py,updater.py,ed25519.py,config.template.toml} "$RES/app/"
echo "$VERSION" > "$RES/app/version.txt"        # the code's version (updates bring their own)
mkdir -p "$RES/app/onboarding" "$RES/app/bin" "$RES/app/models/speaker"
cp "$ROOT"/onboarding/{index.src.html,logo.svg.txt} "$RES/app/onboarding/"
[ -x "$ROOT/bin/apple-stt" ] || swiftc -O -parse-as-library "$ROOT/stt/transcribe.swift" -o "$ROOT/bin/apple-stt"
cp "$ROOT"/bin/{apple-stt,approve_mcp.py,capture-hook} "$RES/app/bin/"
[ -f "$ROOT/models/speaker/wespeaker_en_voxceleb_resnet34_LM.onnx" ] || "$ROOT/scripts/fetch-models.sh"
KWS=sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20
mkdir -p "$RES/app/models/$KWS"
for f in tokens.txt en.phone encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx decoder-epoch-13-avg-2-chunk-16-left-64.onnx joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx; do
  cp "$ROOT/models/$KWS/$f" "$RES/app/models/$KWS/"
done
cp "$ROOT/models/speaker/wespeaker_en_voxceleb_resnet34_LM.onnx" "$RES/app/models/speaker/"

echo "› slim down"
SITE=$("$PY" -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
LIB="$RES/python/lib/python3.11"
rm -rf "${LIB:?}"/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3} "${RES:?}/python/lib"/{tcl*,tk*,itcl*,libtcl*,libtk*,thread*} "${RES:?}/python/share"
rm -rf "${SITE:?}"/{pip,pip-*,setuptools,setuptools-*,_distutils_hack,distutils-precedence.pth,PyObjCTest} "${SITE:?}"/*/tests "${SITE:?}"/*/test
find "${RES:?}" -name "*.dSYM" -type d -prune -exec rm -rf {} +    # debug symbols
find "${RES:?}" -name "__pycache__" -type d -prune -exec rm -rf {} +
# Precompiled (the bundle is read-only), recording install paths rather than this build folder's.
PYTHONDONTWRITEBYTECODE=1 "$PY" -m compileall -q -f -j 0 -d "/Applications/Juniper.app/Contents/Resources/python/lib/python3.11" "$LIB" >/dev/null 2>&1 || true
PYTHONDONTWRITEBYTECODE=1 "$PY" -m compileall -q -f -j 0 -d "/Applications/Juniper.app/Contents/Resources/app" "$RES/app" >/dev/null 2>&1 || true

echo "› icon"
"$ROOT/.venv/bin/python" "$PK/make_icon.py" "$BUILD/icon" >/dev/null
cp "$BUILD/icon/AppIcon.icns" "$RES/AppIcon.icns"

echo "› Info.plist + launcher"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Juniper</string>
  <key>CFBundleDisplayName</key><string>Juniper</string>
  <key>CFBundleIdentifier</key><string>io.github.michaelbroadbrand.juniper</string>
  <key>CFBundleExecutable</key><string>Juniper</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSArchitecturePriority</key><array><string>arm64</string></array>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Juniper listens for "hey Juniper" and records your requests. Audio stays on this Mac; only the words of your request go to Claude.</string>
  <key>NSSpeechRecognitionUsageDescription</key><string>Juniper turns what you say into text on this Mac.</string>
  <key>NSAppleEventsUsageDescription</key><string>Juniper opens Terminal to sign you in to Claude Code, and can add itself to your login items.</string>
</dict></plist>
PLIST
# A native launcher that runs Python inside the app's own process (see launcher.c), so the app
# can come to the front and macOS attributes the microphone to Juniper.
clang -O2 -mmacosx-version-min=14.0 -arch arm64 -o "$APP/Contents/MacOS/Juniper" "$PK/launcher.c"

SIGN_ID="${SIGN_ID:-$(security find-identity -v -p codesigning | awk -F'"' '/Developer ID Application/ {print $2; exit}')}"
NOTARY_PROFILE="${NOTARY_PROFILE:-juniper}"
NOTARIZE=""
if [ -n "$SIGN_ID" ]; then
  echo "› sign ($SIGN_ID)"
  SIGN=(--force --timestamp --options runtime -s "$SIGN_ID")
  if xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" >/dev/null 2>&1; then
    NOTARIZE=1
  else
    echo "  no notarization credentials (xcrun notarytool store-credentials $NOTARY_PROFILE)"
  fi
else
  echo "› sign (ad hoc: no Developer ID Application certificate in the Keychain)"
  SIGN=(--force --options runtime -s -)
fi
if [ -n "${REQUIRE_NOTARIZED:-}" ] && [ -z "$NOTARIZE" ]; then
  echo "✗ this build can't be notarized (see above), so it would need Open Anyway on other Macs"
  exit 1
fi
# Inside out: every library and helper, then the app itself with its entitlements. (The packages
# arrive with placeholder signatures; "replacing existing signature" for each is expected.)
quiet() { "$@" 2> >(grep -v "replacing existing signature" >&2); }
find "$RES" -type f \( -name "*.so" -o -name "*.dylib" \) -print0 | quiet xargs -0 codesign "${SIGN[@]}"
quiet codesign "${SIGN[@]}" "$RES/python/bin/python3.11" "$RES/app/bin/apple-stt"
quiet codesign "${SIGN[@]}" --entitlements "$PK/entitlements.plist" "$APP"
codesign --verify --deep --strict "$APP"
# Apple refuses the whole app over one unsigned binary, so check every Mach-O file here first.
find "$APP" -type f -print0 | xargs -0 file -h | awk -F': ' '/Mach-O/ {sub(/ \(for architecture.*/, "", $1); print $1}' |
  sort -u | while IFS= read -r f; do
    codesign --verify --strict "$f" 2>/dev/null || { echo "✗ not signed: $f"; exit 1; }
  done

# Sends a zip or DMG to Apple and waits for the verdict (usually a few minutes).
notarize() {
  echo "  notarizing ${1:t} …"
  local out id
  out=$(xcrun notarytool submit "$1" --keychain-profile "$NOTARY_PROFILE" --wait 2>&1) || true
  id=$(print -r -- "$out" | awk '/^  id:/ {print $2; exit}')
  if ! print -r -- "$out" | grep -q "status: Accepted"; then
    print -r -- "$out"
    [ -n "$id" ] && xcrun notarytool log "$id" --keychain-profile "$NOTARY_PROFILE"
    echo "✗ Apple didn't accept ${1:t}"
    exit 1
  fi
}
if [ -n "$NOTARIZE" ]; then
  echo "› notarize the app"
  ditto -c -k --keepParent "$APP" "$BUILD/app.zip"
  notarize "$BUILD/app.zip"
  xcrun stapler staple -q "$APP"      # the approval travels with the app, so it opens offline too
  rm -f "$BUILD/app.zip"
fi

echo "› DMG"
"$ROOT/.venv/bin/python" "$PK/make_dmg_art.py" "$BUILD/dmg-background.png" ${NOTARIZE:+notarized}
STAGE="$BUILD/dmg"
mkdir -p "$STAGE/.background"
ditto "$APP" "$STAGE/Juniper.app"
ln -s /Applications "$STAGE/Applications"
cp "$BUILD/dmg-background.png" "$STAGE/.background/background.png"
RW="$BUILD/rw.dmg"; OUT="$DIST/Juniper.dmg"
rm -f "$RW" "$OUT"
hdiutil create -quiet -srcfolder "$STAGE" -volname "Juniper" -fs HFS+ -format UDRW -size 900m "$RW"
DEV=$(hdiutil attach -readwrite -noverify -noautoopen "$RW" | awk '/Apple_HFS/ {print $1}')
sleep 1
osascript <<'OSA' || echo "  (couldn't style the DMG window; it still works)"
tell application "Finder"
  tell disk "Juniper"
    open
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set the bounds of container window to {200, 120, 860, 540}
    set opts to the icon view options of container window
    set arrangement of opts to not arranged
    set icon size of opts to 112
    set text size of opts to 13
    set background picture of opts to file ".background:background.png"
    set position of item "Juniper.app" of container window to {170, 190}
    set position of item "Applications" of container window to {490, 190}
    -- out of sight for anyone whose Finder shows hidden files
    repeat with nm in {".background", ".fseventsd", ".Trashes"}
      try
        set position of item nm of container window to {900, 900}
      end try
    end repeat
    update without registering applications
    delay 1
    close
  end tell
end tell
OSA
sync
hdiutil detach -quiet "$DEV"
hdiutil convert -quiet "$RW" -format UDZO -imagekey zlib-level=9 -o "$OUT"
rm -f "$RW"
if [ -n "$NOTARIZE" ]; then
  echo "› notarize the DMG"
  codesign --timestamp -s "$SIGN_ID" "$OUT"
  notarize "$OUT"
  xcrun stapler staple -q "$OUT"
  spctl -a -t exec "$APP" && spctl -a -t open --context context:primary-signature "$OUT" && echo "  Gatekeeper accepts both"
fi
du -sh "$APP" "$OUT"
echo "✓ $OUT"

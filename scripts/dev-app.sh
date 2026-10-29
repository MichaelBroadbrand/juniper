#!/bin/sh
# Builds "Juniper Dev.app" in the repo: runs your working copy in the background (menu bar only,
# no Terminal window) with its own microphone permission. For the real, self-contained app, see
# packaging/package.sh.
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP="$DIR/Juniper Dev.app"
LOGS="$HOME/Library/Application Support/Juniper"
mkdir -p "$DIR/bin" "$LOGS"
swiftc -O -parse-as-library "$DIR/stt/transcribe.swift" -o "$DIR/bin/apple-stt"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Juniper Dev</string>
  <key>CFBundleIdentifier</key><string>local.juniper.dev</string>
  <key>CFBundleExecutable</key><string>Juniper</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Juniper listens for "hey Juniper" and records your requests. Audio stays on this Mac.</string>
</dict></plist>
PLIST
# The launcher stays running as the parent (no exec), so macOS treats the app, not Python,
# as the one using the microphone. A login shell gives Claude Code your normal PATH.
cat > "$APP/Contents/MacOS/Juniper" <<LAUNCH
#!/bin/zsh -l
export PATH="\$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:\$PATH"
"$DIR/.venv/bin/python" "$DIR/juniper.py" >/dev/null 2>>"$LOGS/juniper.err"
LAUNCH
chmod +x "$APP/Contents/MacOS/Juniper"
codesign --force --deep -s - "$APP" 2>/dev/null || true
echo "Built $APP"

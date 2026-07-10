#!/bin/bash
# Build a double-clickable GDriveFiltering.app that launches the local dashboard.
# Usage:  ./desktop/build_mac_app.sh [output_dir]   (default: ~/Desktop)
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
DEST="${1:-$HOME/Desktop}"
APP="$DEST/GDriveFiltering.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>GDriveFiltering</string>
  <key>CFBundleDisplayName</key><string>GDriveFiltering</string>
  <key>CFBundleIdentifier</key><string>com.soclosesociety.gdrivefiltering</string>
  <key>CFBundleVersion</key><string>1.0.0</string>
  <key>CFBundleShortVersionString</key><string>1.0.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>GDriveFiltering</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>LSMinimumSystemVersion</key><string>10.13</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/GDriveFiltering" <<LAUNCH
#!/bin/bash
PROJECT="$PROJECT"
LOG="\$HOME/Library/Logs/GDriveFiltering.log"
mkdir -p "\$(dirname "\$LOG")"
cd "\$PROJECT" || { /usr/bin/osascript -e 'display alert "GDriveFiltering" message "Project folder not found."'; exit 1; }
if [ ! -x .venv/bin/python ]; then
  /usr/bin/python3 -m venv .venv >>"\$LOG" 2>&1
  .venv/bin/pip install -q -r requirements.txt >>"\$LOG" 2>&1
fi
# Native-window dependency (falls back to the browser dashboard if it fails).
.venv/bin/python -c "import webview" 2>/dev/null || .venv/bin/pip install -q -r requirements-desktop.txt >>"\$LOG" 2>&1
exec .venv/bin/python -m gdrivefilter app --port 8787 >>"\$LOG" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/GDriveFiltering"

# Icon: render a multi-resolution .icns via iconutil (needs python3 + iconutil).
if command -v iconutil >/dev/null 2>&1; then
  ICONSET="$(mktemp -d)/GDF.iconset"; mkdir -p "$ICONSET"
  for sz in 16 32 64 128 256 512; do
    python3 "$HERE/icon.py" "$ICONSET/icon_${sz}x${sz}.png" "$sz" >/dev/null 2>&1 || true
    python3 "$HERE/icon.py" "$ICONSET/icon_${sz}x${sz}@2x.png" "$((sz*2))" >/dev/null 2>&1 || true
  done
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/icon.icns" >/dev/null 2>&1 || true
  rm -rf "$(dirname "$ICONSET")"
fi
touch "$APP"
echo "Built: $APP"

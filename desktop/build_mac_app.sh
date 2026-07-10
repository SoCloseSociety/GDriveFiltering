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
  <key>LSUIElement</key><true/>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/GDriveFiltering" <<LAUNCH
#!/bin/bash
PROJECT="$PROJECT"
URL="http://127.0.0.1:8787/"
LOG="\$HOME/Library/Logs/GDriveFiltering.log"
mkdir -p "\$(dirname "\$LOG")"
if /usr/bin/curl -s -o /dev/null --max-time 1 "\$URL"; then /usr/bin/open "\$URL"; exit 0; fi
cd "\$PROJECT" || { /usr/bin/osascript -e 'display alert "GDriveFiltering" message "Project folder not found."'; exit 1; }
if [ ! -x .venv/bin/python ]; then
  /usr/bin/python3 -m venv .venv >>"\$LOG" 2>&1
  .venv/bin/pip install -q -r requirements.txt >>"\$LOG" 2>&1
fi
/usr/bin/nohup .venv/bin/python -m gdrivefilter dashboard --port 8787 --no-open >>"\$LOG" 2>&1 &
for i in \$(seq 1 30); do /usr/bin/curl -s -o /dev/null --max-time 1 "\$URL" && break; sleep 0.5; done
/usr/bin/open "\$URL"
exit 0
LAUNCH
chmod +x "$APP/Contents/MacOS/GDriveFiltering"

# Icon (best-effort: needs python3 + sips)
if command -v sips >/dev/null 2>&1; then
  python3 "$HERE/icon.py" /tmp/gdf_icon.png >/dev/null 2>&1 || true
  [ -f /tmp/gdf_icon.png ] && sips -s format icns /tmp/gdf_icon.png \
    --out "$APP/Contents/Resources/icon.icns" >/dev/null 2>&1 || true
fi
touch "$APP"
echo "Built: $APP"

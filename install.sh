#!/usr/bin/env bash
# 安装：图标 + 登录自启动
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
AUTOSTART_DIR="$HOME/.config/autostart"

chmod +x "$HERE/screencast-tray"

# 1) 托盘图标
mkdir -p "$ICON_DIR"
cp "$HERE/screencast.svg" "$ICON_DIR/screencast-tray.svg"
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -q "$HOME/.local/share/icons/hicolor" || true
fi

# 2) 登录自启动
mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/screencast-tray.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=ScreenCast Tray
GenericName=Screen Casting
Comment=把屏幕/浏览器窗口内容投到 Chromecast 与 DLNA 电视
Exec=$HERE/screencast-tray
Icon=screencast-tray
Terminal=false
Categories=AudioVideo;Network;Utility;
Keywords=cast;chromecast;dlna;mirror;screencast;
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=3
EOF

echo "已安装："
echo "  图标     : $ICON_DIR/screencast-tray.svg"
echo "  自启动   : $AUTOSTART_DIR/screencast-tray.desktop"
echo "  手动启动 : $HERE/screencast-tray"

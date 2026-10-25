"""系统托盘：优先 AyatanaAppIndicator3（StatusNotifier），退化到 Gtk.StatusIcon（XEmbed）。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from .config import APP_NAME, CACHE_DIR

log = logging.getLogger(__name__)

ICON_CANDIDATES = (
    "screencast-tray",
    "video-display-symbolic",
    "video-display",
    "input-display",
    "display",
    "video-x-generic-symbolic",
    "applications-multimedia",
)


def _pick_icon() -> str:
    theme = Gtk.IconTheme.get_default()
    for name in ICON_CANDIDATES:
        try:
            if theme.has_icon(name):
                return name
        except Exception:  # noqa: BLE001
            continue
    return _generate_icon()


def _generate_icon() -> str:
    """主题里没有可用图标时，自己画一个 PNG 兜底。"""
    path = CACHE_DIR / "tray-icon.png"
    if path.exists():
        return str(path)
    try:
        import cairo  # noqa: PLC0415

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        size = 64
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        cr = cairo.Context(surface)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        # 屏幕
        cr.set_source_rgba(0.16, 0.72, 0.95, 1)
        cr.rectangle(8, 14, 48, 32)
        cr.fill()
        # 底座
        cr.set_source_rgba(0.85, 0.89, 0.95, 1)
        cr.rectangle(26, 47, 12, 5)
        cr.fill()
        cr.rectangle(18, 52, 28, 4)
        cr.fill()
        # 播放三角
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.move_to(28, 24)
        cr.line_to(40, 30)
        cr.line_to(28, 36)
        cr.close_path()
        cr.fill()
        surface.write_to_png(str(path))
        return str(path)
    except Exception as exc:  # noqa: BLE001
        log.debug("生成托盘图标失败: %s", exc)
        return "applications-multimedia"


class Tray:
    def __init__(self, build_menu) -> None:
        self.build_menu = build_menu
        self.menu: Gtk.Menu = Gtk.Menu()
        self._indicator = None
        self._status_icon = None
        self._notifier = None

        gi_name = None
        try:
            gi.require_version("AyatanaAppIndicator3", "0.1")
            from gi.repository import AyatanaAppIndicator3 as appindicator  # noqa: PLC0415

            gi_name = "AyatanaAppIndicator3"
        except Exception:  # noqa: BLE001
            try:
                gi.require_version("AppIndicator3", "0.1")
                from gi.repository import AppIndicator3 as appindicator  # noqa: PLC0415

                gi_name = "AppIndicator3"
            except Exception:  # noqa: BLE001
                appindicator = None

        icon = _pick_icon()
        if appindicator is not None:
            self._indicator = appindicator.Indicator.new(
                APP_NAME, icon, appindicator.IndicatorCategory.APPLICATION_STATUS
            )
            self._indicator.set_status(appindicator.IndicatorStatus.ACTIVE)
            self._indicator.set_menu(self.menu)
            self._indicator.set_title("ScreenCast")
            log.info("托盘：使用 %s", gi_name)
        else:
            self._status_icon = Gtk.StatusIcon.new_from_icon_name(icon)
            self._status_icon.set_title("ScreenCast")
            self._status_icon.set_has_tooltip(True)
            self._status_icon.connect("activate", self._on_activate)
            self._status_icon.connect("popup-menu", self._on_popup)
            log.info("托盘：回退到 Gtk.StatusIcon")

        self._init_notifications()
        self.refresh()

    # -- 菜单 -------------------------------------------------------------
    def refresh(self) -> None:
        menu = self.build_menu()
        self.menu = menu
        if self._indicator is not None:
            self._indicator.set_menu(menu)
        menu.show_all()

    def _on_activate(self, _icon) -> None:
        self._popup(0, Gtk.get_current_event_time())

    def _on_popup(self, _icon, button, activate_time) -> None:
        self._popup(button, activate_time)

    def _popup(self, button: int, activate_time: int) -> None:
        menu = self.build_menu()
        self.menu = menu
        menu.show_all()
        menu.popup(
            None,
            None,
            Gtk.StatusIcon.position_menu,
            self._status_icon,
            button or 0,
            activate_time or Gtk.get_current_event_time(),
        )

    def set_title(self, text: str) -> None:
        if self._indicator is not None:
            self._indicator.set_title(text)
        if self._status_icon is not None:
            self._status_icon.set_tooltip_text(text)

    def set_label(self, text: str) -> None:
        if self._indicator is not None:
            try:
                self._indicator.set_label(text or "", "ScreenCast")
            except Exception:  # noqa: BLE001
                pass
        self.set_title(text)

    # -- 通知 -------------------------------------------------------------
    def _init_notifications(self) -> None:
        try:
            gi.require_version("Notify", "0.7")
            from gi.repository import Notify  # noqa: PLC0415

            Notify.init(APP_NAME)
            self._notifier = Notify
        except Exception:  # noqa: BLE001
            self._notifier = None

    def notify(self, title: str, body: str = "", icon: str = "video-display") -> None:
        log.info("通知: %s — %s", title, body)
        if self._notifier is None:
            return
        try:
            n = self._notifier.Notification.new(title, body, icon)
            n.set_timeout(4000)
            n.show()
        except Exception as exc:  # noqa: BLE001
            log.debug("发送通知失败: %s", exc)

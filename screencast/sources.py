"""采集源枚举：浏览器窗口 / 显示器 / 手动框选区域。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Wnck", "3.0")
from gi.repository import Gdk, Gtk, Wnck  # noqa: E402

log = logging.getLogger(__name__)

BROWSER_HINTS = (
    "chrome",
    "chromium",
    "firefox",
    "firefoxdeveloperedition",
    "brave",
    "edge",
    "msedge",
    "opera",
    "vivaldi",
    "epiphany",
    "floorp",
    "librewolf",
    "waterfox",
    "thorium",
    "browser",
    "navigator",
)


@dataclass
class Source:
    kind: str  # window | monitor | region
    label: str
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    xid: int = 0
    browser: bool = False
    _kind_data: dict = field(default_factory=dict, repr=False)

    @property
    def geometry(self):
        return (self.x, self.y, self.width, self.height)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "xid": self.xid,
            "browser": self.browser,
        }

    @staticmethod
    def from_dict(data: dict) -> Optional["Source"]:
        try:
            return Source(
                kind=data["kind"],
                label=data.get("label", ""),
                x=int(data.get("x", 0)),
                y=int(data.get("y", 0)),
                width=int(data.get("width", 0)),
                height=int(data.get("height", 0)),
                xid=int(data.get("xid", 0)),
                browser=bool(data.get("browser", False)),
            )
        except Exception:  # noqa: BLE001
            return None

    def __str__(self) -> str:  # noqa: D105
        return self.label


def _is_browser(*names: str) -> bool:
    """按 class / app 名的「词」判断，避免 terminal 之类被误判成浏览器。"""
    blob = " ".join(n for n in names if n).lower()
    tokens = {t for t in re.split(r"[^a-z0-9]+", blob) if t}
    for token in tokens:
        for hint in BROWSER_HINTS:
            if token == hint or token.startswith(hint):
                return True
    return False


def list_windows(only_browsers: bool = False) -> List[Source]:
    """返回当前打开的普通窗口（浏览器排在前面）。"""
    Wnck.set_client_type(Wnck.ClientType.PAGER)
    screen = Wnck.Screen.get_default()
    screen.force_update()

    out: List[Source] = []
    seen = set()
    for win in screen.get_windows():
        try:
            if win.get_window_type() != Wnck.WindowType.NORMAL:
                continue
            geo = win.get_client_window_geometry()
            x, y, w, h = int(geo[0]), int(geo[1]), int(geo[2]), int(geo[3])
            if w < 32 or h < 32:
                continue
            xid = int(win.get_xid())
            if xid in seen:
                continue
            seen.add(xid)

            title = win.get_name() or ""
            inst = win.get_class_instance_name() or ""
            group = win.get_class_group_name() or ""
            app = win.get_application()
            app_name = app.get_name() if app else (group or inst)

            browser = _is_browser(inst, group, app_name)
            if only_browsers and not browser:
                continue

            label = title.strip() or app_name or f"窗口 {xid}"
            if len(label) > 70:
                label = label[:67] + "..."
            out.append(
                Source(
                    kind="window",
                    label=f"{label}  ({w}x{h})",
                    x=x,
                    y=y,
                    width=w,
                    height=h,
                    xid=xid,
                    browser=browser,
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("跳过窗口: %s", exc)

    out.sort(key=lambda s: (not s.browser, s.label.lower()))
    return out


def list_monitors() -> List[Source]:
    """返回每个显示器的物理像素区域。"""
    display = Gdk.Display.get_default()
    out: List[Source] = []
    if display is None:
        return out
    for i in range(display.get_n_monitors()):
        mon = display.get_monitor(i)
        geo = mon.get_geometry()
        scale = mon.get_scale_factor() or 1
        model = mon.get_model() or f"显示器 {i + 1}"
        out.append(
            Source(
                kind="monitor",
                label=f"{model}  ({geo.width * scale}x{geo.height * scale})",
                x=geo.x * scale,
                y=geo.y * scale,
                width=geo.width * scale,
                height=geo.height * scale,
            )
        )
    return out


class RegionSelector:
    """全屏半透明覆盖层，拖拽框选一块屏幕区域。"""

    def __init__(self, on_done: Callable[[Optional[Source]], None]) -> None:
        self.on_done = on_done
        self._start: Optional[tuple] = None
        self._cur = (0, 0)
        self._done = False

        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_decorated(False)
        win.set_app_paintable(True)
        win.set_keep_above(True)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        screen = win.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            win.set_visual(visual)
        win.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.KEY_PRESS_MASK
        )
        win.connect("draw", self._on_draw)
        win.connect("button-press-event", self._on_press)
        win.connect("motion-notify-event", self._on_motion)
        win.connect("button-release-event", self._on_release)
        win.connect("key-press-event", self._on_key)
        self.win = win

    def run(self) -> None:
        self.win.fullscreen()
        self.win.show_all()
        gdk_win = self.win.get_window()
        if gdk_win is not None:
            cursor = Gdk.Cursor.new_for_display(Gdk.Display.get_default(), Gdk.CursorType.CROSSHAIR)
            gdk_win.set_cursor(cursor)
        try:
            self.win.grab_focus()
        except Exception:  # noqa: BLE001
            pass

    # -- 交互 -------------------------------------------------------------
    def _on_key(self, _win, event) -> bool:
        keyval = event.get_keyval()[1]
        if keyval in (Gdk.KEY_Escape, Gdk.KEY_q):
            self._finish(None)
            return True
        return False

    def _on_press(self, _win, event) -> bool:
        if event.button == 1:
            self._start = (int(event.x_root), int(event.y_root))
            self._cur = self._start
            self.win.queue_draw()
            return True
        if event.button == 3:
            self._finish(None)
            return True
        return False

    def _on_motion(self, _win, event) -> bool:
        if self._start:
            self._cur = (int(event.x_root), int(event.y_root))
            self.win.queue_draw()
        return False

    def _on_release(self, _win, event) -> bool:
        if not self._start or event.button != 1:
            return False
        x0, y0 = self._start
        x1, y1 = int(event.x_root), int(event.y_root)
        x, y = min(x0, x1), min(y0, y1)
        w, h = abs(x1 - x0), abs(y1 - y0)
        if w < 32 or h < 32:
            self._finish(None)
            return True
        self._finish(
            Source(
                kind="region",
                label=f"区域 {w}x{h} @ {x},{y}",
                x=x,
                y=y,
                width=w,
                height=h,
            )
        )
        return True

    def _finish(self, source: Optional[Source]) -> None:
        if self._done:
            return
        self._done = True
        cb, self.on_done = self.on_done, lambda *_a: None
        self.win.destroy()
        cb(source)

    # -- 绘制 -------------------------------------------------------------
    def _on_draw(self, _win, cr) -> bool:
        try:
            import cairo  # noqa: PLC0415

            has_cairo = True
        except Exception:  # noqa: BLE001
            has_cairo = False

        w, h = self.win.get_size()
        if has_cairo:
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.set_source_rgba(0.05, 0.08, 0.12, 0.42)
            cr.paint()

            if self._start:
                x0, y0 = self._start
                x1, y1 = self._cur
                rx, ry = min(x0, x1), min(y0, y1)
                rw, rh = abs(x1 - x0), abs(y1 - y0)

                cr.set_operator(cairo.OPERATOR_CLEAR)
                cr.rectangle(rx, ry, rw, rh)
                cr.fill()

                cr.set_operator(cairo.OPERATOR_OVER)
                cr.set_source_rgb(0.25, 0.72, 1.0)
                cr.set_line_width(2)
                cr.rectangle(rx + 1, ry + 1, max(rw - 2, 0), max(rh - 2, 0))
                cr.stroke()
        else:
            cr.set_source_rgba(0.05, 0.08, 0.12, 0.42)
            cr.rectangle(0, 0, w, h)
            cr.fill()
            if self._start:
                x0, y0 = self._start
                x1, y1 = self._cur
                cr.set_source_rgb(0.25, 0.72, 1.0)
                cr.rectangle(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
                cr.stroke()
        return False

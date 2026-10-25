"""对话框：设置、日志、文本输入、关于。"""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from .config import Config, LOG_FILE


def _row(grid: Gtk.Grid, top: int, label: str, widget: Gtk.Widget, tip: str = "") -> int:
    lab = Gtk.Label(label=label, xalign=0)
    if tip:
        lab.set_tooltip_text(tip)
        widget.set_tooltip_text(tip)
    grid.attach(lab, 0, top, 1, 1)
    grid.attach(widget, 1, top, 1, 1)
    return top + 1


def _spin(value: float, minimum: float, maximum: float, step: float, digits: int = 0) -> Gtk.SpinButton:
    adj = Gtk.Adjustment(value=value, lower=minimum, upper=maximum, step_increment=step, page_increment=step * 10)
    spin = Gtk.SpinButton(adjustment=adj, climb_rate=0)
    spin.set_digits(digits)
    spin.set_numeric(True)
    return spin


class SettingsDialog(Gtk.Dialog):
    def __init__(self, parent: Optional[Gtk.Window], cfg: Config, on_change: Optional[Callable[[], None]] = None):
        super().__init__(title="投屏设置", transient_for=parent, modal=True, destroy_with_parent=True)
        self.cfg = cfg
        self.on_change = on_change
        self.set_default_size(430, 420)
        self.add_buttons("取消", Gtk.ResponseType.CANCEL, "保存", Gtk.ResponseType.OK)

        box = self.get_content_area()
        box.set_spacing(10)
        box.set_border_width(12)

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        row = 0

        self.framerate = _spin(float(cfg.get("framerate", 30)), 5, 60, 1)
        row = _row(grid, row, "帧率 (fps)", self.framerate, "越高越流畅，也越吃 CPU。一般 25~30 即可。")

        self.bitrate = _spin(float(cfg.get("bitrate_kbps", 4000)), 500, 40000, 100)
        row = _row(grid, row, "码率 (kbps)", self.bitrate, "画面复杂/分辨率高时调大，例如 1080p 用 4000~8000。")

        self.max_width = _spin(float(cfg.get("max_width", 1920)), 480, 7680, 80)
        row = _row(grid, row, "最大宽度 (px)", self.max_width, "超过该宽度会等比缩放，0 表示不限制。")

        self.draw_mouse = Gtk.CheckButton(label="显示鼠标指针")
        self.draw_mouse.set_active(bool(cfg.get("draw_mouse", True)))
        row = _row(grid, row, "鼠标", self.draw_mouse)

        self.audio = Gtk.CheckButton(label="同时投射系统音频（桌面回环）")
        self.audio.set_active(bool(cfg.get("audio", False)))
        row = _row(grid, row, "音频", self.audio, "通过 PulseAudio/PipeWire 的 monitor 源采集桌面声音。")

        self.audio_device = Gtk.Entry()
        self.audio_device.set_placeholder_text("留空 = 默认输出设备的 monitor 源")
        self.audio_device.set_text(str(cfg.get("audio_device") or ""))
        row = _row(grid, row, "音频源", self.audio_device, "可用 pactl list sources 查看，例如 alsa_output.xxx.monitor")

        self.port = _spin(float(cfg.get("port", 8899)), 1024, 65535, 1)
        row = _row(grid, row, "HTTP 端口", self.port, "投屏流的本地服务端口，需要允许局域网访问。")

        self.host = Gtk.Entry()
        self.host.set_placeholder_text("留空 = 自动探测")
        self.host.set_text(str(cfg.get("host_override") or ""))
        row = _row(grid, row, "对外 IP", self.host, "多网卡/VPN 时手动指定电视能访问到的那个 IP。")

        self.hls_time = _spin(float(cfg.get("hls_time", 1)), 0.5, 10, 0.5, digits=1)
        row = _row(grid, row, "HLS 切片(秒)", self.hls_time, "调小可降低 Chromecast 延迟，但会增加请求数。")

        self.dlna_mime = Gtk.Entry()
        self.dlna_mime.set_text(str(cfg.get("dlna_mime") or "video/mpegts"))
        row = _row(grid, row, "DLNA MIME", self.dlna_mime, "电视不识别时试试 video/vnd.dlna.org-mpeg-tts")

        box.pack_start(grid, False, False, 0)
        hint = Gtk.Label(label="修改端口/IP 会在下次开始投屏时生效。")
        hint.get_style_context().add_class("dim-label")
        hint.set_xalign(0)
        box.pack_start(hint, False, False, 0)
        self.show_all()

    def apply(self) -> None:
        cfg = self.cfg
        cfg["framerate"] = int(self.framerate.get_value())
        cfg["bitrate_kbps"] = int(self.bitrate.get_value())
        cfg["max_width"] = int(self.max_width.get_value())
        cfg["draw_mouse"] = self.draw_mouse.get_active()
        cfg["audio"] = self.audio.get_active()
        cfg["audio_device"] = self.audio_device.get_text().strip()
        cfg["port"] = int(self.port.get_value())
        cfg["host_override"] = self.host.get_text().strip()
        cfg["hls_time"] = self.hls_time.get_value()
        cfg["dlna_mime"] = self.dlna_mime.get_text().strip() or "video/mpegts"
        cfg.save()
        if self.on_change:
            self.on_change()


class LogDialog(Gtk.Dialog):
    def __init__(self, parent, title: str, text: str, log_file=None):
        super().__init__(title=title, transient_for=parent, modal=False, destroy_with_parent=True)
        self.log_file = log_file
        self.set_default_size(760, 520)
        self.add_buttons("刷新", 1, "关闭", Gtk.ResponseType.CLOSE)
        self.connect("response", self._on_response)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.textview = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.textview.get_buffer().set_text(text)
        sw.add(self.textview)
        self.get_content_area().pack_start(sw, True, True, 0)
        self.show_all()

    def _on_response(self, _dialog, response):
        if response == 1:
            try:
                data = self.log_file.read_text(errors="ignore") if self.log_file else "(无日志文件)"
            except Exception:  # noqa: BLE001
                data = "(读取失败)"
            self.textview.get_buffer().set_text(data)
            return
        self.destroy()


class TextInputDialog(Gtk.Dialog):
    def __init__(self, parent, title: str, label: str, value: str = "", placeholder: str = ""):
        super().__init__(title=title, transient_for=parent, modal=True, destroy_with_parent=True)
        self.add_buttons("取消", Gtk.ResponseType.CANCEL, "确定", Gtk.ResponseType.OK)
        self.set_default_size(400, 140)
        box = self.get_content_area()
        box.set_spacing(10)
        box.set_border_width(12)
        box.pack_start(Gtk.Label(label=label, xalign=0), False, False, 0)
        self.entry = Gtk.Entry()
        self.entry.set_text(value)
        self.entry.set_placeholder_text(placeholder)
        self.entry.set_activates_default(True)
        box.pack_start(self.entry, False, False, 0)
        self.set_default_response(Gtk.ResponseType.OK)
        self.show_all()

    @property
    def value(self) -> str:
        return self.entry.get_text().strip()


class AboutDialog(Gtk.AboutDialog):
    def __init__(self, parent):
        super().__init__(transient_for=parent, modal=True)
        self.set_program_name("ScreenCast Tray")
        self.set_version("1.0.0")
        self.set_comments("把屏幕 / 浏览器窗口内容投到 Chromecast 与 DLNA 电视")
        self.set_website("https://github.com/")
        self.set_license_type(Gtk.License.MIT_X11)
        self.connect("response", lambda d, r: d.destroy())


def show_error(parent, title: str, message: str) -> None:
    dialog = Gtk.MessageDialog(
        transient_for=parent,
        modal=True,
        destroy_with_parent=True,
        message_type=Gtk.MessageType.ERROR,
        buttons=Gtk.ButtonsType.CLOSE,
        text=title,
    )
    dialog.format_secondary_text(message)
    dialog.connect("response", lambda d, r: d.destroy())
    dialog.show()


def show_info(parent, title: str, message: str) -> None:
    dialog = Gtk.MessageDialog(
        transient_for=parent,
        modal=True,
        destroy_with_parent=True,
        message_type=Gtk.MessageType.INFO,
        buttons=Gtk.ButtonsType.CLOSE,
        text=title,
    )
    dialog.format_secondary_text(message)
    dialog.connect("response", lambda d, r: d.destroy())
    dialog.show()


APP_LOG = LOG_FILE

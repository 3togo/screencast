"""应用主控：托盘菜单、投屏会话生命周期。"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from typing import List, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from . import ui as ui_mod
from .config import APP_NAME, CACHE_DIR, Config, LOG_FILE
from .devices import Device, add_chromecast_by_host, discover_async, play, stop as device_stop
from .encoder import Encoder, audio_available
from .sources import RegionSelector, Source, list_monitors, list_windows
from .streamer import StreamServer
from .tray import Tray

log = logging.getLogger(__name__)

FFMPEG_LOG = CACHE_DIR / "ffmpeg.log"


def setup_logging() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)


class CastApp:
    def __init__(self) -> None:
        self.cfg = Config()
        self.source: Optional[Source] = None
        self.device: Optional[Device] = None
        self.devices: List[Device] = []
        self.server: Optional[StreamServer] = None
        self.encoder: Optional[Encoder] = None
        self.state = "idle"  # idle | starting | casting
        self._discovering = False
        self._selector = None
        self._audio_supported = audio_available()

        self.tray = Tray(self.build_menu)
        self.tray.set_label("ScreenCast")
        GLib.timeout_add_seconds(3, self._health_check)
        if not os.environ.get("DISPLAY"):
            self.tray.notify(
                "无法采集屏幕",
                "当前会话没有 DISPLAY（可能是纯 Wayland 会话）。请在登录界面选择 “Ubuntu on Xorg” 会话，"
                "或把 XDG_SESSION_TYPE 设为 x11。",
            )
        self.discover_devices()

    # ================================================================ 菜单
    def build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()

        status = Gtk.MenuItem(label=self._status_text())
        status.set_sensitive(False)
        menu.append(status)
        menu.append(Gtk.SeparatorMenuItem())

        if self.state == "casting":
            action = Gtk.MenuItem(label="停止投屏")
            action.connect("activate", lambda *_a: self.stop())
        else:
            action = Gtk.MenuItem(label="开始投屏")
            action.set_sensitive(self.source is not None and self.device is not None)
            action.connect("activate", lambda *_a: self.start())
        menu.append(action)

        if self.state == "casting" and self.server:
            preview = Gtk.MenuItem(label="在浏览器中预览流…")
            preview.connect("activate", lambda *_a: self._open_url(self.server.base_url))
            menu.append(preview)

        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self._submenu("选择源", self._source_menu()))
        menu.append(self._submenu("选择设备", self._device_menu()))
        menu.append(Gtk.SeparatorMenuItem())

        audio = Gtk.CheckMenuItem(label="投射系统音频（桌面回环）")
        audio.set_active(bool(self.cfg.get("audio")))
        audio.set_sensitive(self._audio_supported)
        if not self._audio_supported:
            audio.set_tooltip_text("ffmpeg 未编译 pulse 输入，无法采集桌面音频")
        audio.connect("toggled", self._on_audio_toggled)
        menu.append(audio)

        settings = Gtk.MenuItem(label="设置…")
        settings.connect("activate", lambda *_a: self._open_settings())
        menu.append(settings)

        logs = Gtk.MenuItem(label="查看日志…")
        logs.connect("activate", lambda *_a: self._open_logs())
        menu.append(logs)

        about = Gtk.MenuItem(label="关于")
        about.connect("activate", lambda *_a: ui_mod.AboutDialog(None))
        menu.append(about)

        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="退出")
        quit_item.connect("activate", lambda *_a: self.quit())
        menu.append(quit_item)
        return menu

    @staticmethod
    def _submenu(label: str, sub: Gtk.Menu) -> Gtk.MenuItem:
        item = Gtk.MenuItem(label=label)
        item.set_submenu(sub)
        return item

    def _status_text(self) -> str:
        if self.state == "casting":
            return f"正在投屏：{self.source} → {self.device.name if self.device else '?'}"
        if self.state == "starting":
            return "正在启动…"
        src = str(self.source) if self.source else "未选择"
        dev = self.device.name if self.device else "未选择"
        return f"空闲   源：{src}   设备：{dev}"

    def _source_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()
        refresh = Gtk.MenuItem(label="刷新窗口列表")
        refresh.connect("activate", lambda *_a: self.refresh_ui())
        menu.append(refresh)
        menu.append(Gtk.SeparatorMenuItem())

        try:
            windows = list_windows()
        except Exception as exc:  # noqa: BLE001
            log.warning("枚举窗口失败: %s", exc)
            windows = []
            item = Gtk.MenuItem(label="枚举窗口失败（见日志）")
            item.set_sensitive(False)
            menu.append(item)

        browsers = [w for w in windows if w.browser]
        others = [w for w in windows if not w.browser]
        if browsers:
            menu.append(self._submenu("浏览器窗口", self._items_for(browsers)))
        if others:
            menu.append(self._submenu("其他窗口", self._items_for(others)))
        if not windows:
            item = Gtk.MenuItem(label="没有可投屏的窗口")
            item.set_sensitive(False)
            menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())
        for mon in list_monitors():
            menu.append(self._source_item(mon))
        menu.append(Gtk.SeparatorMenuItem())
        region = Gtk.MenuItem(label="框选区域…")
        region.connect("activate", lambda *_a: self._select_region())
        menu.append(region)
        return menu

    def _items_for(self, sources: List[Source]) -> Gtk.Menu:
        menu = Gtk.Menu()
        for src in sources[:40]:
            menu.append(self._source_item(src))
        return menu

    def _source_item(self, src: Source) -> Gtk.MenuItem:
        prefix = "● " if self._same_source(src) else ""
        item = Gtk.MenuItem(label=f"{prefix}{src.label}")
        item.connect("activate", lambda *_a, s=src: self.set_source(s))
        return item

    def _same_source(self, src: Source) -> bool:
        if self.source is None:
            return False
        if src.kind == "window":
            return self.source.kind == "window" and self.source.xid == src.xid
        return (
            self.source.kind == src.kind
            and self.source.x == src.x
            and self.source.y == src.y
            and self.source.width == src.width
            and self.source.height == src.height
        )

    def _device_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()
        refresh = Gtk.MenuItem(label="刷新设备")
        refresh.connect("activate", lambda *_a: self.discover_devices())
        menu.append(refresh)
        manual = Gtk.MenuItem(label="手动添加 Chromecast…")
        manual.connect("activate", lambda *_a: self._add_chromecast_manual())
        menu.append(manual)
        menu.append(Gtk.SeparatorMenuItem())

        if self._discovering:
            item = Gtk.MenuItem(label="正在搜索局域网设备…")
            item.set_sensitive(False)
            menu.append(item)

        if not self.devices and not self._discovering:
            item = Gtk.MenuItem(label="未发现设备（点“刷新设备”重试）")
            item.set_sensitive(False)
            menu.append(item)

        for dev in self.devices:
            prefix = "● " if self.device and self.device.key() == dev.key() else ""
            item = Gtk.MenuItem(label=f"{prefix}{dev.label()}  {dev.host}")
            item.connect("activate", lambda *_a, d=dev: self.set_device(d))
            menu.append(item)
        return menu

    def _on_audio_toggled(self, item) -> None:
        self.cfg["audio"] = item.get_active()
        self.cfg.save()
        if self.state == "casting":
            self.tray.notify("音频设置已保存", "下次开始投屏时生效。")

    # ============================================================ 交互动作
    def refresh_ui(self) -> None:
        self.tray.refresh()

    def set_source(self, source: Source) -> None:
        self.source = source
        log.info("选择源: %s (%s)", source.label, source.kind)
        self.refresh_ui()
        if self.device and self.state == "idle":
            self.start()

    def set_device(self, device: Device) -> None:
        self.device = device
        self.cfg["last_device"] = device.to_dict()
        self.cfg.save()
        log.info("选择设备: %s", device.label())
        self.refresh_ui()
        if self.source and self.state == "idle":
            self.start()

    def _select_region(self) -> None:
        def done(source: Optional[Source]) -> None:
            self._selector = None
            if source:
                self.set_source(source)

        self._selector = RegionSelector(done)
        self._selector.run()

    def _add_chromecast_manual(self) -> None:
        dialog = ui_mod.TextInputDialog(None, "添加 Chromecast", "IP 地址或主机名：", "", "例如 192.168.1.50")
        if dialog.run() != Gtk.ResponseType.OK:
            dialog.destroy()
            return
        host = dialog.value
        dialog.destroy()
        if not host:
            return
        self._discovering = True
        self.refresh_ui()

        def worker():
            dev = add_chromecast_by_host(host)
            GLib.idle_add(self._on_manual_added, dev, host)

        threading.Thread(target=worker, daemon=True).start()

    def _on_manual_added(self, dev: Optional[Device], host: str) -> bool:
        self._discovering = False
        if dev is None:
            self.tray.notify("连接失败", f"无法连接到 {host} 上的 Chromecast。")
            self.refresh_ui()
            return False
        self.devices = [d for d in self.devices if d.key() != dev.key()]
        self.devices.append(dev)
        self.devices.sort(key=lambda d: (d.kind != "chromecast", d.name.lower()))
        self.set_device(dev)
        return False

    def _open_settings(self) -> None:
        dialog = ui_mod.SettingsDialog(None, self.cfg, on_change=lambda: self.refresh_ui())
        try:
            if dialog.run() == Gtk.ResponseType.OK:
                dialog.apply()
                self._audio_supported = audio_available()
                self.refresh_ui()
        finally:
            dialog.destroy()

    def _open_logs(self) -> None:
        text = ""
        if self.encoder:
            text = "=== ffmpeg 日志 ===\n" + self.encoder.last_error(200) + "\n\n"
        try:
            text += "=== 应用日志 ===\n" + LOG_FILE.read_text(errors="ignore")[-20000:]
        except Exception:  # noqa: BLE001
            text += "=== 应用日志 ===\n(读取失败)"
        ui_mod.LogDialog(None, "日志", text, FFMPEG_LOG)

    def _open_url(self, url: str) -> None:
        try:
            Gtk.show_uri_on_window(None, url, Gdk.CURRENT_TIME)
        except Exception as exc:  # noqa: BLE001
            log.info("打开浏览器失败: %s", exc)

    # ================================================================ 发现
    def discover_devices(self) -> None:
        if self._discovering:
            return
        self._discovering = True
        self.refresh_ui()

        def on_done(found: List[Device]) -> None:
            GLib.idle_add(self._on_devices_found, found)

        discover_async(on_done)

    def _on_devices_found(self, found: List[Device]) -> bool:
        self._discovering = False
        self.devices = found
        log.info("发现 %d 个设备", len(found))
        if self.device is None:
            self._restore_last_device()
        elif found:
            match = next((d for d in found if d.key() == self.device.key()), None)
            if match:
                match.extra.update({k: v for k, v in self.device.extra.items() if k not in match.extra})
                self.device = match
        elif self.state == "idle":
            pass
        self.refresh_ui()
        return False

    def _restore_last_device(self) -> None:
        saved = self.cfg.get("last_device")
        dev = Device.from_dict(saved) if isinstance(saved, dict) else None
        if not dev:
            return
        match = next((d for d in self.devices if d.key() == dev.key()), None)
        if match:
            self.device = match
            log.info("恢复上次设备: %s", match.label())
        elif dev.kind == "chromecast" and dev.host:
            # 还没被发现到，尝试直连
            direct = add_chromecast_by_host(dev.host)
            if direct:
                self.devices.append(direct)
                self.device = direct

    # ================================================================ 投屏
    def start(self) -> None:
        if self.state != "idle":
            return
        if self.source is None:
            self.tray.notify("请先选择投屏源", "在“选择源”里挑一个浏览器窗口、显示器或屏幕区域。")
            return
        if self.device is None:
            self.tray.notify("请先选择设备", "在“选择设备”里刷新并选中 Chromecast / DLNA 电视。")
            return
        self.state = "starting"
        self.refresh_ui()
        threading.Thread(target=self._start_worker, daemon=True).start()

    def _start_worker(self) -> None:
        server = None
        encoder = None
        try:
            cfg = self.cfg
            server = StreamServer(port=int(cfg.get("port") or 8899))
            server.dlna_mime = str(cfg.get("dlna_mime") or "video/mpegts")
            server.set_public_host(str(cfg.get("host_override") or ""))
            server.start()

            encoder = Encoder(FFMPEG_LOG)
            encoder.on_data = server.broadcast
            server.hls_dir = encoder.hls_dir
            encoder.start(self.source, cfg)

            if not encoder.wait_ready(12):
                raise RuntimeError("ffmpeg 未能在 12 秒内产出可用流。\n" + encoder.last_error(12))

            self.server = server
            self.encoder = encoder
            self.state = "casting"

            url = server.hls_url if self.device.kind == "chromecast" else server.ts_url
            log.info("推送地址: %s", url)
            play(self.device, url, mime=server.dlna_mime, title=str(self.source))

            GLib.idle_add(self._on_started, url)
        except Exception as exc:  # noqa: BLE001
            log.exception("启动投屏失败")
            if encoder:
                encoder.stop()
            if server:
                server.stop()
            detail = str(exc)
            GLib.idle_add(self._on_start_failed, detail)

    def _on_started(self, url: str) -> bool:
        self.tray.set_label("● 投屏中")
        self.tray.notify(
            "开始投屏",
            f"{self.source} → {self.device.name}\n{url}",
        )
        self.refresh_ui()
        return False

    def _on_start_failed(self, detail: str) -> bool:
        self.state = "idle"
        self.encoder = None
        self.server = None
        self.tray.set_label("ScreenCast")
        self.tray.notify("投屏失败", detail[:300])
        self.refresh_ui()
        return False

    def stop(self) -> None:
        if self.encoder:
            self.encoder.stop()
            self.encoder = None
        if self.server:
            self.server.stop()
            self.server = None
        device = self.device
        if device:
            threading.Thread(target=device_stop, args=(device,), daemon=True).start()
        self.state = "idle"
        self.tray.set_label("ScreenCast")
        self.refresh_ui()
        self.tray.notify("已停止投屏")

    def _health_check(self) -> bool:
        if self.state == "casting" and self.encoder is not None and not self.encoder.is_alive():
            log.error("ffmpeg 意外退出，结束投屏")
            detail = self.encoder.last_error(10)
            self.encoder.stop()
            self.encoder = None
            if self.server:
                self.server.stop()
                self.server = None
            device = self.device
            if device:
                threading.Thread(target=device_stop, args=(device,), daemon=True).start()
            self.state = "idle"
            self.tray.set_label("ScreenCast")
            self.refresh_ui()
            self.tray.notify("投屏中断", f"采集进程已退出：\n{detail[:300]}")
        return True

    # ================================================================ 退出
    def quit(self) -> None:
        log.info("退出")
        try:
            if self.encoder:
                self.encoder.stop()
            if self.server:
                self.server.stop()
            if self.device and self.state == "casting":
                device_stop(self.device)
        except Exception:  # noqa: BLE001
            pass
        self.cfg.save()
        Gtk.main_quit()


def main(argv: Optional[List[str]] = None) -> int:
    setup_logging()
    log.info("%s 启动", APP_NAME)
    Gtk.init([])

    app = CastApp()

    def _signal(*_a):
        GLib.idle_add(app.quit)
        return True

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, _signal)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _signal)

    try:
        Gtk.main()
    except KeyboardInterrupt:
        app.quit()
    return 0

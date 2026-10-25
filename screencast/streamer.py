"""本地 HTTP 串流服务器。

同时对外提供两种格式：

* ``/live.ts``   —— 持续 MPEG-TS 流，给 DLNA / UPnP 渲染器用
* ``/hls/live.m3u8`` —— HLS 切片，给 Chromecast 用

TS 数据由 ffmpeg 通过管道写进来，服务器负责向所有订阅者广播。
"""

from __future__ import annotations

import html
import logging
import mimetypes
import queue
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)

_CHUNK = 65536
_QUEUE_MAX = 300  # ≈ 20MB @ 1MB/s，防止客户端卡住时无限堆积


def lan_ip() -> str:
    """探测用于对外访问的局域网 IP（不会真的发包）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.2)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    except Exception:  # noqa: BLE001
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except Exception:  # noqa: BLE001
            ip = "127.0.0.1"
    finally:
        sock.close()
    return ip


class StreamServer:
    def __init__(self, port: int = 8899, hls_dir: Optional[Path] = None, bind: str = "0.0.0.0") -> None:
        self.port = int(port)
        self.hls_dir = Path(hls_dir) if hls_dir else None
        self.bind = bind
        self.host = "127.0.0.1"  # ffmpeg 写入用的地址
        self.public_host = lan_ip()

        self._subs: List[queue.Queue] = []
        self._lock = threading.Lock()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.dropped_frames = 0

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> None:
        if self._httpd:
            return
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "ScreenCastTray/1.0"

            def log_message(self, fmt, *args):  # noqa: A002
                log.debug("http %s - %s", self.address_string(), fmt % args)

            # 通用
            def _send_head(self, status=200, ctype="text/plain", length=None, extra=None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                if length is not None:
                    self.send_header("Content-Length", str(length))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

            def do_HEAD(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path.startswith("/hls/"):
                    self._file_head(path)
                elif path in ("/live.ts", "/live.mpegts", "/stream.ts"):
                    self._send_head(200, outer.dlna_mime, extra=outer._ts_headers())
                else:
                    self._send_head(404, "text/plain", 0)

            def do_GET(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path == "/":
                    body = outer._info_page().encode("utf-8")
                    self._send_head(200, "text/html; charset=utf-8", len(body))
                    try:
                        self.wfile.write(body)
                    except OSError:
                        pass
                elif path in ("/live.ts", "/live.mpegts", "/stream.ts"):
                    self._stream_ts()
                elif path.startswith("/hls/"):
                    self._serve_file(path)
                else:
                    self._send_head(404, "text/plain", 0)

            # TS 直播流
            def _stream_ts(self):
                q = outer.subscribe()
                self.close_connection = True
                self._send_head(200, outer.dlna_mime, extra=outer._ts_headers())
                try:
                    while True:
                        try:
                            data = q.get(timeout=15)
                        except queue.Empty:
                            if not outer.running:
                                break
                            continue
                        if not data:  # 结束哨兵
                            break
                        self.wfile.write(data)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    outer.unsubscribe(q)

            # HLS 静态文件
            def _file_head(self, path):
                fpath = outer._resolve_hls(path)
                if fpath is None or not fpath.exists():
                    self._send_head(404, "text/plain", 0)
                    return
                self._send_head(200, outer._mime(fpath), fpath.stat().st_size)

            def _serve_file(self, path):
                fpath = outer._resolve_hls(path)
                if fpath is None or not fpath.exists():
                    self._send_head(404, "text/plain", 0)
                    try:
                        self.wfile.write(b"not ready")
                    except OSError:
                        pass
                    return
                data = fpath.read_bytes()
                self._send_head(200, outer._mime(fpath), len(data))
                try:
                    self.wfile.write(data)
                except OSError:
                    pass

        self._httpd = ThreadingHTTPServer((self.bind, self.port), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="stream-http", daemon=True)
        self._thread.start()
        log.info("串流服务已启动: http://%s:%d/", self.public_host, self.port)

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd:
            try:
                httpd.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            subs, self._subs = list(self._subs), []
        for q in subs:
            try:
                q.put_nowait(b"")
            except Exception:  # noqa: BLE001
                pass
        log.info("串流服务已停止")

    @property
    def running(self) -> bool:
        return self._httpd is not None

    # -- 订阅 / 广播 ------------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=_QUEUE_MAX)
        with self._lock:
            self._subs.append(q)
        log.debug("新的 TS 订阅者，共 %d", len(self._subs))
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)
        log.debug("TS 订阅者离开，剩 %d", len(self._subs))

    def broadcast(self, data: bytes) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(data)
            except queue.Full:
                self.dropped_frames += 1
                try:
                    q.get_nowait()
                    q.put_nowait(data)
                except Exception:  # noqa: BLE001
                    pass

    # -- URL --------------------------------------------------------------
    def set_public_host(self, host: str) -> None:
        self.public_host = host or lan_ip()

    @property
    def base_url(self) -> str:
        return f"http://{self.public_host}:{self.port}"

    @property
    def ts_url(self) -> str:
        return f"{self.base_url}/live.ts"

    @property
    def hls_url(self) -> str:
        return f"{self.base_url}/hls/live.m3u8"

    # -- 辅助 -------------------------------------------------------------
    dlna_mime = "video/mpegts"

    def _ts_headers(self) -> dict:
        return {
            "Connection": "close",
            "contentFeatures.dlna.org": "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000",
            "transferMode.dlna.org": "Streaming",
            "icy-name": "ScreenCast",
        }

    def _resolve_hls(self, path: str) -> Optional[Path]:
        if self.hls_dir is None:
            return None
        name = path[len("/hls/") :]
        name = name.replace("\\", "/").split("?")[0]
        if ".." in name or name.startswith("/"):
            return None
        return self.hls_dir / name

    @staticmethod
    def _mime(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".m3u8":
            return "application/vnd.apple.mpegurl"
        if suffix == ".ts":
            return "video/mp2t"
        guess = mimetypes.guess_type(path.name)[0]
        return guess or "application/octet-stream"

    def _info_page(self) -> str:
        return """<!doctype html><meta charset="utf-8">
<title>ScreenCast Tray</title>
<style>body{font-family:system-ui;background:#111827;color:#e5e7eb;padding:2rem}
a{color:#60a5fa}code{background:#1f2937;padding:.2rem .4rem;border-radius:4px}</style>
<h1>ScreenCast Tray</h1>
<p>本机的实时投屏流：</p>
<ul>
<li><a href="/hls/live.m3u8">HLS</a> — <code>/hls/live.m3u8</code>（Chromecast 用）</li>
<li><a href="/live.ts">MPEG-TS</a> — <code>/live.ts</code>（DLNA 用）</li>
</ul>
<p>时间：{ts}</p>
""".replace("{ts}", html.escape(time.strftime("%Y-%m-%d %H:%M:%S")))

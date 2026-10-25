#!/usr/bin/env python3
"""自检：不依赖真实的 Chromecast / DLNA 设备，验证「采集 → 编码 → HTTP 串流」整条链路。

    source ~/venv/bin/activate && python selftest.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from screencast.app import FFMPEG_LOG, setup_logging  # noqa: E402
from screencast.config import Config  # noqa: E402
from screencast.encoder import Encoder, audio_available  # noqa: E402
from screencast.sources import list_monitors, list_windows  # noqa: E402
from screencast.streamer import StreamServer  # noqa: E402

Gtk.init([])


def main() -> int:
    setup_logging()
    cfg = Config()
    port = int(cfg.get("port") or 8899)
    if os.environ.get("SCREENCAST_TEST_AUDIO"):
        cfg["audio"] = True

    windows = list_windows()
    browsers = [w for w in windows if w.browser]
    print(f"窗口: {len(windows)} 个（其中浏览器 {len(browsers)} 个）")
    for w in browsers[:5]:
        print(f"  [browser] {w.label}  xid={w.xid}")
    for w in windows[:3]:
        if not w.browser:
            print(f"  [window ] {w.label}")
    mons = list_monitors()
    print(f"显示器: {len(mons)} 个")
    for m in mons:
        print(f"  {m.label} @ {m.x},{m.y}")
    print(f"桌面音频可用: {audio_available()}")

    if not mons:
        print("!! 没有可采集的显示器，终止")
        return 1
    source = browsers[0] if browsers else mons[0]
    print(f"\n采集源: {source.label} ({source.kind})\n")

    server = StreamServer(port=port)
    server.dlna_mime = str(cfg.get("dlna_mime") or "video/mpegts")
    server.start()

    encoder = Encoder(FFMPEG_LOG)
    encoder.on_data = server.broadcast
    server.hls_dir = encoder.hls_dir
    encoder.start(source, cfg)

    ok = True
    try:
        print("等待 HLS 就绪 ...", end="", flush=True)
        ready = encoder.wait_ready(15)
        print(" OK" if ready else " 超时")
        ok = ok and ready
        if not ready:
            print(encoder.last_error(30))
            return 1

        print(f"  HLS : {server.hls_url}")
        print(f"  TS  : {server.ts_url}")

        time.sleep(2)
        with urllib.request.urlopen(server.hls_url, timeout=5) as resp:
            body = resp.read().decode("utf-8", "ignore")
        print("\n--- live.m3u8 ---")
        print(body.strip()[:400])
        ok = ok and "#EXTINF" in body

        print("\n用 ffprobe 检查 /live.ts ...")
        proc = subprocess.run(
            [
                "ffprobe",
                "-hide_banner",
                "-loglevel",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name,width,height",
                "-of",
                "default=noprint_wrappers=1",
                "-analyzeduration",
                "3000000",
                "-probesize",
                "3000000",
                server.ts_url,
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
        out = (proc.stdout or "").strip()
        print(out or "(无输出)")
        if proc.returncode != 0:
            print("ffprobe stderr:", (proc.stderr or "")[:600])
        ok = ok and "video" in out
    finally:
        encoder.stop()
        server.stop()

    print("\n结果:", "PASS" if ok else "FAIL")
    print(f"ffmpeg 日志: {FFMPEG_LOG}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

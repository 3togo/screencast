"""ffmpeg 采集 + 编码 + 输出（MPEG-TS 直播流 & HLS）。"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import List, Optional

from .config import Config
from .sources import Source

log = logging.getLogger(__name__)

_CHUNK = 65536


def _even(value: int) -> int:
    value = int(value)
    value -= value % 2
    return max(value, 2)


def _fit(width: int, height: int, max_width: int):
    w, h = _even(width), _even(height)
    if max_width and w > max_width:
        h = _even(int(round(h * max_width / w)))
        w = _even(max_width)
    return max(w, 2), max(h, 2)


def default_audio_source(cfg: Config) -> str:
    """返回 PulseAudio/PipeWire 的桌面音频 monitor 源。"""
    override = (cfg.get("audio_device") or "").strip()
    if override:
        return override
    try:
        out = subprocess.run(
            ["pactl", "get-default-sink"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        sink = out.stdout.strip()
        if sink:
            return f"{sink}.monitor"
    except Exception as exc:  # noqa: BLE001
        log.debug("pactl 不可用: %s", exc)
    return "default"


def audio_available() -> bool:
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-devices"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        return "pulse" in (out.stdout or "")
    except Exception:  # noqa: BLE001
        return False


class Encoder:
    """管理 ffmpeg 子进程。"""

    def __init__(self, log_path: Path) -> None:
        self.log_path = Path(log_path)
        self.proc: Optional[subprocess.Popen] = None
        # 提前建好，供 StreamServer 直接使用
        self.hls_dir: Optional[Path] = Path(tempfile.mkdtemp(prefix="screencast-hls-"))
        self.cmd: List[str] = []
        self.on_data = None  # callable(bytes)
        self._reader: Optional[threading.Thread] = None
        self._logfile = None

    # -- 命令构造 ---------------------------------------------------------
    def build_cmd(self, source: Source, cfg: Config, ts_target: str = "pipe:1") -> List[str]:
        display = os.environ.get("DISPLAY", ":0")
        fps = int(cfg.get("framerate") or 30)
        bitrate = int(cfg.get("bitrate_kbps") or 4000)
        max_width = int(cfg.get("max_width") or 0)
        audio = bool(cfg.get("audio"))
        hls_time = max(float(cfg.get("hls_time") or 1), 0.5)
        hls_list = max(int(cfg.get("hls_list_size") or 4), 2)

        cap_w, cap_h = _even(source.width), _even(source.height)
        out_w, out_h = _fit(source.width, source.height, max_width)

        if self.hls_dir is None:
            self.hls_dir = Path(tempfile.mkdtemp(prefix="screencast-hls-"))
        self.hls_dir.mkdir(parents=True, exist_ok=True)
        m3u8 = self.hls_dir / "live.m3u8"

        cmd: List[str] = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning", "-y"]

        # 视频输入
        cmd += ["-f", "x11grab", "-framerate", str(fps)]
        cmd += ["-draw_mouse", "1" if cfg.get("draw_mouse", True) else "0"]
        if source.kind == "window" and source.xid:
            # 跟随该窗口，窗口移动/最小化后恢复也无需重新选择
            cmd += ["-window_id", str(source.xid)]
            cmd += ["-i", display]
        else:
            cmd += ["-video_size", f"{cap_w}x{cap_h}"]
            cmd += ["-i", f"{display}+{source.x},{source.y}"]

        # 音频输入
        if audio:
            cmd += ["-thread_queue_size", "1024", "-f", "pulse", "-i", default_audio_source(cfg)]

        # 视频编码
        cmd += ["-map", "0:v"]
        cmd += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            f"{bitrate}k",
            "-maxrate",
            f"{int(bitrate * 1.4)}k",
            "-bufsize",
            f"{bitrate * 2}k",
            # 1 秒一个关键帧：HLS 才能按 1s 切片，降低 Chromecast 端延迟
            "-g",
            str(fps),
            "-keyint_min",
            str(max(fps // 2, 1)),
            "-sc_threshold",
            "0",
        ]
        # 始终过一遍 scale：窗口实际尺寸可能是奇数，libx264 要求宽高为偶数
        cmd += ["-vf", f"scale={out_w}:{out_h}"]

        # 音频编码
        if audio:
            cmd += ["-map", "1:a", "-c:a", "aac", "-b:a", "128k", "-ac", "2"]

        # 输出：一路 MPEG-TS（DLNA），一路 HLS（Chromecast）
        hls_opts = (
            f"f=hls"
            f":hls_time={hls_time}"
            f":hls_list_size={hls_list}"
            f":hls_flags=delete_segments+append_list"
            f":hls_segment_type=mpegts"
            f":hls_allow_cache=0"
            f":hls_segment_filename={self.hls_dir}/seg_%d.ts"
        )
        ts_opts = "f=mpegts"
        cmd += ["-f", "tee", f"[{ts_opts}]{ts_target}|[{hls_opts}]{m3u8}"]
        return cmd

    # -- 生命周期 ---------------------------------------------------------
    def start(self, source: Source, cfg: Config) -> None:
        self.cmd = self.build_cmd(source, cfg)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._logfile = open(self.log_path, "ab", buffering=0)
        log.info("启动 ffmpeg: %s", " ".join(self.cmd))
        self._logfile.write(("\n=== " + time.strftime("%F %T") + " ===\n" + " ".join(self.cmd) + "\n").encode())

        self.proc = subprocess.Popen(
            self.cmd,
            stdout=subprocess.PIPE,
            stderr=self._logfile,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._reader = threading.Thread(target=self._read_stdout, name="ffmpeg-stdout", daemon=True)
        self._reader.start()

    def _read_stdout(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        stream = proc.stdout
        try:
            while True:
                chunk = stream.read1(_CHUNK)
                if not chunk:
                    break
                if self.on_data:
                    self.on_data(chunk)
        except (ValueError, OSError):
            pass
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout: float = 4.0) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            self._cleanup()
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:  # noqa: BLE001
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        try:
            proc.wait(timeout=timeout)
        except Exception:  # noqa: BLE001
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:  # noqa: BLE001
                proc.kill()
        if self._logfile:
            try:
                self._logfile.close()
            except Exception:  # noqa: BLE001
                pass
            self._logfile = None
        self._cleanup()

    def _cleanup(self) -> None:
        if self.hls_dir and self.hls_dir.exists():
            shutil.rmtree(self.hls_dir, ignore_errors=True)
        self.hls_dir = None

    # -- 就绪检测 ---------------------------------------------------------
    def wait_ready(self, timeout: float = 12.0) -> bool:
        """等待 HLS 播放列表生成（说明编码链路已经跑通）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.is_alive():
                return False
            if self.hls_dir:
                m3u8 = self.hls_dir / "live.m3u8"
                try:
                    if m3u8.exists() and m3u8.stat().st_size > 0:
                        text = m3u8.read_text(errors="ignore")
                        if "#EXTINF" in text:
                            return True
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(0.3)
        return False

    def last_error(self, lines: int = 40) -> str:
        try:
            data = self.log_path.read_text(errors="ignore")
        except Exception:  # noqa: BLE001
            return "(无日志)"
        tail = data.strip().splitlines()[-lines:]
        return "\n".join(tail) or "(无日志)"

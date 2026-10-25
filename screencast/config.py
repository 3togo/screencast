"""持久化配置。"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "screencast-tray"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / APP_NAME

CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CACHE_DIR / "app.log"

DEFAULTS = {
    # 采集
    "framerate": 30,
    "bitrate_kbps": 4000,
    "max_width": 1920,
    "draw_mouse": True,
    # 音频
    "audio": False,
    "audio_device": "",  # 留空 = 默认输出设备的 monitor 源
    # 网络
    "port": 8899,
    "host_override": "",  # 留空 = 自动探测局域网 IP
    # 串流
    "hls_time": 1,
    "hls_list_size": 4,
    "dlna_mime": "video/mpegts",
    # 上次选择
    "last_device": None,  # {"kind":..., "name":..., "uuid"/"location":...}
}


class Config:
    def __init__(self) -> None:
        self._data = dict(DEFAULTS)
        self.load()

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value

    def get(self, key, default=None):
        return self._data.get(key, default)

    def load(self) -> None:
        try:
            if CONFIG_FILE.exists():
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._data.update(data)
        except Exception as exc:  # noqa: BLE001
            log.warning("读取配置失败，使用默认值: %s", exc)

    def save(self) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(CONFIG_FILE)
        except Exception as exc:  # noqa: BLE001
            log.warning("保存配置失败: %s", exc)

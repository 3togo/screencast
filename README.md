# ScreenCast Tray

Ubuntu 桌面托盘应用：把**浏览器窗口 / 整个显示器 / 屏幕区域**的实时画面投到局域网里的
**Chromecast** 或 **DLNA / UPnP 电视**。

```
采集 (ffmpeg x11grab) ──► 本地 HTTP 服务 ──► Chromecast  (HLS  /hls/live.m3u8)
                                        └──► DLNA 电视   (MPEG-TS /live.ts)
```

## 特性

| 项目 | 说明 |
| --- | --- |
| 采集源 | 浏览器窗口（跟随窗口移动，且浏览器窗口会排在最前）、任意窗口、显示器、鼠标框选区域 |
| 目标设备 | Chromecast / Google TV（mDNS 自动发现）、DLNA / UPnP MediaRenderer（SSDP 自动发现）、手动按 IP 添加 Chromecast |
| 编码 | H.264（libx264, zerolatency），可选桌面音频回环（PulseAudio / PipeWire monitor 源） |
| 托盘 | AyatanaAppIndicator3（SNI），自动回退到 `Gtk.StatusIcon`（XEmbed，兼容老 XFCE/MATE） |
| 其他 | 帧率/码率/最大宽度/端口/对外 IP 可调，实时健康监控，日志查看 |

## 依赖

```bash
# 系统包（GTK / 托盘 / 窗口枚举 / 编码）
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 \
     gir1.2-wnck-3.0 gir1.2-ayatanaappindicator3-0.1 \
     gir1.2-notify-0.7 ffmpeg pulseaudio-utils

# Python 包
source ~/venv/bin/activate
pip install -r requirements.txt
```

> 若你的 venv 是独立创建的，需要让它能看到系统的 PyGObject：
> 把 `pyvenv.cfg` 里的 `include-system-site-packages` 改成 `true`。

## 运行

```bash
./install.sh          # 装图标 + 登录自启动（可选）
./screencast-tray     # 直接运行
```

托盘里点图标 → `选择源` → `选择设备`，两个都选好就会自动开始投屏。

## 自检（不需要真实设备）

```bash
source ~/venv/bin/activate
python selftest.py                      # 采集第一个浏览器窗口/显示器并校验 HLS + MPEG-TS
SCREENCAST_TEST_AUDIO=1 python selftest.py   # 顺带验证桌面音频采集
```

脚本会启动采集与 HTTP 服务，用 `ffprobe` 拉流确认 `h264(+aac)` 正常，然后自动停止。

## 常见问题

**托盘图标不显示**
XFCE 需要装托盘插件：`sudo apt install xfce4-statusnotifier-plugin`（XFCE 4.18+）
或 `xfce4-indicator-plugin`，然后在面板里添加对应插件项。

**电视找不到设备**
- Chromecast 依赖 mDNS，确认本机与设备同网段、防火墙放行 5353/UDP 与 HTTP 端口。
- DLNA 用 SSDP（1900/UDP）。也可以用「手动添加 Chromecast…」按 IP 直连。

**电视能连上但没画面**
- 确认防火墙放行本机的 HTTP 端口（默认 8899/TCP）。
- 多网卡 / VPN 时在「设置」里手动填「对外 IP」。
- DLNA 电视识别不了流时，把「DLNA MIME」改成 `video/vnd.dlna.org-mpeg-tts`。

**延迟偏高**
设置里把「HLS 切片」调到 0.5、帧率 30、码率适中；zerolatency 已默认开启。

**纯 Wayland 会话**
`ffmpeg` 的 `x11grab` 需要 X11。请在登录界面选择 “Ubuntu on Xorg”，
或改用 XFCE / MATE 的 X11 会话。

## 目录结构

```
screencast/
├── app.py       主控：托盘菜单、投屏生命周期、健康监控
├── tray.py      AppIndicator / StatusIcon 托盘封装
├── sources.py   窗口(Wnck) / 显示器(Gdk) / 区域选择
├── encoder.py   ffmpeg 采集编码，tee 输出 TS + HLS
├── streamer.py  本地 HTTP 服务（TS 广播 + HLS 静态文件）
├── devices.py   Chromecast(pychromecast) / DLNA(SSDP+SOAP)
└── ui.py        设置、日志、输入对话框
```

日志：`~/.cache/screencast-tray/app.log`、`~/.cache/screencast-tray/ffmpeg.log`
配置：`~/.config/screencast-tray/config.json`

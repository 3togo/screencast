"""设备发现与控制：Chromecast（mDNS / pychromecast）与 DLNA/UPnP 渲染器（SSDP）。"""

from __future__ import annotations

import logging
import socket
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, List, Optional
from urllib.parse import urljoin

log = logging.getLogger(__name__)

SSDP_ADDR = ("239.255.255.250", 1900)
RENDERER_ST = "urn:schemas-upnp-org:device:MediaRenderer:1"
AVT = "urn:schemas-upnp-org:service:AVTransport:1"


@dataclass
class Device:
    kind: str  # chromecast | dlna
    name: str
    host: str = ""
    uid: str = ""
    extra: dict = field(default_factory=dict)

    def label(self) -> str:
        tag = "Chromecast" if self.kind == "chromecast" else "DLNA"
        return f"{self.name}  [{tag}]"

    def key(self) -> str:
        return f"{self.kind}:{self.uid or self.name}:{self.host}"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "name": self.name, "uid": self.uid, "host": self.host}

    @staticmethod
    def from_dict(d: dict) -> Optional["Device"]:
        if not isinstance(d, dict) or "kind" not in d:
            return None
        return Device(kind=d["kind"], name=d.get("name", ""), uid=d.get("uid", ""), host=d.get("host", ""))


# ---------------------------------------------------------------- Chromecast
def discover_chromecasts(timeout: float = 6.0) -> List[Device]:
    try:
        import pychromecast  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        log.warning("pychromecast 不可用: %s", exc)
        return []

    devices: List[Device] = []
    browser = None
    try:
        casts, browser = pychromecast.get_chromecasts(timeout=timeout)
        for cast in casts:
            try:
                info = cast.cast_info
                name = getattr(info, "friendly_name", None) or "Chromecast"
                devices.append(
                    Device(
                        kind="chromecast",
                        name=name,
                        host=getattr(info, "host", "") or "",
                        uid=str(getattr(cast, "uuid", "") or getattr(info, "host", "")),
                        extra={"cast": cast},
                    )
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("忽略 Chromecast: %s", exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("Chromecast 发现失败: %s", exc)
    finally:
        if browser is not None:
            try:
                browser.stop_discovery()
            except Exception:  # noqa: BLE001
                pass
    return devices


def add_chromecast_by_host(host: str, timeout: float = 8.0) -> Optional[Device]:
    try:
        import pychromecast  # noqa: PLC0415

        cast = pychromecast.Chromecast(host, timeout=timeout)
        cast.wait(timeout=timeout)
        info = cast.cast_info
        return Device(
            kind="chromecast",
            name=getattr(info, "friendly_name", None) or host,
            host=getattr(info, "host", host) or host,
            uid=str(getattr(cast, "uuid", "") or host),
            extra={"cast": cast},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("连接 %s 失败: %s", host, exc)
        return None


def _get_cast(device: Device):
    cast = device.extra.get("cast")
    if cast is None and device.host:
        dev = add_chromecast_by_host(device.host)
        if dev:
            cast = dev.extra.get("cast")
            device.extra["cast"] = cast
    return cast


def chromecast_play(device: Device, url: str, title: str = "ScreenCast") -> None:
    cast = _get_cast(device)
    if cast is None:
        raise RuntimeError(f"无法连接 Chromecast {device.name}")
    cast.wait(timeout=10)
    mc = cast.media_controller
    mc.play_media(url, content_type="application/vnd.apple.mpegurl", title=title, stream_type="LIVE")
    log.info("已向 Chromecast %s 推送 %s", device.name, url)


def chromecast_stop(device: Device) -> None:
    cast = device.extra.get("cast")
    if cast is None:
        return
    try:
        cast.wait(timeout=5)
        cast.media_controller.stop()
        cast.quit_app()
    except Exception as exc:  # noqa: BLE001
        log.debug("停止 Chromecast 失败: %s", exc)


# --------------------------------------------------------------------- DLNA
def _localname(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def discover_dlna(timeout: float = 4.0) -> List[Device]:
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {int(max(timeout - 1, 1))}\r\n"
        f"ST: {RENDERER_ST}\r\n"
        "\r\n"
    ).encode()

    locations = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(timeout)
        try:
            sock.sendto(msg, SSDP_ADDR)
        except Exception as exc:  # noqa: BLE001
            log.debug("SSDP 发送失败: %s", exc)
            return []
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, _addr = sock.recvfrom(4096)
            except socket.timeout:
                break
            except Exception:  # noqa: BLE001
                break
            text = data.decode("utf-8", "ignore")
            headers = {}
            for line in text.splitlines()[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().upper()] = v.strip()
            loc = headers.get("LOCATION")
            if loc:
                locations.add(loc)
    finally:
        sock.close()

    devices: List[Device] = []
    for loc in locations:
        dev = _describe_renderer(loc)
        if dev:
            devices.append(dev)
    devices.sort(key=lambda d: d.name)
    return devices


def _describe_renderer(location: str, timeout: float = 4.0) -> Optional[Device]:
    try:
        req = urllib.request.Request(location, headers={"User-Agent": "ScreenCastTray/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            xml = resp.read()
        root = ET.fromstring(xml)
    except Exception as exc:  # noqa: BLE001
        log.debug("读取设备描述失败 %s: %s", location, exc)
        return None

    name = None
    control_url = None
    for elem in root.iter():
        tag = _localname(elem.tag)
        if tag == "friendlyName" and not name:
            name = (elem.text or "").strip()
        elif tag == "serviceType" and (elem.text or "").strip() == AVT:
            parent = None
            for svc in root.iter():
                if _localname(svc.tag) == "service" and elem in list(svc):
                    parent = svc
                    break
            container = parent if parent is not None else None
            if container is not None:
                for child in container:
                    if _localname(child.tag) == "controlURL":
                        control_url = urljoin(location, (child.text or "").strip())
    if control_url is None:
        return None
    return Device(
        kind="dlna",
        name=name or location,
        host=urllib.request.urlparse(location).hostname or "",
        uid=location,
        extra={"control_url": control_url, "location": location},
    )


def _soap(control_url: str, action: str, args_xml: str, timeout: float = 6.0) -> None:
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{AVT}">{args_xml}</u:{action}></s:Body></s:Envelope>'
    )
    req = urllib.request.Request(
        control_url,
        data=body.encode("utf-8"),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{AVT}#{action}"',
            "User-Agent": "ScreenCastTray/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:  # 部分电视返回 500 但仍会执行
        log.warning("%s 返回 %s: %s", action, exc.code, exc.read()[:200])
    except Exception as exc:  # noqa: BLE001
        log.warning("%s 失败: %s", action, exc)
        raise


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def dlna_play(device: Device, url: str, mime: str = "video/mpegts", title: str = "ScreenCast") -> None:
    control = device.extra.get("control_url")
    if not control:
        raise RuntimeError(f"DLNA 设备 {device.name} 缺少 AVTransport 控制地址")

    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="screencast" parentID="0" restricted="1">'
        f"<dc:title>{_escape(title)}</dc:title>"
        "<upnp:class>object.item.videoItem</upnp:class>"
        f'<res protocolInfo="http-get:*:{mime}:*">{_escape(url)}</res>'
        "</item></DIDL-Lite>"
    )
    _soap(
        control,
        "SetAVTransportURI",
        "<InstanceID>0</InstanceID>"
        f"<CurrentURI>{_escape(url)}</CurrentURI>"
        f"<CurrentURIMetaData>{_escape(didl)}</CurrentURIMetaData>",
    )
    time.sleep(0.4)
    _soap(control, "Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
    log.info("已向 DLNA %s 推送 %s", device.name, url)


def dlna_stop(device: Device) -> None:
    control = device.extra.get("control_url")
    if not control:
        return
    try:
        _soap(control, "Stop", "<InstanceID>0</InstanceID>", timeout=3)
    except Exception as exc:  # noqa: BLE001
        log.debug("停止 DLNA 失败: %s", exc)


# ------------------------------------------------------------------ 统一入口
def discover_all(timeout_chromecast: float = 6.0, timeout_dlna: float = 4.0) -> List[Device]:
    """并发发现两类设备，返回排序后的列表（Chromecast 在前）。"""
    result: List[Device] = []
    lock = threading.Lock()

    def worker(fn, *args):
        try:
            found = fn(*args)
        except Exception as exc:  # noqa: BLE001
            log.debug("发现任务异常: %s", exc)
            found = []
        with lock:
            result.extend(found)

    threads = [
        threading.Thread(target=worker, args=(discover_chromecasts, timeout_chromecast), daemon=True),
        threading.Thread(target=worker, args=(discover_dlna, timeout_dlna), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=max(timeout_chromecast, timeout_dlna) + 6)

    result.sort(key=lambda d: (d.kind != "chromecast", d.name.lower()))
    return result


def play(device: Device, url: str, mime: str = "video/mpegts", title: str = "ScreenCast") -> None:
    if device.kind == "chromecast":
        chromecast_play(device, url, title)
    elif device.kind == "dlna":
        dlna_play(device, url, mime, title)
    else:
        raise RuntimeError(f"未知设备类型: {device.kind}")


def stop(device: Device) -> None:
    try:
        if device.kind == "chromecast":
            chromecast_stop(device)
        elif device.kind == "dlna":
            dlna_stop(device)
    except Exception as exc:  # noqa: BLE001
        log.debug("停止设备失败: %s", exc)


def discover_async(on_done: Callable[[List[Device]], None], timeout_dlna: float = 4.0) -> threading.Thread:
    def runner():
        found = discover_all(timeout_dlna=timeout_dlna)
        try:
            on_done(found)
        except Exception as exc:  # noqa: BLE001
            log.debug("设备回调异常: %s", exc)

    t = threading.Thread(target=runner, name="discover", daemon=True)
    t.start()
    return t

#!/usr/bin/env python3
"""
Bambu Lab X2D -> OBS browser-source overlay server.

Pure Python standard library (3.10+), no dependencies.

READ-ONLY BY DESIGN -- nothing in this file can make the printer do anything:

  * MQTT (TLS 8883): the client only ever sends CONNECT, SUBSCRIBE (to
    device/<serial>/report), PINGREQ and DISCONNECT.  There is no PUBLISH code
    path at all, so no command (not even "pushall") can be sent.  The X2D pushes
    a full status report every ~1 s on its own.
  * File channel (TLS 6000, the protocol Bambu Studio's file browser uses):
    only MEDIA_ABILITY (7), LIST_INFO (1) and FILE_DOWNLOAD (4) requests are
    allowed.  Anything else (delete, upload, ...) raises before a byte is sent.
    Used once per job to fetch the sliced .3mf for the plate preview + metadata.
  * SSDP (UDP 2021): passive listening only, to follow the printer if its DHCP
    address changes.
  * Credentials are only sent after the printer's TLS certificate CN matches the
    expected serial number, so a different device that grabbed the old IP never
    receives the access code.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import math
import os
import random
import re
import select
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
CACHE_DIR = ROOT / "cache"
MEDIA_DIR = CACHE_DIR / "media"
SETTINGS_FILE = CACHE_DIR / "settings.json"
JOBS_FILE = CACHE_DIR / "jobs.json"

log = logging.getLogger("overlay")

# --------------------------------------------------------------------------- config

LAYOUTS = ["glass", "ticker", "hud", "sidebar", "orb", "minimal", "none"]
DEFAULT_SETTINGS = {
    "layout": "glass",
    "accent": "auto",      # "auto" = colour of the filament being printed, or "#rrggbb"
    "position": "auto",    # corner for glass / orb / minimal: auto tl tr bl br
    "side": "left",        # sidebar side: left | right
    "scale": 1.0,          # widget scale multiplier
    "idle": "show",        # what to do with no job: show | hide
    "name": "file",        # job name: file (printer's task name) | title (model title from the 3mf)
    "ams": True,           # show AMS / filament slots
    "credit": False,       # show designer + MakerWorld link
    "build": True,         # lower-third model preview fills up layer by layer
    "meter": True,         # Winamp-style L/R audio meter fed by OBS InputVolumeMeters
    "meterInput": "",      # OBS input to meter ("" = loudest of all inputs)
    "demo": False,         # simulate a print (for setting things up)
    # OBS camera crop automation (obs-websocket)
    "obsCrop": True,       # animate the camera crop in OBS when printing starts / stops
    "cropMode": "auto",    # auto (follow printer) | printing | idle (force one, e.g. while adjusting)
    "cropPrint": [80, 280],    # [top, bottom] crop while printing
    "cropIdle": [180, 180],    # [top, bottom] crop while idle
    "cropDuration": 2.0,   # seconds for the smooth transition
}
_SETTING_RULES = {
    "layout": lambda v: v in LAYOUTS,
    "accent": lambda v: v == "auto" or bool(re.fullmatch(r"#[0-9a-fA-F]{6}", str(v))),
    "position": lambda v: v in ("auto", "tl", "tr", "bl", "br"),
    "side": lambda v: v in ("left", "right"),
    "scale": lambda v: isinstance(v, (int, float)) and 0.5 <= v <= 2.0,
    "idle": lambda v: v in ("show", "hide"),
    "name": lambda v: v in ("title", "file"),
    "ams": lambda v: isinstance(v, bool),
    "credit": lambda v: isinstance(v, bool),
    "build": lambda v: isinstance(v, bool),
    "meter": lambda v: isinstance(v, bool),
    "meterInput": lambda v: isinstance(v, str) and len(v) <= 256,
    "demo": lambda v: isinstance(v, bool),
    "obsCrop": lambda v: isinstance(v, bool),
    "cropMode": lambda v: v in ("auto", "printing", "idle"),
    "cropPrint": lambda v: _crop_pair(v),
    "cropIdle": lambda v: _crop_pair(v),
    "cropDuration": lambda v: isinstance(v, (int, float)) and 0 <= v <= 10,
}


def _crop_pair(v) -> bool:
    return (isinstance(v, list) and len(v) == 2
            and all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 4000 for x in v))


def load_env(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip().removeprefix("export ").strip()] = value.strip().strip('"').strip("'")
    return out


class Config:
    def __init__(self):
        env = load_env(ROOT / ".env")
        env.update({k: v for k, v in os.environ.items() if k.startswith(("BAMBU_", "OVERLAY_", "OBS_"))})
        self.ip = env.get("BAMBU_X2D_IP") or env.get("BAMBU_IP", "")
        self.access_code = (env.get("BAMBU_X2D_ACCESS_KEY") or env.get("BAMBU_X2D_ACCESS_CODE")
                            or env.get("BAMBU_ACCESS_CODE", ""))
        self.serial = (env.get("BAMBU_X2D_SERIAL") or env.get("BAMBU_SERIAL", "")).strip().upper()
        self.host = env.get("OVERLAY_HOST", "0.0.0.0")
        self.port = int(env.get("OVERLAY_PORT", "8787"))
        # OBS: URL/password default to OBS's own obs-websocket config (read at connect time, never logged)
        self.obs_url = env.get("OBS_WS_URL", "")
        self.obs_password = env.get("OBS_WS_PASSWORD", "")
        self.obs_source = env.get("OBS_SOURCE", "X2D")
        if not self.ip or not self.access_code:
            sys.exit("Missing BAMBU_X2D_IP / BAMBU_X2D_ACCESS_KEY in .env")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- TLS helpers

def _tls_context() -> ssl.SSLContext:
    # The printer presents a certificate signed by Bambu's private CA; instead of
    # shipping that CA we pin the certificate's CN to the printer serial number.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _cert_subject_cn(der: bytes) -> str:
    """Extract the subject CN from a DER certificate (issuer CN comes first, subject second)."""
    oid = b"\x06\x03\x55\x04\x03"
    found, start = [], 0
    while (idx := der.find(oid, start)) >= 0:
        p = idx + len(oid)
        if p + 2 <= len(der) and der[p] in (0x0C, 0x13, 0x16) and der[p + 1] < 0x80:
            found.append(der[p + 2:p + 2 + der[p + 1]].decode("utf-8", "replace"))
        start = idx + 1
    return found[1] if len(found) >= 2 else (found[0] if found else "")


def open_printer_tls(ip: str, port: int, expected_serial: str, timeout: float) -> tuple[ssl.SSLSocket, str]:
    raw = socket.create_connection((ip, port), timeout=timeout)
    try:
        sock = _tls_context().wrap_socket(raw)
    except Exception:
        raw.close()
        raise
    cn = _cert_subject_cn(sock.getpeercert(binary_form=True) or b"").strip().upper()
    if expected_serial and cn and cn != expected_serial:
        sock.close()
        raise PermissionError(f"device at {ip} identifies as {cn}, expected {expected_serial}; "
                              "refusing to send credentials")
    return sock, cn


# --------------------------------------------------------------------------- MQTT (subscribe-only)

def hub_ip(hub) -> str:
    return hub.ip

class ReadOnlyMqtt(threading.Thread):
    """Minimal MQTT 3.1.1 client that can only subscribe. Deliberately has no publish()."""

    _ALLOWED_OUT = {0x1: "CONNECT", 0x8: "SUBSCRIBE", 0xC: "PINGREQ", 0xE: "DISCONNECT"}
    KEEPALIVE = 30

    def __init__(self, hub: "Hub"):
        super().__init__(name="mqtt", daemon=True)
        self.hub = hub
        self.sock: ssl.SSLSocket | None = None
        self.lan_hint_shown = False

    @staticmethod
    def _str(s: str) -> bytes:
        b = s.encode()
        return struct.pack("!H", len(b)) + b

    @staticmethod
    def _varlen(n: int) -> bytes:
        out = bytearray()
        while True:
            n, d = divmod(n, 128)
            out.append(d | (0x80 if n else 0))
            if not n:
                return bytes(out)

    def _send(self, header: int, body: bytes = b"") -> None:
        if header >> 4 not in self._ALLOWED_OUT:  # hard guarantee: nothing but the 4 packets above
            raise PermissionError(f"refusing to send MQTT packet type {header >> 4}")
        self.sock.sendall(bytes([header]) + self._varlen(len(body)) + body)

    def run(self):
        backoff = 2
        while True:
            try:
                self._session()
                backoff = 2
            except PermissionError as e:
                log.error("MQTT: %s", e)
                backoff = 30
            except Exception as e:
                log.warning("MQTT: %s: %s", type(e).__name__, e)
                if sys.platform == "darwin" and getattr(e, "errno", None) == 65 and not self.lan_hint_shown:
                    self.lan_hint_shown = True
                    log.warning("MQTT: if `ping %s` works, macOS is blocking local network access. Start the server "
                                "with 'Bambu Overlay.app' (tools/build-app.sh) and allow it in System Settings > "
                                "Privacy & Security > Local Network", hub_ip(self.hub))
            finally:
                if self.sock:
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None
                self.hub.set_mqtt(False, "disconnected")
            # wait for backoff, or wake early when SSDP tells us the IP changed
            if self.hub.ip_changed.wait(backoff):
                self.hub.ip_changed.clear()
                backoff = 1
            backoff = min(backoff * 2, 30)

    def _session(self):
        hub = self.hub
        ip = hub.ip
        self.hub.set_mqtt(False, f"connecting to {ip}")
        self.sock, cn = open_printer_tls(ip, 8883, hub.serial, timeout=10)
        if not hub.serial:
            if not cn:
                raise RuntimeError("could not read printer serial from certificate; set BAMBU_X2D_SERIAL")
            hub.set_serial(cn)
        client_id = "obs-overlay-ro-%06x" % random.getrandbits(24)
        body = (self._str("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", self.KEEPALIVE)
                + self._str(client_id) + self._str("bblp") + self._str(hub.cfg.access_code))
        self._send(0x10, body)

        buf = bytearray()
        self.sock.settimeout(10)
        packets = self._read_packets(buf, wait_for=0x2)
        hdr, payload = packets[0]
        if len(payload) < 2 or payload[1] != 0:
            raise PermissionError(f"printer rejected MQTT login (code {payload[1] if len(payload) > 1 else '?'})"
                                  " - check BAMBU_X2D_ACCESS_KEY")

        topic = f"device/{hub.serial}/report"
        self._send(0x82, struct.pack("!H", 1) + self._str(topic) + b"\x00")  # QoS 0
        log.info("MQTT: connected to %s, subscribed to %s (read-only)", ip, topic)
        hub.set_mqtt(True, "connected")

        self.sock.settimeout(1.0)
        last_rx = last_ping = time.monotonic()
        for hdr, payload in packets[1:]:
            self._handle(hdr, payload)
        while True:
            if hub.ip_changed.is_set():
                log.info("MQTT: printer IP changed, reconnecting")
                return
            try:
                data = self.sock.recv(65536)
                if not data:
                    raise ConnectionError("connection closed by printer")
                buf += data
                last_rx = time.monotonic()
                for hdr, payload in self._parse(buf):
                    self._handle(hdr, payload)
            except (socket.timeout, TimeoutError):
                pass
            now = time.monotonic()
            if now - last_ping > self.KEEPALIVE / 2:
                self._send(0xC0)
                last_ping = now
            if now - last_rx > self.KEEPALIVE * 2:
                raise TimeoutError("no data from printer for %ds" % (self.KEEPALIVE * 2))

    def _read_packets(self, buf: bytearray, wait_for: int):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionError("connection closed during handshake")
            buf += data
            pkts = self._parse(buf)
            for i, (hdr, _) in enumerate(pkts):
                if hdr >> 4 == wait_for:
                    return pkts[i:]
        raise TimeoutError("MQTT handshake timed out")

    @staticmethod
    def _parse(buf: bytearray):
        out = []
        while len(buf) >= 2:
            mul, length, i = 1, 0, 1
            while True:
                if i >= len(buf):
                    return out
                d = buf[i]
                length += (d & 0x7F) * mul
                mul *= 128
                i += 1
                if not d & 0x80:
                    break
                if i > 4:
                    raise ValueError("malformed MQTT length")
            if len(buf) < i + length:
                break
            out.append((buf[0], bytes(buf[i:i + length])))
            del buf[:i + length]
        return out

    def _handle(self, hdr: int, payload: bytes):
        ptype = hdr >> 4
        if ptype == 3:  # PUBLISH (QoS 0: no packet id, nothing to acknowledge)
            tlen = struct.unpack("!H", payload[:2])[0]
            body = payload[2 + tlen + (2 if hdr & 0x06 else 0):]
            try:
                msg = json.loads(body)
            except ValueError:
                return
            self.hub.on_report(msg)
        elif ptype == 9 and payload[-1:] == b"\x80":
            raise PermissionError("printer refused the subscription")


# --------------------------------------------------------------------------- file channel (read-only)

class ReadOnlyFileChannel:
    """Bambu file-browser protocol on TCP 6000 (same as Bambu Studio), restricted to reads."""

    MAGIC_LOGIN = 0x0101013F
    MAGIC_CTRL = 0x0102013F
    ALLOWED_CMDS = {1: "LIST_INFO", 4: "FILE_DOWNLOAD", 7: "MEDIA_ABILITY"}

    def __init__(self, ip: str, serial: str, access_code: str):
        self.ip, self.serial, self.code = ip, serial, access_code
        self.sock: ssl.SSLSocket | None = None
        self.frame_seq = random.randint(1, 0x7FFFFFFF)
        self.cmd_seq = 1

    def __enter__(self):
        self.sock, _ = open_printer_tls(self.ip, 6000, self.serial, timeout=8)
        self.sock.settimeout(20)
        self._frame(self.MAGIC_LOGIN, b"bblp".ljust(8, b"\0") + self.code.encode()[:8].ljust(8, b"\0"))
        self._read_frame()
        setup = {"sequence": 0, "mtype": 12291,
                 "req": {"t_av": 1, "mtype": 12289, "peer_t": 3, "pid": "%08x" % self.frame_seq,
                         "ver": "02.03.00.00"}}
        self._frame(self.MAGIC_CTRL, json.dumps(setup, separators=(",", ":")).encode())
        reply, _ = self._split(self._read_frame())
        if not reply or reply.get("result") != 0:
            raise ConnectionError(f"file channel setup failed: {reply}")
        return self

    def __exit__(self, *exc):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

    def _frame(self, magic: int, payload: bytes):
        self.sock.sendall(struct.pack("<IIII", len(payload), magic, self.frame_seq, 0) + payload)
        self.frame_seq += 1

    def _rx(self, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError("file channel closed by printer")
            out += chunk
        return bytes(out)

    def _read_frame(self) -> bytes:
        length, _magic, _seq, _ = struct.unpack("<IIII", self._rx(16))
        return self._rx(length) if length else b""

    @staticmethod
    def _split(data: bytes):
        if not data.startswith(b"{"):
            return None, data
        depth, in_str, esc = 0, False, False
        for i, b in enumerate(data):
            if in_str:
                if esc:
                    esc = False
                elif b == 0x5C:
                    esc = True
                elif b == 0x22:
                    in_str = False
            elif b == 0x22:
                in_str = True
            elif b == 0x7B:
                depth += 1
            elif b == 0x7D:
                depth -= 1
                if depth == 0:
                    rest = data[i + 1:]
                    if rest.startswith(b"\n\n"):
                        rest = rest[2:]
                    elif rest.startswith(b"\r\n\r\n"):
                        rest = rest[4:]
                    try:
                        return json.loads(data[:i + 1]), rest
                    except ValueError:
                        return None, data
        return None, data

    def _request(self, cmdtype: int, req: dict) -> int:
        if cmdtype not in self.ALLOWED_CMDS:  # hard guarantee: read-only commands only
            raise PermissionError(f"file channel command {cmdtype} is not allowed (read-only)")
        seq = self.cmd_seq
        self.cmd_seq += 1
        body = {"mtype": 12289, "cmdtype": cmdtype, "sequence": seq, "req": req}
        self._frame(self.MAGIC_CTRL, json.dumps(body, separators=(",", ":")).encode())
        return seq

    def _reply(self, cmdtype: int, seq: int, timeout: float = 20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            reply, binary = self._split(self._read_frame())
            if reply and reply.get("sequence") == seq and reply.get("cmdtype", cmdtype) == cmdtype:
                return reply, binary
        raise TimeoutError(f"file channel: no reply to cmd {cmdtype}")

    def storages(self) -> list[str]:
        reply, _ = self._reply(7, self._request(7, {"peer": "studio", "api_version": 2}))
        body = reply.get("reply") or {}
        return [str(s) for s in body.get("storage") or []] or ["emmc"]

    def list_models(self, storage: str) -> list[dict]:
        seq = self._request(1, {"type": "model", "api_version": 2, "notify": "DETAIL", "storage": storage})
        reply, _ = self._reply(1, seq)
        if reply.get("result") != 0:
            return []
        return [f for f in (reply.get("reply") or {}).get("file_lists") or [] if isinstance(f, dict)]

    def download(self, path: str, fh, max_bytes: int = 512 * 1024 * 1024) -> int:
        seq = self._request(4, {"path": path, "offset": 0})
        md5 = None
        total = 0
        written = 0
        while True:
            reply, chunk = self._reply(4, seq, timeout=60)
            body = reply.get("reply") or {}
            result = reply.get("result")
            if result not in (0, 1):
                raise FileNotFoundError(f"{path}: result {result}")
            total = int(body.get("total") or total or 0)
            if total > max_bytes:
                raise ValueError(f"{path} is too large ({total} bytes)")
            if chunk and not body.get("mem_dl_param_size"):
                fh.seek(int(body.get("offset", written)))
                fh.write(chunk)
                written += len(chunk)
            if result == 0:
                md5 = str(body.get("file_md5") or "").lower()
                break
        fh.flush()
        if total and written != total:
            raise IOError(f"{path}: got {written} of {total} bytes")
        if md5:
            fh.seek(0)
            h = hashlib.md5()
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
            if h.hexdigest() != md5:
                raise IOError(f"{path}: checksum mismatch")
        return written


# --------------------------------------------------------------------------- 3mf parsing

BED_TYPES = {"textured_plate": "Textured PEI", "cool_plate": "Cool Plate", "eng_plate": "Engineering Plate",
             "hot_plate": "High Temp Plate", "supertack_plate": "SuperTack Plate", "pc_plate": "PC Plate"}


class StaleModelFile(Exception):
    """The printer's .3mf with the job's name is not (yet) the one this job prints."""


def parse_3mf(path: Path, plate: int | None) -> tuple[dict, bytes | None]:
    meta: dict = {"filaments": []}
    with zipfile.ZipFile(path) as z:
        names = {n.lower(): n for n in z.namelist()}

        def read(name):
            real = names.get(name.lower())
            return z.read(real) if real else None

        # A printed .3mf holds the gcode of the plate(s) it was sliced for. A different plate means it is an
        # older print's file with the same name (the printer saves the new copy a few seconds into the job).
        sliced = {int(m.group(1)) for n in names if (m := re.fullmatch(r"metadata/plate_(\d+)\.gcode", n))}
        if plate is None:
            plate = min(sliced, default=1)
        if sliced and plate not in sliced:
            raise StaleModelFile(f"file has plate {', '.join(map(str, sorted(sliced)))}, job prints plate {plate}")
        meta["plate"] = plate
        cover = read(f"Metadata/plate_{plate}.png")
        if (si := read("Metadata/slice_info.config")):
            meta.update(parse_slice_info(si, plate))
        if (pj := read(f"Metadata/plate_{plate}.json")):
            try:
                bed = json.loads(pj).get("bed_type") or ""
                meta["bedType"] = BED_TYPES.get(bed, bed.replace("_", " ").title())
            except ValueError:
                pass
        if (model := read("3D/3dmodel.model")):
            head = model[:200_000].decode("utf-8", "replace")
            for key, value in re.findall(r'<metadata name="([^"]+)">([^<]*)</metadata>', head):
                if key in ("Title", "Designer", "License", "ProfileTitle"):
                    meta[key[0].lower() + key[1:]] = html.unescape(value).strip()
    return meta, cover


def parse_slice_info(data: bytes, plate: int) -> dict:
    out: dict = {"filaments": []}
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return out
    plates = root.findall("plate")
    node = next((p for p in plates if p.find(f"metadata[@key='index'][@value='{plate}']") is not None),
                plates[0] if len(plates) == 1 and plates[0].find("metadata[@key='index']") is None else None)
    if node is None:
        return out
    md = {m.get("key"): m.get("value") for m in node.findall("metadata")}
    try:
        out["predictionSec"] = int(float(md.get("prediction") or 0))
        out["weightG"] = round(float(md.get("weight") or 0), 1)
    except ValueError:
        pass
    out["objects"] = len(node.findall("object"))
    for f in node.findall("filament"):
        try:
            out["filaments"].append({
                "id": int(f.get("id")), "type": f.get("type") or "", "color": (f.get("color") or "")[:7].upper(),
                "usedG": round(float(f.get("used_g") or 0), 1), "usedM": round(float(f.get("used_m") or 0), 2),
            })
        except (TypeError, ValueError):
            continue
    return out


# --------------------------------------------------------------------------- media worker

class MediaWorker(threading.Thread):
    """Fetches the active job's .3mf once (read-only) for the cover image + slice metadata."""

    def __init__(self, hub: "Hub"):
        super().__init__(name="media", daemon=True)
        self.hub = hub
        self.attempts: dict[str, tuple[int, float]] = {}

    def run(self):
        while True:
            self.hub.media_wakeup.wait(5)
            self.hub.media_wakeup.clear()
            want = self.hub.wanted_media()
            if not want or self.hub.media.get("key") == want["key"]:
                continue
            if self._load_cached(want):
                continue
            count, not_before = self.attempts.get(want["key"], (0, 0))
            if time.monotonic() < not_before or count >= 25:
                continue
            try:
                self.hub.set_media_status(f"fetching model for {want['name']}")
                self._fetch(want)
                self.attempts.pop(want["key"], None)
            except Exception as e:
                delay = 15 if want["state"] == "PREPARE" or count < 4 else 120
                self.attempts[want["key"]] = (count + 1, time.monotonic() + delay)
                log.info("Media: %s (retry in %ds)", e, delay)
                self.hub.set_media_status(f"model not available yet: {e}")

    def _dir(self, key: str) -> Path:
        return MEDIA_DIR / hashlib.sha1(key.encode()).hexdigest()[:16]

    def _load_cached(self, want) -> bool:
        d = self._dir(want["key"])
        meta = load_json(d / "meta.json", None)
        if not meta or meta.get("v") != MEDIA_CACHE_V:
            return False
        cover = (d / "cover.png").read_bytes() if (d / "cover.png").exists() else None
        self.hub.set_media(want["key"], meta, cover)
        return True

    def _fetch(self, want):
        hub = self.hub
        with ReadOnlyFileChannel(hub.ip, hub.serial, hub.cfg.access_code) as fc:
            match = None
            for storage in fc.storages():
                files = fc.list_models(storage)
                match = pick_model_file(files, want["name"], want["gcode_file"])
                if match:
                    break
            if not match:
                raise FileNotFoundError(f"no .3mf matching '{want['name']}' on printer storage")
            if (reason := stale_file_reason(match, want, time.time())):
                raise StaleModelFile(reason)
            log.info("Media: downloading %s (%.1f MB, read-only)", match["path"], (match.get("size") or 0) / 1e6)
            with tempfile.NamedTemporaryFile(prefix="job-", suffix=".3mf", dir=CACHE_DIR, delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                with open(tmp_path, "w+b") as fh:
                    fc.download(match["path"], fh)
                meta, cover = parse_3mf(tmp_path, want["plate"])
            finally:
                tmp_path.unlink(missing_ok=True)
        meta["file"] = match.get("name")
        meta["fileTime"] = match.get("time")
        meta["v"] = MEDIA_CACHE_V
        d = self._dir(want["key"])
        d.mkdir(parents=True, exist_ok=True)
        if cover:
            (d / "cover.png").write_bytes(cover)
        save_json(d / "meta.json", meta)
        hub.set_media(want["key"], meta, cover)
        log.info("Media: ready - %s by %s, %s g, cover %s", meta.get("title") or want["name"],
                 meta.get("designer") or "?", meta.get("weightG"), "yes" if cover else "no")


MEDIA_CACHE_V = 2        # bump to ignore cached job media written by older versions (v1 could hold a stale plate)
FRESH_FILE_SLACK = 120   # s: allowed difference between the printer's clock and ours
FRESH_FILE_GRACE = 180   # s: after this, accept an older file (a re-print from the printer's history keeps it)


def stale_file_reason(match: dict, want: dict, now: float) -> str | None:
    """The printer saves a job's .3mf a few seconds after the job starts, so right at the start a same-named
    file from an earlier print may still be there. Wait for the new copy while the job is young."""
    start, file_time = want.get("start"), _int(match.get("time"))
    if want.get("src") != "observed" or not start or not file_time or now - start > FRESH_FILE_GRACE:
        return None
    if file_time < start - FRESH_FILE_SLACK:
        return f"{match.get('name')} is from an earlier print, waiting for the printer to save this job's copy"
    return None


def job_plate(p: dict) -> int | None:
    """Plate number of the running job: plate_idx, else the N in gcode_file '.../plate_N.gcode'."""
    return (_int(p.get("plate_idx")) or _int((re.findall(r"plate_(\d+)", str(p.get("gcode_file") or "")) or [0])[0])
            or None)


def _norm_name(s: str) -> str:
    s = s.lower().replace("/", "2f")
    s = re.sub(r"(\.gcode)?\.3mf$", "", s)
    return re.sub(r"[^0-9a-z]+", "", s)


def pick_model_file(files: list[dict], task_name: str, gcode_file: str) -> dict | None:
    wanted = {_norm_name(task_name)} if task_name else set()
    base = (gcode_file or "").rsplit("/", 1)[-1]
    if base.lower().endswith(".3mf"):
        wanted.add(_norm_name(base))
    wanted.discard("")
    hits = [f for f in files if str(f.get("name", "")).lower().endswith(".3mf")
            and _norm_name(str(f.get("name"))) in wanted]
    if gcode_file and gcode_file.lower().endswith(".3mf"):
        hits += [f for f in files if f.get("path") == gcode_file]
    return max(hits, key=lambda f: f.get("time") or 0) if hits else None


# --------------------------------------------------------------------------- SSDP (passive)

class SsdpWatcher(threading.Thread):
    """Listens for the printer's own SSDP broadcasts to follow DHCP address changes."""

    def __init__(self, hub: "Hub"):
        super().__init__(name="ssdp", daemon=True)
        self.hub = hub

    def run(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            s.bind(("", 2021))
        except OSError as e:
            log.warning("SSDP: cannot listen on UDP 2021 (%s); IP auto-follow disabled", e)
            return
        while True:
            try:
                data, addr = s.recvfrom(4096)
            except OSError:
                time.sleep(1)
                continue
            headers = {}
            for line in data.decode("utf-8", "replace").split("\r\n")[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            location = headers.get("location") or addr[0]
            if "bambulab" not in headers.get("nt", "") or not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", location):
                continue
            self.hub.on_ssdp(headers.get("usn", "").upper(), location, headers)


# --------------------------------------------------------------------------- OBS camera crop (obs-websocket v5)

OBS_CONFIG_PATHS = [
    Path.home() / "Library/Application Support/obs-studio/plugin_config/obs-websocket/config.json",  # macOS
    Path.home() / ".config/obs-studio/plugin_config/obs-websocket/config.json",                      # Linux
    Path(os.environ.get("APPDATA", "~")) / "obs-studio/plugin_config/obs-websocket/config.json",     # Windows
]


def obs_connection_params(cfg: Config) -> tuple[str, str]:
    """URL + password from .env, falling back to OBS's own obs-websocket config (never logged)."""
    url, password = cfg.obs_url, cfg.obs_password
    if not url or not password:
        for path in OBS_CONFIG_PATHS:
            conf = load_json(path, None)
            if isinstance(conf, dict):
                url = url or f"ws://127.0.0.1:{conf.get('server_port', 4455)}"
                if not password and conf.get("auth_required", True):
                    password = conf.get("server_password") or ""
                break
    return url or "ws://127.0.0.1:4455", password


class WebSocketClosed(ConnectionError):
    def __init__(self, code: int, reason: str = ""):
        super().__init__(f"websocket closed ({code}) {reason}".strip())
        self.code = code


class MiniWebSocket:
    """Just enough RFC 6455 client for obs-websocket: text frames, ping/pong, close."""

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, url: str, protocol: str | None = None, timeout: float = 5):
        u = urlparse(url)
        if u.scheme != "ws":
            raise ValueError("only ws:// URLs are supported")
        host, port = u.hostname or "127.0.0.1", u.port or 80
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = bytearray()
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [f"GET {u.path or '/'} HTTP/1.1", f"Host: {host}:{port}", "Upgrade: websocket",
                 "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
        if protocol:
            lines.append(f"Sec-WebSocket-Protocol: {protocol}")
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake: connection closed")
            self.buf += chunk
        head, _, rest = bytes(self.buf).partition(b"\r\n\r\n")
        self.buf = bytearray(rest)
        status_line = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            raise ConnectionError(f"websocket handshake failed: {status_line!r}")
        accept = base64.b64encode(hashlib.sha1((key + self.GUID).encode()).digest())
        if accept not in head:
            raise ConnectionError("websocket handshake: bad accept key")

    def _send_frame(self, opcode: int, payload: bytes):
        n = len(payload)
        header = bytearray([0x80 | opcode])
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        self.sock.sendall(bytes(header) + mask + bytes(b ^ mask[i & 3] for i, b in enumerate(payload)))

    def send_text(self, text: str):
        self._send_frame(0x1, text.encode())

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed by peer")
            self.buf += chunk
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def recv_text(self) -> str:
        parts = []
        while True:
            b1, b2 = self._read(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
            opcode = b1 & 0x0F
            if opcode == 0x8:
                raise WebSocketClosed(struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 1005,
                                      data[2:].decode("utf-8", "replace"))
            if opcode == 0x9:
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            parts.append(data)
            if b1 & 0x80:
                return b"".join(parts).decode("utf-8", "replace")

    def wait_readable(self, timeout: float) -> bool:
        if self.buf:
            return True
        return bool(select.select([self.sock], [], [], timeout)[0])

    def close(self):
        try:
            self._send_frame(0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        self.sock.close()


class ObsRequestError(RuntimeError):
    def __init__(self, code, comment):
        super().__init__(f"OBS request failed ({code}): {comment}")
        self.code = code


class ObsClient:
    EVENT_INPUTS = 1 << 3                 # InputMuteStateChanged, InputNameChanged, InputRemoved, ...
    EVENT_INPUT_VOLUME_METERS = 1 << 16

    def __init__(self, url: str, password: str, events: int = 0):
        self.ws = MiniWebSocket(url, protocol="obswebsocket.json")
        self.n = 0
        try:
            hello = json.loads(self.ws.recv_text()).get("d", {})
            ident = {"rpcVersion": 1, "eventSubscriptions": events}
            if (auth := hello.get("authentication")):
                if not password:
                    raise PermissionError("OBS WebSocket needs a password: set OBS_WS_PASSWORD in .env")
                secret = base64.b64encode(hashlib.sha256((password + auth["salt"]).encode()).digest()).decode()
                ident["authentication"] = base64.b64encode(
                    hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
            self.ws.send_text(json.dumps({"op": 1, "d": ident}))
            if json.loads(self.ws.recv_text()).get("op") != 2:
                raise ConnectionError("unexpected reply to OBS Identify")
        except WebSocketClosed as e:
            self.ws.close()
            if e.code == 4009:
                raise PermissionError("OBS WebSocket rejected the password") from None
            raise
        except Exception:
            self.ws.close()
            raise

    def call(self, request_type: str, data: dict | None = None, on_event=None) -> dict:
        self.n += 1
        rid = str(self.n)
        self.ws.send_text(json.dumps({"op": 6, "d": {"requestType": request_type, "requestId": rid,
                                                      "requestData": data or {}}}))
        while True:
            msg = json.loads(self.ws.recv_text())
            d = msg.get("d") or {}
            if msg.get("op") == 5 and on_event:   # events arriving while we wait for the response
                on_event(d)
            if msg.get("op") == 7 and d.get("requestId") == rid:
                status = d.get("requestStatus") or {}
                if not status.get("result"):
                    raise ObsRequestError(status.get("code"), status.get("comment"))
                return d.get("responseData") or {}

    def close(self):
        self.ws.close()


def _ease_in_out(k: float) -> float:
    return 4 * k * k * k if k < 0.5 else 1 - (-2 * k + 2) ** 3 / 2


class ObsCropController(threading.Thread):
    """Smoothly pans the camera crop in OBS: one framing while printing, another while idle.

    Only touches cropTop / cropBottom of scene items whose source is OBS_SOURCE; the rest of the
    transform (left/right crop, position, bounds) is left exactly as configured in OBS.
    """

    FRAME_SLEEP = 0.005

    def __init__(self, hub: "Hub"):
        super().__init__(name="obs", daemon=True)
        self.hub = hub
        self.last_msg = None

    def _status(self, msg: str, level=logging.INFO, **extra):
        if msg != self.last_msg:
            log.log(level, "OBS: %s", msg)
            self.last_msg = msg
        self.hub.set_obs(status=msg, **extra)

    def run(self):
        while True:
            if not self.hub.settings.get("obsCrop"):
                self._status("crop automation off", items=0)
                time.sleep(2)
                continue
            try:
                self._session()
            except PermissionError as e:
                self._status(str(e), logging.WARNING, items=0)
                time.sleep(15)
            except (ConnectionRefusedError, TimeoutError, socket.timeout):
                self._status("not reachable (is OBS running with Tools > WebSocket Server enabled?)", items=0)
                time.sleep(5)
            except Exception as e:
                self._status(f"{type(e).__name__}: {e}", logging.WARNING, items=0)
                time.sleep(5)

    def _find_items(self, obs: ObsClient) -> list[tuple[str, int]]:
        src = self.hub.cfg.obs_source
        found = []
        for scene in obs.call("GetSceneList").get("scenes", []):
            name = scene.get("sceneName")
            for it in obs.call("GetSceneItemList", {"sceneName": name}).get("sceneItems", []):
                if it.get("sourceName") == src:
                    found.append((name, it["sceneItemId"]))
                elif it.get("isGroup"):
                    group = it.get("sourceName")
                    for g in obs.call("GetGroupSceneItemList", {"sceneName": group}).get("sceneItems", []):
                        if g.get("sourceName") == src:
                            found.append((group, g["sceneItemId"]))
        return found

    @staticmethod
    def _read_crop(obs: ObsClient, item) -> list[int]:
        t = obs.call("GetSceneItemTransform", {"sceneName": item[0], "sceneItemId": item[1]})["sceneItemTransform"]
        return [int(t.get("cropTop", 0)), int(t.get("cropBottom", 0))]

    def _session(self):
        url, password = obs_connection_params(self.hub.cfg)
        obs = ObsClient(url, password)
        try:
            items = self._find_items(obs)
            if not items:
                self._status(f"source '{self.hub.cfg.obs_source}' not found in any scene (set OBS_SOURCE)", items=0)
                time.sleep(10)
                return
            cur = self._read_crop(obs, items[0])
            self._status(f"connected, controlling '{self.hub.cfg.obs_source}' in {len(items)} scene item(s)",
                         items=len(items), crop=cur)
            goal = start = None
            t0 = 0.0
            sent = cur
            next_scan = time.monotonic() + 15
            while self.hub.settings.get("obsCrop"):
                want = self.hub.crop_target()
                animating = goal is not None and sent != goal
                if want and want[1] != goal:
                    if not animating:
                        cur = self._read_crop(obs, items[0])  # respect manual tweaks made in OBS meanwhile
                        sent = cur
                    goal, start, t0 = want[1], list(sent), time.monotonic()
                    self.hub.set_obs(target=want[0])
                    if start != goal:
                        log.info("OBS: crop -> %s (top %d, bottom %d)", want[0], *goal)
                    animating = start != goal
                if animating:
                    dur = float(self.hub.settings.get("cropDuration") or 0)
                    k = 1.0 if dur <= 0 else min(1.0, (time.monotonic() - t0) / dur)
                    e = _ease_in_out(k)
                    val = [round(start[i] + (goal[i] - start[i]) * e) for i in range(2)]
                    if k >= 1:
                        val = list(goal)
                    if val != sent:
                        try:
                            for scene, item_id in items:
                                obs.call("SetSceneItemTransform", {"sceneName": scene, "sceneItemId": item_id,
                                                                   "sceneItemTransform": {"cropTop": val[0],
                                                                                          "cropBottom": val[1]}})
                        except ObsRequestError as err:
                            if err.code == 600:  # item gone (scene edited): rescan
                                items = self._find_items(obs) or items
                                continue
                            raise
                        sent = val
                    if val == goal:
                        self.hub.set_obs(crop=list(goal))
                    # Short sleep on purpose: macOS stretches timers of background processes ~4x
                    # (20 ms -> 80 ms). Easing is time-based, so this only affects smoothness.
                    time.sleep(self.FRAME_SLEEP)
                else:
                    if time.monotonic() > next_scan:
                        items = self._find_items(obs) or items
                        self.hub.set_obs(items=len(items), crop=self._read_crop(obs, items[0]))
                        next_scan = time.monotonic() + 15
                    time.sleep(0.25)
        finally:
            obs.close()



def aggregate_meter(inputs: list, selected: str = "", muted=frozenset()) -> tuple[list[float], list[str]]:
    """InputVolumeMeters inputs -> [magL, magR, peakL, peakR] (linear 0..1) plus the names seen.

    Each input reports inputLevelsMul = [[magnitude, peak, input_peak], ...] per channel (post-fader,
    but OBS still reports levels for muted inputs, so those are skipped). With no selection the loudest
    unmuted input wins per channel; mono inputs feed both channels.
    """
    out = [0.0, 0.0, 0.0, 0.0]
    names = []
    for inp in inputs or []:
        name = str(inp.get("inputName") or "")
        chans = inp.get("inputLevelsMul") or []
        if not chans:
            continue
        names.append(name)
        if name in muted or (selected and name != selected):
            continue
        left = chans[0]
        right = chans[1] if len(chans) > 1 else chans[0]
        for ch, levels in enumerate((left, right)):
            if len(levels) >= 2:
                out[ch] = max(out[ch], float(levels[0] or 0))
                out[2 + ch] = max(out[2 + ch], float(levels[1] or 0))
    return [round(min(1.0, max(0.0, x)), 4) for x in out], sorted(set(names))


class ObsMeterReader(threading.Thread):
    """Second, read-only OBS connection that only listens to InputVolumeMeters (every 50 ms)."""

    def __init__(self, hub: "Hub"):
        super().__init__(name="obs-meter", daemon=True)
        self.hub = hub
        self.last_msg = None

    def _status(self, msg: str, level=logging.INFO):
        if msg != self.last_msg:
            log.log(level, "OBS meter: %s", msg)
            self.last_msg = msg
        self.hub.set_obs(meter=msg)
        self.hub.push_meter(None)

    def run(self):
        while True:
            if not self.hub.settings.get("meter"):
                self._status("off")
                time.sleep(2)
                continue
            try:
                self._session()
            except PermissionError as e:
                self._status(str(e), logging.WARNING)
                time.sleep(15)
            except (ConnectionRefusedError, TimeoutError, socket.timeout):
                self._status("OBS not reachable")
                time.sleep(5)
            except Exception as e:
                self._status(f"{type(e).__name__}: {e}", logging.WARNING)
                time.sleep(5)

    def _input_event(self, d: dict) -> bool:
        """Track mute state from Inputs events. Returns True if the muted set changed."""
        et, ed = d.get("eventType"), d.get("eventData") or {}
        if et == "InputMuteStateChanged" and ed.get("inputName"):
            self.muted[ed["inputName"]] = bool(ed.get("inputMuted"))
        elif et == "InputNameChanged" and ed.get("oldInputName") in self.muted:
            self.muted[ed.get("inputName")] = self.muted.pop(ed["oldInputName"])
        elif et == "InputRemoved":
            self.muted.pop(ed.get("inputName"), None)
        else:
            return False
        self.hub.set_obs(meterMuted=sorted(n for n, m in self.muted.items() if m))
        return True

    def _session(self):
        url, password = obs_connection_params(self.hub.cfg)
        obs = ObsClient(url, password, events=ObsClient.EVENT_INPUT_VOLUME_METERS | ObsClient.EVENT_INPUTS)
        self.muted: dict[str, bool] = {}
        try:
            obs.ws.sock.settimeout(5)
            seen: list[str] = []
            self._status("live")
            while self.hub.settings.get("meter"):
                if not obs.ws.wait_readable(1.0):
                    self.hub.push_meter([0.0, 0.0, 0.0, 0.0])  # no audio inputs / OBS quiet
                    continue
                msg = json.loads(obs.ws.recv_text())
                d = msg.get("d") or {}
                if msg.get("op") != 5 or self._input_event(d) or d.get("eventType") != "InputVolumeMeters":
                    continue
                inputs = (d.get("eventData") or {}).get("inputs") or []
                for inp in inputs:  # first time we see an input: ask whether it is muted
                    name = inp.get("inputName")
                    if name and inp.get("inputLevelsMul") and name not in self.muted:
                        try:
                            r = obs.call("GetInputMute", {"inputName": name}, on_event=self._input_event)
                            self.muted[name] = bool(r.get("inputMuted"))
                        except ObsRequestError:
                            self.muted[name] = False
                        self.hub.set_obs(meterMuted=sorted(n for n, m in self.muted.items() if m))
                levels, names = aggregate_meter(inputs, self.hub.settings.get("meterInput") or "",
                                                {n for n, m in self.muted.items() if m})
                if names != seen:
                    seen = names
                    self.hub.set_obs(meterInputs=names)
                self.hub.push_meter(levels)
        finally:
            obs.close()

# --------------------------------------------------------------------------- state -> view

STAGES = {
    0: "Printing", 1: "Auto bed leveling", 2: "Heatbed preheating", 3: "Vibration compensation",
    4: "Changing filament", 5: "M400 pause", 6: "Paused: filament runout", 7: "Heating hotend",
    8: "Calibrating extrusion", 9: "Scanning bed surface", 10: "Inspecting first layer",
    11: "Identifying build plate", 12: "Calibrating Micro Lidar", 13: "Homing toolhead",
    14: "Cleaning nozzle tip", 15: "Checking extruder temperature", 16: "Paused by user",
    17: "Paused: front cover falling", 18: "Calibrating Micro Lidar", 19: "Calibrating extrusion flow",
    20: "Paused: nozzle temperature malfunction", 21: "Paused: heatbed temperature malfunction",
    22: "Unloading filament", 23: "Paused: skipped step", 24: "Loading filament",
    25: "Calibrating motor noise", 26: "Paused: AMS lost", 27: "Paused: heatbreak fan too slow",
    28: "Paused: chamber temperature error", 29: "Cooling chamber", 30: "Paused by G-code",
    31: "Motor noise showoff", 32: "Paused: nozzle covered with filament", 33: "Paused: cutter error",
    34: "Paused: first layer error", 35: "Paused: nozzle clog", 36: "Checking absolute accuracy",
    37: "Absolute accuracy calibration", 38: "Checking absolute accuracy", 39: "Calibrating nozzle offset",
    40: "High-temp bed leveling", 41: "Checking quick-release lever", 42: "Checking door and cover",
    43: "Laser calibration", 44: "Checking platform", 45: "Checking BirdsEye camera",
    46: "Calibrating BirdsEye camera", 47: "Bed leveling (phase 1)", 48: "Bed leveling (phase 2)",
    49: "Heating chamber", 50: "Cooling heatbed", 51: "Printing calibration lines", 52: "Checking material",
    53: "Calibrating live view camera", 54: "Waiting for heatbed temperature", 55: "Checking material position",
    56: "Calibrating cutter offset", 57: "Measuring surface", 58: "Thermal preconditioning",
    59: "Homing blade holder", 60: "Calibrating camera offset", 61: "Calibrating blade holder",
    62: "Hotend pick & place test", 63: "Equalizing chamber temperature", 64: "Preparing hotend",
    65: "Calibrating clump detection", 66: "Purifying chamber air", 67: "Measuring rotary attachment",
    68: "Moving to purge chute", 69: "Cooling nozzle", 70: "Centering toolhead", 71: "Active arc fitting",
    72: "Detecting hotend type", 73: "Detecting plate alignment", 74: "Scanning bed for objects",
    75: "Scanning bed underside", 76: "Pre-extruding", 77: "Preparing AMS",
}
STATE_LABELS = {"IDLE": "Idle", "PREPARE": "Preparing", "RUNNING": "Printing", "PAUSE": "Paused",
                "FINISH": "Print complete", "FAILED": "Print stopped", "SLICING": "Slicing",
                "INIT": "Starting", "OFFLINE": "Offline"}
SPEEDS = {1: "Silent", 2: "Standard", 3: "Sport", 4: "Ludicrous"}
ACTIVE_STATES = ("PREPARE", "RUNNING", "PAUSE", "SLICING", "INIT")
CROP_PRINTING_STATES = ("PREPARE", "RUNNING", "PAUSE")
JOB_STATES = ACTIVE_STATES + ("FINISH", "FAILED")
POST_PRINT_STAGES = (66,)  # Purifying chamber air


def print_done(p: dict) -> bool:
    """The model is printed but gcode_state is still RUNNING: the end routine (filament unload, ~20 s) and
    chamber air purification (~3 min on the X2D) run before the printer reports FINISH."""
    if str(p.get("gcode_state") or "").upper() != "RUNNING":
        return False
    if _int(p.get("stg_cur"), -1) in POST_PRINT_STAGES:
        return True
    total = _int(p.get("total_layer_num"))
    return _int(p.get("mc_percent")) >= 100 and (total <= 0 or _int(p.get("layer_num")) >= total)


def _int(v, d=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


def _float(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _unpack_temp(v):
    v = _int(v)
    return v & 0xFFFF, (v >> 16) & 0xFFFF


def _fan_pct(v):
    v = _int(v)
    return 0 if v <= 0 else min(100, math.ceil(v / 15 * 10) * 10)


def _color(c):
    c = str(c or "").strip().lstrip("#")
    if re.fullmatch(r"[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", c):
        alpha = int(c[6:8], 16) / 255 if len(c) == 8 else 1.0
        return "#" + c[:6].upper(), round(alpha, 2)
    return None, 0.0


def _nozzle_type(code: str) -> str:
    code = str(code or "")
    if len(code) < 4:
        return ""
    material = {"00": "Stainless steel", "01": "Hardened steel", "05": "Tungsten carbide"}.get(code[2:4], "")
    flow = "TPU high flow" if code[1] == "U" else ("High flow" if code[1] in "HE" else "")
    return " · ".join(x for x in (material, flow) if x)


def _ams_name(ams_id: int) -> str:
    if 0 <= ams_id < 26:
        return chr(ord("A") + ams_id)
    if 128 <= ams_id < 254:
        return f"HT{ams_id - 127}"
    return str(ams_id)


def _slot_label(ams_id: int, tray_id: int) -> str:
    if ams_id == 255:
        return "Ext-R"
    if ams_id == 254:
        return "Ext-L"
    return _ams_name(ams_id) + ("" if ams_id >= 128 else str(tray_id + 1))


def _tray(t: dict, label: str, active: bool = False) -> dict:
    ttype = str(t.get("tray_type") or "").strip()
    color, alpha = _color(t.get("tray_color"))
    cols = [c for c in (_color(x)[0] for x in t.get("cols") or []) if c]
    remain = _int(t.get("remain"), -1)
    return {"slot": label, "type": ttype, "name": str(t.get("tray_sub_brands") or ttype).strip(),
            "color": color, "alpha": alpha, "colors": cols if len(cols) > 1 else [],
            "remain": remain if 0 <= remain <= 100 else None, "empty": not ttype, "active": active}


def _find_tray(p: dict, ams_id: int, tray_id: int):
    if ams_id in (254, 255):
        for v in p.get("vir_slot") or []:
            if _int(v.get("id"), -1) == ams_id:
                return v
        return p.get("vt_tray") if ams_id == 254 else None
    for unit in (p.get("ams") or {}).get("ams") or []:
        if _int(unit.get("id"), -1) == ams_id:
            for t in unit.get("tray") or []:
                if _int(t.get("id"), -1) == tray_id:
                    return t
    return None


def _loc_from_snow(snow: int):
    if snow in (0xFFFF, -1) or (snow & 0xFF) == 0xFF:
        return None
    return snow >> 8, snow & 0xFF


def _pretty_name(name: str) -> str:
    name = re.sub(r"(\.gcode)?\.3mf$", "", name or "", flags=re.I)
    return re.sub(r"\s+", " ", name.replace("_", " ")).strip()


_assets_cache = [0.0, ""]


def web_assets_version() -> str:
    """Changes whenever a file in web/ changes; overlays reload themselves (no manual OBS refresh)."""
    now = time.monotonic()
    if now - _assets_cache[0] > 2:
        stamp = max((f.stat().st_mtime_ns for f in WEB_DIR.iterdir() if f.is_file()), default=0)
        _assets_cache[:] = [now, format(stamp, "x")]
    return _assets_cache[1]


def build_view(hub: "Hub") -> dict:
    p = hub.report
    dev = p.get("device") or {}
    now = time.time()
    has_data = bool(p)
    online = hub.mqtt_ok and has_data and now - hub.last_report < 30
    state = str(p.get("gcode_state") or ("UNKNOWN" if has_data else "OFFLINE")).upper()
    media = hub.media if hub.media.get("key") == hub.job_key else {}
    meta = media.get("meta") or {}

    # --- nozzles / extruders
    ext = dev.get("extruder") or {}
    estate = _int(ext.get("state"))
    active_idx = (estate >> 4) & 0xF if ext.get("info") else 0
    nozzle_info = {_int(n.get("id"), -1): n for n in (dev.get("nozzle") or {}).get("info") or []}
    nozzles, loaded = [], []
    infos = ext.get("info") or []
    dual = len(infos) > 1
    for e in infos:
        nid = _int(e.get("id"), -1)
        cur, tgt = _unpack_temp(e.get("temp"))
        side = ("R" if nid == 0 else "L") if dual else ""
        ninfo = nozzle_info.get(nid, {})
        nozzles.append({"id": nid, "side": side, "label": f"Nozzle {side}".strip(), "cur": cur, "tgt": tgt,
                        "active": nid == active_idx, "diameter": _float(ninfo.get("diameter")),
                        "type": _nozzle_type(ninfo.get("type"))})
        loc = _loc_from_snow(_int(e.get("snow"), 0xFFFF))
        if loc and (t := _find_tray(p, *loc)) and str(t.get("tray_type") or "").strip():
            loaded.append({**_tray(t, _slot_label(*loc)), "side": side, "active": nid == active_idx, "loc": loc})
    if not nozzles:
        nozzles.append({"id": 0, "side": "", "label": "Nozzle", "cur": round(_float(p.get("nozzle_temper"), 0)),
                        "tgt": round(_float(p.get("nozzle_target_temper"), 0)), "active": True,
                        "diameter": _float(p.get("nozzle_diameter")), "type": _nozzle_type(p.get("nozzle_type"))})
        tray_now = _int((p.get("ams") or {}).get("tray_now"), 255)
        loc = (254, 0) if tray_now == 254 else ((tray_now // 4, tray_now % 4) if tray_now < 128 else None)
        if loc and (t := _find_tray(p, *loc)):
            loaded.append({**_tray(t, _slot_label(*loc)), "side": "", "active": True, "loc": loc})
    nozzles.sort(key=lambda n: (n["side"] != "L", n["id"]))

    def heater(key_dev, key_cur, key_tgt):
        info = (dev.get(key_dev) or {}).get("info") or {}
        if "temp" in info:
            cur, tgt = _unpack_temp(info["temp"])
        else:
            cur, tgt = round(_float(p.get(key_cur), 0)), round(_float(p.get(key_tgt), 0))
        return {"cur": cur, "tgt": tgt}

    bed = heater("bed", "bed_temper", "bed_target_temper")
    chamber = heater("ctc", "chamber_temper", "chamber_target_temper") if (dev.get("ctc") or "chamber_temper" in p) else None

    # --- AMS
    active_loc = next((l["loc"] for l in loaded if l["active"]), None) if state in ACTIVE_STATES else None
    ams_units = []
    for unit in (p.get("ams") or {}).get("ams") or []:
        uid = _int(unit.get("id"), -1)
        trays = [_tray(t, _slot_label(uid, _int(t.get("id"))), active_loc == (uid, _int(t.get("id"))))
                 for t in sorted(unit.get("tray") or [], key=lambda t: _int(t.get("id")))]
        hum = _int(unit.get("humidity_raw"), -1)
        ams_units.append({"id": uid, "name": _ams_name(uid), "humidity": hum if 0 <= hum <= 100 else None,
                          "temp": _float(unit.get("temp")), "trays": trays})
    external = []
    for v in p.get("vir_slot") or ([p["vt_tray"]] if p.get("vt_tray") else []):
        vid = _int(v.get("id"), 254)
        tr = _tray(v, _slot_label(vid, 0), bool(active_loc) and active_loc[0] == vid)
        if not tr["empty"]:
            external.append(tr)

    # --- filaments used by this job (slice info joined with the printer's AMS mapping)
    mapping = p.get("mapping") or []
    filaments = []
    for f in meta.get("filaments") or []:
        fid = f.get("id", 0)
        m = _int(mapping[fid - 1], 0xFFFF) if 0 < fid <= len(mapping) else 0xFFFF
        loc = None if m == 0xFFFF else (m >> 8, m & 0xFF)
        tray = _tray(_find_tray(p, *loc) or {}, _slot_label(*loc)) if loc else None
        filaments.append({"type": f.get("type"), "name": (tray or {}).get("name") or f.get("type"),
                          "color": f.get("color") or (tray or {}).get("color"), "colors": (tray or {}).get("colors", []),
                          "usedG": f.get("usedG"), "usedM": f.get("usedM"), "slot": (tray or {}).get("slot"),
                          "remain": (tray or {}).get("remain"), "active": loc is not None and loc == active_loc})
    if not filaments and loaded:
        a = next((l for l in loaded if l["active"]), loaded[0])
        filaments.append({"type": a["type"], "name": a["name"], "color": a["color"], "colors": a["colors"],
                          "usedG": None, "usedM": None, "slot": a["slot"], "remain": a["remain"], "active": True})
    accent_src = next((f["color"] for f in filaments if f["active"] and f["color"]), None) \
        or next((f["color"] for f in filaments if f["color"]), None)

    # --- job
    stg_cur = _int(p.get("stg_cur"), -1)
    stg = [_int(x) for x in p.get("stg") or []]
    stage = None
    if state in ACTIVE_STATES and stg_cur not in (-1, 255) and not (stg_cur == 0 and state == "RUNNING"):
        stage = STAGES.get(stg_cur, f"Stage {stg_cur}")
    finishing = print_done(p)
    if finishing:
        state = "FINISH"  # show it as complete; the stage (e.g. "Purifying chamber air") stays visible
    err = _int(p.get("print_error"))
    task = str(p.get("subtask_name") or "")
    design_id = str(p.get("design_id") or "")
    plate = job_plate(p) or 1
    job_rec = hub.jobs.get(hub.job_key or "", {})
    started = job_rec.get("start")
    if (not started or job_rec.get("src") == "first-seen") and meta.get("fileTime"):
        ft = _int(meta["fileTime"])
        if not started or ft < started:
            started = ft
    view_job = {
        "state": state, "label": STATE_LABELS.get(state, state.title()), "stage": stage, "stageId": stg_cur,
        "step": (stg.index(stg_cur) + 1) if state == "PREPARE" and stg_cur in stg else None,
        "steps": len(stg) if state == "PREPARE" else None,
        "hasJob": state in JOB_STATES and bool(task), "active": state in ACTIVE_STATES, "finishing": finishing,
        "task": task, "fileName": _pretty_name(task), "title": meta.get("title"),
        "designer": meta.get("designer"), "license": meta.get("license"),
        "makerworld": f"makerworld.com/models/{design_id}" if design_id.isdigit() and design_id != "0" else None,
        "plate": plate, "plateCount": _int(p.get("plate_cnt")) or None,
        "progress": max(0, min(100, _int(p.get("mc_percent")))),
        "remainingMin": max(0, _int(p.get("mc_remaining_time"))),
        "layer": _int(p.get("layer_num")), "totalLayers": _int(p.get("total_layer_num")),
        "startedAt": started if state in JOB_STATES else None,
        "finishedAt": job_rec.get("end") if state in ("FINISH", "FAILED") else None,
        "durationSec": (job_rec["end"] - started) if state in ("FINISH", "FAILED") and job_rec.get("end")
        and started and job_rec["end"] > started else None,
        "predictionSec": meta.get("predictionSec"), "weightG": meta.get("weightG"),
        "bedType": meta.get("bedType"), "objects": meta.get("objects"),
        "speed": {"level": _int(p.get("spd_lvl")), "name": SPEEDS.get(_int(p.get("spd_lvl")), ""),
                  "pct": _int(p.get("spd_mag"))},
        "prepPercent": _int(p.get("gcode_file_prepare_percent"), 100),
        "printType": p.get("print_type"),
        "error": f"{(err >> 16) & 0xFFFF:04X}-{err & 0xFFFF:04X}" if err else None,
        "cover": f"/api/cover.png?k={media['coverTag']}" if media.get("coverTag") else None,
        "filaments": filaments,
    }
    return {
        "v": hub.version, "now": now, "online": online, "hasData": has_data, "assets": web_assets_version(),
        "printer": {"ip": hub.ip, "serialTail": (hub.serial or "")[-4:], "wifi": p.get("wifi_signal"),
                    "status": hub.mqtt_status, "media": hub.media_status, "lastReport": hub.last_report},
        "settings": hub.settings,
        "obs": dict(hub.obs),
        "job": view_job,
        "temps": {"nozzles": nozzles, "bed": bed, "chamber": chamber},
        "fans": {"part": _fan_pct(p.get("cooling_fan_speed")), "aux": _fan_pct(p.get("big_fan1_speed")),
                 "chamber": _fan_pct(p.get("big_fan2_speed"))},
        "ams": ams_units, "external": external,
        "loaded": [{k: v for k, v in l.items() if k != "loc"} for l in loaded],
        "accentSource": accent_src,
    }


# --------------------------------------------------------------------------- hub

def _merge(dst: dict, src: dict):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v


class Hub:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cond = threading.Condition()
        self.ip = cfg.ip
        self.serial = cfg.serial
        self.report: dict = {}
        self.mqtt_ok = False
        self.mqtt_status = "starting"
        self.last_report = 0.0
        self.media: dict = {}
        self.media_status = "idle"
        self.job_key: str | None = None
        self.prev_state: str | None = None
        self.jobs: dict = load_json(JOBS_FILE, {})
        stored = load_json(SETTINGS_FILE, {})
        self.settings = {**DEFAULT_SETTINGS, **{k: v for k, v in stored.items()
                                                if k in _SETTING_RULES and _SETTING_RULES[k](v)}}
        self.version = 0
        self._view = None
        self.ip_changed = threading.Event()
        self.media_wakeup = threading.Event()
        self.obs = {"status": "starting", "source": cfg.obs_source, "items": 0, "crop": None, "target": None,
                    "meter": "starting", "meterInputs": [], "meterMuted": []}
        self.meter: list[float] | None = None   # [magL, magR, peakL, peakR], pushed ~20x/s
        self.meter_seq = 0

    # -- changes (all called from worker threads)
    def _bump(self):
        self.version += 1
        self._view = None
        self.cond.notify_all()

    def set_mqtt(self, ok: bool, status: str):
        with self.cond:
            if (ok, status) != (self.mqtt_ok, self.mqtt_status):
                self.mqtt_ok, self.mqtt_status = ok, status
                self._bump()

    def set_serial(self, serial: str):
        with self.cond:
            self.serial = serial.upper()
        log.info("Printer serial from TLS certificate: %s (add BAMBU_X2D_SERIAL=%s to .env to pin it)",
                 serial, serial)

    def on_ssdp(self, usn: str, location: str, headers: dict):
        with self.cond:
            if not self.serial and location == self.ip and usn:
                self.serial = usn
                log.info("SSDP: printer at %s is %s", location, usn)
            if usn and usn == self.serial and location and location != self.ip:
                log.warning("SSDP: printer %s moved %s -> %s, following it "
                            "(tip: reserve the IP in your router's DHCP settings)", usn, self.ip, location)
                self.ip = location
                self.ip_changed.set()
                self._bump()

    def on_report(self, msg: dict):
        p = msg.get("print")
        if not isinstance(p, dict) or p.get("command") != "push_status":
            return
        with self.cond:
            _merge(self.report, p)
            self.last_report = time.time()
            self._track_job()
            self._bump()

    def _track_job(self):
        p = self.report
        state = str(p.get("gcode_state") or "").upper()
        task = str(p.get("subtask_name") or "")
        key = None
        if task and state in JOB_STATES:
            key = f"{p.get('task_id') or p.get('job_id') or '0'}|{task}|{p.get('plate_idx') or ''}"
        restarted = key and key == self.job_key and state in ACTIVE_STATES and self.prev_state in ("FINISH", "FAILED")
        if key != self.job_key or restarted:
            self.job_key = key
            self.media_wakeup.set()
            if key and state in ACTIVE_STATES and (restarted or key not in self.jobs):
                fresh = state == "PREPARE" or _int(p.get("mc_percent")) == 0
                self.jobs[key] = {"start": int(time.time()), "src": "observed" if fresh else "first-seen"}
                self.jobs = dict(list(self.jobs.items())[-50:])
                save_json(JOBS_FILE, self.jobs)
        # the job's end is when the model is done, not when the printer finishes purifying the air
        if key and (print_done(p) or (state in ("FINISH", "FAILED") and self.prev_state in ACTIVE_STATES)):
            rec = self.jobs.setdefault(key, {})
            if "end" not in rec:
                rec["end"] = int(time.time())
                save_json(JOBS_FILE, self.jobs)
        self.prev_state = state

    def wanted_media(self):
        with self.cond:
            p = self.report
            if not self.job_key or not self.serial:
                return None
            rec = self.jobs.get(self.job_key, {})
            return {"key": self.job_key, "name": str(p.get("subtask_name") or ""),
                    "gcode_file": str(p.get("gcode_file") or ""), "plate": job_plate(p),
                    "state": str(p.get("gcode_state") or "").upper(),
                    "start": rec.get("start"), "src": rec.get("src")}

    def set_media(self, key: str, meta: dict, cover: bytes | None):
        with self.cond:
            tag = hashlib.sha1(cover).hexdigest()[:10] if cover else None
            self.media = {"key": key, "meta": meta, "cover": cover, "coverTag": tag}
            self.media_status = "ready"
            self._bump()

    def set_media_status(self, status: str):
        with self.cond:
            self.media_status = status
            self._bump()

    def update_settings(self, patch: dict) -> dict:
        bad = [k for k, v in patch.items() if k not in _SETTING_RULES or not _SETTING_RULES[k](v)]
        if bad:
            raise ValueError(f"invalid setting(s): {', '.join(bad)}")
        with self.cond:
            self.settings = {**self.settings, **patch}
            # persist only what differs from the defaults, so improved defaults still reach old installs
            save_json(SETTINGS_FILE, {k: v for k, v in self.settings.items() if DEFAULT_SETTINGS.get(k) != v})
            self._bump()
            return dict(self.settings)

    def push_meter(self, levels: list[float] | None):
        with self.cond:
            if levels == self.meter:   # e.g. silence: OBS keeps sending zeros 20x/s, nothing to relay
                return
            self.meter = levels
            self.meter_seq += 1
            self.cond.notify_all()

    def set_obs(self, **changes):
        with self.cond:
            if any(self.obs.get(k) != v for k, v in changes.items()):
                self.obs.update(changes)
                self._bump()

    def crop_target(self):
        """(mode, [top, bottom]) the OBS camera crop should be at, or None to leave it alone."""
        with self.cond:
            st = self.settings
            if not st.get("obsCrop"):
                return None
            mode = st.get("cropMode", "auto")
            if mode == "auto":
                online = self.mqtt_ok and self.report and time.time() - self.last_report < 30
                if not online:
                    return None  # printer state unknown: hold the current framing
                state = str(self.report.get("gcode_state") or "").upper()
                mode = "printing" if state in CROP_PRINTING_STATES and not print_done(self.report) else "idle"
            return mode, list(st["cropPrint"] if mode == "printing" else st["cropIdle"])

    def view(self) -> dict:
        with self.cond:
            if self._view is None:
                self._view = build_view(self)
            return self._view


# --------------------------------------------------------------------------- HTTP

CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                 ".jpg": "image/jpeg", ".json": "application/json", ".woff2": "font/woff2", ".ico": "image/x-icon"}


def make_handler(hub: Hub):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BambuOverlay/1.0"

        def log_message(self, fmt, *args):
            log.debug("http: " + fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/api/state":
                return self._json(hub.view())
            if path == "/api/settings":
                return self._json(hub.settings)
            if path == "/api/raw":
                with hub.cond:
                    return self._json(hub.report)
            if path == "/api/cover.png":
                cover = hub.media.get("cover") if hub.media.get("key") == hub.job_key else None
                if not cover:
                    return self._send(404, b"no cover", "text/plain")
                return self._send(200, cover, "image/png", {"Cache-Control": "max-age=86400"})
            if path == "/api/events":
                return self._events()
            if path in ("/", ""):
                path = "/index.html"
            elif path in ("/control", "/preview") or path.endswith("/"):
                path = path.rstrip("/") + ".html"
            target = (WEB_DIR / path.lstrip("/")).resolve()
            if WEB_DIR.resolve() not in target.parents or not target.is_file():
                return self._send(404, b"not found", "text/plain")
            self._send(200, target.read_bytes(), CONTENT_TYPES.get(target.suffix, "application/octet-stream"))

        def do_POST(self):
            if urlparse(self.path).path != "/api/settings":
                return self._send(404, b"not found", "text/plain")
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 10_000)
                patch = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(patch, dict):
                    raise ValueError("expected a JSON object")
                self._json(hub.update_settings(patch))
            except ValueError as e:
                self._json({"error": str(e)}, 400)

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            last = last_meter = -1
            try:
                while True:
                    with hub.cond:
                        hub.cond.wait_for(lambda: hub.version != last or hub.meter_seq != last_meter, timeout=15)
                        changed = hub.version != last
                        meter_changed = hub.meter_seq != last_meter
                        last_meter, meter = hub.meter_seq, hub.meter
                    if changed:
                        view = hub.view()
                        last = view["v"]
                        self.wfile.write(b"data: " + json.dumps(view, separators=(",", ":")).encode() + b"\n\n")
                    if meter_changed:
                        self.wfile.write(b"event: meter\ndata: " + json.dumps(meter).encode() + b"\n\n")
                    if not changed and not meter_changed:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    return Handler


def lan_hostname() -> str:
    try:
        name = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True,
                              timeout=2).stdout.strip()
        if name:
            return name + ".local"
    except (OSError, subprocess.SubprocessError):
        pass
    host = socket.gethostname()
    return host if host.endswith(".local") else host + ".local"


class OverlayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # browsers / OBS drop connections all the time (page reloads, closed previews); not worth a traceback
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


# --------------------------------------------------------------------------- main

def main():
    logging.basicConfig(level=logging.DEBUG if "-v" in sys.argv else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    cfg = Config()
    CACHE_DIR.mkdir(exist_ok=True)
    hub = Hub(cfg)
    for worker in (SsdpWatcher(hub), ReadOnlyMqtt(hub), MediaWorker(hub), ObsCropController(hub), ObsMeterReader(hub)):
        worker.start()
    server = OverlayHTTPServer((cfg.host, cfg.port), make_handler(hub))
    base = f"http://{'127.0.0.1' if cfg.host in ('0.0.0.0', '') else cfg.host}:{cfg.port}"
    log.info("Overlay (OBS browser source, 1280x720): %s/", base)
    log.info("Control panel (layouts & options):     %s/control", base)
    log.info("Preview over stream frame:              %s/preview", base)
    if cfg.host in ("0.0.0.0", ""):
        log.info("On your network:                        http://%s:%d/control", lan_hostname(), cfg.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("bye")


if __name__ == "__main__":
    main()

"""
Fleet Remote v3.91 — all-in-one app
===========================================
One file. Runs a local control server and opens its own native window (no
browser, no extension, no extra apps). Control any number of Amazon Fire TV
Cube / Stick devices over ADB and Apple TVs, each by IP, sorted into groups:
remote keys, text, power, Settings, screenshots, live video, Never sleep, HDMI
output info and EDID download for the selected device.

Run it (Windows, cmd):
    pip install -r requirements.txt
    python fleet_remote_v3.91.py

Run it (Mac, Terminal; needs macOS 13.3 or newer for live video):
    pip3 install -r requirements.txt
    python3 fleet_remote_v3.91.py

Flags:
    python fleet_remote_v3.91.py --browser      open in default browser instead of a window
    python fleet_remote_v3.91.py --port 9000    use a specific port
    python fleet_remote_v3.91.py --share        also open to other computers (browser + access code)
"""

import argparse
import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import struct
import sys
import threading
import time
import traceback
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Optional

# Under pythonw (the launcher, the shortcut) there is no console, so sys.stdout/stderr
# are None. Anything that touches them (print, Python's last-resort log handler) would
# crash. Give them a sink (not a buffer: that kept every warning for as long as the app
# ran); warnings go to error.log instead, see _ErrorLogHandler.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _alert(msg: str) -> None:
    """Error dialog for a start without a console (Windows message box, macOS alert)."""
    with suppress(Exception):
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "Fleet Remote", 0x10)
        elif sys.platform == "darwin":
            import subprocess
            q = msg.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(["osascript", "-e", f'display alert "Fleet Remote" message "{q}" as critical'],
                           timeout=120)


if sys.version_info < (3, 11):      # the pinned packages have no builds for older Pythons
    _alert(f"Fleet Remote needs Python 3.11 or newer (this is Python {sys.version.split()[0]}).")
    sys.exit("Fleet Remote needs Python 3.11 or newer.")

import importlib.util
_missing = [m for m in ("uvicorn", "fastapi", "pydantic", "adb_shell", "pyatv")
            if importlib.util.find_spec(m) is None]
if _missing:
    _alert(f"Fleet Remote is missing Python packages: {', '.join(_missing)}.\n\n"
           "Start it with 'Fleet Remote.pyw' (it installs what is needed), or run:\n"
           "python -m pip install -r requirements.txt")
    sys.exit("Missing packages: " + ", ".join(_missing))
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, FileResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from adb_shell.adb_device_async import AdbDeviceTcpAsync
from adb_shell.transport.tcp_transport_async import TcpTransportAsync

# adb_shell reads again at once after an empty read, until read_timeout_s (a day
# for live video): when the Cube closes the connection (restart, power back,
# adbd restart) that spins without a pause and freezes the whole app. An empty
# read means the link is closed, so say so.
_bulk_read = TcpTransportAsync.bulk_read


async def _bulk_read_closed(self, numbytes, transport_timeout_s):
    data = await _bulk_read(self, numbytes, transport_timeout_s)
    if not data and numbytes:
        raise ConnectionResetError(f"{self._host}:{self._port} closed the connection")
    return data


TcpTransportAsync.bulk_read = _bulk_read_closed
from adb_shell import constants as adb_const
from adb_shell.adb_message import AdbMessage
from adb_shell.auth.keygen import keygen, write_public_keyfile
from adb_shell.auth.sign_pythonrsa import PythonRSASigner
from adb_shell.exceptions import AdbTimeoutError, TcpTimeoutException



def _load_pyatv():
    """pyatv takes about a second to import and only Apple TVs need it, so it loads on
    first use (in a worker thread where possible: _pyatv_ready). The global then holds the
    real module."""
    global pyatv
    import pyatv as module
    pyatv = module
    return module


class _LazyPyatv:
    def __getattr__(self, name):
        return getattr(_load_pyatv(), name)


pyatv = _LazyPyatv()


async def _pyatv_ready() -> None:
    if isinstance(pyatv, _LazyPyatv):
        await asyncio.to_thread(_load_pyatv)


# Fire key names -> pyatv remote_control method names
ATV_RC = {
    "up": "up", "down": "down", "left": "left", "right": "right",
    "select": "select", "menu": "menu", "back": "menu", "home": "home",
    "play_pause": "play_pause", "enter": "select",
    "rewind": "skip_backward", "fast_forward": "skip_forward",
    "next": "next", "previous": "previous",
}
ATV_AUDIO = {"volume_up", "volume_down"}

# ---- app data lives in the user's home folder, not next to the app file ----
APP_VERSION = "3.91"
SCRIPT_NAME = Path(__file__).name       # fleet_remote_v<version>.py
DATA_DIR = Path.home() / ".firecube-fleet"
ICON = Path(__file__).resolve().with_name("icon.ico")
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError as _exc:
    _alert(f"Fleet Remote can't make its data folder {DATA_DIR}:\n{_exc}")
    raise
if os.name != "nt":
    with suppress(OSError):
        os.chmod(DATA_DIR, 0o700)
DEVICES_FILE = DATA_DIR / "devices.json"
GROUPS_FILE = DATA_DIR / "groups.json"
ADBKEY = DATA_DIR / "adbkey"
ADB_PORT = 5555

KEYS = {
    "up": 19, "down": 20, "left": 21, "right": 22, "select": 23,
    "back": 4, "home": 3, "menu": 82,
    "play_pause": 85, "next": 87, "previous": 88, "rewind": 89, "fast_forward": 90,
    "volume_up": 24, "volume_down": 25, "mute": 164,
    "wake": 224, "sleep": 223, "power": 26,
    "enter": 66, "delete": 67,
}
@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # start: load the saved devices, watch their power state
    try:
        for g in load_groups():
            _note_group(g)
        known = len(groups)
        skipped = 0
        for d in load_devices():
            try:
                if not isinstance(d, dict) or not isinstance(d.get("ip"), str) or not d["ip"]:
                    raise ValueError("no ip")
                register(d["ip"], d.get("name", d["ip"]),
                         d.get("type", "fire"), d.get("atv_credentials") or {},
                         d.get("sleep_prev"), d.get("group") if isinstance(d.get("group"), str) else "")
            except Exception as exc:
                skipped += 1
                _log_error(f"devices.json: skipped entry {d!r}: {exc}")
        if skipped:
            with suppress(Exception):            # the next save drops them: keep what was there
                import shutil
                shutil.copy(DEVICES_FILE, DEVICES_FILE.with_suffix(".bad"))
        if len(groups) != known:
            save_groups()                       # groups made before groups.json existed
        await asyncio.to_thread(get_signer)      # make the ADB key now, not on the first Connect
        if any(d.get("type") == "appletv" for d in fleet.values()):
            _spawn(_pyatv_ready())               # load it while the window opens
    except Exception:
        _log_error(traceback.format_exc())
    power = _spawn(_power_loop())
    yield
    # stop (window closed): end live video properly, drop every link
    power.cancel()
    await asyncio.gather(*(_close_stream(st) for st in list(_streams.values())),
                         return_exceptions=True)
    await asyncio.gather(*(_drop_links(d) for d in list(fleet.values())),
                         return_exceptions=True)


def _log_error(text: str) -> None:
    with suppress(Exception):
        f = DATA_DIR / "error.log"
        if f.is_file() and f.stat().st_size > 500_000:
            f.unlink()
        with f.open("a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text.rstrip() + "\n")


class _ErrorLogHandler(logging.Handler):
    """Warnings from uvicorn, adb_shell, pyatv... go to error.log (capped by _log_error),
    not to a console that pythonw doesn't have."""

    def emit(self, record: logging.LogRecord) -> None:
        with suppress(Exception):
            _log_error(f"{record.name}: {self.format(record)}")


logging.basicConfig(level=logging.WARNING, handlers=[_ErrorLogHandler()])


app = FastAPI(title="Fleet Remote", lifespan=_lifespan)

# --share: let other computers on the network open the app in a browser.
# They must enter the access code (any user name). This computer never needs it.
SHARE_CODE: Optional[str] = None
SHARE_URL: Optional[str] = None
CODE_LOCK_TRIES, CODE_LOCK_WINDOW = 10, 300.0   # wrong codes from one computer in 5 min: locked out
_bad_codes: dict[str, list] = {}                # client address -> when it sent wrong codes


_HOSTS: Optional[set] = None       # Host headers this computer's own window may use (main sets it)


async def _plain(send, status: int, text: str, headers=()) -> None:
    body = text.encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode()), *headers]})
    await send({"type": "http.response.body", "body": body})


class _Guard:
    """Three checks on every request. 1. A web page open in a browser on this
    computer may not reach the app through a made-up host name (DNS rebinding):
    the Host header has to be ours. 2. A request that changes something (not GET)
    has to come from our own page, and the browser has to say so (Sec-Fetch-Site
    or Origin). 3. With --share, other computers need the access code; ten wrong
    codes in five minutes lock that computer out for the rest of the five minutes."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        hdr = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        client = (scope.get("client") or ("", 0))[0]
        local = client in ("127.0.0.1", "::1")
        if local and _HOSTS is not None and hdr.get("host", "").lower() not in _HOSTS:
            return await _plain(send, 403, "Wrong host name for Fleet Remote.")
        site, origin = hdr.get("sec-fetch-site"), hdr.get("origin")
        if (site in ("cross-site", "same-site")
                or (origin is not None and origin.lower() != "http://" + hdr.get("host", "").lower())):
            return await _plain(send, 403, "Only Fleet Remote's own page can use it.")
        if scope.get("method") not in ("GET", "HEAD") and origin is None and site not in ("same-origin", "none"):
            return await _plain(send, 403, "Only Fleet Remote's own page can change things.")
        if SHARE_CODE and not local:
            now = time.monotonic()
            bad = [at for at in _bad_codes.get(client, ()) if now - at < CODE_LOCK_WINDOW]
            if len(bad) >= CODE_LOCK_TRIES:
                left = int(CODE_LOCK_WINDOW - (now - bad[0])) // 60 + 1
                return await _plain(send, 429, f"Too many wrong access codes. Try again in {left} min.")
            ok = False
            h = hdr.get("authorization", "")
            if h.lower().startswith("basic "):
                with suppress(Exception):
                    pw = base64.b64decode(h[6:]).decode("utf-8").split(":", 1)[-1]
                    ok = secrets.compare_digest(pw, SHARE_CODE)
            if not ok:
                if h:
                    bad.append(now)
                if bad:
                    _bad_codes[client] = bad
                else:
                    _bad_codes.pop(client, None)
                if len(_bad_codes) > 500:           # strikes of computers that never came back
                    for c in [c for c, ats in _bad_codes.items() if now - ats[-1] >= CODE_LOCK_WINDOW]:
                        del _bad_codes[c]
                if h:
                    await asyncio.sleep(1.0)        # slows down guessing the access code
                return await _plain(send, 401, "Access code required",
                                    [(b"www-authenticate", b'Basic realm="Fleet Remote"')])
            _bad_codes.pop(client, None)
        return await self.app(scope, receive, send)


app.add_middleware(_Guard)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.get("/api/share")
async def share_info():
    return {"url": SHARE_URL, "code": SHARE_CODE} if SHARE_CODE else {}

fleet: dict[str, dict] = {}
groups: list[str] = []          # group names, including ones with no devices yet
_signer_cache = []


def get_signer():
    if _signer_cache:
        return _signer_cache[0]
    pub = ADBKEY.with_suffix(".pub")
    if not ADBKEY.exists():
        keygen(str(ADBKEY))
        with suppress(Exception):
            os.chmod(ADBKEY, 0o600)          # the private key: this user only (Mac / Linux)
    elif not pub.exists():
        write_public_keyfile(str(ADBKEY), str(pub))     # same key, so the TVs still know it
    signer = PythonRSASigner(ADBKEY.with_suffix(".pub").read_text(encoding="utf-8"),
                             ADBKEY.read_text(encoding="utf-8"))
    _signer_cache.append(signer)
    return signer


def load_devices() -> list:
    if not DEVICES_FILE.exists():
        return []
    try:
        data = json.loads(DEVICES_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("not a list")
        return data
    except (ValueError, OSError) as exc:
        # a damaged file would stop the app from starting: keep it aside, start empty
        bad = DEVICES_FILE.with_suffix(".bad")
        with suppress(Exception):
            os.replace(DEVICES_FILE, bad)
        _log_error(f"devices.json could not be read ({exc}); moved to {bad}\n")
        return []


def load_groups() -> list:
    if not GROUPS_FILE.exists():
        return []
    try:
        data = json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("not a list")
        return [g for g in data if isinstance(g, str)]
    except (ValueError, OSError) as exc:
        bad = GROUPS_FILE.with_suffix(".bad")
        with suppress(Exception):
            os.replace(GROUPS_FILE, bad)
        _log_error(f"groups.json could not be read ({exc}); moved to {bad}\n")
        return []


def _swap_in(tmp: Path, dst: Path) -> None:
    """os.replace, retried: on Windows a virus scanner or OneDrive can hold the target briefly."""
    for i in range(5):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if i == 4:
                raise
            time.sleep(0.05 * (i + 1))


def save_groups() -> None:
    tmp = GROUPS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(groups, indent=2), encoding="utf-8")
    _swap_in(tmp, GROUPS_FILE)


def _find_group(name: str) -> Optional[str]:
    low = name.casefold()
    return next((g for g in groups if g.casefold() == low), None)


def _note_group(group: Optional[str]) -> str:
    # "studio a" and "Studio A" are the same group: keep the spelling it was made with
    g = _clean_group(group)
    if not g:
        return ""
    known = _find_group(g)
    if known:
        return known
    groups.append(g)
    return g


def save_devices() -> None:
    # write next to it, then swap: a crash mid-write can't leave half a file
    tmp = DEVICES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(
        [{"ip": ip, "name": d["name"], "type": d.get("type", "fire"),
          "atv_credentials": d.get("atv_credentials") or {},
          # the sleep settings Never sleep replaced (Disable puts them back)
          **({"sleep_prev": d["sleep_prev"]} if d.get("sleep_prev") else {}),
          **({"group": d["group"]} if d.get("group") else {})}
         for ip, d in fleet.items()], indent=2), encoding="utf-8")
    if os.name != "nt":
        with suppress(OSError):
            os.chmod(tmp, 0o600)             # Apple TV pairing keys: this user only
    _swap_in(tmp, DEVICES_FILE)


def _clean_group(group: Optional[str]) -> str:
    return " ".join((group or "").split())[:40]


def register(ip: str, name: str, dtype: str = "fire",
             atv_credentials: Optional[dict] = None,
             sleep_prev: Optional[dict] = None, group: Optional[str] = None) -> None:
    if ip not in fleet:
        # want: the user connected it (so a dropped link is reconnected quietly)
        fleet[ip] = {"name": name or ip, "type": dtype, "dev": None, "atv": None,
                     "lock": asyncio.Lock(), "clock": asyncio.Lock(), "want": False,
                     "atv_credentials": atv_credentials or {},
                     "sleep_prev": sleep_prev or None, "epoch": 0, "group": _note_group(group)}
    else:
        fleet[ip]["name"] = name or fleet[ip]["name"]
        if group is not None:
            fleet[ip]["group"] = _note_group(group)


def is_connected(d: dict) -> bool:
    if d.get("type") == "appletv":
        return d.get("atv") is not None
    return d.get("dev") is not None and d["dev"].available


async def _quiet_close(obj) -> None:
    """Close an ADB link (async) or an Apple TV connection (sync), never raising."""
    if obj is None:
        return
    with suppress(Exception):
        r = obj.close()
        if asyncio.iscoroutine(r):
            await asyncio.wait_for(r, 5)


def _drop_dev(ip: str, dev) -> None:
    """Forget a Fire link that failed (only while it is still the current one)
    and close it in the background, so its socket is not left open."""
    d = fleet.get(ip)
    if d is not None and d.get("dev") is dev:
        d["dev"] = None
    _spawn(_quiet_close(dev))


async def _drop_links(entry: dict) -> None:
    """Disconnect: forget and close both kinds of link of one device."""
    dev, atv = entry.get("dev"), entry.get("atv")
    entry["dev"] = entry["atv"] = None
    await _quiet_close(dev)
    await _quiet_close(atv)


# macOS 15 blocks the TVs (and the Apple TV scan) until Python may use the local network
_MAC_LAN_HINT = (" (on a Mac also check System Settings > Privacy & Security > Local Network)"
                 if sys.platform == "darwin" else "")


async def ensure_connected(ip: str) -> AdbDeviceTcpAsync:
    if ip not in fleet:
        raise HTTPException(404, f"{ip} is not in the fleet.")
    entry = fleet[ip]
    dev = entry["dev"]
    if dev is not None and dev.available:
        return dev
    # one connect at a time per device: a Connect click and the quiet reconnect
    # after a dropped link must not open two links
    epoch = entry.get("epoch", 0)        # before the wait: a Disconnect meanwhile must win
    async with entry["clock"]:
        dev = entry["dev"]
        if dev is not None and dev.available:
            return dev
        dev = AdbDeviceTcpAsync(ip, ADB_PORT, default_transport_timeout_s=9.0)
        try:
            await dev.connect(rsa_keys=[get_signer()], auth_timeout_s=20.0,
                              read_timeout_s=8.0, transport_timeout_s=8.0)
        except Exception as exc:
            await _quiet_close(dev)
            if sys.platform == "darwin" and ("No route to host" in str(exc) or getattr(exc, "errno", None) == 65):
                raise HTTPException(502, f"Couldn't reach {ip}. If the IP is right, allow Python under "
                                         "System Settings > Privacy & Security > Local Network, then try again.")
            raise HTTPException(
                502,
                f"Couldn't connect to {ip}. Check the IP and that ADB debugging is ON "
                f"(Settings > My Fire TV > Developer options; no Developer options? Open About and press "
                f"Select 7 times on the device name), then approve the prompt on the TV. ({exc})")
        # Disconnect or Remove was clicked while this connected: don't keep the link
        if entry.get("epoch", 0) != epoch or fleet.get(ip) is not entry:
            await _quiet_close(dev)
            raise HTTPException(409, f"{ip} was disconnected.")
        entry["dev"] = dev
        entry["slow"] = False
        return dev


def require_connected(ip: str) -> AdbDeviceTcpAsync:
    """Commands only run on devices the user has explicitly connected."""
    if ip not in fleet:
        raise HTTPException(404, f"{ip} is not in the fleet.")
    dev = fleet[ip]["dev"]
    if dev is None or not dev.available:
        raise HTTPException(409, f"{ip} is not connected. Click Connect first.")
    return dev


async def run_shell_connected(ip: str, command: str, decode: bool = True):
    """Run a shell command on a device the user has connected (no auto-connect)."""
    dev = require_connected(ip)
    e = fleet[ip]
    async with e["lock"]:
        # the link dropped while this waited its turn: fail now, not after another timeout
        if e["dev"] is not dev:
            raise HTTPException(409, f"{ip} is not answering. Fleet Remote keeps trying to reconnect.")
        try:
            out = await dev.shell(command, decode=decode, read_timeout_s=10.0)
        except Exception as exc:
            # One slow answer (a busy Cube) is not a dead link: keep it the first
            # time, drop it when the next command times out too or anything else fails.
            slow = isinstance(exc, (TcpTimeoutException, AdbTimeoutError, asyncio.TimeoutError))
            if slow and not e.get("slow"):
                e["slow"] = True
            else:
                _drop_dev(ip, dev)
            raise HTTPException(502, f"Command failed on {ip}: {exc}")
        e["slow"] = False
        e["ok_at"] = time.monotonic()        # the link answered: no knock needed for a moment
        return out


# ---- power: standby indicator, Power key, quiet reconnect -------------------
# Fire: dumpsys power says Awake or Asleep (standby: TV picture off, ADB still
# up). Asked every 2 s while connected, every 1 s while its live video runs:
# waking a Cube while our live video runs crashes its display service
# (SurfaceFlinger; Android then restarts itself and the app loses the Cube),
# so live video stops as soon as the Cube goes to standby.
POWER_EVERY = 2.0
POWER_EVERY_LIVE = 1.0
RECONNECT_EVERY = 10.0               # a connected device whose link dropped
# Power pulled or cable out: the open link just goes quiet (no reset), so a command
# would wait its full timeout. So every PROBE_EVERY s knock on the ADB port (unless a
# command of ours got its answer within that time): a Cube that is up answers that at
# once however busy it is, so PROBE_MISSES knocks in a row that go unanswered mean it
# is gone (one alone can get lost on a busy network).
PROBE_EVERY = 1.0
PROBE_TIMEOUT = 1.0
PROBE_MISSES = 3
_power: dict[str, dict] = {}         # ip -> {"state": "on" | "asleep", "t": last check, "busy"}
_bg: set = set()                     # background tasks (kept so they are not garbage collected)


def _spawn(coro) -> asyncio.Task:
    t = asyncio.get_running_loop().create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)
    return t


_WAKE_CMD = "dumpsys power 2>/dev/null | grep -o -E 'mWakefulness=[A-Za-z]+'"


def _wake_of(out: Optional[str]) -> Optional[str]:
    m = re.search(r"mWakefulness=(\w+)", out or "")
    if not m:
        return None
    return "asleep" if m.group(1) in ("Asleep", "Dozing") else "on"


async def fire_wakefulness(ip: str) -> Optional[str]:
    """on | asleep | None (no answer)"""
    return _wake_of(await run_shell_connected(ip, _WAKE_CMD))


# Just powered up: ADB answers about a minute before Android has finished
# starting (a Cube boots in ~70 s), and a video server started before then fails
# or freezes. Live video waits until it has booted and had a moment to settle.
BOOT_SETTLE = 90.0                   # seconds since power-on before live video starts
_STATE_CMD = _WAKE_CMD + "; echo \"b=$(getprop sys.boot_completed) u=$(cut -d' ' -f1 /proc/uptime)\""


async def fire_state(ip: str) -> tuple:
    """(wakefulness, ready) from one shell call; ready is False while the Cube
    is still starting up."""
    out = await run_shell_connected(ip, _STATE_CMD)
    m = re.search(r"b=(\S*) u=([\d.]+)", out or "")
    ready = True                     # no clear answer: let the stream try
    if m:
        ready = m.group(1) == "1" and float(m.group(2)) >= BOOT_SETTLE
    return _wake_of(out), ready


async def _check_power(ip: str, p: dict) -> None:
    seq = p.get("seq")
    try:
        w = await fire_wakefulness(ip)
        if w and p.get("seq") == seq:    # Power wasn't pressed while this asked
            p["state"] = w
        st = _streams.get(ip)
        if w == "asleep" and st:
            _begin_close(st, fast=True)
    except Exception:
        pass
    finally:
        p["t"] = time.monotonic()
        p["busy"] = False


# struct linger {on, 0 s}: closing the socket sends a reset instead of the normal
# FIN hand-shake, so the PC doesn't keep the closed knock for 2-4 minutes (TIME_WAIT).
# One knock per device per second would otherwise park 120+ ports per device and
# Windows runs out of ports with ~70 devices. A plain socket, not an asyncio stream:
# the Windows transport calls shutdown() before close(), which sends the FIN anyway.
# sock_connect on Windows takes only a numeric address and the socket family has to
# match it: an IP address is taken apart here (no thread-pool job for every knock), a
# host name is looked up at most once a minute and the answer kept.
_LINGER0 = struct.pack("HH" if sys.platform == "win32" else "ii", 1, 0)
RESOLVE_KEEP = 60.0
_resolved: dict[str, tuple] = {}     # host name -> (when, [(family, type, proto, sockaddr)])


async def _resolve(ip: str) -> list:
    """[(family, type, proto, sockaddr)] of the device's ADB port; [] when the name is unknown."""
    if "%" not in ip:                 # (a scoped IPv6 address goes through getaddrinfo)
        with suppress(ValueError):
            v = ipaddress.ip_address(ip).version
            return [(socket.AF_INET if v == 4 else socket.AF_INET6, socket.SOCK_STREAM, 0,
                     (ip, ADB_PORT) if v == 4 else (ip, ADB_PORT, 0, 0))]
    now = time.monotonic()
    hit = _resolved.get(ip)
    if hit and now - hit[0] < RESOLVE_KEEP:
        return hit[1]
    try:
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(ip, ADB_PORT, type=socket.SOCK_STREAM), PROBE_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return []
    found = [(family, kind, proto, addr) for family, kind, proto, _, addr in infos]
    if found:
        _resolved[ip] = (now, found)
    return found


async def _knock(ip: str) -> bool:
    loop = asyncio.get_running_loop()
    for family, kind, proto, addr in await _resolve(ip):
        s = socket.socket(family, kind, proto)
        s.setblocking(False)
        try:
            await asyncio.wait_for(loop.sock_connect(s, addr), PROBE_TIMEOUT)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _LINGER0)
            return True
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            s.close()
    return False


async def _probe_link(ip: str, p: dict, dev) -> None:
    try:
        for _ in range(PROBE_MISSES):
            if await _knock(ip):
                return
        _drop_dev(ip, dev)
    except Exception:
        pass
    finally:
        p["probing"] = False


ATV_RECONNECT_EVERY = 30.0           # an Apple TV whose connection dropped (its scan takes ~6 s)


async def _reconnect(ip: str) -> None:
    d = fleet.get(ip)
    try:
        if d is not None and d.get("want"):
            if d.get("type") == "appletv":
                await atv_connect(ip)
            else:
                await ensure_connected(ip)
                _power.pop(ip, None)
    except Exception:
        pass
    finally:
        if d is not None:
            d["retry"] = time.monotonic()
            d["reconnecting"] = False


async def _power_loop() -> None:
    while True:
        await asyncio.sleep(0.5)
        with suppress(Exception):
            now = time.monotonic()
            for ip, d in list(fleet.items()):
                atv = d.get("type") == "appletv"
                if is_connected(d):
                    if atv:
                        continue     # pyatv keeps its own power state up to date
                    p = _power.setdefault(ip, {})
                    every = POWER_EVERY_LIVE if ip in _streams else POWER_EVERY
                    if not p.get("busy") and now - p.get("t", -1e9) >= every:
                        p["busy"] = True
                        _spawn(_check_power(ip, p))
                    if (not p.get("probing") and now - p.get("knocked", -1e9) >= PROBE_EVERY
                            and now - d.get("ok_at", -1e9) >= PROBE_EVERY):
                        p["probing"] = True
                        p["knocked"] = now
                        _spawn(_probe_link(ip, p, d["dev"]))
                else:
                    # Link lost (unplugged, off the network): its video socket gets no
                    # end and would hang forever on the last frame, so close it here.
                    st = _streams.get(ip)
                    if st is not None and st.get("closer") is None:
                        _begin_close(st, fast=True)
                    every = ATV_RECONNECT_EVERY if atv else RECONNECT_EVERY
                    if (d.get("want") and not d.get("reconnecting")
                            and now - d.get("retry", -1e9) >= every):
                        d["reconnecting"] = True
                        d["retry"] = now
                        _spawn(_reconnect(ip))


def power_of(ip: str, d: dict) -> Optional[str]:
    """on | asleep | offline (connected before, not answering now) | None (not known)"""
    if d.get("type") == "appletv":
        atv = d.get("atv")
        if atv is None:
            return None
        with suppress(Exception):
            return {pyatv.const.PowerState.On: "on",
                    pyatv.const.PowerState.Off: "asleep"}.get(atv.power.power_state)
        return None
    if is_connected(d):
        return (_power.get(ip) or {}).get("state")
    return "offline" if d.get("want") else None


async def fire_power(ip: str, value: str) -> str:
    """Standby (off) or wake (on). Live video stops first, never runs across a
    sleep or a wake (see above)."""
    require_connected(ip)
    if value == "toggle":
        # the power watcher asked a moment ago: use its answer, else ask now
        p = _power.get(ip) or {}
        state = p.get("state") if time.monotonic() - p.get("t", -1e9) < POWER_EVERY * 2 else None
        if state is None:
            state = await fire_wakefulness(ip)
        value = "on" if state == "asleep" else "off"
    st = _streams.get(ip)
    if st:
        await _close_stream(st)
    await run_shell_connected(ip, f"input keyevent {KEYS['sleep' if value == 'off' else 'wake']}")
    p = _power.setdefault(ip, {})        # same dict: a check in flight keeps its busy flag
    p.update(state="asleep" if value == "off" else "on", t=time.monotonic(), seq=p.get("seq", 0) + 1)
    return value


async def fire_reboot(ip: str) -> None:
    """Restart, as `adb reboot` does (adbd asks Android to reboot; no root needed).
    Live video stops first. The link is forgotten at once instead of waiting for
    the knocks to notice, so the device shows "Not responding" until it is back
    (about a minute) and the watcher reconnects it as after a power cut."""
    dev = require_connected(ip)
    e = fleet[ip]
    st = _streams.get(ip)
    if st:
        await _close_stream(st)
    async with e["lock"]:
        if e["dev"] is not dev:
            raise HTTPException(409, f"{ip} is not answering. Fleet Remote keeps trying to reconnect.")
        try:
            await dev.reboot(read_timeout_s=10.0)
        except Exception as exc:
            _drop_dev(ip, dev)
            raise HTTPException(502, f"Restart failed on {ip}: {exc}")
    _drop_dev(ip, dev)
    _power.pop(ip, None)


class _AtvWatch:
    """pyatv tells us when the Apple TV connection drops: forget it, so the
    list shows it offline and the quiet reconnect brings it back."""

    def __init__(self, ip: str):
        self.ip = ip
        self.atv = None

    def _gone(self):
        d = fleet.get(self.ip)
        if d is not None and d.get("atv") is self.atv:
            d["atv"] = None
            _spawn(_quiet_close(self.atv))   # pyatv keeps its sockets until close()

    def connection_lost(self, exception):
        self._gone()

    def connection_closed(self):
        self._gone()


async def atv_connect(ip: str):
    """Connect to a paired Apple TV using stored credentials."""
    entry = fleet[ip]
    creds = entry.get("atv_credentials") or {}
    if not creds:
        raise HTTPException(428, "This Apple TV needs pairing first.")
    epoch = entry.get("epoch", 0)        # before the wait: a Disconnect meanwhile must win
    async with entry["clock"]:
        if entry.get("atv") is not None:
            return                   # connected meanwhile (Connect click + quiet reconnect)
        await _pyatv_ready()
        loop = asyncio.get_running_loop()
        results = await pyatv.scan(loop, hosts=[ip], timeout=6)
        if not results:
            raise HTTPException(502, f"Couldn't find an Apple TV at {ip}. "
                                     "Check the IP and that it's awake." + _MAC_LAN_HINT)
        conf = results[0]
        for proto_name, cred in creds.items():
            with suppress(Exception):
                conf.set_credentials(pyatv.const.Protocol[proto_name], cred)
        try:
            atv = await pyatv.connect(conf, loop)
        except Exception as exc:
            raise HTTPException(502, f"Apple TV connect failed on {ip}: {exc}" + _MAC_LAN_HINT)
        if fleet.get(ip) is not entry or entry.get("epoch", 0) != epoch:   # removed / disconnected meanwhile
            await _quiet_close(atv)
            raise HTTPException(409, f"{ip} was disconnected.")
        watch = _AtvWatch(ip)
        watch.atv = atv
        atv.listener = watch         # pyatv keeps only a weak reference: hold it here
        entry["atv_watch"] = watch
        entry["atv"] = atv


def require_atv(ip: str):
    if ip not in fleet:
        raise HTTPException(404, f"{ip} is not in the fleet.")
    atv = fleet[ip].get("atv")
    if atv is None:
        raise HTTPException(409, f"{ip} is not connected. Click Connect first.")
    return atv


async def atv_command(ip: str, ctype: str, value: str) -> Optional[str]:
    atv = require_atv(ip)
    try:
        async with fleet[ip]["lock"]:
            if ctype == "power":
                pw = atv.power
                on = (pw.power_state == pyatv.const.PowerState.Off) if value == "toggle" else value == "on"
                await (pw.turn_on() if on else pw.turn_off())
                return "on" if on else "off"
            if ctype == "key":
                if value in ATV_AUDIO:
                    await getattr(atv.audio, value)()
                elif value in ATV_RC:
                    await getattr(atv.remote_control, ATV_RC[value])()
                elif value == "delete":
                    raise HTTPException(400, "Backspace isn't supported on Apple TV.")
                else:
                    raise HTTPException(400, f"'{value}' isn't supported on Apple TV.")
            elif ctype == "text":
                await atv.keyboard.text_append(value)
            elif ctype == "clear":
                await atv.keyboard.text_clear()
            elif ctype == "settings":
                try:
                    await atv.apps.launch_app("com.apple.TVSettings")
                except Exception as exc:          # not a dead link: older tvOS / no Companion pairing
                    raise HTTPException(400, f"This Apple TV didn't open Settings ({exc}).")
            elif ctype == "reboot":           # pyatv has no restart
                raise HTTPException(400, "Restart isn't available for Apple TV from here. "
                                         "On the TV: Settings > System > Restart.")
            else:
                raise HTTPException(400, f"'{ctype}' isn't supported on Apple TV.")
    except HTTPException:
        raise
    except (NotImplementedError, pyatv.exceptions.BlockedStateError, pyatv.exceptions.InvalidStateError,
            pyatv.exceptions.CommandError) as exc:          # "can't do that now": the link is fine
        raise HTTPException(400, f"{fleet[ip]['name']} can't do that: {exc or type(exc).__name__}")
    except Exception as exc:
        if fleet.get(ip, {}).get("atv") is atv:
            fleet[ip]["atv"] = None
            _spawn(_quiet_close(atv))
        raise HTTPException(502, f"Command failed on {ip}: {exc}. Reconnect and retry.")


# ---- Apple TV pairing (one at a time) ----
atv_pairing = {"ip": None, "handler": None, "queue": [], "config": None}
_pair_step = asyncio.Lock()           # Start/PIN clicked twice: the second waits, the first wins


async def _pair_reset() -> None:
    """Forget the pairing in progress (done, failed or cancelled)."""
    h = atv_pairing["handler"]
    atv_pairing.update({"ip": None, "handler": None, "queue": [], "config": None})
    if h is not None:
        with suppress(Exception):
            await asyncio.wait_for(h.close(), 5)


async def _atv_pair_next():
    ip = atv_pairing["ip"]
    conf = atv_pairing["config"]
    loop = asyncio.get_running_loop()
    while atv_pairing["queue"]:
        proto = atv_pairing["queue"][0]
        if conf.get_service(proto) is None:
            atv_pairing["queue"].pop(0)
            continue
        try:
            pairing = await pyatv.pair(conf, proto, loop)
            atv_pairing["handler"] = pairing
            await pairing.begin()
        except Exception as exc:
            await _pair_reset()
            raise HTTPException(502, f"Couldn't start {proto.name} pairing with {ip}: {exc}")
        if pairing.device_provides_pin:
            return {"status": "need_pin", "protocol": proto.name}
        try:
            await pairing.finish()
        except Exception as exc:
            await _pair_reset()
            raise HTTPException(400, f"{proto.name} pairing with {ip} failed: {exc}")
        if pairing.has_paired and ip in fleet:
            fleet[ip]["atv_credentials"][proto.name] = pairing.service.credentials
        with suppress(Exception):
            await pairing.close()
        atv_pairing["handler"] = None
        atv_pairing["queue"].pop(0)
    if ip in fleet and not fleet[ip].get("atv_credentials"):
        await _pair_reset()
        raise HTTPException(400, f"{fleet[ip]['name']} doesn't offer Companion pairing "
                                 "(it needs tvOS 13 or newer).")
    save_devices()
    try:
        if ip in fleet:
            await atv_connect(ip)
            fleet[ip]["want"] = True
    finally:
        await _pair_reset()
    return {"status": "paired"}



class AddReq(BaseModel):
    ip: str
    name: Optional[str] = None
    type: str = "fire"
    group: Optional[str] = None


class RenameReq(BaseModel):
    name: str


class GroupReq(BaseModel):
    group: str


class GroupNameReq(BaseModel):
    name: str


class IpReq(BaseModel):
    ip: str


class AtvPairStartReq(BaseModel):
    ip: str


class AtvPinReq(BaseModel):
    pin: str


class CommandReq(BaseModel):
    targets: list[str]
    type: str = "key"
    value: str


@app.get("/api/logo")
async def logo():
    # Look for a user-supplied logo in the likely locations.
    candidates = [
        DATA_DIR / "logo.png",
        Path.cwd() / "logo.png",
        Path(sys.argv[0]).resolve().parent / "logo.png",
    ]
    for c in candidates:
        with suppress(Exception):
            if c.is_file():
                return FileResponse(str(c), media_type="image/png")
    raise HTTPException(404, "No logo.png found")


@app.get("/api/meta")
async def meta():
    return {"app_version": APP_VERSION}


class ClientLogReq(BaseModel):
    msg: str


@app.post("/api/clientlog")
async def client_log(req: ClientLogReq):
    """Live video decoder trouble seen by the page lands in error.log too."""
    _log_error("page: " + " ".join(req.msg.split())[:1000])
    return {"ok": True}


@app.get("/api/devices")
async def list_devices():
    return {"devices": [
        {"ip": ip, "name": d["name"], "type": d.get("type", "fire"), "group": d.get("group", ""),
         "connected": is_connected(d), "power": power_of(ip, d)}
        for ip, d in fleet.items()], "groups": groups}


_HOSTNAME = re.compile(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*")


def _valid_host(ip: str) -> bool:
    with suppress(ValueError):
        ipaddress.ip_address(ip)
        return True
    if re.fullmatch(r"[\d.]+", ip):
        return False                      # digits and dots that aren't an IP (1.2.3.4.5, 999.1.1.1)
    return len(ip) <= 253 and bool(_HOSTNAME.fullmatch(ip))


@app.post("/api/devices")
async def add_device(req: AddReq):
    ip = req.ip.strip()
    if not ip:
        raise HTTPException(400, "IP is required.")
    if not _valid_host(ip):
        raise HTTPException(400, f"'{ip}' is not an IP address or host name.")
    if req.type not in ("fire", "appletv"):
        raise HTTPException(400, "type must be 'fire' or 'appletv'")
    if ip in fleet and fleet[ip].get("type", "fire") != req.type:
        raise HTTPException(409, f"{ip} is already in the list as "
                                 f"{'an Apple TV' if fleet[ip].get('type') == 'appletv' else 'a Fire TV'}. "
                                 "Remove it first to change its type.")
    n = len(groups)
    register(ip, " ".join((req.name or "").split())[:60] or ip, req.type, group=req.group)
    save_devices()
    if len(groups) != n:
        save_groups()
    return {"ok": True}


@app.put("/api/devices/{ip}/name")
async def rename_device(ip: str, req: RenameReq):
    if ip not in fleet:
        raise HTTPException(404, "Device not found.")
    name = " ".join(req.name.split())[:60]
    fleet[ip]["name"] = name or ip
    save_devices()
    return {"ok": True, "name": fleet[ip]["name"]}


@app.put("/api/devices/{ip}/ip")
async def change_ip(ip: str, req: IpReq):
    if ip not in fleet:
        raise HTTPException(404, "Device not found.")
    new = req.ip.strip()
    if not _valid_host(new):
        raise HTTPException(400, f"'{new}' is not an IP address or host name.")
    if new == ip:
        return {"ok": True, "ip": ip, "was_connected": is_connected(fleet[ip])}
    if new in fleet:
        raise HTTPException(409, f"{new} is already in the list ({fleet[new]['name']}).")
    entry = fleet[ip]
    was = bool(entry.get("want")) or is_connected(entry)
    # the old address is let go like Disconnect: a connect in flight must not keep its link
    entry["want"] = False
    entry["epoch"] = entry.get("epoch", 0) + 1
    if atv_pairing["ip"] == ip:
        await _pair_reset()
    st = _streams.get(ip)
    if st is not None:
        with suppress(Exception):
            await _close_stream(st)
    await _drop_links(entry)
    for per_ip in (_power, _last_end, _stream_locks):
        per_ip.pop(ip, None)
    if entry["name"] == ip:
        entry["name"] = new
    moved = [(new if k == ip else k, v) for k, v in fleet.items()]    # keeps its place in the list
    fleet.clear()
    fleet.update(moved)
    save_devices()
    return {"ok": True, "ip": new, "was_connected": was}


@app.put("/api/devices/{ip}/group")
async def set_group(ip: str, req: GroupReq):
    if ip not in fleet:
        raise HTTPException(404, "Device not found.")
    n = len(groups)
    fleet[ip]["group"] = _note_group(req.group)
    save_devices()
    if len(groups) != n:
        save_groups()
    return {"ok": True, "group": fleet[ip]["group"]}


@app.post("/api/groups")
async def add_group(req: GroupNameReq):
    g = _clean_group(req.name)
    if not g:
        raise HTTPException(400, "Enter a group name.")
    if _find_group(g):
        raise HTTPException(409, f"There is already a group called '{_find_group(g)}'.")
    groups.append(g)
    save_groups()
    return {"ok": True, "name": g}


@app.put("/api/groups/{name:path}")
async def rename_group(name: str, req: GroupNameReq):
    old = _find_group(name)
    if not old:
        raise HTTPException(404, "Group not found.")
    new = _clean_group(req.name)
    if not new:
        raise HTTPException(400, "Enter a group name.")
    other = _find_group(new)
    if other and other != old:
        raise HTTPException(409, f"There is already a group called '{other}'.")
    groups[groups.index(old)] = new
    for d in fleet.values():
        if d.get("group") == old:
            d["group"] = new
    save_groups()
    save_devices()
    return {"ok": True, "name": new}


@app.delete("/api/groups/{name:path}")
async def delete_group(name: str):
    old = _find_group(name)
    if not old:
        raise HTTPException(404, "Group not found.")
    groups.remove(old)
    moved = 0
    for d in fleet.values():
        if d.get("group") == old:
            d["group"] = ""
            moved += 1
    save_groups()
    save_devices()
    return {"ok": True, "moved": moved}


@app.delete("/api/devices/{ip}")
async def remove_device(ip: str):
    entry = fleet.pop(ip, None)
    _power.pop(ip, None)
    _last_end.pop(ip, None)
    st = _streams.get(ip)
    if st is not None:
        with suppress(Exception):
            await _close_stream(st)
    _stream_locks.pop(ip, None)
    if entry:
        entry["epoch"] = entry.get("epoch", 0) + 1     # a connect in flight must not keep its link
        entry["want"] = False
        await _drop_links(entry)
    save_devices()
    return {"ok": True}


@app.post("/api/devices/{ip}/connect")
async def connect_device(ip: str):
    if ip not in fleet:
        raise HTTPException(404, "Unknown device.")
    if fleet[ip].get("type") == "appletv":
        if not fleet[ip].get("atv_credentials"):
            return {"ok": False, "need_pairing": True}
        await atv_connect(ip)
        fleet[ip]["want"] = True
        return {"ok": True}
    await ensure_connected(ip)
    fleet[ip]["want"] = True
    _power.pop(ip, None)
    return {"ok": True}


@app.post("/api/devices/{ip}/disconnect")
async def disconnect_device(ip: str):
    if ip not in fleet:
        raise HTTPException(404, "Unknown device.")
    entry = fleet[ip]
    entry["want"] = False
    entry["epoch"] = entry.get("epoch", 0) + 1
    st = _streams.get(ip)
    if st is not None:
        with suppress(Exception):
            await _close_stream(st)
    await _drop_links(entry)
    return {"ok": True}


@app.post("/api/atv/pair/start")
async def atv_pair_start(req: AtvPairStartReq):
    ip = req.ip
    if ip not in fleet or fleet[ip].get("type") != "appletv":
        raise HTTPException(404, "Unknown Apple TV.")
    if _pair_step.locked():
        raise HTTPException(409, "A pairing step is already running. Wait a moment.")
    async with _pair_step:
        await _pair_reset()                 # cancel any stale pairing
        await _pyatv_ready()
        loop = asyncio.get_running_loop()
        results = await pyatv.scan(loop, hosts=[ip], timeout=6)
        if not results:
            raise HTTPException(502, f"No Apple TV found at {ip}. Check the IP and that it's awake."
                                     + _MAC_LAN_HINT)
        # one PIN: Companion covers remote, keyboard and volume
        atv_pairing.update({"ip": ip, "handler": None,
                            "queue": [pyatv.const.Protocol.Companion], "config": results[0]})
        return await _atv_pair_next()


@app.post("/api/atv/pair/pin")
async def atv_pair_pin(req: AtvPinReq):
    if _pair_step.locked():
        raise HTTPException(409, "A pairing step is already running. Wait a moment.")
    async with _pair_step:
        return await _atv_pair_pin(req)


async def _atv_pair_pin(req: AtvPinReq):
    pairing = atv_pairing["handler"]
    if pairing is None or not atv_pairing["queue"]:
        raise HTTPException(400, "No pairing in progress.")
    ip = atv_pairing["ip"]
    proto = atv_pairing["queue"][0]
    try:
        pairing.pin(int(req.pin.strip()))
    except ValueError:
        raise HTTPException(400, "PIN must be numbers only.")
    ok, why = False, ""
    try:
        await pairing.finish()
        ok = pairing.has_paired
    except Exception as exc:
        why = f" ({exc})"
    if atv_pairing["handler"] is not pairing:      # cancelled or restarted meanwhile
        raise HTTPException(409, "Pairing was cancelled.")
    if not ok:
        await _pair_reset()
        raise HTTPException(400, f"{proto.name} pairing failed{why}. Wrong code? Start again.")
    if ip in fleet:
        fleet[ip]["atv_credentials"][proto.name] = pairing.service.credentials
        save_devices()                  # persist the moment we have it
    with suppress(Exception):
        await pairing.close()
    atv_pairing["handler"] = None
    atv_pairing["queue"].pop(0)
    return await _atv_pair_next()


@app.post("/api/atv/pair/cancel")
async def atv_pair_cancel():
    await _pair_reset()
    return {"ok": True}


@app.post("/api/command")
async def command(req: CommandReq):
    if not req.targets:
        raise HTTPException(400, "No target devices selected.")
    if req.type not in ("key", "clear", "text", "power", "settings", "reboot"):
        raise HTTPException(400, f"Unknown command type: {req.type}")
    if req.type == "power" and req.value not in ("on", "off", "toggle"):
        raise HTTPException(400, f"Unknown power command: {req.value}")
    if req.type == "key" and req.value not in KEYS:
        raise HTTPException(400, f"Unknown key: {req.value}")

    atv_targets = [ip for ip in req.targets
                   if ip in fleet and fleet[ip].get("type") == "appletv"]
    fire_targets = [ip for ip in req.targets if ip not in atv_targets]

    async def atv_one(ip):
        try:
            v = await atv_command(ip, req.type, req.value)
            return {"ip": ip, "ok": True, **({"power": v} if v else {})}
        except HTTPException as e:
            return {"ip": ip, "ok": False, "error": e.detail}
        except Exception as e:
            return {"ip": ip, "ok": False, "error": f"{ip}: {e or type(e).__name__}"}

    if req.type in ("power", "reboot"):
        async def pw(ip):
            try:
                if req.type == "reboot":
                    await fire_reboot(ip)
                    return {"ip": ip, "ok": True}
                return {"ip": ip, "ok": True, "power": await fire_power(ip, req.value)}
            except HTTPException as e:
                return {"ip": ip, "ok": False, "error": e.detail}
            except Exception as e:
                return {"ip": ip, "ok": False, "error": f"{ip}: {e or type(e).__name__}"}
        jobs = [atv_one(ip) for ip in atv_targets] + [pw(ip) for ip in fire_targets]
        return {"results": list(await asyncio.gather(*jobs))}

    # While live video runs, keys and text go over its control link in a few
    # ms (ctl_send); `input` on the Cube takes ~0.7 s per call and is the fallback.
    fast_keys: Optional[list] = None
    fast_text: Optional[str] = None
    shell_cmd = ""
    power_key = req.type == "key" and req.value in ("wake", "sleep", "power")
    if req.type == "key":
        shell_cmd = f"input keyevent {KEYS[req.value]}"
        if not power_key:
            fast_keys = [KEYS[req.value]]
    elif req.type == "settings":
        # Fire OS's own Settings page (the launcher's); WAKEUP first so it also works from standby
        shell_cmd = "input keyevent 224; am start -a android.settings.SETTINGS"
    elif req.type == "clear":
        # Jump to end of the focused field, then delete backwards 60 times.
        fast_keys = [123] + [67] * 60
        shell_cmd = "input keyevent 123 " + " ".join(["67"] * 60)
    elif req.type == "text":
        # `input text` quirks: spaces must be %s; quote safely for the shell.
        t = req.value
        if not t:
            raise HTTPException(400, "No text to send.")
        fast_text = t
        t = t.replace("'", "'\\''").replace(" ", "%s")
        # Fire OS kills `input` on characters it can't type (exit 137, trace only in logcat)
        shell_cmd = f"input text '{t}' 2>&1; echo \"fr_rc=$?\""

    async def one(ip):
        try:
            # a key wakes a Cube in standby, Power puts it in standby: its live
            # video must be off by then (see fire_power)
            st = _streams.get(ip)
            if st and (power_key or (_power.get(ip) or {}).get("state") == "asleep"):
                await _close_stream(st)
            elif st and (fast_keys or fast_text) and await ctl_send(st, fast_keys, fast_text):
                return {"ip": ip, "ok": True, "via": "live"}
            cmd = shell_cmd
            if req.type == "settings" and st and st.get("closer") is None:
                cmd = "am start -a android.settings.SETTINGS"     # live video runs, so it is awake
            out = await run_shell_connected(ip, cmd)
            if req.type == "text" and ("Exception" in str(out) or "fr_rc=0" not in str(out)):
                return {"ip": ip, "ok": False, "error": f"{fleet[ip]['name']} can't type some of these characters "
                                                        "(accents, emoji). With Live video on, accents work."}
            if req.type == "settings" and "Error" in str(out):
                return {"ip": ip, "ok": False, "error": f"{fleet[ip]['name']} didn't open Settings."}
            return {"ip": ip, "ok": True}
        except HTTPException as e:
            return {"ip": ip, "ok": False, "error": e.detail}
        except Exception as e:
            return {"ip": ip, "ok": False, "error": f"{ip}: {e or type(e).__name__}"}

    jobs = [atv_one(ip) for ip in atv_targets] + [one(ip) for ip in fire_targets]
    return {"results": list(await asyncio.gather(*jobs))}


@app.get("/api/devices/{ip}/screenshot")
async def screenshot(ip: str):
    if ip in fleet and fleet[ip].get("type") == "appletv":
        raise HTTPException(501, "Screenshots aren't supported on Apple TV.")
    dev = require_connected(ip)
    remote = "/sdcard/.fleetctl_screen.png"
    local = DATA_DIR / f".shot_{re.sub(r'[^0-9A-Za-z]', '_', ip)}.png"
    async with fleet[ip]["lock"]:
        if fleet.get(ip, {}).get("dev") is not dev:
            raise HTTPException(409, f"{ip} is not answering. Fleet Remote keeps trying to reconnect.")
        # the PNG comes straight down the ADB link (exec-out): one round trip
        data = b""
        with suppress(Exception):
            data = await dev.exec_out("screencap -p", decode=False, read_timeout_s=15.0) or b""
        if not data.startswith(b"\x89PNG"):
            # no exec-out on this device: write it to its storage, then fetch it
            try:
                await dev.shell(f"screencap -p {remote}", read_timeout_s=15.0)
                await dev.pull(remote, str(local))
                data = local.read_bytes()
            except Exception as exc:
                raise HTTPException(502, f"Screenshot failed on {ip}: {exc}")
            finally:
                with suppress(Exception):
                    local.unlink()
                with suppress(Exception):
                    await dev.shell(f"rm -f {remote}", read_timeout_s=5.0)
    return Response(content=data, media_type="image/png")


# ---- protected video ----------------------------------------------------------
# Fire TV keeps playing video (DRM content, the hardware video layer) out of
# screenshots and live video, so the picture comes back black. When that
# happens the page asks here whether a video is playing, and says so on the
# screen instead of looking broken.
APP_NAMES = {
    "com.amazon.avod": "Prime Video",
    "com.amazon.firebat": "Prime Video",
    "com.amazon.avod.thirdpartyclient": "Prime Video",
    "com.netflix.ninja": "Netflix",
    "com.amazon.firetv.youtube": "YouTube",
    "com.disney.disneyplus": "Disney+",
    "com.amazon.tv.launcher": "Fire TV Home",
    "com.amazon.tv.settings": "Settings",
    "com.hulu.plus": "Hulu",
    "com.wbd.stream": "Max",
    "com.hbo.hbonow": "Max",
    "com.peacocktv.peacockandroid": "Peacock",
    "com.cbs.ott": "Paramount+",
    "com.apple.atve.amazon.appletv": "Apple TV",
    "com.espn.score_center": "ESPN",
    "com.dazn": "DAZN",
    "com.plexapp.android": "Plex",
    "org.xbmc.kodi": "Kodi",
}
# media session states with a video on screen: paused, playing, fast forward,
# rewind, buffering, connecting, skipping
_ON_SCREEN = {2, 3, 4, 5, 6, 8, 9, 10, 11}


@app.get("/api/devices/{ip}/playing")
async def playing(ip: str):
    if ip in fleet and fleet[ip].get("type") == "appletv":
        raise HTTPException(501, "Not supported on Apple TV.")
    out = await run_shell_connected(
        ip, "dumpsys window windows | grep -E 'mCurrentFocus'; "
            "dumpsys media_session | grep -E 'ownerPid=|package=|state=PlaybackState'; "
            # apps whose media audio is running right now (paused players don't count)
            "for u in $(dumpsys audio | grep -E ' -- state:started' | grep -E 'usage=USAGE_(MEDIA|GAME)' "
            "| grep -o -E 'u/pid:[0-9]+' | cut -d: -f2 | sort -u); do cmd package list packages --uid $u; done; "
            # processes holding a video decoder: a paused video keeps its decoder,
            # an app's own menu doesn't
            "echo ===RM; dumpsys media.resource_manager | sed -n '/Processes:/,/Events logs/p' "
            "| grep -E 'Pid:|Name:|video-codec|kVideoCodec'; "
            # where the TV draws the video (its own hardware plane, ~35 ms to ask)
            "echo ===SF; dumpsys SurfaceFlinger | sed -n '/^Display 0 HWC layers/,/^$/p'; wm size | grep Physical")
    return _parse_playing(out)


def _parse_playing(out: Optional[str]) -> dict:
    out, _, sf = (out or "").partition("===SF")
    out, _, rm = out.partition("===RM")
    focus, pkg, opid, sessions, running = None, None, None, [], set()
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            running.add(line[8:].split()[0])
        elif line.startswith("mCurrentFocus"):
            m = re.search(r"\s([\w.]+)/", line)
            if m and focus is None:
                focus = m.group(1)
        elif line.startswith("ownerPid="):
            m = re.match(r"ownerPid=(\d+)", line)
            opid = int(m.group(1)) if m else None
        elif line.startswith("package="):
            pkg = line[8:].strip()
        elif line.startswith("state=PlaybackState") and pkg:
            m = re.search(r"\{state=(\d+)", line)
            if m:
                sessions.append((pkg, int(m.group(1)), opid))
            pkg = opid = None
    decoders, rpid, rname, rm_ok = set(), None, "", False
    for line in rm.splitlines():
        line = line.strip()
        m = re.match(r"Pid:\s*(\d+)", line)
        if m:
            rpid, rname, rm_ok = int(m.group(1)), "", True
        elif line.startswith("Name:"):
            rname = line[5:].strip().lower()
        elif ("video-codec" in line or "kVideoCodec" in line) \
                and rpid is not None and "encoder" not in rname:
            decoders.add(rpid)       # (our own live video holds an encoder)
    # the app on screen with a video in it (even paused); else an app that says
    # it plays AND has its audio running (YouTube keeps a 'playing' session for
    # a while after you leave it: without the audio check that put a paused
    # YouTube label over a dark Settings screen, 11-21)
    ses = next((x for x in sessions if x[0] == focus and x[1] in _ON_SCREEN), None) \
        or next((x for x in sessions if x[1] == 3 and x[0] in running), None)
    hit = ses[0] if ses else None
    # active: the video is running, not paused. While the player is open (paused
    # too) the Cube's capture of that area is not what the TV shows: black, old
    # copies of the controls, or a frame that never updates again.
    active = bool(hit) and hit in running
    # Paused counts only while the app still holds a video decoder. YouTube keeps
    # its video layer and an old 'playing' session on its home page too (no
    # decoder there): v3.30 put 'Paused on the TV' over the YouTube menu, 11-21.
    # (resource manager list missing or in a format we don't know: cover any
    # paused player, as v3.30 did)
    held = bool(ses) and (not rm_ok or (ses[2] in decoders if ses[2] else bool(decoders)))
    if not (active or held):
        hit = None
    return {"playing": bool(hit), "active": active and bool(hit),
            "package": hit, "focus": focus, "app": APP_NAMES.get(hit or ""),
            "video": _video_plane(sf, hit) if hit else None}


def _video_plane(sf: str, pkg: Optional[str]) -> Optional[list]:
    """Where the playing video sits on the screen, as [left, top, right, bottom]
    fractions. Fire TV draws video on its own hardware plane (a SurfaceView
    layer), which the Cube's screen capture can't see: that area comes out
    black or as an old frozen frame (YouTube Shorts: a stuck copy flipping
    with the empty background), so the page covers it."""
    lines = sf.splitlines()
    frames = []
    for i, ln in enumerate(lines[:-1]):
        m = re.search(r"\|\s*(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s*\|", lines[i + 1])
        if m and "|" not in ln:
            frames.append((ln.strip(), [int(v) for v in m.groups()]))
    if not frames:
        return None
    m = re.search(r"Physical size:\s*(\d+)x(\d+)", sf)
    w, h = (int(m.group(1)), int(m.group(2))) if m else \
        (max(f[2] for _, f in frames), max(f[3] for _, f in frames))
    if w <= 0 or h <= 0:
        return None
    views = [(n, f) for n, f in frames if n.startswith("SurfaceView")]
    own = [v for v in views if pkg and pkg + "/" in v[0]]
    best = None
    for _, f in (own or views):
        x0, y0 = max(0, f[0]) / w, max(0, f[1]) / h
        x1, y1 = min(w, f[2]) / w, min(h, f[3]) / h
        area = (x1 - x0) * (y1 - y0)
        if area >= 0.01 and (best is None or area > best[0]):
            best = (area, [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)])
    return best[1] if best else None


# ---- never sleep (Fire TV) ----------------------------------------------------
# Fire OS starts its screen saver after system screen_off_timeout (5 min by
# default) and sleeps after secure sleep_timeout (20 min). Never sleep sets what
# the Fire TV on-call runbook uses to keep test devices awake (sleep timeout 0,
# stay awake while plugged in, a very long screen timeout), plus Amazon's
# contextual timeout (it kept .52 awake, 11-12) and the screen saver off. The
# values it replaces are kept in devices.json, and Disable puts them back.
# Sleep Mode Behavior (persist.sys.hdmi.sleep_mode: on = Black Screen, HDMI
# stays on; off = No Signal) is asked for too, but retail Fire OS lets only its
# own Settings app change it (setprop is refused), so it then stays as the TV says.
SLEEP_KEYS = (("secure", "sleep_timeout"), ("system", "screen_off_timeout"),
              ("secure", "contextual_screen_off_timeout"),
              ("global", "stay_on_while_plugged_in"), ("secure", "screensaver_enabled"))
SLEEP_NEVER = {"sleep_timeout": "0", "screen_off_timeout": "600000000",
               "contextual_screen_off_timeout": "2147483647",
               "stay_on_while_plugged_in": "7", "screensaver_enabled": "0",
               "hdmi_sleep_mode": "on"}
# Fire OS defaults: for Disable on a device that was already set to never sleep
SLEEP_DEFAULTS = {"sleep_timeout": "1200000", "screen_off_timeout": "300000",
                  "contextual_screen_off_timeout": None,
                  "stay_on_while_plugged_in": "0", "screensaver_enabled": "1",
                  "hdmi_sleep_mode": None}
DAY_MS = 86_400_000


class SleepReq(BaseModel):
    never: bool


def _fire_only(ip: str) -> None:
    if ip not in fleet:
        raise HTTPException(404, "Unknown device.")
    if fleet[ip].get("type") == "appletv":
        raise HTTPException(501, "Not supported on Apple TV.")


def _num(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


_SLEEP_GET = "; ".join(f"echo {k}=$(settings get {ns} {k})" for ns, k in SLEEP_KEYS) \
    + "; echo hdmi_sleep_mode=$(getprop persist.sys.hdmi.sleep_mode)"


async def _sleep_read(ip: str, before: str = "") -> dict:
    """The sleep settings as the Fire TV has them (None = not set), read in
    the same shell call as `before` (the changes, for a PUT)."""
    out = await run_shell_connected(ip, before + _SLEEP_GET)
    raw = {}
    for line in (out or "").splitlines():
        k, sep, v = line.strip().partition("=")
        if sep and k in SLEEP_NEVER:
            v = v.strip()
            raw[k] = None if v in ("", "null") else v
    return raw


def _sleep_view(ip: str, raw: dict) -> dict:
    st, so = _num(raw.get("sleep_timeout")), _num(raw.get("screen_off_timeout"))
    ct, on = _num(raw.get("contextual_screen_off_timeout")), _num(raw.get("stay_on_while_plugged_in"))
    no_sleep = st is not None and (st <= 0 or st >= DAY_MS)
    no_off = bool(on) or (so is not None and so >= DAY_MS) or (ct is not None and ct >= DAY_MS)
    return {"never": no_sleep and no_off, "sleep_ms": st, "screen_off_ms": so,
            "contextual_ms": ct, "stay_on": on or 0,
            "screensaver": raw.get("screensaver_enabled") != "0",
            "hdmi_sleep_mode": raw.get("hdmi_sleep_mode"),
            "saved": bool(fleet.get(ip, {}).get("sleep_prev"))}


@app.get("/api/devices/{ip}/sleep")
async def get_sleep(ip: str):
    _fire_only(ip)
    return _sleep_view(ip, await _sleep_read(ip))


@app.put("/api/devices/{ip}/sleep")
async def set_sleep(ip: str, req: SleepReq):
    _fire_only(ip)
    cur = await _sleep_read(ip)
    if req.never:
        # what Disable puts back. Keep the first copy: a second Enable after
        # something reset one timer would otherwise save half-never values.
        if not _sleep_view(ip, cur)["never"] and not fleet[ip].get("sleep_prev"):
            fleet[ip]["sleep_prev"] = cur
            save_devices()
        want = SLEEP_NEVER
    else:
        prev = fleet[ip].get("sleep_prev") or {}
        want = {k: (prev[k] if k in prev else d) for k, d in SLEEP_DEFAULTS.items()}
    cmds = []
    for ns, k in SLEEP_KEYS:
        v = _num(want.get(k))
        cmds.append(f"settings delete {ns} {k}" if v is None else f"settings put {ns} {k} {v}")
    h = want.get("hdmi_sleep_mode")
    if h in ("on", "off") and h != cur.get("hdmi_sleep_mode"):
        cmds.append(f"setprop persist.sys.hdmi.sleep_mode {h}")     # refused on retail Fire OS
    new = await _sleep_read(ip, "".join(c + " >/dev/null 2>&1; " for c in cmds))
    view = _sleep_view(ip, new)
    # forget the saved timers only once they are really back (a refused write
    # keeps them for the next try)
    if not req.never and not view["never"] and fleet[ip].get("sleep_prev"):
        fleet[ip]["sleep_prev"] = None
        save_devices()
        view["saved"] = False
    return view


# ---- HDMI output (Fire TV) ----------------------------------------------------
# What the Cube sends to the TV right now: picture mode and audio format from
# the HDMI driver (sysfs, ~35 ms on a Cube). MediaTek Fire TV Sticks don't let
# the shell read their HDMI driver: there the mode comes from dumpsys display,
# colour and HDR from Fire OS's sys.display-* properties and the audio from the
# HDMI output stream in dumpsys media.audio_flinger. 'active' = an app's media
# audio is running this moment. Fire OS gives no per-channel levels without
# root, so the page shows the channel count as presence bars, not meters.
_OUTPUT_GET = (
    "if [ -r /sys/class/amhdmitx/amhdmitx0/disp_mode ]; then echo ===DM; cat /sys/class/amhdmitx/amhdmitx0/disp_mode; "
    "echo ===CF; cat /sys/class/amhdmitx/amhdmitx0/config; "
    "echo ===HS; cat /sys/class/amhdmitx/amhdmitx0/hdmi_hdr_status 2>/dev/null; echo; "
    "echo ===HC; cat /sys/class/amhdmitx/amhdmitx0/hdr_cap 2>/dev/null; "
    "echo ===DV; cat /sys/class/amhdmitx/amhdmitx0/dv_cap 2>/dev/null; "
    "else echo ===DD; dumpsys display | grep -E 'mActiveModeId=|modeId |supportedModes'; "
    "echo ===AF; dumpsys media.audio_flinger | grep -E '^(Output|Input) thread|Standby:|HAL format:|Channel count:|Sample rate:|Output device:'; "
    "echo ===PR; for k in hdr hdr-mode pix-depth pix-format; do echo \"$k=$(getprop sys.display-$k)\"; done; fi; "
    "echo ===AU; dumpsys audio | grep -E ' -- state:started' | grep -c -E 'usage=USAGE_(MEDIA|GAME)'")
_CHROMA = {"420": "4:2:0", "422": "4:2:2", "444": "4:4:4", "rgb": "RGB"}
# hdmi_hdr_status names in the Amlogic HDMI driver
_HDR = {"sdr": "SDR", "hdr10-gamma_st2084": "HDR10", "hdr10-gamma_hlg": "HLG",
        "hdr10plus-vsif": "HDR10+", "hdr10-others": "HDR", "dolbyvision-std": "Dolby Vision",
        "dolbyvision-lowlatency": "Dolby Vision (low latency)"}


def _parse_hdr(status: str, cap: str, dv: str) -> tuple:
    """(HDR format sent right now, HDR formats the TV says it accepts) - None when unknown."""
    s = status.strip()
    now = _HDR.get(s.lower(), s) if s else None
    tv = None
    if cap.strip():
        tv = [name for pat, name in ((r"SMPTE ST 2084:\s*1", "HDR10"), (r"Hybrid Log-Gamma:\s*1", "HLG"),
                                     (r"HDR10Plus Supported:\s*1", "HDR10+")) if re.search(pat, cap)]
        if dv.strip() and not re.search(r"don't support", dv, re.I):
            tv.append("Dolby Vision")
    return now, tv


# Fire OS display properties (Settings > Display & Sounds > Display): the colour
# format and depth Fire OS set up for the HDMI link ('up to'), the HDR formats
# the TV announced in its EDID and the Dynamic Range setting. The only colour and
# HDR info a Fire TV Stick gives without root: it says what was configured, not
# what the frame going out this moment looks like.
_PROP_CHROMA = {"ycbcr": "YCbCr", "rgb": "RGB"}
_PROP_HDR = (("dolbyvision", "Dolby Vision"), ("hdr10plus", "HDR10+"), ("hdr10", "HDR10"), ("hlg", "HLG"))


def _props_video(pr: str) -> dict:
    """depth/chroma/hdr/hdr_tv from the sys.display-* lines, flagged 'configured'."""
    p = {k: v.strip().lower() for k, v in re.findall(r"(?m)^([\w-]+)=(.*)$", pr)}
    d = re.search(r"\d+", p.get("pix-depth", ""))
    tv, hdr_tv = p.get("hdr", ""), None
    if tv:
        hdr_tv = []
        for key, name in _PROP_HDR:          # longest names first: 'hdr10' is inside 'hdr10plus'
            if key in tv:
                hdr_tv.append(name)
                tv = tv.replace(key, "")
    mode = p.get("hdr-mode", "")
    if hdr_tv is None:
        hdr = None
    elif not hdr_tv or mode in ("disabled", "disable", "sdr", "off"):
        hdr = "SDR"
    elif mode == "forced":
        hdr = "always on"
    elif mode == "adaptive":
        hdr = "when the content is HDR"
    else:
        hdr = "not readable on this model"
    return {"depth": int(d.group(0)) if d else None, "chroma": _PROP_CHROMA.get(p.get("pix-format", "")),
            "hdr": hdr, "hdr_tv": hdr_tv, "configured": True}


# audio_flinger format names as the page shows them (the Cube's driver says L-PCM)
_AF_FORMATS = {"PCM": "L-PCM", "AC3": "Dolby Digital", "E_AC3": "Dolby Digital Plus", "E_AC3_JOC": "Dolby Atmos",
               "AC4": "Dolby AC-4", "MAT": "Dolby TrueHD", "DTS": "DTS", "DTS_HD": "DTS-HD", "IEC61937": "bitstream"}


def _af_audio(af: str) -> Optional[dict]:
    """The HDMI output stream of dumpsys media.audio_flinger: the one playing now
    (a coded one first, then the most channels), else the idle one the Fire TV keeps open."""
    best = None
    chunks = re.split(r"(?m)^(Output|Input) thread ", af)
    for kind, body in zip(chunks[1::2], chunks[2::2]):
        if kind != "Output" or not re.search(r"Output device:.*HDMI", body):
            continue
        f = re.search(r"HAL format:.*\(AUDIO_FORMAT_(\w+)\)", body)
        ch = re.search(r"Channel count:\s*(\d+)", body)
        sr = re.search(r"Sample rate:\s*(\d+)", body)
        name = f.group(1) if f else "PCM"
        if name.startswith("PCM"):
            name = "PCM"
        key = (re.search(r"Standby:\s*no", body) is not None, name != "PCM",
               int(ch.group(1)) if ch else 0, int(sr.group(1)) if sr else 0)
        if best is None or key > best[0]:
            best = (key, name)
    if best is None:
        return None
    (_, _, ch, sr), name = best
    return {"format": _AF_FORMATS.get(name, name.replace("_", " ")), "channels": ch or None,
            "rate_khz": round(sr / 1000) if sr else None, "on": True}


def _parse_output(out: str) -> dict:
    parts: dict[str, str] = {}
    for chunk in ("\n" + (out or "")).split("\n==="):
        tag, _, body = chunk.partition("\n")
        parts[tag.strip()] = body
    video: Optional[dict] = None
    dm, cf, dd = parts.get("DM", ""), parts.get("CF", ""), parts.get("DD", "")
    m = re.search(r"name:\s*(\d{3,4})x(\d{3,4})([pi])(\d+)hz", dm)
    if m:
        w, h, scan, hz = int(m.group(1)), int(m.group(2)), m.group(3), int(m.group(4))
        fps = float(hz)
        f = re.search(r"sync_duration_num/den:\s*(\d+)/(\d+)", dm)
        if f and int(f.group(2)) > 0:
            fps = int(f.group(1)) / int(f.group(2))
        elif re.search(r"frac:\s*1\b", dm):
            fps = hz * 1000 / 1001          # 60 -> 59.94, 30 -> 29.97, 24 -> 23.98
        d = re.search(r"colordepth:\s*(\d+)", cf)
        c = re.search(r"colorspace:\s*(\w+)", cf)
        hdr, hdr_tv = _parse_hdr(parts.get("HS", ""), parts.get("HC", ""), parts.get("DV", ""))
        video = {"width": w, "height": h, "scan": scan, "fps": round(fps, 2),
                 "depth": int(d.group(1)) if d else None,
                 "chroma": _CHROMA.get(c.group(1).lower(), c.group(1)) if c else None,
                 "hdr": hdr, "hdr_tv": hdr_tv}
    else:
        a = re.search(r"(?:mActiveModeId=|modeId )(\d+)", dd)
        mm = a and re.search(r"\{id=" + a.group(1) + r", width=(\d+), height=(\d+), fps=([\d.]+)", dd)
        if mm:
            video = {"width": int(mm.group(1)), "height": int(mm.group(2)), "scan": "p",
                     "fps": round(float(mm.group(3)), 2), **_props_video(parts.get("PR", ""))}
    audio: Optional[dict] = None
    t = re.search(r"audio type:\s*(.+)", cf)
    if t:
        ch = re.search(r"audio channel num:\s*(\d+)", cf)
        sr = re.search(r"audio sample rate:\s*(\d+)\s*kHz", cf, re.I)
        on = re.search(r"audio on/off:\s*(\w+)", cf)
        audio = {"format": t.group(1).strip().replace("Dobly", "Dolby"),   # driver typo
                 "channels": int(ch.group(1)) if ch else None,
                 "rate_khz": int(sr.group(1)) if sr else None,
                 "on": (on.group(1).lower() == "on") if on else True}
    elif parts.get("AF"):
        audio = _af_audio(parts["AF"])
    n = (parts.get("AU", "").strip().splitlines() or ["0"])[-1].strip()
    return {"video": video, "audio": audio, "active": n.isdigit() and int(n) > 0}


@app.get("/api/devices/{ip}/output")
async def output(ip: str):
    _fire_only(ip)
    return _parse_output(await run_shell_connected(ip, _OUTPUT_GET))


# ---- EDID (Fire TV) -----------------------------------------------------------
# The EDID the Fire TV read from the TV (or EDID emulator) it is plugged into,
# from the Amlogic HDMI driver: rawedid = the bytes as hex, edid = the driver's
# own decode. Both are readable without root. MediaTek Fire TV Sticks don't let
# the shell read their HDMI driver; there the copy Fire OS saved when it last
# detected the TV is used (LAST_AUTO_ONBOARDED_RAW_EDID: on a Cube it equals the
# live EDID, while CURRENT_ONBOARDED_RAW_EDID is the TV of the first setup).
_EDID_GET = ("echo ===RAW; if [ -e /sys/class/amhdmitx/amhdmitx0/rawedid ]; then cat /sys/class/amhdmitx/amhdmitx0/rawedid 2>&1; "
             "else echo STORED; e=$(settings get secure LAST_AUTO_ONBOARDED_RAW_EDID); "
             "[ \"$e\" = null ] && e=$(settings get secure CURRENT_ONBOARDED_RAW_EDID); echo \"$e\"; fi; echo; "
             "echo ===TXT; cat /sys/class/amhdmitx/amhdmitx0/edid 2>/dev/null; "
             "echo ===MODEL; getprop ro.product.model")


def _file_part(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-.")[:40]


def _parse_edid(out: str) -> tuple:
    """(EDID bytes, the driver's decode, model, stored copy?) - HTTPException when there is no EDID."""
    parts: dict[str, str] = {}
    for chunk in ("\n" + (out or "")).split("\n==="):
        tag, _, body = chunk.partition("\n")
        parts[tag.strip()] = body
    raw = parts.get("RAW", "")
    stored = raw.lstrip().startswith("STORED")
    if re.search(r"Permission denied", raw):
        raise HTTPException(501, "This Fire TV model doesn't share the EDID.")
    hx = re.sub(r"[^0-9a-fA-F]", "", raw.replace("STORED", "")).lower()
    if len(hx) < 256 or not hx.startswith("00ffffffffffff00"):
        raise HTTPException(404, "Fire OS has no saved EDID for this Fire TV: was a TV ever detected?" if stored
                            else "No EDID: is a TV (or EDID emulator) plugged in and on?")
    data = bytes.fromhex(hx[:len(hx) // 256 * 256])
    data = data[:128 * (1 + data[126])] if len(data) >= 128 * (1 + data[126]) else data
    return data, parts.get("TXT", "").strip(), parts.get("MODEL", "").strip(), stored


def _edid_name(data: bytes) -> str:
    """The display name from the EDID's monitor name descriptor ('' when there is none)."""
    for i in (0x36, 0x48, 0x5A, 0x6C):
        d = data[i:i + 18]
        if len(d) == 18 and d[:3] == b"\0\0\0" and d[3] == 0xFC:
            return d[5:].split(b"\n")[0].decode("ascii", "ignore").strip()
    return ""


def _edid_text(data: bytes, decoded: str, name: str, ip: str, model: str, when: str,
               tv: str = "", stored: bool = False) -> str:
    blocks = [data[i:i + 128] for i in range(0, len(data), 128)]
    bad = [str(i) for i, b in enumerate(blocks) if sum(b) % 256]
    lines = [f"EDID saved by Fleet Remote v{APP_VERSION}",
             f"Device:   {name} ({ip})" + (f", {model}" if model else ""),
             f"Read on:  {when}"]
    if tv:
        lines.append(f"TV:       {tv}")
    lines += [f"Size:     {len(data)} bytes, {len(blocks)} block(s), checksum "
              + ("OK" if not bad else "WRONG in block " + ", ".join(bad))]
    if stored:
        lines += ["This Fire TV model doesn't let the EDID be read live. This is the copy",
                  "Fire OS saved when it last detected the TV, so it can be older than the",
                  "TV plugged in right now.", ""]
    else:
        lines += ["This is the EDID of the TV or display the Fire TV is plugged into",
                  "(or of an EDID emulator between them).", "",
                  "---- Decoded by the Fire TV's HDMI driver ----", decoded or "(none)", ""]
    lines.append("---- Raw bytes (hex) ----")
    lines += [f"{i:03x}: " + " ".join(f"{x:02x}" for x in data[i:i + 16]) for i in range(0, len(data), 16)]
    return "\r\n".join(lines) + "\r\n"


def _downloads_dir() -> Path:
    if sys.platform == "win32":
        with suppress(Exception):     # the real Downloads folder, also when it was moved (OneDrive etc.)
            import ctypes
            import uuid
            guid = ctypes.create_string_buffer(uuid.UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le, 16)
            p = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(p)) == 0:
                path = Path(p.value)
                ctypes.windll.ole32.CoTaskMemFree(p)
                if path.is_dir():
                    return path
    d = Path.home() / "Downloads"
    return d if d.is_dir() else Path.home()


@app.post("/api/devices/{ip}/edid")
async def edid(ip: str, request: Request):
    """The app window (this computer) saves the .bin and .txt into Downloads; a
    shared browser on another computer gets them back and downloads them itself."""
    _fire_only(ip)
    data, decoded, model, stored = _parse_edid(await run_shell_connected(ip, _EDID_GET))
    name = fleet[ip].get("name") or ip
    m = re.search(r"Rx Product Name:\s*(.+)", decoded)
    tv = m.group(1).strip() if m else _edid_name(data)
    now = time.localtime()
    base = "_".join(x for x in ("EDID", _file_part(name), _file_part(tv) if tv else "",
                                time.strftime("%Y-%m-%d_%H%M%S", now)) if x)
    text = _edid_text(data, decoded, name, ip, model, time.strftime("%Y-%m-%d %H:%M:%S", now), tv, stored)
    if (request.client.host if request.client else "") in ("127.0.0.1", "::1"):
        folder = _downloads_dir()
        def write():
            (folder / (base + ".bin")).write_bytes(data)
            (folder / (base + ".txt")).write_text(text, encoding="utf-8", newline="")
        try:
            await asyncio.to_thread(write)
        except PermissionError as exc:
            hint = (" Allow Python to use the Downloads folder: System Settings > Privacy & Security > "
                    "Files and Folders." if sys.platform == "darwin" else "")
            raise HTTPException(500, f"Couldn't save the EDID in {folder}: {exc}.{hint}")
        except OSError as exc:
            raise HTTPException(500, f"Couldn't save the EDID in {folder}: {exc}")
        return {"saved": str(folder), "files": [base + ".bin", base + ".txt"], "bytes": len(data), "stored": stored}
    return {"file": base, "hex": data.hex(), "text": text, "bytes": len(data), "stored": stored}


# ---- live video (scrcpy server, pure Python client) --------------------------
# The scrcpy server (open source, Apache 2.0, github.com/Genymobile/scrcpy) is
# pushed to the Cube and started over the same ADB link. It sends the screen as
# raw H.264, which the page decodes and draws in real time.
SCRCPY_VERSION = "5.0.1"
SCRCPY_SHA256 = "764eb6f79811d5211fe9df341120882ba9994c7a61b897d7bf3fb662e53bc536"
SCRCPY_FILE = f"scrcpy-server-v{SCRCPY_VERSION}"
SCRCPY_URL = f"https://github.com/Genymobile/scrcpy/releases/download/v{SCRCPY_VERSION}/{SCRCPY_FILE}"
SCRCPY_REMOTE = "/data/local/tmp/fleet-scrcpy-server.jar"
_streams: dict[str, dict] = {}


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


_scrcpy_dl = threading.Lock()


def scrcpy_server_file() -> Path:
    """Find the scrcpy server next to this script (or in the data folder);
    download it once from GitHub if it is not there. Checked against a hash."""
    with _scrcpy_dl:                      # two first streams at once: one download
        return _scrcpy_server_file()


def _scrcpy_server_file() -> Path:
    for base in (Path(__file__).resolve().parent, DATA_DIR):
        f = base / SCRCPY_FILE
        if f.is_file() and _sha256(f.read_bytes()) == SCRCPY_SHA256:
            return f
    import ssl
    import urllib.request
    ctx = None
    with suppress(ImportError):
        # python.org Python on a Mac ships without CA certificates, so this download failed
        # with CERTIFICATE_VERIFY_FAILED: verify against the computer's own trust store
        # instead (Keychain on a Mac, the certificate store on Windows)
        import truststore
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        data = urllib.request.urlopen(SCRCPY_URL, timeout=30, context=ctx).read()
    except Exception as exc:
        raise RuntimeError(
            f"The live video helper file '{SCRCPY_FILE}' was not found and could not be "
            f"downloaded ({exc}). Download it from {SCRCPY_URL} and put it in the same "
            f"folder as {SCRIPT_NAME}.")
    if _sha256(data) != SCRCPY_SHA256:
        raise RuntimeError("The downloaded live video helper failed its safety check.")
    f = DATA_DIR / SCRCPY_FILE
    tmp = f.with_name(f.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, f)
    return f


_stream_locks: dict[str, asyncio.Lock] = {}
_last_end: dict[str, float] = {}     # ip -> time.monotonic() when its last stream stopped
STREAM_GAP = 1.5                     # seconds the Cube's encoder gets between two streams


class _EncoderStuck(Exception):
    pass


async def _remote_sh(ip: str, cmd: str, timeout: float = 10.0) -> str:
    """Run one short command on the Cube over its own ADB link (the stream's
    links are busy or closed)."""
    d = AdbDeviceTcpAsync(ip, ADB_PORT, default_transport_timeout_s=timeout)
    try:
        await d.connect(rsa_keys=[get_signer()], auth_timeout_s=10)
        return await d.shell(cmd, read_timeout_s=timeout) or ""
    finally:
        with suppress(Exception):
            await asyncio.wait_for(d.close(), 5)


def _stop_cmd(pat: str) -> str:
    # pat is written like 'scid=[1]2ab ' so pkill/pgrep never match this shell
    # itself; the trailing space keeps it from matching a longer id with the same start
    return (f"pkill -f '{pat}'; i=0; while pgrep -f '{pat}' >/dev/null && [ $i -lt 25 ]; "
            f"do sleep 0.2; i=$((i+1)); done")


# ---- instant keys over the live video link ------------------------------------
# `input keyevent` starts a Java process on the Cube for every key (~0.7 s).
# While live video runs, the scrcpy server's control socket is open too, and a
# key sent through it is injected in a few ms (what the scrcpy app does).
# Message layout (scrcpy 5.x, big endian): INJECT_KEYCODE = type 0, action
# (0 down / 1 up), keycode i32, repeat i32, meta state i32; INJECT_TEXT = type 1,
# length u32, UTF-8 bytes (300 max per message).
CTL_KEYCODE, CTL_TEXT = 0, 1
CTL_DOWN, CTL_UP = 0, 1
CTL_TEXT_MAX = 300


def _ctl_text_msgs(text: str) -> list:
    msgs, part = [], b""
    for ch in text:
        b = ch.encode("utf-8", "replace")      # half an emoji pasted: '?' instead of a crash
        if part and len(part) + len(b) > CTL_TEXT_MAX:
            msgs.append(struct.pack(">BI", CTL_TEXT, len(part)) + part)
            part = b""
        part += b
    if part:
        msgs.append(struct.pack(">BI", CTL_TEXT, len(part)) + part)
    return msgs


async def ctl_send(st: dict, keys: Optional[list], text: Optional[str]) -> bool:
    """Send keys (press and release each) or text over a stream's control
    socket. False when that link is not usable, so the caller uses `input`."""
    info = st.get("cinfo")
    if info is None or st.get("closer") is not None:
        return False
    if text:
        msgs = _ctl_text_msgs(text)
    else:
        msgs = [struct.pack(">BBiii", CTL_KEYCODE, a, k, 0, 0)
                for k in keys for a in (CTL_DOWN, CTL_UP)]
    c = st["c"]
    # one WRTE per 4 KB at most: older adbd builds refuse bigger packets
    packets, cur = [], b""
    for m in msgs:
        if cur and len(cur) + len(m) > 4000:
            packets.append(cur)
            cur = b""
        cur += m
    if cur:
        packets.append(cur)
    try:
        async with st["clock"]:
            for data in packets:
                msg = AdbMessage(adb_const.WRTE, info.local_id, info.remote_id, data)
                await asyncio.wait_for(c._io_manager.send(msg, info), 3)
                await asyncio.wait_for(c._io_manager.read([adb_const.OKAY], info), 3)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        st["cinfo"] = None          # the link is gone: keys take the slow way from now on
        return False


def _kill_link(d) -> None:
    """Drop an ADB link's socket at once. Its close() waits for a lock that a
    reader may hold for a long time (the video socket waits for frames)."""
    with suppress(Exception):
        d._available = False
        w = d._io_manager._transport._writer
        if w is not None:
            w.close()


async def _stream_sh(ip: str, cmd: str, timeout: float = 10.0) -> str:
    """A short command on the Cube for the live video code: over the device's
    own connected link when it is free, else over a fresh one."""
    d = fleet.get(ip)
    if d is not None and is_connected(d) and not d["lock"].locked():
        with suppress(Exception):
            return await run_shell_connected(ip, cmd) or ""
    return await _remote_sh(ip, cmd, timeout)


async def _close_stream_now(st: dict) -> None:
    # 1. Close the video socket first. The server notices on its next frame and
    #    stops by itself, which releases the Cube's hardware encoder properly.
    #    (Killing it while it encodes can crash the Cube's media service; after
    #    that live video stays dead until the Cube restarts.)
    st["cinfo"] = None
    for k in ("b", "c"):
        _kill_link(st[k])
        with suppress(Exception):
            await asyncio.wait_for(st[k].close(), 3)
    t = st.get("task")
    if t is not None and not t.done():
        with suppress(Exception):
            await asyncio.wait_for(asyncio.shield(t), 0.3 if st.get("fast") else 3)
    # 2. Still running (a still screen sends nothing, so it hasn't noticed): stop it.
    if t is not None and not t.done():
        with suppress(Exception):
            await _stream_sh(st["ip"], _stop_cmd(st["pat"]) + "; echo ok")
    with suppress(Exception):
        await asyncio.wait_for(st["a"].close(), 3)
    if t is not None:
        t.cancel()
    await asyncio.sleep(0.5)          # give the encoder a moment before the next start
    _last_end[st["ip"]] = time.monotonic()
    if _streams.get(st["ip"]) is st:
        _streams.pop(st["ip"], None)


def _begin_close(st: dict, fast: bool = False) -> asyncio.Task:
    """Close a stream in its own task, so a cancelled request can't cut it short.
    fast: the Cube is in standby and sends no frames, so its server would not
    notice the closed socket; stop it right away."""
    t = st.get("closer")
    if t is None:
        st["fast"] = fast
        t = st["closer"] = _spawn(_close_stream_now(st))
    return t


async def _close_stream(st: dict, timeout: float = 20.0) -> bool:
    """Close a stream and wait until its server on the Cube has stopped.
    False when it is still stopping after `timeout` seconds."""
    t = _begin_close(st)
    with suppress(Exception):
        await asyncio.wait_for(asyncio.shield(t), timeout)
    return t.done()


_jar_cache: list = []
_jar_lock = threading.Lock()


def _jar_file() -> Path:
    with _jar_lock:
        if not _jar_cache or not _jar_cache[0].is_file():
            _jar_cache[:] = [scrcpy_server_file()]
        return _jar_cache[0]


def _logs_text(logs: list) -> str:
    return " | ".join(x for x in logs if x)


async def _encoder_stuck(ip: str, t0: str) -> bool:
    """The Cube logs 'VPU_EncOpen failed' when its hardware encoder is wedged
    (after a crash): look at its log since the server was started."""
    if not t0:
        return False
    n = ""
    with suppress(Exception):
        n = await _stream_sh(ip, f"logcat -d -t '{t0}' 2>/dev/null | grep -c 'VPU_EncOpen failed'")
    last = ((n or "").strip().splitlines() or ["0"])[-1].strip()
    return last.isdigit() and int(last) > 0


@app.get("/api/devices/{ip}/stream")
async def live_stream(ip: str, size: int = 1280, bitrate: int = 4_000_000, fps: int = 30):
    if ip in fleet and fleet[ip].get("type") == "appletv":
        raise HTTPException(501, "Live video isn't supported on Apple TV.")
    require_connected(ip)
    # Never in standby: waking the Cube with live video running crashes its
    # display service. The page starts it again once the Cube is awake.
    wake, ready = await fire_state(ip)
    if wake == "asleep":
        _power.setdefault(ip, {})["state"] = "asleep"
        raise HTTPException(409, f"{fleet[ip]['name']} is in standby, so there is no picture. "
                                 "Press Power on the remote in Fleet Remote to wake it.")
    if not ready:
        raise HTTPException(503, f"{fleet[ip]['name']} is still starting up. "
                                 "The picture starts by itself when it is ready.")
    size = max(320, min(size, 1920))
    bitrate = max(500_000, min(bitrate, 16_000_000))
    fps = max(5, min(fps, 60))
    try:
        jar = await asyncio.to_thread(_jar_file)
        jar_size = jar.stat().st_size
    except Exception as exc:
        raise HTTPException(502, str(exc))

    lock = _stream_locks.setdefault(ip, asyncio.Lock())
    scid = secrets.randbelow(0x7FFFFFFF)
    hx = f"{scid:x}"
    sock = f"scrcpy_{scid:08x}".encode()
    # a: runs the server; b: video socket; c: control socket (instant keys, ctl_send)
    a = AdbDeviceTcpAsync(ip, ADB_PORT, default_transport_timeout_s=None)
    b = AdbDeviceTcpAsync(ip, ADB_PORT, default_transport_timeout_s=None)
    c = AdbDeviceTcpAsync(ip, ADB_PORT, default_transport_timeout_s=3.0)
    st = {"ip": ip, "a": a, "b": b, "c": c, "cinfo": None, "clock": asyncio.Lock(),
          "task": None, "pat": f"scid=[{hx[0]}]{hx[1:]} ", "t0": ""}
    logs: list[str] = []

    async def run_server():
        # Each video packet comes with a 12-byte header (PTS + config / key frame
        # flags, size), so the page knows every frame boundary at once (no
        # waiting for the next start code). No device / codec header, no dummy byte.
        cmd = (f"CLASSPATH={SCRCPY_REMOTE} app_process / com.genymobile.scrcpy.Server "
               f"{SCRCPY_VERSION} scid={scid:x} tunnel_forward=true video=true audio=false "
               f"control=true clipboard_autosync=false power_on=false cleanup=false "
               f"send_device_meta=false send_stream_meta=false send_dummy_byte=false "
               f"send_frame_meta=true max_size={size} "
               f"video_bit_rate={bitrate} max_fps={fps} log_level=info")
        try:
            async for line in a.streaming_shell(cmd, transport_timeout_s=None, read_timeout_s=86400):
                logs.append(line.strip())
                del logs[:-20]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logs.append(f"(server ended: {exc})")

    async def chunks():
        # Wait until the server says it is up, then open its socket (retry a few times).
        t_end = time.monotonic() + 12
        while not any("Device:" in x for x in logs):
            if time.monotonic() > t_end:
                raise RuntimeError("the Cube did not start the video server. " + _logs_text(logs))
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.2)
        while True:
            try:
                info = await b._open(b"localabstract:" + sock, None, 2.0, None)
                break
            except Exception as exc:
                if time.monotonic() > t_end:
                    raise RuntimeError(f"could not open the video socket ({exc or type(exc).__name__}). "
                                       + _logs_text(logs))
                await asyncio.sleep(0.3)
        # The server accepts the video socket first, then the control socket,
        # and only then starts the encoder.
        try:
            cinfo = await c._open(b"localabstract:" + sock, None, 5.0, None)
        except Exception as exc:
            raise RuntimeError(f"could not open the control socket ({exc or type(exc).__name__}). "
                               + _logs_text(logs))
        cinfo.read_timeout_s = 3.0
        st["cinfo"] = cinfo
        info.read_timeout_s = 86400     # a still screen sends nothing for a long time
        info.transport_timeout_s = 86400
        async for data in b._read_until_close(info):
            yield data

    it = None
    try:
        async with lock:
            # Never two streams on one Cube: its encoder can't take a second one,
            # so wait until the previous server has fully stopped.
            old = _streams.get(ip)
            if old and not await _close_stream(old):
                raise HTTPException(503, f"The previous live video on {ip} is still stopping. "
                                         "Try again in a moment.")
            # Quick Off/On or quality flips: let the Cube's encoder settle first.
            # (Opening it again too fast is what crashed it before.)
            gap = STREAM_GAP - (time.monotonic() - _last_end.get(ip, -1e9))
            if gap > 0:
                await asyncio.sleep(gap)
            _streams[ip] = st
            signer = get_signer()
            await asyncio.gather(a.connect(rsa_keys=[signer], auth_timeout_s=10),
                                 b.connect(rsa_keys=[signer], auth_timeout_s=10),
                                 c.connect(rsa_keys=[signer], auth_timeout_s=10))
            # A video server already on the Cube is either left over (it sends
            # nothing) or another Fleet Remote window / PC / scrcpy streaming right
            # now (it keeps writing, even for a still screen). Stopping a busy one
            # mid-picture can crash the Cube's encoder, so only left-over ones go.
            r = await a.shell(
                "for p in $(pgrep -f 'com.genymobile.scrcpy.Serve[r]'); do "
                "e=$(tr '\\0' '\\n' < /proc/$p/environ 2>/dev/null | grep '^CLASSPATH='); [ -n \"$e\" ] || continue; "
                "own=0; case \"$e\" in *fleet-scrcpy*) own=1;; esac; "
                "w1=$(grep wchar /proc/$p/io | cut -d' ' -f2); sleep 1; w2=$(grep wchar /proc/$p/io | cut -d' ' -f2); "
                "echo \"srv $p $own $w1 $w2\"; done; "
                f"echo jar $(stat -c %s {SCRCPY_REMOTE} 2>/dev/null); "
                "date '+%m-%d %H:%M:%S.000'", read_timeout_s=20) or ""
            busy, idle, have = [], [], None
            for ln in r.splitlines():
                f = ln.split()
                if len(f) == 5 and f[0] == "srv" and f[3].isdigit() and f[4].isdigit():
                    if int(f[4]) > int(f[3]):
                        busy.append(f[1])
                    elif f[2] == "1":
                        idle.append(f[1])
                elif len(f) == 2 and f[0] == "jar" and f[1].isdigit():
                    have = int(f[1])
            if busy:
                raise HTTPException(409, f"Live video is already running on {ip} in another Fleet Remote "
                                         "window, or on another PC. Turn it off there first, then try again.")
            if idle:
                pids = " ".join(idle)
                await a.shell(f"kill {pids}; i=0; while [ $i -lt 25 ]; do alive=0; for p in {pids}; do "
                              f"[ -d /proc/$p ] && alive=1; done; [ $alive = 0 ] && break; sleep 0.2; "
                              f"i=$((i+1)); done; echo ok", read_timeout_s=15)
                await asyncio.sleep(STREAM_GAP)
            st["t0"] = (r.strip().splitlines() or [""])[-1]
            if have != jar_size:        # same file already there from last time: skip the push
                await a.push(str(jar), SCRCPY_REMOTE)
            # Standby or a lost link while this connected: its closer has run with
            # nothing to stop, so the server must not start now.
            if st.get("closer") is not None:
                raise HTTPException(409, f"Live video on {ip} was stopped while it started.")
            st["task"] = asyncio.create_task(run_server())
        it = chunks().__aiter__()

        async def watch():
            # Don't make the user wait 20 s when the Cube already said why it failed.
            while True:
                await asyncio.sleep(2)
                if st["task"].done():
                    detail = _logs_text(logs)
                    raise RuntimeError("the Cube's video server stopped" + (f" ({detail})" if detail else ""))
                if await _encoder_stuck(ip, st["t0"]):
                    raise _EncoderStuck()

        first_t = asyncio.ensure_future(it.__anext__())
        watch_t = asyncio.ensure_future(watch())
        try:
            done, _ = await asyncio.wait({first_t, watch_t}, timeout=20,
                                         return_when=asyncio.FIRST_COMPLETED)
            if first_t in done:
                first = first_t.result()
            elif watch_t in done:
                watch_t.result()
            else:
                raise asyncio.TimeoutError()
        finally:
            for t in (first_t, watch_t):
                if not t.done():
                    t.cancel()
    except BaseException as exc:
        _begin_close(st)
        if isinstance(exc, (HTTPException, asyncio.CancelledError)):
            raise
        timed_out = isinstance(exc, (asyncio.TimeoutError, TimeoutError))
        stuck = isinstance(exc, _EncoderStuck) or (timed_out and await _encoder_stuck(ip, st["t0"]))
        asleep = False
        if timed_out and not stuck:
            with suppress(Exception):
                asleep = _wake_of(await _stream_sh(ip, "dumpsys power | grep -m1 mWakefulness=")) == "asleep"
        if stuck:
            raise HTTPException(502, f"The video encoder on {ip} is stuck (it crashed earlier). "
                                     "Restart the Cube, then turn Live video on again.")
        if asleep:
            raise HTTPException(502, f"{ip} is asleep, so there is no picture. Press Home on the remote "
                                     "to wake it, then turn Live video on again.")
        if timed_out:
            raise HTTPException(502, f"No picture from {ip} after 20 seconds. "
                                     "If this keeps happening, restart the Cube.")
        detail = _logs_text(logs)
        raise HTTPException(502, f"Live video failed on {ip}: {exc or type(exc).__name__}"
                                 + (f" ({detail})" if detail else ""))

    async def gen():
        try:
            yield first
            try:
                async for data in it:
                    yield data
            except Exception:
                # We closed the socket on purpose (standby, Off): end quietly.
                if st.get("closer") is None:
                    raise
        finally:
            with suppress(Exception):
                _begin_close(st)

    async def cleanup():
        _begin_close(st)

    return StreamingResponse(gen(), media_type="application/octet-stream",
                             headers={"Cache-Control": "no-store"}, background=BackgroundTask(cleanup))


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML.replace("__SCRIPT__", SCRIPT_NAME).replace("__PY__", "python" if sys.platform == "win32" else "python3")


# ---- launcher: run server on a thread, open a native window ------------------
def _port_free(host: str, port: int) -> bool:
    with socket.socket() as s:
        if sys.platform != "win32":
            # bind like uvicorn does: a port whose last connections are only in TIME_WAIT (the
            # copy closed a minute ago) counts as free; Mac and Linux still refuse one that
            # another program listens on. Windows would let two listeners share a port with
            # this flag - there the mutex tells copies apart, so the plain bind stays.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _free_port(preferred: int, host: str = "127.0.0.1") -> int:
    if _port_free(host, preferred):
        return preferred
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _local_open(url_or_req, timeout: float):
    """urllib without the computer's proxy: a work proxy would get 127.0.0.1 itself."""
    import urllib.request
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url_or_req, timeout=timeout)


def _running_copy(port: int) -> Optional[str]:
    """Version of a Fleet Remote already serving on this port, else None."""
    if _port_free("127.0.0.1", port):
        return None          # nothing listens; asking anyway can hang 2 s on Windows (stealth firewall)
    with suppress(Exception):
        with _local_open(f"http://127.0.0.1:{port}/api/meta", 2) as r:
            return str(json.loads(r.read().decode("utf-8")).get("app_version") or "") or None
    return None


_RUNNING = DATA_DIR / "running.json"      # the port this copy serves on, for a second start


def _note_running(port: int) -> None:
    with suppress(Exception):
        _RUNNING.write_text(json.dumps({"port": port, "pid": os.getpid(), "version": APP_VERSION}),
                            encoding="utf-8")


def _forget_running() -> None:
    with suppress(Exception):
        if json.loads(_RUNNING.read_text(encoding="utf-8")).get("pid") == os.getpid():
            _RUNNING.unlink()


def _noted_port() -> Optional[int]:
    """The port in running.json (a copy that is or was running), else None."""
    with suppress(Exception):
        return int(json.loads(_RUNNING.read_text(encoding="utf-8"))["port"])
    return None


def _join_running_copy(port: int, version: str) -> None:
    """Fleet Remote already runs: show its window (or open it in the browser) and stop.
    Two copies would both save devices.json and wipe each other's changes."""
    import urllib.request
    shown, info = False, {}
    if sys.platform == "win32":
        with suppress(Exception):
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)    # ASFW_ANY: let it take the front
    for _ in range(10):                  # a copy started a moment ago has its window soon
        with suppress(Exception):
            rq = urllib.request.Request(f"http://127.0.0.1:{port}/api/show", data=b"", method="POST",
                                        headers={"Origin": f"http://127.0.0.1:{port}"})   # _Guard: not a GET
            with _local_open(rq, 5) as r:
                info = json.loads(r.read().decode("utf-8"))
                shown = bool(info.get("window"))
        if shown or info.get("reason") not in ("starting", "closing"):
            break
        time.sleep(0.5)
    reason = info.get("reason")
    if not shown and reason == "closing":
        _alert("Fleet Remote is still closing. Wait a moment, then open it again.")
        return
    if not shown:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}")
        if reason != "browser":          # a copy started with --browser wants exactly this
            why = f"\n\n{info['error']}" if info.get("error") else ""
            _alert("Fleet Remote is already running, but its own window is not open: it has been "
                   f"opened in your browser instead.{why}\n\nDetails: {DATA_DIR / 'error.log'}")
    if version != APP_VERSION:
        _alert(f"Fleet Remote v{version} is still running. Close it, then start "
               f"Fleet Remote again to use v{APP_VERSION}.")


_mac_activity: list = []


def _mac_branding() -> None:
    """The window runs in a plain python process: give it Fleet Remote's name and icon
    in the menu bar and Dock instead of Python's. Also keep App Nap from slowing the
    status checks and reconnects down while the window is hidden."""
    with suppress(Exception):
        from Foundation import NSProcessInfo
        _mac_activity.append(NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            0x00FFFFFF & ~(1 << 20), "Fleet Remote keeps the TV links alive"))   # user initiated, idle sleep allowed
    with suppress(Exception):
        from Foundation import NSProcessInfo
        NSProcessInfo.processInfo().setProcessName_("Fleet Remote")
    with suppress(Exception):
        from Foundation import NSBundle
        info = NSBundle.mainBundle().infoDictionary()
        if info is not None:
            info["CFBundleName"] = "Fleet Remote"
    with suppress(Exception):
        from AppKit import NSApplication, NSImage
        if ICON.is_file():
            img = NSImage.alloc().initWithContentsOfFile_(str(ICON))
            if img is not None:
                NSApplication.sharedApplication().setApplicationIconImage_(img)


def _lan_ip() -> str:
    with suppress(Exception):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    return "127.0.0.1"


def _share_code() -> str:
    f = DATA_DIR / "share_code.txt"
    with suppress(Exception):
        c = f.read_text(encoding="utf-8").strip()
        if len(c) >= 6:
            return c
    c = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(8))
    with suppress(OSError):
        f.write_text(c, encoding="utf-8")
    return c


_server: Optional[uvicorn.Server] = None
_server_thread: Optional[threading.Thread] = None
_window = None                          # the pywebview window, to bring it to the front
_window_state = "starting"              # why there is no window: starting / browser / failed / closing
_window_error = ""
_win_state = {"min": False, "max": False}   # restore only a minimized window; re-maximize one that was


def _shutdown(wait: float) -> None:
    """Stop the server: it ends live video on the Cubes and drops every link (lifespan)."""
    if _server is not None:
        _server.should_exit = True
    th = _server_thread
    if th is not None and th is not threading.current_thread():
        th.join(wait)
    _forget_running()


@app.post("/api/show")
async def show_window():
    """A second start asks this copy to come to the front instead of running twice."""
    w = _window
    if w is None:
        return {"window": False, "reason": _window_state, "error": _window_error}
    with suppress(Exception):
        if _win_state["min"]:
            w.restore()
            if _win_state["max"] and sys.platform == "win32":    # restore() makes it Normal
                w.maximize()
        w.show()                         # brings it to the front and focuses it
    return {"window": True}


def _claim_instance(port: int) -> bool:
    """Windows lets two sockets bind one port (SO_REUSEADDR), so two copies started at the
    same moment both pass the free-port probe: a named mutex tells them apart."""
    if sys.platform != "win32":
        return True
    with suppress(Exception):
        import ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.restype = ctypes.c_void_p
        k.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
        h = k.CreateMutexW(None, False, f"Local\\FleetRemote.{port}")
        if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS: another copy holds it
            k.CloseHandle(ctypes.c_void_p(h))
            return False
        _mutex.append(h)                        # held until exit
        return True
    return True


_mutex: list = []


def _say(msg: str) -> None:
    """print that can't fail: no console under pythonw, or one that can't show the text."""
    with suppress(Exception):
        print(msg)


_ready = threading.Event()               # set once the server listens, or gave up


class _Server(uvicorn.Server):
    async def startup(self, sockets=None):
        await super().startup(sockets)
        _ready.set()


def _serve(port: int, host: str = "127.0.0.1"):
    global _server
    try:
        _server = _Server(uvicorn.Config(app, host=host, port=port, timeout_graceful_shutdown=3,
                                         log_level="warning", log_config=None))
        _server.run()
    except BaseException:
        _log_error(traceback.format_exc())
        raise
    finally:
        _ready.set()


def _load_webview():
    """pywebview: Edge WebView2 on Windows, WebKit on a Mac. Loading .NET takes about a
    second, so this runs while the server thread starts."""
    import webview
    if sys.platform == "win32":
        # without the WebView2 runtime pywebview falls back to the old IE engine,
        # where the page can't work: use the browser instead
        from webview.platforms import winforms
        if getattr(winforms, "renderer", "") != "edgechromium":
            raise RuntimeError("Microsoft Edge WebView2 is not installed "
                               "(https://developer.microsoft.com/microsoft-edge/webview2/)")
        # its own taskbar button with our icon, not under Python's (same id as the shortcut)
        with suppress(Exception):
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("emironat.FleetRemote")
    return webview


def _fast_exit() -> None:
    """Window closed, server stopped, everything saved: end the process now. Otherwise
    Python.NET (the bridge to the Windows window) spends another 2-4 s taking the .NET
    runtime down; os._exit doesn't help, .NET's own exit handlers still run."""
    if sys.platform != "win32" or "pythonnet" not in sys.modules:
        return
    with suppress(Exception):
        sys.stdout.flush()
        sys.stderr.flush()
    import ctypes
    ctypes.windll.kernel32.TerminateProcess(ctypes.c_void_p(-1), 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", action="store_true",
                    help="open in the default browser instead of a native window")
    ap.add_argument("--port", type=int, default=8756)
    ap.add_argument("--share", action="store_true",
                    help="also allow other computers on the network (browser + access code)")
    args = ap.parse_args()

    host = "0.0.0.0" if args.share else "127.0.0.1"
    other = _running_copy(args.port)
    if other:
        _join_running_copy(args.port, other)
        return
    noted = _noted_port()                # a copy that another program pushed off args.port
    if noted and noted != args.port:
        other = _running_copy(noted)
        if other:
            _join_running_copy(noted, other)
            return
    if not _claim_instance(args.port):
        for _ in range(40):              # the other copy is starting (join it) or closing (take over)
            for p in {args.port, _noted_port() or args.port}:
                other = _running_copy(p)
                if other:
                    _join_running_copy(p, other)
                    return
            if _claim_instance(args.port):
                break
            time.sleep(0.5)
        else:                            # still held by a live copy: never run a second one
            msg = ("Fleet Remote is already running (or still closing) but doesn't answer.\n"
                   "Wait a moment, then open it again.")
            _say(msg)
            _alert(msg)
            sys.exit(1)
    # Taken by something that isn't a Fleet Remote (a copy of ours answering here or on the
    # port in running.json was joined above; on Windows the mutex rules out one that is still
    # starting or closing): take a free port at once rather than probing a silent program
    # 16 times at 2 s each. A copy that is just closing takes its port with it.
    port = _free_port(args.port, host)
    url = f"http://127.0.0.1:{port}"

    global SHARE_CODE, SHARE_URL, _HOSTS, _window, _window_state, _window_error, _server_thread
    _HOSTS = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if args.share:
        SHARE_CODE = _share_code()
        SHARE_URL = f"http://{_lan_ip()}:{port}"
        _HOSTS.add(SHARE_URL.split("//", 1)[1])
        _say(f"\nSHARING ON THE NETWORK\n  Open:        {SHARE_URL}\n"
             f"  Access code: {SHARE_CODE}   (user name: anything)\n")

    th = _server_thread = threading.Thread(target=_serve, args=(port, host), daemon=True)
    th.start()

    webview = None
    if args.browser:
        _window_state = "browser"
    else:
        try:
            webview = _load_webview()
        except Exception as exc:
            _window_state, _window_error = "failed", f"Native window unavailable: {exc}"
            _say(f"({_window_error}) - opening in browser.")
            _log_error(_window_error)

    # no window before the server answers (a first start also makes the ADB key here)
    _ready.wait(90)
    if _server is None or not _server.started:
        for _ in range(6):               # a copy that started a moment earlier may own the port
            other = _running_copy(port)
            if other:
                _join_running_copy(port, other)
                return
            time.sleep(0.5)
        log = DATA_DIR / "error.log"
        msg = (f"The internal server failed to start.\n\n"
               f"Details (if any): {log}")
        _say(msg)
        _alert(msg)
        sys.exit(1)
    _note_running(port)

    try:
        if webview is not None:
            try:
                _window = webview.create_window("Fleet Remote v" + APP_VERSION, url,
                                                width=1200, height=820, min_size=(760, 560))
                with suppress(Exception):
                    _window.events.minimized += lambda *a: _win_state.__setitem__("min", True)
                    _window.events.restored += lambda *a: _win_state.update(min=False, max=False)
                    _window.events.maximized += lambda *a: _win_state.update(min=False, max=True)
                    if sys.platform == "darwin":
                        # Cmd+Q / Dock > Quit end the process right after this event, before
                        # main() gets its turn: stop the server (live video, links) here
                        _window.events.closing += lambda *a: _shutdown(10)
                if sys.platform == "darwin":
                    _mac_branding()
                try:
                    webview.start(icon=str(ICON) if ICON.is_file() else None,
                                  private_mode=False, storage_path=str(DATA_DIR / "webview"))
                finally:
                    _window, _window_state = None, "closing"     # a second start now gets told
                return
            except Exception as exc:
                _window_state, _window_error = "failed", f"Native window unavailable: {exc}"
                _say(f"({_window_error}) - opening in browser.")
                _log_error(_window_error)

        import webbrowser
        webbrowser.open(url)
        _say(f"Fleet Remote running at {url}\nPress Ctrl+C to quit.")
        try:
            while th.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    finally:
        # window closed / Ctrl+C: stop live video and close every link properly
        _shutdown(25)


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fleet Remote v3.91</title>
<style>
  :root{
    --bg:#0b0d10; --panel:#14181d; --panel-2:#1b2027; --line:#2a313a;
    --ink:#e8edf2; --ink-dim:#8b96a3; --accent:#1fa2e8; --accent-2:#4da3ff;
    --ok:#3ecf8e; --off:#5a636e; --warn:#ffb454; --press:#20262e;
    --mono:"SF Mono",ui-monospace,"JetBrains Mono",Menlo,Consolas,monospace;
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,system-ui,sans-serif;
  }
  *{box-sizing:border-box}
  html,body{margin:0;min-height:100%}
  body{background:radial-gradient(130% 90% at 15% -10%,#141a20 0%,var(--bg) 55%);
    color:var(--ink);font-family:var(--sans);-webkit-font-smoothing:antialiased;padding:16px 20px}
  button{font-family:var(--sans);cursor:pointer;color:var(--ink);border:none;background:none}
  input,select{background:#0d1116;border:1px solid var(--line);border-radius:10px;color:var(--ink);
    padding:9px 10px;font-family:var(--mono);font-size:13px;width:100%}
  select{font-family:var(--sans);font-size:12px;width:auto;padding:6px 8px;cursor:pointer}
  input:focus,select:focus{outline:none;border-color:var(--accent)}
  input:disabled{opacity:.55}
  .btn:disabled{opacity:.45;cursor:default;filter:none}

  /* header */
  .top{display:flex;align-items:center;gap:16px;margin:0 0 14px;max-width:1400px}
  .top h1{font-size:20px;font-weight:600;letter-spacing:-.01em;margin:0;display:flex;align-items:center;gap:10px;flex:none}
  .top h1 .fx{color:var(--accent)}
  #appver{font-family:var(--mono);font-size:11px;color:var(--ink-dim);font-weight:400}
  #tickbtn{display:inline-flex;align-items:center;gap:6px;color:var(--ink-dim);flex:none}
  #tickbtn svg{width:14px;height:14px}
  #tickbtn.off{opacity:.6}
  .log{margin-left:auto;font-family:var(--mono);font-size:12px;color:var(--ink-dim);text-align:right;
    min-height:15px;max-width:70%;overflow-wrap:anywhere}
  .log .bad{color:var(--warn)}

  /* layout: Devices | Remote | Screen + Keyboard + HDMI Signal & Sleep */
  .wrap{display:grid;gap:14px;align-items:start;max-width:1400px;
    grid-template-columns:260px 300px minmax(0,1fr);grid-template-areas:"dev remote screen"}
  #devcard{grid-area:dev} #remote{grid-area:remote} #screencol{grid-area:screen}
  @media(max-width:1060px){
    .wrap{grid-template-columns:300px minmax(0,1fr);grid-template-rows:auto 1fr;
      grid-template-areas:"dev screen" "remote screen"}
  }
  @media(max-width:700px){
    .wrap{grid-template-columns:1fr;grid-template-rows:none;grid-template-areas:"dev" "remote" "screen"}
    #sleepcard .slp.two{grid-template-columns:1fr}   /* id: the base .slp.two rules below would win otherwise */
    #sleepcard .slp.two .colR .outl.first{margin-top:7px;padding-top:6px;border-top:1px solid var(--line)}
  }
  .card{background:linear-gradient(180deg,var(--panel),var(--panel-2));
    border:1px solid var(--line);border-radius:16px;padding:14px}
  .head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:0 0 10px;min-height:32px}
  .card h2{font-size:12px;font-family:var(--mono);letter-spacing:.16em;text-transform:uppercase;
    color:var(--ink-dim);margin:0;font-weight:600}
  .tip{font-size:11px;color:var(--ink-dim);margin-top:10px;line-height:1.45}
  .warn{color:var(--warn)}

  .btn{padding:9px 12px;border-radius:10px;background:var(--accent);color:#04121f;
    font-weight:600;font-size:13px;transition:filter .12s;white-space:nowrap}
  .btn:hover{filter:brightness(1.12)}
  .btn:active{filter:brightness(.9)}
  .btn.ghost{background:var(--panel-2);color:var(--ink);border:1px solid var(--line)}
  .btn.ghost:hover{background:var(--press);border-color:#3a434e;filter:none}
  .btn.small{padding:6px 10px;font-size:12px}

  /* segmented switch: one choice at a time */
  .seg{display:flex;gap:3px;padding:3px;background:#0d1116;border:1px solid var(--line);border-radius:10px}
  .seg button{flex:1;padding:7px 10px;border-radius:7px;font-size:12px;font-weight:600;color:var(--ink-dim);
    white-space:nowrap;transition:background .12s,color .12s}
  .seg button:hover{color:var(--ink);background:var(--press)}
  .seg button.on{background:var(--accent);color:#04121f}
  .seg button.dis{opacity:.35;pointer-events:none}

  /* devices */
  .addbox{display:flex;flex-direction:column;gap:8px;padding:10px;margin-bottom:10px;
    border:1px solid var(--line);border-radius:12px;background:#0f1318}
  .addbox .two{display:flex;gap:8px}
  .addbox .two .btn{flex:1}
  .dev{display:flex;align-items:center;gap:6px;padding:9px 8px 9px 10px;border-radius:12px;
    border:1px solid var(--line);background:#0d1116;margin-top:6px;cursor:pointer}
  .dev:hover{border-color:#3a434e;background:#10151b}
  .dev.active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset,0 0 12px rgba(31,162,232,.22)}
  .dev .meta{flex:1;min-width:0}
  .dev .nmrow{display:flex;align-items:center;gap:4px;min-width:0}
  .dev .nm{font-weight:600;font-size:14px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.25;min-width:0}
  .dev .nmrow .edbox{flex:1;min-width:0;display:flex;flex-direction:column;gap:4px}
  .dev .nmrow .edbox ~ .tag{display:none}
  .dev .nmrow.editing + .ip{display:none}
  /* groups: a header you click to fold / unfold the devices under it */
  .grp{display:flex;align-items:center;gap:7px;margin-top:12px;padding:4px 4px 2px;cursor:pointer;-webkit-user-select:none;user-select:none;
    font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-dim)}
  .grp:first-child{margin-top:2px}
  .grp:hover{color:var(--ink)}
  .grp .car{width:13px;height:13px;flex:none;transition:transform .12s}
  .grp.fold .car{transform:rotate(-90deg)}
  .grp .gnm{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
  .grp.fold.has-active{color:var(--accent)}
  .grp.empty{cursor:default}
  .grp.empty .car{visibility:hidden}
  /* group manager */
  .headr2{display:flex;gap:6px}
  /* Devices title on its own line, the two buttons share the row under it */
  #devcard > .head{flex-direction:column;align-items:stretch;gap:10px}
  #devcard > .head .headr2{gap:8px}
  #devcard > .head .headr2 .btn{flex:1}
  .gnew{display:flex;gap:8px}
  .gnew input{flex:1;min-width:0;padding:7px 9px;font-family:var(--sans)}
  .grow{display:flex;align-items:center;gap:8px;padding:6px 8px 6px 10px;border:1px solid var(--line);border-radius:10px;background:#0d1116}
  .grow .gname{flex:1;min-width:0;font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
    cursor:text;border-bottom:1px dashed transparent}
  .grow .gname:hover{border-bottom-color:var(--ink-dim)}
  .grow input{flex:1;min-width:0;padding:3px 6px;font-size:13px;font-family:var(--sans);font-weight:600}
  .grow .gn{font-family:var(--mono);font-size:11px;color:var(--ink-dim);flex:none}
  .gnone{font-size:12px;color:var(--ink-dim)}
  .addbox select,.edbox select{width:100%;font-size:13px;padding:8px 9px}
  .edbox select{font-size:12px;padding:3px 6px;border-radius:8px}
  .dev .ip{font-family:var(--mono);font-size:11px;color:var(--ink-dim);white-space:nowrap;margin-top:2px}
  .tag{font-family:var(--mono);font-size:9px;padding:1px 5px;border:1px solid var(--line);border-radius:5px;
    color:var(--ink-dim);vertical-align:middle;flex:none}
  .st{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;line-height:1;border-radius:999px;flex:none;width:76px;height:24px;padding:0 0 1px;box-sizing:border-box;
    display:inline-flex;align-items:center;justify-content:center;text-align:center;white-space:nowrap;overflow:hidden;
    border:1px solid #3a434e;transition:background .12s,border-color .12s,color .12s}
  .st:hover{border-color:var(--accent);color:var(--accent)}
  /* hover text (Disconnect / Connect / Enable / Disable) lives in CSS, so a rebuilt list keeps it */
  .st{position:relative}
  .st[data-hv]:hover{color:transparent;transition:background .12s,border-color .12s}
  .st[data-hv]:hover::after{content:attr(data-hv);position:absolute;inset:0;display:flex;align-items:center;justify-content:center;padding-bottom:1px;color:var(--accent)}
  .st.on[data-hv]:hover::after{color:#ff7a7a}
  .st.busy{color:var(--ink-dim);border-color:var(--line);pointer-events:none;letter-spacing:0}
  .st.on{background:rgba(62,207,142,.14);border-color:rgba(62,207,142,.5);color:var(--ok)}
  .st.on:hover{background:rgba(255,122,122,.14);border-color:#ff7a7a;color:#ff7a7a}
  .st.on.pw{background:rgba(255,180,84,.12);border-color:rgba(255,180,84,.5);color:var(--warn)}
  .st.on.pw:hover{background:rgba(255,122,122,.14);border-color:#ff7a7a;color:#ff7a7a}
  .st.gone{background:rgba(255,122,122,.1);border-color:rgba(255,122,122,.5);color:#ff7a7a}
  .st.gone:hover{background:transparent;border-color:var(--accent);color:var(--accent)}
  /* must outrank the coloured .on/.pw/.gone hovers above, or the label shows through the hover text */
  .st[data-hv]:hover,.st.on[data-hv]:hover,.st.on.pw[data-hv]:hover,.st.gone[data-hv]:hover{color:transparent}
  .x{color:var(--off);font-size:14px;padding:2px 1px;flex:none;white-space:nowrap}
  .x[data-sure]{font-size:10px;font-weight:600;color:#ff7a7a}
  .x:hover{color:#ff7a7a}
  .link{color:var(--accent-2);font-size:12px;margin-top:10px}
  .link:hover{text-decoration:underline}
  .empty{font-size:12px;color:var(--ink-dim);padding:8px 2px}

  /* selected device: never sleep (left) + HDMI output (right); one column while not connected */
  .slp{padding:8px 8px 8px 10px;border-radius:12px;border:1px solid var(--line);background:#0d1116;margin-top:6px}
  .slp.two{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.15fr);column-gap:22px;align-items:start}
  .slp .colL,.slp .colR{min-width:0}
  .slp.two .colR .outl.first{margin-top:0;padding-top:0;border-top:0}
  .slp.two .colR .abars{margin-top:8px}
  .slrow{display:flex;align-items:center;gap:6px;min-height:24px}
  .slnm{flex:1;min-width:0;font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .sl{font-size:11px;color:var(--ink-dim);margin-top:3px;line-height:1.4}
  .sl.good{color:var(--ok)}
  .sl.warn{color:var(--warn)}
  .slp.off{padding-top:5px;padding-bottom:5px}
  .sloff{font-size:11px;color:var(--ink-dim);flex:none;white-space:nowrap}
  .slrow.sub{margin-top:4px;gap:10px}               /* "Never sleep" with its Enabled/Disabled pill right next to it */
  .sllab{flex:none;font-size:12px;white-space:nowrap}
  .outl{font-size:11px;color:var(--ink-dim);margin-top:3px;line-height:1.4;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .outl b{color:var(--ink);font-weight:600}
  .outl.first{margin-top:7px;padding-top:6px;border-top:1px solid var(--line)}
  /* one bar per audio channel on the HDMI output: lit = channel present, moving = sound playing */
  .abars{display:flex;gap:3px;margin-top:7px}
  .abc{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
  .ab{height:14px;border-radius:2px;background:#232a32;border:1px solid var(--line);box-sizing:border-box;transform-origin:bottom}
  .ab.on{background:var(--ok);border-color:var(--ok);opacity:.55}
  .ab.on.live{opacity:1;animation:ab .9s ease-in-out infinite alternate}
  .abn{font-size:9px;line-height:1;text-align:center;color:var(--ink-dim);font-family:var(--mono)}
  @keyframes ab{from{transform:scaleY(.5)}to{transform:scaleY(1)}}

  /* remote */
  .scope{font-size:13px;color:var(--ink-dim);margin:-2px 0 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  #remote .head{margin-bottom:2px}   /* the 32 px power button makes the row tall; keep a normal line gap to Controlling */
  .scopest{font-size:12px;white-space:nowrap;min-width:0;overflow:hidden;text-overflow:ellipsis}   /* shortens before it pushes the buttons out of the card */
  .scope b{color:var(--ink)}
  .headr{display:flex;align-items:center;gap:8px;min-width:0}
  .pwr{width:32px;height:32px;border-radius:50%;flex:none;display:inline-flex;align-items:center;justify-content:center;
    background:var(--panel-2);border:1px solid var(--line);color:var(--ink-dim);transition:color .12s,border-color .12s,background .12s}
  .pwr:hover{color:#ff7a7a;border-color:#ff7a7a;background:var(--press)}
  .pwr.sleep{color:var(--warn);border-color:rgba(255,180,84,.6)}
  .pwr.gear:hover{color:var(--accent);border-color:var(--accent)}
  .pwr.sleep:hover{color:var(--ok);border-color:var(--ok)}
  .pwr.busy{opacity:.45;pointer-events:none}
  .pwr.sure,.pwr.sure:hover{color:#fff;background:#c43c3c;border-color:#ff7a7a}   /* Restart clicked once: click again */
  .pad{position:relative;width:200px;height:200px;margin:8px auto 6px;border-radius:50%;
    background:radial-gradient(circle at 50% 40%,#20262e,#161b21);border:1px solid var(--line);
    box-shadow:inset 0 1px 0 rgba(255,255,255,.04),0 12px 30px rgba(0,0,0,.4)}
  .pad::before{content:"";position:absolute;inset:22px;border-radius:50%;border:1px solid #232a32}
  .dir{position:absolute;width:54px;height:54px;color:var(--ink-dim);font-size:17px;
    display:flex;align-items:center;justify-content:center;border-radius:14px}
  .dir:hover{color:var(--ink);background:rgba(31,162,232,.10)}
  .dir:active{color:var(--ink);background:rgba(31,162,232,.2)}
  .dir.up{top:4px;left:50%;transform:translateX(-50%)}
  .dir.down{bottom:4px;left:50%;transform:translateX(-50%)}
  .dir.left{left:4px;top:50%;transform:translateY(-50%)}
  .dir.right{right:4px;top:50%;transform:translateY(-50%)}
  .ok{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:80px;height:80px;
    border-radius:50%;background:radial-gradient(circle at 50% 35%,#2b333c,#171c22);
    border:1px solid #2f3843;color:var(--ink);font-family:var(--mono);font-size:12px;letter-spacing:.1em;
    transition:box-shadow .12s}
  .ok:hover{box-shadow:0 0 0 1px var(--accent-2) inset;color:#fff}
  .ok:active{box-shadow:0 0 0 2px var(--accent) inset}
  .row{display:flex;gap:8px;margin-top:8px}
  .key{flex:1;background:var(--panel-2);border:1px solid var(--line);border-radius:11px;min-height:40px;
    padding:9px 6px;font-size:12px;font-weight:600;display:flex;align-items:center;justify-content:center;
    gap:5px;white-space:nowrap;transition:background .12s,border-color .12s,transform .08s}
  .key:hover{background:var(--press);border-color:#3a434e;color:#fff;transform:translateY(-1px)}
  .key:active{background:var(--press);transform:translateY(0)}
  .key .lbl{font-size:11px;color:var(--ink-dim);font-weight:500}
  .key:hover .lbl{color:var(--ink)}

  /* screen */
  .shot{margin-top:10px;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:#000;
    aspect-ratio:16/9;display:flex;align-items:center;justify-content:center;position:relative}
  .shot img,.shot canvas{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;display:none;background:#000}
  .shot.loading #shotimg{opacity:.45;transition:opacity .15s}
  .shot.masked #livecv{visibility:hidden}
  .vbox{position:absolute;display:none;background:#000;z-index:1;pointer-events:none}
  .vbox.show{display:block}
  .shot .ph{display:flex;flex-direction:column;align-items:center;gap:8px;max-width:min(460px,86%);text-align:center;
    padding:22px 26px;border-radius:14px;background:#11161c;border:1px solid var(--line);border-top:3px solid var(--ink-dim)}
  .ph .phic{width:38px;height:38px;color:var(--ink-dim)}
  .ph .phic svg{width:100%;height:100%}
  .ph .phic .spin{width:30px;height:30px;border-width:3px;margin:4px}
  .ph .pht{font-size:16px;font-weight:600;color:var(--ink);line-height:1.3}
  .ph .phs{font-size:13px;line-height:1.5;color:var(--ink-dim)}
  .ph .btn{margin-top:4px}
  .ph.k-info{border-top-color:var(--accent)} .ph.k-info .phic{color:var(--accent)}
  .ph.k-wait{border-top-color:var(--accent)}
  .ph.k-warn{border-top-color:var(--warn);background:#1a150d} .ph.k-warn .phic{color:var(--warn)}
  .ph.k-bad{border-top-color:#ff7a7a;background:#1c1012} .ph.k-bad .phic{color:#ff7a7a}
  .shotbadge{position:absolute;top:10px;right:10px;display:none;align-items:center;gap:7px;
    background:rgba(13,17,22,.82);border:1px solid var(--line);border-radius:999px;
    padding:5px 12px;font-family:var(--mono);font-size:11px;color:var(--ink);z-index:2}
  .shotbadge.show{display:inline-flex}
  .prot{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);display:none;flex-direction:column;
    align-items:center;gap:5px;max-width:78%;text-align:center;background:rgba(13,17,22,.82);
    border:1px solid var(--line);border-radius:12px;padding:12px 18px;z-index:2;pointer-events:none}
  .prot.show{display:flex}
  .prot b{display:flex;align-items:center;gap:7px;font-size:13px;font-weight:600;color:var(--ink)}
  .prot b svg{width:13px;height:13px;flex:none;stroke:var(--ink-dim)}
  .prot .sub{font-size:12px;line-height:1.45;color:var(--ink-dim)}
  .spin{width:11px;height:11px;border:2px solid var(--ink-dim);border-top-color:var(--accent);
    border-radius:50%;animation:sp .7s linear infinite}
  @keyframes sp{to{transform:rotate(360deg)}}
  .inline{display:flex;gap:8px}
</style>
</head>
<body>
  <header class="top">
    <h1>
      <img id="logo" alt="" style="height:30px;width:auto;display:none;border-radius:6px">
      <span id="titletext"><span class="fx">Fleet</span> Remote</span>
      <span id="appver"></span>
    </h1>
    <button class="btn small ghost" id="tickbtn" title="Tick sound when the pointer moves over a button" onclick="toggleTicks()"></button>
    <div class="log" id="log"></div>
  </header>

  <div class="wrap">
    <!-- 1. DEVICES -->
    <div class="card" id="devcard">
      <div class="head">
        <h2>Devices</h2>
        <span class="headr2">
          <button class="btn small ghost" id="grptoggle" onclick="toggleGroups()" title="Add groups, rename or delete them">+ Add group</button>
          <button class="btn small ghost" id="addtoggle" onclick="toggleAdd()">+ Add device</button>
        </span>
      </div>
      <div class="addbox" id="grpbox" style="display:none">
        <div class="gnew">
          <input id="grp-new" placeholder="New group, e.g. Studio A" maxlength="40">
          <button class="btn small" onclick="addGroup()">Add</button>
        </div>
        <div id="grpmgr" style="display:flex;flex-direction:column;gap:6px"></div>
        <div class="gnone">Click a group's name to rename it. Deleting a group keeps its devices; they move to No group.</div>
      </div>
      <div class="addbox" id="addbox" style="display:none">
        <div class="seg" id="typeseg">
          <button data-t="fire" class="on" onclick="setType('fire')">Fire Cube</button>
          <button data-t="appletv" onclick="setType('appletv')">Apple TV</button>
        </div>
        <input id="add-ip" placeholder="IP address, e.g. 10.0.0.52">
        <input id="add-name" placeholder="Name (optional)">
        <select id="add-group" title="Group: make new ones with + Add group"></select>
        <div class="two">
          <button class="btn" onclick="addDevice()">Add and connect</button>
          <button class="btn ghost" onclick="toggleAdd(false)">Cancel</button>
        </div>
        <div class="tip" id="addhint"></div>
      </div>
      <div id="pairbox" style="display:none;margin-bottom:10px;padding:12px;border:1px solid var(--accent);border-radius:12px;background:#0e1620">
        <div id="pairmsg" style="font-size:13px;margin-bottom:8px">Pairing…</div>
        <div style="display:flex;gap:8px">
          <input id="pairpin" placeholder="Code on TV" inputmode="numeric" maxlength="4"
                 style="font-family:var(--mono);font-size:16px;letter-spacing:.3em;text-align:center">
          <button class="btn" onclick="pairPin()">OK</button>
          <button class="btn ghost" onclick="pairCancel()">Cancel</button>
        </div>
        <div id="pairerr" style="font-size:12px;color:var(--warn);margin-top:6px;min-height:14px"></div>
      </div>
      <div id="devlist"></div>
      <div class="empty" id="devempty" style="display:none">No devices yet. Add one with its IP address.</div>
      <button class="link" id="connall" style="display:none" onclick="connectAll()">Connect all</button>
      <div class="tip">Click a device to control it. Double-click its name or IP to change its name, IP address or group. Click a group to fold or unfold it. Make groups with + Add group.</div>
    </div>

    <!-- 2. REMOTE -->
    <div class="card" id="remote">
      <div class="head"><h2>Remote</h2><span class="headr"><span class="warn scopest" id="scopest"></span><button class="pwr gear" id="setbtn" title="Settings: opens the TV's Settings page" onclick="send('settings', 'Settings')">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg></button><button class="pwr" id="rbtbtn" title="Restart" onclick="restartDev()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg></button><button class="pwr" id="pwrbtn" title="Power" onclick="powerKey()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 3v8"/><path d="M6.6 6.6a7.6 7.6 0 1 0 10.8 0"/></svg></button></span></div>
      <div class="scope" id="scope">Add a device to start</div>
      <div class="pad">
        <button class="dir up"    title="Up"    onclick="key('up')">▲</button>
        <button class="dir down"  title="Down"  onclick="key('down')">▼</button>
        <button class="dir left"  title="Left"  onclick="key('left')">◀</button>
        <button class="dir right" title="Right" onclick="key('right')">▶</button>
        <button class="ok"        title="OK"    onclick="key('select')">OK</button>
      </div>
      <div class="row">
        <button class="key" title="Back" onclick="key('back')">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 14 4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-1"/></svg><span class="lbl">Back</span></button>
        <button class="key" title="Home" onclick="key('home')">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11l9-8 9 8"/><path d="M5 10v9a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-9"/></svg><span class="lbl">Home</span></button>
        <button class="key" title="Menu" onclick="key('menu')">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="4" y1="7" x2="20" y2="7"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="17" x2="20" y2="17"/></svg><span class="lbl">Menu</span></button>
      </div>
      <div class="row">
        <button class="key" title="Rewind" onclick="key('rewind')">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M11 12l9-6v12z"/><path d="M3 12l9-6v12z"/></svg></button>
        <button class="key" title="Play / Pause" onclick="key('play_pause')">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z" transform="translate(-1,0)"/><rect x="4" y="5" width="2.4" height="14" rx="0.4"/></svg></button>
        <button class="key" title="Fast forward" onclick="key('fast_forward')">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M13 12L4 6v12z"/><path d="M21 12l-9-6v12z"/></svg></button>
      </div>
      <div class="row">
        <button class="key" title="Volume down" onclick="key('volume_down')">Vol −</button>
        <button class="key" title="Volume up" onclick="key('volume_up')">Vol +</button>
      </div>
      <div class="tip">Your keyboard works too: arrows, Enter = OK, Backspace = Back, Space = Play/Pause.</div>
    </div>

    <!-- 3. SCREEN + 4. KEYBOARD + 5. HDMI SIGNAL & SLEEP -->
    <div id="screencol">
      <div class="card">
        <div class="head">
          <h2>Screen</h2>
          <select id="livequal" title="Live video quality" onchange="liveQualityChanged()" style="display:none">
            <option value="low">Quality: Low (960 wide, 2 Mbps)</option>
            <option value="normal" selected>Quality: Normal (1280 wide, 4 Mbps)</option>
            <option value="high">Quality: High (1920 wide, 8 Mbps)</option>
          </select>
        </div>
        <div class="seg" id="scrseg">
          <button data-m="off"  onclick="setScreen('off')">Off</button>
          <button data-m="shot" onclick="setScreen('shot')">Screenshot</button>
          <button data-m="auto" onclick="setScreen('auto')">Auto (every 3 s)</button>
          <button data-m="live" onclick="setScreen('live')">Live video</button>
        </div>
        <div class="shot" id="shot">
          <div class="ph" id="shotph"><div class="phic"></div><div class="pht"></div><div class="phs"></div></div>
          <img id="shotimg" alt="">
          <canvas id="livecv"></canvas>
          <div class="vbox" id="vbox"></div>
          <span class="shotbadge" id="livebadge"><span class="spin"></span><span id="livebadgetxt">Starting</span></span>
          <span class="shotbadge" id="shotbadge"><span class="spin"></span><span>Capturing</span></span>
          <div class="prot" id="prot">
            <b><svg viewBox="0 0 16 16" fill="none" stroke-width="1.6"><rect x="3" y="7" width="10" height="7" rx="1.5"/><path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2"/></svg><span id="prottitle">Protected video</span></b>
            <span class="sub" id="protsub">It is playing on the TV. Fire TV keeps video out of screen captures, so the picture here stops.</span>
          </div>
        </div>
        <div class="tip" id="scrhint"></div>
      </div>

      <div class="card" style="margin-top:14px">
        <div class="head">
          <h2>Keyboard</h2>
          <div class="seg" id="kbseg" style="min-width:250px">
            <button data-k="send" class="on" onclick="setKb('send')">Type + Send</button>
            <button data-k="live" onclick="setKb('live')">Live typing</button>
          </div>
        </div>
        <div class="inline">
          <input id="typetext" placeholder="Type text, press Enter to send">
          <button class="btn" id="sendbtn" onclick="sendText()">Send</button>
        </div>
        <div class="row">
          <button class="key" onclick="key('delete')"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 5H9l-7 7 7 7h11a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2Z"/><path d="m18 9-6 6"/><path d="m12 9 6 6"/></svg>Backspace</button>
          <button class="key" onclick="clearField()">Clear field</button>
          <button class="key" onclick="key('enter')">Enter<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 10 4 15 9 20"/><path d="M20 4v7a4 4 0 0 1-4 4H4"/></svg></button>
        </div>
        <div class="tip" id="kbhint"></div>
      </div>

      <!-- 5. SELECTED DEVICE: HDMI signal + never sleep (Fire TV only; hidden for an Apple TV) -->
      <div class="card" id="sleepcard" style="margin-top:14px">
        <div class="head"><h2>HDMI Signal &amp; Sleep</h2>
          <button class="btn small ghost" id="edidbtn" onclick="downloadEdid()" title="Save the EDID of the TV this Fire TV is plugged into (.bin and .txt) to Downloads">Download EDID</button></div>
        <div id="sleeplist"></div>
        <div class="tip">Never sleep: Enabled keeps it awake (no screen saver, no sleep), Disabled puts its own timers back. Video, Colour, HDR and Audio are what it sends over HDMI right now; the bars are the audio channels present, not levels. Download EDID saves the EDID of the TV it's plugged into, as .bin and .txt, to Downloads.</div>
      </div>
    </div>
  </div>

<script>
const $ = id => document.getElementById(id);
const esc = t => String(t).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const devUrl = (ip, tail) => "/api/devices/" + encodeURIComponent(ip) + (tail || "");   // every device URL is built here
let devices = [];        // [{ip,name,connected,type,group}]
let allGroups = [];      // every group name, also ones with no devices yet
let focused = null;      // ip of the device being controlled
let prevFocus = null;    // to notice when the focused device changes
let autoTimer = null;
let scrMode = "off";     // off | shot | auto | live
let liveFails = [];      // decoder errors in the last minute (live restarts itself up to 3 times)
// How the H.264 reaches the decoder. "avcc": avcC description + length-prefixed NAL units
// (the MP4 form: Chromium, and WebKit by the WebCodecs spec). "annexb": start codes, no
// description, SPS + PPS in front of every key frame. A decoder that fails before its
// first picture gets the other form next; the form that showed a picture is remembered.
let liveForm = (()=>{ try{ return localStorage.getItem("fr_liveform") === "annexb" ? "annexb" : "avcc"; }catch(e){ return "avcc"; } })();
let liveFlipped = false;
function rememberForm(f){
  liveForm = f;
  try{ if(localStorage.getItem("fr_liveform") !== f) localStorage.setItem("fr_liveform", f); }catch(e){}
}

// ms: how long to wait for the answer (a connect or a command can take a while on a slow device)
// AbortSignal.timeout is missing in Safari before 16 (older Macs)
function timeoutSignal(ms){
  if(AbortSignal.timeout) return AbortSignal.timeout(ms);
  const c = new AbortController();
  setTimeout(()=>c.abort(new DOMException("timed out", "TimeoutError")), ms);
  return c.signal;
}
async function api(method, path, body, ms){
  const opt = {method, headers:{"Content-Type":"application/json"}, signal: timeoutSignal(ms || 30000)};
  if(body!==undefined) opt.body = JSON.stringify(body);
  let r;
  try{ r = await fetch(path, opt); }
  catch(e){ throw new Error(e.name === "TimeoutError" || e.name === "AbortError" ? "No answer from Fleet Remote (timed out)" : "Fleet Remote is not reachable"); }
  const data = await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(data.detail || ("HTTP "+r.status));
  return data;
}
function log(msg, bad){
  const el = $("log");
  el.innerHTML = bad ? `<span class="bad">${msg}</span>` : msg;
  el.title = el.textContent;
}

// ---- which device the controls act on ----
function focusedDev(){ return devices.find(x=>x.ip===focused); }
function setFocusQuiet(ip){ focused = ip; prevFocus = ip; render(); }
function updateScope(){
  const d = focusedDev();
  $("scopest").textContent = !d ? "" : d.power==="offline" ? "Not responding" : !d.connected ? "Not connected"
    : d.power==="asleep" ? "Standby" : "";
  const zz = !!d && d.connected && d.power==="asleep";
  $("pwrbtn").classList.toggle("sleep", zz);
  $("pwrbtn").title = !d ? "Power" : zz ? "Power: wake " + d.name : "Power: put " + d.name + " in standby";
  $("rbtbtn").style.display = d && d.type === "appletv" ? "none" : "";     // pyatv has no restart
  $("rbtbtn").title = !d ? "Restart" : "Restart " + d.name + " (it is back in about a minute)";
  if(!d) $("scope").textContent = devices.length ? "Click a device to control it" : "Add a device to start";
  else $("scope").innerHTML = `Controlling <b>${esc(d.name)}</b>`;
  $("scope").title = $("scope").textContent;
}

// ---- device list ----
// Two list requests can be in flight at once (the 2 s poll and a refresh after
// a click); an answer older than one already shown is dropped.
let listSeq = 0, listShown = 0;
async function fetchDevices(){
  const seq = ++listSeq;
  const {devices:list, groups} = await api("GET","/api/devices");
  if(seq < listShown) return null;
  listShown = seq;
  allGroups = groups || [];
  return list;
}
async function refresh(){
  const list = await fetchDevices();
  if(!list) return;
  devices = list; devSig = JSON.stringify([list, allGroups]);
  pickFocus();
  render();
}
// Focus the first connected device, or the first device, if nothing is focused.
function pickFocus(){
  if(!devices.find(d=>d.ip===focused)){
    const c = devices.find(d=>d.connected);
    focused = c ? c.ip : (devices[0] ? devices[0].ip : null);
  }
}
// The list is asked again every 2 s, so standby, wake and a dropped link show by themselves.
let devSig = "", pollBusy = false, pollHold = false;
const pillBusy = {};     // ip -> pill text while connecting / disconnecting
function applyDevices(list){
  const sig = JSON.stringify([list, allGroups]);
  if(sig === devSig) return;
  devSig = sig; devices = list; pickFocus();
  render();                       // waits by itself while a name is being edited
}
async function pollDevices(){
  if(pollBusy || pollHold) return;
  pollBusy = true;
  try{ const list = await fetchDevices(); if(list) applyDevices(list); }catch(e){}
  pollBusy = false;
  if(renderLater && !pressing() && !$("devlist").querySelector("input")){ renderLater = false; render(); }
  pollSleep();
}
// The list is not rebuilt while the mouse button is down on it (the click would
// land on a new element and get lost) or while a name is being edited.
let ptrDown = false, ptrAt = 0, renderLater = false;
const pressing = () => ptrDown && performance.now() - ptrAt < 2000;   // a release outside the window may never arrive
// Groups: devices with the same group name are listed together under a header that
// folds. No groups at all = the plain list. Folded groups are remembered on this computer.
const folded = new Set((()=>{ try{ return JSON.parse(localStorage.getItem("fr_folded") || "[]"); }catch(e){ return []; } })());
function saveFolded(){ try{ localStorage.setItem("fr_folded", JSON.stringify([...folded])); }catch(e){} }
const groupNames = () => [...new Set([...allGroups, ...devices.map(d=>d.group).filter(Boolean)])]
  .sort((a,b)=>a.localeCompare(b, undefined, {numeric:true, sensitivity:"base"}));
const groupOptions = sel => [`<option value="">No group</option>`]
  .concat(groupNames().map(g=>`<option value="${esc(g)}"${g===sel ? " selected" : ""}>${esc(g)}</option>`)).join("");
function toggleGroup(g){
  if(folded.has(g)) folded.delete(g); else folded.add(g);
  saveFolded(); render();
}
function render(){
  const box = $("devlist");
  if(pressing() || box.querySelector("input")){ renderLater = true; updateScope(); powerScreen(); pollSleep(); return; }
  box.innerHTML = "";
  const names = groupNames();
  const ag = $("add-group"), keep = ag.value;
  ag.innerHTML = groupOptions(names.includes(keep) ? keep : "");
  renderGroups();
  // an empty group still shows, so a group just made is there to see
  const sections = names.length ? [...names, ""].map(g=>[g, devices.filter(d=>(d.group || "") === g)]).filter(([g, l])=>g || l.length)
    : [[null, devices]];
  sections.forEach(([g, list])=>{
  if(g !== null){
    const gkey = g || "\u0000none";
    const h = document.createElement("div");
    const fold = folded.has(gkey);
    if(!list.length){
      h.className = "grp empty"; h.title = "No devices in '" + g + "' yet: pick it when you add a device, or click a device's name";
      h.innerHTML = `<svg class="car" viewBox="0 0 10 10"><path d="M1 2.8h8L5 7.8z" fill="currentColor"/></svg><span class="gnm">${esc(g)}</span>`;
      box.appendChild(h);
      return;
    }
    h.className = "grp" + (fold ? " fold" : "") + (list.some(d=>d.ip===focused) ? " has-active" : "");
    h.title = (fold ? "Click to show " : "Click to hide ") + "the devices in " + (g ? "'" + g + "'" : "No group");
    h.innerHTML = `<svg class="car" viewBox="0 0 10 10"><path d="M1 2.8h8L5 7.8z" fill="currentColor"/></svg><span class="gnm">${esc(g || "No group")}</span>`;
    h.onclick = ()=>toggleGroup(gkey);
    box.appendChild(h);
    if(fold) return;
  }
  list.forEach(d=>{
    const el = document.createElement("div");
    el.className = "dev" + (d.ip===focused ? " active":"");
    el.dataset.ip = d.ip;
    el.onclick = e=>{
      if(e.target.classList.contains("x")) return;
      const now = performance.now();
      const dbl = !!e.target.closest(".meta") && lastTap.ip === d.ip && now - lastTap.t < 450;
      lastTap = dbl ? {ip:null, t:0} : {ip:d.ip, t:now};
      focused = d.ip; render();
      if(dbl) editDevice(d.ip);
    };

    const meta = document.createElement("div"); meta.className="meta";
    const tag = d.type==="appletv" ? '<span class="tag">ATV</span>' : "";
    meta.title = d.name + (d.name===d.ip ? "" : "\n" + d.ip) + "\nDouble-click to change the name, IP address or group";
    meta.innerHTML = `<div class="nmrow"><div class="nm">${esc(d.name)}</div>${tag}</div>${d.name===d.ip ? "" : `<div class="ip">${esc(d.ip)}</div>`}`;

    const st = document.createElement("button");
    // Standby / offline show in the pill itself, so the card keeps its size.
    const asleep = d.connected && d.power==="asleep", gone = !d.connected && d.power==="offline";
    const label = asleep ? "Standby" : gone ? "Offline" : d.connected ? "Connected" : "Connect";
    st.className = "st" + (d.connected ? " on" : "") + (asleep ? " pw" : "") + (gone ? " gone" : "");
    st.textContent = label;
    st.title = asleep ? "In standby: the TV picture is off. Press Power on the remote here to wake it.\nClick to disconnect."
      : gone ? "Not answering: it may be unplugged, off the network or restarting. Fleet Remote keeps trying to reconnect.\nClick to try now."
      : d.connected ? "Click to disconnect" : "Connect to this device";
    if(d.connected){
      st.dataset.hv = "Disconnect";
      st.onclick = e=>{ e.stopPropagation(); disconnect(d.ip); };
    } else {
      if(gone) st.dataset.hv = "Connect";
      st.onclick = e=>{ e.stopPropagation(); connect(d.ip); };
    }
    if(pillBusy[d.ip]){ st.className = "st busy"; st.textContent = pillBusy[d.ip]; }

    // ✕ asks once: the first click shows "Sure?", a second click within 3 s removes the device
    const x = document.createElement("button"); x.className="x"; x.textContent="✕"; x.title="Remove device";
    x.onclick = e=>{
      e.stopPropagation();
      if(x.dataset.sure){ removeDevice(d.ip); return; }
      x.dataset.sure = "1"; x.textContent = "Sure?"; x.title = "Click again to remove " + d.name;
      setTimeout(()=>{ if(x.isConnected){ delete x.dataset.sure; x.textContent = "✕"; x.title = "Remove device"; } }, 3000);
    };

    el.append(meta, st, x);
    box.appendChild(el);
  });
  });
  $("devempty").style.display = devices.length ? "none" : "";
  if(!devices.length && $("addbox").style.display==="none" && $("grpbox").style.display==="none") toggleAdd(true);
  $("connall").style.display = (devices.length > 1 && devices.some(d=>!d.connected)) ? "" : "none";
  updateScope();
  if(focused !== prevFocus){ prevFocus = focused; onFocusChange(); }
  else paintScreen();
  powerScreen();
  pollSleep();
}

// ---- selected device (Fire TV only): never sleep + HDMI output. The sleep
// timers are asked every 15 s, the HDMI output (video mode, audio) every 3 s. ----
const DAY_MS = 86400000;
const sleepState = {}, sleepAt = {}, sleepBusy = {}, sleepLoading = {};  // ip -> server view | when asked | pill text while changing
const outState = {}, outAt = {}, outLoading = {};                        // ip -> HDMI output | when asked
const sleepGen = {};             // ip -> bumped when its readings are dropped, so a late answer from before is ignored
let sleepSig = "";
const SLEEP_MODE = {on: "Black Screen", off: "No Signal"};
const SLEEP_TIP = "Sleep Mode Behavior, set on the TV: Settings > Display & Sounds > Display.\n"
  + "No Signal: the HDMI signal stops while it sleeps. Black Screen: HDMI stays on with a black picture.\n"
  + "Enabled asks for Black Screen, but Fire OS lets only the TV's own Settings change it.";
const AUDIO_TIP = "Audio channels on the HDMI output, as the Fire TV reports them: lit = present, moving = sound is playing.\n"
  + "Fire OS gives no per-channel levels without root, so these are not meters.";
const mins = ms => ms >= 3600000 ? +(ms / 3600000).toFixed(1) + " h" : Math.max(1, Math.round(ms / 60000)) + " min";
function sleepText(s){
  const so = s.screen_off_ms, st = s.sleep_ms;
  const saverOff = !s.screensaver || !!s.stay_on || (so != null && so >= DAY_MS) || (s.contextual_ms != null && s.contextual_ms >= DAY_MS);
  const noSleep = st != null && (st <= 0 || st >= DAY_MS);
  const saver = saverOff ? "no screen saver" : so ? "screen saver " + mins(so) : "screen saver on";
  const sleep = noSleep ? "never sleeps" : st ? "sleep " + mins(st) : "sleep at the Fire OS default";
  const t = s.never ? "never sleeps, " + saver : saver + ", " + sleep;
  const tip = (saverOff ? "No screen saver" : so ? "Screen saver starts after " + mins(so) + " idle" : "Screen saver on")
    + ". " + (noSleep ? "It never goes to sleep." : st ? "It goes to sleep after " + mins(st) + "." : "");
  return [t.charAt(0).toUpperCase() + t.slice(1), tip];
}
// "3840x2160p 59.94 fps", "4:2:0, 10-bit" and "Dolby Digital+, 2 ch, 48 kHz"
function outText(o){
  const v = o && o.video, a = o && o.audio;
  const fps = x => x % 1 ? x.toFixed(2) : String(x);
  const vt = !v ? "no HDMI output info" : v.width + "x" + v.height + (v.scan || "") + " " + fps(v.fps) + " fps";
  const ct = !v ? "" : [v.chroma, v.depth ? (v.configured ? "up to " : "") + v.depth + "-bit" : ""].filter(Boolean).join(", ");
  const ht = !v || !v.hdr ? "" : v.hdr + (v.hdr_tv ? " (TV: " + (v.hdr_tv.length ? v.hdr_tv.join(", ") : "SDR only") + ")" : "");
  const at = !a ? "no HDMI output info" : !a.on ? "off"
    : a.format + (a.channels ? ", " + a.channels + " ch" : "") + (a.rate_khz ? ", " + a.rate_khz + " kHz" : "");
  return [vt, ct, at, ht];
}
function renderSleep(){
  const d = focusedDev(), show = !!d && d.type !== "appletv";
  $("sleepcard").style.display = show ? "" : "none";
  if(!show){ sleepSig = ""; return; }
  $("edidbtn").disabled = !d.connected;
  const s = d.connected ? sleepState[d.ip] : null, o = d.connected ? outState[d.ip] : null;
  const sig = JSON.stringify([d.ip, d.name, d.connected, d.power, s, sleepBusy[d.ip], o]);
  if(sig === sleepSig) return;
  sleepSig = sig;
  const box = $("sleeplist"); box.innerHTML = "";
  const el = document.createElement("div"); el.className = "slp";
  const L = document.createElement("div"); L.className = "colL"; el.append(L);   // name, Never sleep pill, sleep lines
  const row = document.createElement("div"); row.className = "slrow";
  const nm = document.createElement("span"); nm.className = "slnm";
  nm.textContent = d.name; nm.title = d.name === d.ip ? d.ip : d.name + " (" + d.ip + ")";
  row.append(nm); L.append(row); box.appendChild(el);
  let col = L;
  const line = (cls, text, title) => { const x = document.createElement("div"); x.className = cls; x.textContent = text; if(title) x.title = title; col.append(x); return x; };
  if(!d.connected){
    el.classList.add("off");
    const off = document.createElement("span"); off.className = "sloff";
    off.textContent = d.power === "offline" ? "Not responding" : "Not connected";
    off.title = "Connect it to see its sleep settings and HDMI output";
    row.append(off);
    return;
  }
  const l1 = line("sl", "");
  if(!s){ l1.textContent = "Reading…"; }
  else if(s.err){ l1.textContent = "Couldn't read it: " + s.err; l1.classList.add("warn"); }
  else {
    const busy = sleepBusy[d.ip], label = s.never ? "Enabled" : "Disabled";
    const st = document.createElement("button");
    st.className = busy ? "st busy" : "st" + (s.never ? " on" : "");
    st.textContent = busy || label;
    st.dataset.hv = s.never ? "Disable" : "Enable";
    st.title = s.never ? "Never sleep is on. Click to put " + d.name + "'s own sleep timers back."
      : "Click to keep " + d.name + " awake: no screen saver, never sleeps.";
    st.onclick = ()=>toggleSleep(d.ip);
    const r2 = document.createElement("div"); r2.className = "slrow sub";
    const lab = document.createElement("span"); lab.className = "sllab"; lab.textContent = "Never sleep"; lab.title = st.title;
    r2.append(lab, st); L.insertBefore(r2, l1);
    const [t, tip] = sleepText(s);
    l1.textContent = t; l1.title = tip;
    if(s.never) l1.classList.add("good");
    const l2 = line("sl", "Sleep Mode: " + (SLEEP_MODE[s.hdmi_sleep_mode] || "not on this model"), SLEEP_TIP);
    if(s.never && s.hdmi_sleep_mode === "off"){ l2.textContent += " (set it on the TV)"; l2.classList.add("warn"); }
  }
  // HDMI output: what the Fire TV sends to the TV right now (right column)
  el.classList.add("two");
  col = document.createElement("div"); col.className = "colR"; el.append(col);
  if(o && o.err){ line("outl first warn", "HDMI output: couldn't read it (" + o.err + ")"); return; }
  const asleep = d.power === "asleep";     // the mode is still in the driver, but nothing goes out
  const [vt, ct, at, ht] = outText(o);
  const lv1 = line("outl first", "");
  lv1.innerHTML = "<b>Video:</b> " + esc(asleep ? (s && s.hdmi_sleep_mode === "on" ? "standby, black picture" : "standby, HDMI output off") : o ? vt : "Reading…");
  lv1.title = "Resolution and frame rate of the HDMI output" + (o && o.video ? ": " + vt : "");
  const cfg = o && o.video && o.video.configured;    // Fire OS's settings, not the live signal (Fire TV Stick)
  if(ct && !asleep) line("outl", "", cfg ? "Colour format and colour depth Fire OS set up for the HDMI link (Settings > Display & Sounds > Display). "
    + "This Fire TV model doesn't let the live signal be read, so the chroma subsampling and the exact depth going out right now are unknown."
    : "Chroma subsampling and bit depth of the HDMI output").innerHTML = "<b>Colour:</b> " + esc(ct);
  if(ht && !asleep) line("outl", "", cfg ? "Whether this Fire TV sends HDR, from its Dynamic Range setting and the HDR formats the TV announced (in brackets). "
    + "This Fire TV model doesn't let the live signal be read."
    : "HDR format of the HDMI output right now; in brackets the HDR formats the TV accepts").innerHTML = "<b>HDR:</b> " + esc(ht);
  const la = line("outl", "");
  la.innerHTML = "<b>Audio:</b> " + esc(asleep ? "standby" : o ? at : "Reading…");
  la.title = "Audio format on the HDMI output" + (o && o.audio ? ": " + at : "");
  const bars = document.createElement("div"); bars.className = "abars"; bars.title = AUDIO_TIP;
  const n = !asleep && o && o.audio && o.audio.on ? Math.min(8, o.audio.channels || 0) : 0;
  for(let i = 0; i < 8; i++){
    const c = document.createElement("div"); c.className = "abc";
    const b = document.createElement("div"); b.className = "ab" + (i < n ? " on" : "") + (i < n && o.active ? " live" : "");
    b.style.animationDelay = (i * 0.11) + "s";
    const t = document.createElement("div"); t.className = "abn"; t.textContent = i + 1;
    c.append(b, t); bars.append(c);
  }
  col.append(bars);
}
async function loadSleep(ip){
  if(sleepBusy[ip] || sleepLoading[ip]) return;
  sleepLoading[ip] = true; sleepAt[ip] = Date.now();
  const gen = sleepGen[ip] || 0;
  let r;
  try{ r = await api("GET", devUrl(ip, "/sleep")); }
  catch(e){ r = {err: e.message}; }
  delete sleepLoading[ip];
  if((sleepGen[ip] || 0) !== gen) return;        // dropped or disconnected while we waited
  sleepState[ip] = r;
  renderSleep();
}
async function loadOutput(ip){
  if(outLoading[ip]) return;
  outLoading[ip] = true; outAt[ip] = Date.now();
  const gen = sleepGen[ip] || 0;
  let r;
  try{ r = await api("GET", devUrl(ip, "/output")); }
  catch(e){ r = {err: e.message}; }
  delete outLoading[ip];
  if((sleepGen[ip] || 0) !== gen) return;
  outState[ip] = r;
  renderSleep();
}
function forgetSleep(ip){
  sleepGen[ip] = (sleepGen[ip] || 0) + 1;
  for(const m of [sleepState, sleepAt, sleepLoading, outState, outAt, outLoading]) delete m[ip];
}
// Only the selected device is asked. A device that lost its link forgets its
// readings; one that left the list forgets everything.
function pollSleep(){
  const now = Date.now(), d = focusedDev();
  for(const ip of new Set([...Object.keys(sleepAt), ...Object.keys(outAt), ...Object.keys(sleepState), ...Object.keys(outState)])){
    const x = devices.find(k => k.ip === ip);
    if(!x || !x.connected) forgetSleep(ip);
  }
  for(const m of [backAt, bootWait]) for(const ip of Object.keys(m)) if(!devices.some(k => k.ip === ip)) delete m[ip];
  if(d && d.type !== "appletv" && d.connected){
    if(!(now - (sleepAt[d.ip] || 0) < 15000)) loadSleep(d.ip);
    if(!(now - (outAt[d.ip] || 0) < 3000)) loadOutput(d.ip);
  }
  renderSleep();
}
function saveBlob(blob, name){
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = name;
  document.body.append(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href), 10000);
}
async function downloadEdid(){
  const d = focusedDev(), b = $("edidbtn");
  if(!d || !d.connected || b.dataset.busy) return;
  const nm = esc(d.name);
  b.dataset.busy = "1"; b.textContent = "Reading…";
  try{
    const r = await api("POST", devUrl(d.ip, "/edid"));
    const src = r.stored ? " (the copy Fire OS saved when it last detected the TV: this model doesn't share the live EDID)" : "";
    if(r.saved) log("EDID of the TV on " + nm + " (" + r.bytes + " bytes) saved to " + esc(r.saved) + ": " + r.files.map(esc).join(" and ") + src);
    else {
      const hx = (r.hex || "").match(/../g);
      if(!hx) throw new Error("no EDID came back");
      saveBlob(new Blob([Uint8Array.from(hx, h => parseInt(h, 16))], {type:"application/octet-stream"}), r.file + ".bin");
      await new Promise(ok=>setTimeout(ok, 500));        // Safari drops a second download that comes at once
      saveBlob(new Blob([r.text], {type:"text/plain"}), r.file + ".txt");
      log("EDID of the TV on " + nm + " (" + r.bytes + " bytes) downloaded: " + esc(r.file) + ".bin and .txt" + src);
    }
  }catch(e){ log(nm + ": " + esc(e.message), true); }
  delete b.dataset.busy; b.textContent = "Download EDID";
}
async function toggleSleep(ip){
  const s = sleepState[ip]; if(!s || s.err || sleepBusy[ip]) return;
  const want = !s.never, d = devices.find(x => x.ip === ip), nm = esc(d ? d.name : ip);
  const gen = sleepGen[ip] || 0;
  sleepBusy[ip] = want ? "Enabling" : "Disabling"; renderSleep();
  try{
    const r = await api("PUT", devUrl(ip, "/sleep"), {never: want}, 60000);
    if((sleepGen[ip] || 0) === gen){ sleepState[ip] = r; sleepAt[ip] = Date.now(); }
    if(r.never !== want) log(nm + ": the Fire TV didn't keep the change", true);
    else if(want && r.hdmi_sleep_mode === "off")
      log("Never sleep on for " + nm + ". Sleep Mode stays No Signal: change that on the TV");
    else log(want ? "Never sleep on for " + nm : "Never sleep off for " + nm + ": its own sleep timers are back");
  }catch(e){ log(esc(e.message), true); }
  delete sleepBusy[ip]; renderSleep();
}

// The first click rebuilds the list (to show the selection), so a native dblclick
// would land on a replaced element: two clicks on the same device count instead.
let lastTap = {ip:null, t:0};
function editDevice(ip){
  const d = devices.find(x=>x.ip === ip);
  const row = [...$("devlist").querySelectorAll(".dev")].find(n=>n.dataset.ip === ip);
  const nm = row && row.querySelector(".nm");
  if(d && nm && !$("devlist").querySelector("input")) startRename(d, nm);
}
function startRename(d, nm){
  const box = document.createElement("div"); box.className = "edbox";
  const inp = document.createElement("input");
  inp.value = d.name; inp.maxLength = 60; inp.title = "Name";
  inp.style.cssText = "width:100%;padding:3px 6px;font-size:14px;font-weight:600";
  const iin = document.createElement("input");
  iin.value = d.ip; iin.maxLength = 253; iin.placeholder = "IP address"; iin.title = "IP address";
  iin.style.cssText = "width:100%;padding:3px 6px;font-size:12px";
  const gin = document.createElement("select");
  gin.innerHTML = groupOptions(d.group || "");
  gin.title = "Group: make new ones with + Add group";
  box.append(inp, iin, gin);
  nm.replaceWith(box); inp.focus(); inp.select();
  box.closest(".nmrow")?.classList.add("editing");
  let done = false;
  const finish = ()=>{ box.closest(".nmrow")?.classList.remove("editing"); box.replaceWith(nm); render(); };
  const save = async ()=>{
    if(done) return; done = true;
    const v = inp.value.trim(), g = gin.value, nip = iin.value.trim();
    finish();
    const newName = v && v !== d.name, newGroup = g !== (d.group || ""), newIp = nip && nip !== d.ip;
    if(!newName && !newGroup && !newIp) return;
    let ip = d.ip, reconnect = false;
    try{
      if(newIp){
        const r = await api("PUT", devUrl(d.ip, "/ip"), {ip:nip});
        if(lv && lv.ip === d.ip) stopLive(true);
        forgetSleep(d.ip); delete pillBusy[d.ip];
        if(focused === d.ip) focused = r.ip;
        ip = r.ip; reconnect = r.was_connected;
        log(esc(d.name === d.ip ? r.ip : d.name) + " moved from " + esc(d.ip) + " to " + esc(r.ip));
      }
      if(newName){ await api("PUT", devUrl(ip, "/name"), {name:v}); log("Renamed "+esc(ip)+" to "+esc(v)); }
      if(newGroup){
        await api("PUT", devUrl(ip, "/group"), {group:g});
        if(folded.delete(g || "\u0000none")) saveFolded();      // show it where it went
        log(esc(newName ? v : d.name) + (g ? " is now in " + esc(g) : " is in no group now"));
      }
      await refresh();
      if(reconnect) connect(ip);
    }catch(e){ log(esc(e.message), true); if(newIp) refresh(); }
  };
  box.onkeydown = e=>{
    if(e.key==="Enter"){ e.preventDefault(); save(); }
    else if(e.key==="Escape"){ done = true; finish(); }
  };
  // Safari doesn't focus a <select> or button on click (relatedTarget is null then): decide
  // after the click has landed, from where the focus really is
  let inBox = false;
  box.addEventListener("mousedown", ()=>{ inBox = true; setTimeout(()=>inBox = false, 300); });
  box.addEventListener("focusout", ()=>setTimeout(()=>{ if(!done && !inBox && !box.contains(document.activeElement)) save(); }, 0));
  box.onclick = e=>e.stopPropagation();
}

// ---- groups: make, rename, delete ----
function toggleGroups(show){
  const b = $("grpbox");
  const on = show===undefined ? b.style.display==="none" : show;
  b.style.display = on ? "" : "none";
  $("grptoggle").textContent = on ? "Close" : "+ Add group";
  if(on){ toggleAdd(false); renderGroups(); setTimeout(()=>$("grp-new").focus(), 0); }
}
function renderGroups(){
  const box = $("grpmgr");
  if($("grpbox").style.display==="none" || box.querySelector("input")) return;
  const names = groupNames();
  box.innerHTML = names.length ? "" : `<div class="gnone">No groups yet.</div>`;
  names.forEach(g=>{
    const n = devices.filter(d=>d.group===g).length;
    const row = document.createElement("div"); row.className = "grow";
    row.innerHTML = `<span class="gname" title="${esc(g)}&#10;Click to rename">${esc(g)}</span><span class="gn" title="Devices in this group">${n}</span>`;
    row.querySelector(".gname").onclick = ()=>renameGroup(g, row.querySelector(".gname"));
    const x = document.createElement("button"); x.className = "x"; x.textContent = "✕"; x.title = "Delete group";
    x.onclick = ()=>{
      if(x.dataset.sure){ deleteGroup(g); return; }
      x.dataset.sure = "1"; x.textContent = "Sure?";
      x.title = "Click again to delete " + g + (n ? " (its " + n + " device" + (n>1 ? "s go" : " goes") + " to No group)" : "");
      setTimeout(()=>{ if(x.isConnected){ delete x.dataset.sure; x.textContent = "✕"; x.title = "Delete group"; } }, 3000);
    };
    row.append(x);
    box.appendChild(row);
  });
}
async function addGroup(){
  const inp = $("grp-new"), g = inp.value.split(/\s+/).filter(Boolean).join(" ");
  if(!g) return log("Type a name for the group", true);
  try{
    const r = await api("POST", "/api/groups", {name:g});
    inp.value = ""; log("Group " + esc(r.name) + " made");
    await refresh(); inp.focus();
  }catch(e){ log(esc(e.message), true); }
}
function renameGroup(g, span){
  const inp = document.createElement("input");
  inp.value = g; inp.maxLength = 40; inp.title = "Group name";
  span.replaceWith(inp); inp.focus(); inp.select();
  let done = false;
  const finish = ()=>{ inp.replaceWith(span); renderGroups(); render(); };
  const save = async ()=>{
    if(done) return; done = true;
    const v = inp.value.split(/\s+/).filter(Boolean).join(" ");
    finish();
    if(!v || v === g) return;
    try{
      const r = await api("PUT", "/api/groups/" + encodeURIComponent(g), {name:v});
      if(folded.delete(g)){ folded.add(r.name); saveFolded(); }
      log("Group " + esc(g) + " is now " + esc(r.name));
      await refresh();
    }catch(e){ log(esc(e.message), true); }
  };
  inp.onkeydown = e=>{
    if(e.key==="Enter"){ e.preventDefault(); save(); }
    else if(e.key==="Escape"){ e.stopPropagation(); done = true; finish(); }
  };
  inp.addEventListener("blur", save);
}
async function deleteGroup(g){
  try{
    const r = await api("DELETE", "/api/groups/" + encodeURIComponent(g));
    if(folded.delete(g)) saveFolded();
    log("Group " + esc(g) + " deleted" + (r.moved ? ", its devices are in No group now" : ""));
    await refresh();
  }catch(e){ log(esc(e.message), true); }
}

// ---- add / remove / connect ----
let addType = "fire";
const ADD_HINT = {
  fire: "On the Fire TV first: Settings > My Fire TV > About, highlight the first line (the device name) and press Select 7 times "
      + "until it says you are a developer. Then Settings > My Fire TV > Developer options > ADB debugging: ON. "
      + "The first connection asks \"Allow USB debugging?\" on the TV: tick Always allow and choose OK.",
  appletv: "The Apple TV must be awake and on the same network. On the first connection it shows a 4-digit code: type it in here."
};
function setType(t){
  addType = t;
  document.querySelectorAll("#typeseg button").forEach(b=>b.classList.toggle("on", b.dataset.t===t));
  $("addhint").textContent = ADD_HINT[t];
}
function toggleAdd(show){
  const b = $("addbox");
  const on = show===undefined ? b.style.display==="none" : show;
  b.style.display = on ? "" : "none";
  $("addtoggle").textContent = on ? "Close" : "+ Add device";
  if(on){ toggleGroups(false); setType(addType); setTimeout(()=>$("add-ip").focus(), 0); }
}
async function addDevice(){
  const ip = $("add-ip").value.trim(); if(!ip) return log("Enter the device's IP address", true);
  const group = $("add-group").value;
  try{ await api("POST","/api/devices",{ip, name:$("add-name").value.trim(), type:addType, group});
    if(folded.delete(group || "\u0000none")) saveFolded();
    $("add-ip").value=""; $("add-name").value=""; toggleAdd(false); await refresh();
    connect(ip); }
  catch(e){ log(esc(e.message), true); }
}
async function removeDevice(ip){
  try{
    await api("DELETE", devUrl(ip));
    if(lv && lv.ip === ip) stopLive(true);
    if(focused === ip) focused = null;
    forgetSleep(ip);
    log("Removed " + esc(ip));
    await refresh();
  }catch(e){ log(esc(e.message), true); }
}
// Resolves to "ok", "pairing" (Apple TV: the PIN box is open) or "fail".
async function connect(ip, focusIt = true){
  const d = devices.find(x=>x.ip===ip), was = focused;
  log(d && d.type==="appletv" ? "Connecting to Apple TV "+esc(ip)+"…"
      : "Connecting "+esc(ip)+". Approve the prompt on the TV if it appears…");
  pillBusy[ip] = "Connecting"; render();
  let r;
  try{ r = await api("POST", devUrl(ip, "/connect"), undefined, 90000); }
  catch(e){ delete pillBusy[ip]; log(esc(e.message), true); await refresh().catch(()=>{}); return "fail"; }
  delete pillBusy[ip];
  if(r.need_pairing){ startPair(ip); return "pairing"; }
  if(focusIt && focused === was) focused = ip;     // the device you just connected, unless you picked another meanwhile
  log("Connected "+esc(ip));
  await refresh().catch(()=>{});
  return "ok";
}

// ---- Apple TV pairing flow ----
let pairIp = null;
async function startPair(ip){
  pairIp = ip;
  $("pairbox").style.display = "block";
  $("pairerr").textContent = ""; $("pairpin").value = "";
  $("pairmsg").textContent = "Starting pairing with "+ip+"…";
  try{
    const r = await api("POST","/api/atv/pair/start",{ip}, 60000);
    handlePairStep(r);
  }catch(e){ $("pairerr").textContent = e.message; }
}
function handlePairStep(r){
  if(r.status === "need_pin"){
    $("pairmsg").textContent = "Enter the code shown on the TV";
    $("pairpin").value = ""; $("pairpin").focus();
  } else if(r.status === "paired"){
    $("pairbox").style.display = "none";
    focused = pairIp; pairIp = null;
    log("Apple TV paired and connected ✓");
    refresh().catch(()=>{});
  } else {
    $("pairerr").textContent = "Unexpected answer from pairing (" + (r.status || "no status") + "). Click Cancel and try Connect again.";
  }
}
async function pairPin(){
  const pin = $("pairpin").value.trim();
  if(!pin) return;
  $("pairerr").textContent = "";
  try{
    const r = await api("POST","/api/atv/pair/pin",{pin}, 60000);
    handlePairStep(r);
  }catch(e){ $("pairerr").textContent = e.message + " Try the code again, or Cancel and start over."; $("pairpin").select(); }
}
function pairCancel(){
  $("pairbox").style.display="none"; pairIp=null;
  api("POST","/api/atv/pair/cancel").catch(()=>{});
  refresh().catch(()=>{});
}
async function disconnect(ip){
  pillBusy[ip] = "Disconnect…"; render();
  if(lv && lv.ip === ip) stopLive(true);
  try{ await api("POST", devUrl(ip, "/disconnect")); log("Disconnected "+esc(ip)); }
  catch(e){ log(esc(e.message), true); }
  delete pillBusy[ip];
  await refresh().catch(()=>{});
}
// One after the other; stops at an Apple TV that needs its PIN (the box is open then).
async function connectAll(){
  for(const d of devices.slice()) if(!d.connected){ if(await connect(d.ip, false) === "pairing") break; }
}

// ---- commands ----
let sendQ = Promise.resolve();
function send(type, value, ip){
  if(type !== "text" && type !== "clear") return sendNow(type, value, ip);
  const run = sendQ.then(()=>sendNow(type, value, ip));
  sendQ = run.catch(()=>{});
  return run;
}
// Resolves to true when the device took the command.
async function sendNow(type, value, ip){
  const d = ip ? devices.find(x=>x.ip===ip) : focusedDev();
  if(!d){ log("Pick a device first", true); return false; }
  if(!d.connected){ log(esc(d.name) + " is not connected. Click Connect first.", true); return false; }
  try{
    const {results} = await api("POST","/api/command",{targets:[d.ip], type, value}, 60000);
    const l = lv;
    if(l && l.vid && l.ip === d.ip) setTimeout(()=>{ if(lv === l && l.ip === focused) checkProt(l.ip, blackShare($("livecv"))); }, 1200);
    const r = (results || [])[0] || {ok: false, error: "No answer"};
    if(r.ok) log(`${esc(value)} → ${esc(d.name)} ✓`);
    else log(esc(r.error || "Command failed"), true);
    return !!r.ok;
  }catch(e){ log(esc(e.message), true); return false; }
}
const key = k => send("key", k);

// ---- power: Power key (standby / wake) and the screen view in standby ----
const napping = d => !!d && d.type !== "appletv" && d.connected && d.power === "asleep";
// Not answering (unplugged, off the network, restarting): Fleet Remote keeps reconnecting.
const lost = d => !!d && d.type !== "appletv" && !d.connected && d.power === "offline";
let napIp = null, napGone = false, wakeTimer = null;   // napIp: device whose screen view waits for it to wake / come back
let backAt = {}, bootTries = 0;   // ip -> when it came back or woke; live retries while it settles
let bootWait = {};                // ip -> when live video first heard "still starting up" (just powered on)
async function powerKey(){
  const d = focusedDev();
  if(!d) return log("Pick a device first", true);
  if(!d.connected) return log(esc(d.name) + " is not connected. Click Connect first.", true);
  const b = $("pwrbtn");
  if(b.classList.contains("busy")) return;
  const value = d.power === "asleep" ? "on" : d.power === "on" ? "off" : "toggle";
  b.classList.add("busy"); pollHold = true;
  if(value !== "on" && lv && lv.ip === d.ip) stopLive(true);      // live video off before it sleeps
  if(value !== "toggle"){ d.power = value === "on" ? "on" : "asleep"; render(); }
  log(value === "on" ? "Waking " + esc(d.name) + "…" : "Power: " + esc(d.name) + "…");
  try{
    const {results} = await api("POST","/api/command",{targets:[d.ip], type:"power", value}, 60000);
    const r = results[0] || {};
    if(!r.ok) log(esc(r.error || "Power failed"), true);
    else log(r.power === "on" ? esc(d.name) + " is waking up ✓" : esc(d.name) + " is going to standby ✓");
  }catch(e){ log(esc(e.message), true); }
  b.classList.remove("busy"); pollHold = false;
  devSig = "";                                   // the guess above may be wrong: take the next answer as is
  await pollDevices();
  const f = focusedDev();
  if(scrMode === "live" && !lv && value !== "off" && f && f.ip === d.ip && f.connected && !napping(f) && !napIp) startLive(d.ip);
}
// Restart asks once, like Remove: the first click arms the button (red) for 4 s,
// a second click restarts the device. It then shows "Not responding" and comes
// back by itself (about a minute), as after a power cut.
let rbtArm = null;
async function restartDev(){
  const d = focusedDev();
  if(!d) return log("Pick a device first", true);
  if(!d.connected) return log(esc(d.name) + " is not connected. Click Connect first.", true);
  const b = $("rbtbtn");
  if(b.classList.contains("busy")) return;
  if(rbtArm !== d.ip){
    rbtArm = d.ip; b.classList.add("sure");
    log("Restart " + esc(d.name) + "? Click Restart again within 4 seconds. It is away for about a minute.");
    setTimeout(()=>{ if(rbtArm === d.ip){ rbtArm = null; b.classList.remove("sure"); } }, 4000);
    return;
  }
  rbtArm = null; b.classList.remove("sure");
  b.classList.add("busy"); pollHold = true;
  if(lv && lv.ip === d.ip) stopLive(true);
  log("Restarting " + esc(d.name) + "…");
  try{
    const {results} = await api("POST","/api/command",{targets:[d.ip], type:"reboot", value:"restart"}, 60000);
    const r = results[0] || {};
    if(!r.ok) log(esc(r.error || "Restart failed"), true);
    else log(esc(d.name) + " is restarting ✓ It comes back by itself in about a minute.");
  }catch(e){ log(esc(e.message), true); }
  b.classList.remove("busy"); pollHold = false;
  devSig = "";
  await pollDevices();
}
// In standby the screen view waits (live video must be off then) and comes back
// by itself when the device wakes up.
function powerScreen(){
  const d = focusedDev();
  const gone = lost(d);
  if((gone || napping(d)) && scrMode !== "off"){
    // lost, then back but in standby (a TV can switch it off over HDMI as it starts):
    // keep "came back", so the picture still waits for it to finish starting up
    napGone = gone || (napIp === d.ip && napGone); napIp = d.ip; clearTimeout(wakeTimer); wakeTimer = null;
    if(gone) delete bootWait[d.ip];
    if(lv) stopLive(true);          // no frozen last frame on the screen
    if(gone){
      showView("ph", ph("bad", d.name + " is offline",
        "It is not answering: unplugged, off the network or restarting. Fleet Remote keeps trying, and the picture comes back by itself when it answers again."));
      $("scrhint").textContent = "Fleet Remote keeps trying to reconnect. The screen view comes back by itself when it answers again.";
    } else {
      showView("ph", ph("warn", d.name + " is in standby",
        "The TV picture is off. The screen view comes back by itself when it wakes up.", ["Wake it up", ()=>powerKey()]));
      $("scrhint").textContent = "The screen view waits while it is in standby and comes back when it wakes up.";
    }
    return;
  }
  if(!napIp) return;
  if(!d || d.ip !== napIp || scrMode === "off"){ napIp = null; clearTimeout(wakeTimer); wakeTimer = null; return; }
  if(!d.connected || wakeTimer) return;
  const back = napGone;
  backAt[d.ip] = performance.now(); bootTries = 0;
  showView("ph", ph("wait", back ? d.name + " is back" : d.name + " is waking up", "Starting the picture…"));
  // give it a moment after waking (or coming back) before the picture starts again
  wakeTimer = setTimeout(()=>{
    wakeTimer = null;
    if(napIp !== d.ip || focused !== d.ip) return;
    napIp = null; napGone = false; paintScreen();
    if(scrMode === "live"){ if(!lv) startLive(d.ip); }
    else if(scrMode === "auto" || scrMode === "shot") shot(true);
  }, back ? 4000 : 2500);
}

// ---- screen: one switch, Off | Screenshot | Auto | Live video ----
const SCR_HINT = {
  off:  "Pick Screenshot, Auto or Live video to see the TV screen.",
  shot: "One still picture. Click Screenshot again to take a new one.",
  auto: "Takes a new screenshot every 3 seconds.",
  live: "Real-time video of the TV screen. Higher quality shows more detail on small text."
};
const PH_ICON = {
  off:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/></svg>',
  info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-5M12 8h.01"/></svg>',
  warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9z"/></svg>',
  bad:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M2 2l20 20M8.5 16.4a5 5 0 0 1 7 0M5 12.9a10 10 0 0 1 5.2-2.8M19 12.9a10 10 0 0 0-2.3-1.6M2 8.8a15 15 0 0 1 4.2-2.6M22 8.8A15 15 0 0 0 10.7 5M12 20h.01"/></svg>',
  wait: '<span class="spin"></span>'
};
// The message card in the screen area: kind (off | info | warn | bad | wait), a title,
// a line saying what happens next, and optionally one button.
function ph(kind, title, sub, act){ return {kind, title, sub, act}; }
let view = "ph";
function showView(v, msg){
  view = v;
  if(v === "ph") hideProt();
  $("shotph").style.display = v==="ph" ? "" : "none";
  $("shotimg").style.display = v==="img" ? "block" : "none";
  $("livecv").style.display = v==="cv" ? "block" : "none";
  if(msg === undefined) return;
  const box = $("shotph");
  box.className = "ph k-" + msg.kind;
  box.querySelector(".phic").innerHTML = PH_ICON[msg.kind] || "";
  box.querySelector(".pht").textContent = msg.title;
  box.querySelector(".phs").textContent = msg.sub || "";
  box.querySelector(".phs").style.display = msg.sub ? "" : "none";
  const old = box.querySelector(".btn"); if(old) old.remove();
  if(msg.act){
    const b = document.createElement("button");
    b.className = "btn small"; b.textContent = msg.act[0]; b.onclick = msg.act[1];
    box.append(b);
  }
}
function paintScreen(){
  const f = focusedDev(), atv = !!(f && f.type==="appletv");
  document.querySelectorAll("#scrseg button").forEach(b=>{
    b.classList.toggle("on", b.dataset.m===scrMode);
    b.classList.toggle("dis", atv && b.dataset.m!=="off");
  });
  $("livequal").style.display = scrMode==="live" ? "" : "none";
  const keep = (scrMode==="live" && lv && lv.w) || (f && (napIp === f.ip || bootWait[f.ip]));   // a live message is showing
  if(!keep) $("scrhint").textContent = atv ? "Apple TV can't share its screen." : SCR_HINT[scrMode];
  if(scrMode==="off" && view==="ph") showView("ph",
    atv ? ph("info", "No screen view for Apple TV",
             "Apple TV doesn't let other apps see its screen. The remote, power and keyboard work as usual.")
    : f ? ph("off", "Screen view is off", "Pick Screenshot, Auto or Live video above to see " + f.name + "'s screen.")
    : ph("off", "No device selected", "Click a device in the list on the left."));
}
function clearView(){
  showView("ph");
  $("shotimg").removeAttribute("src");
  if(lastShotUrl){ URL.revokeObjectURL(lastShotUrl); lastShotUrl = null; }
}
function setScreen(m){
  if(m==="shot" && scrMode==="shot"){ shot(); return; }      // click again = new picture
  if(m===scrMode) return;
  const f = focusedDev();
  if(m!=="off" && f && f.type==="appletv") return log("Apple TV can't share its screen.", true);
  if(lv) stopLive(true);
  if(autoTimer){ clearInterval(autoTimer); autoTimer = null; }
  scrMode = m;
  if(m==="off"){ clearView(); paintScreen(); return; }
  paintScreen();
  if(m==="live"){ startLive(); return; }
  if(view!=="img") showView("ph", ph("wait", "Taking a screenshot…"));
  shot();
  if(m==="auto") autoTimer = setInterval(shot, 3000);
}
function screenFail(msg){
  if(msg) log(msg, true);
  if(lv) stopLive(true);
  if(autoTimer){ clearInterval(autoTimer); autoTimer = null; }
  scrMode = "off"; clearView(); paintScreen();
}
function onFocusChange(){
  paintScreen(); pollSleep();
  if(scrMode==="off") return;
  const f = focusedDev();
  if(lost(f)){ powerScreen(); return; }              // waits for it to answer again
  if(!f || f.type==="appletv" || !f.connected){ screenFail(); return; }
  if(scrMode==="live"){ if(!lv || lv.ip!==f.ip){ stopLive(true); startLive(f.ip); } }
  else { showView("ph", ph("wait", "Taking a screenshot…")); shot(true); }
}

// ---- protected video: Fire TV keeps playing video (DRM, the hardware video
// layer) out of captures, so the picture is black. Say so on the screen. ----
let protSeq = 0;
// Share of black pixels in the whole picture and in the middle band, where the
// video is: player controls (title, progress bar, thumbnails) sit above and
// below it and can cover almost half the picture.
function blackShare(src){
  try{
    const c = blackShare.c || (blackShare.c = document.createElement("canvas"));
    c.width = 64; c.height = 36;
    const x = c.getContext("2d", {willReadFrequently: true});
    x.drawImage(src, 0, 0, 64, 36);
    const d = x.getImageData(0, 0, 64, 36).data;
    let all = 0, mid = 0, nmid = 0;
    for(let y = 0; y < 36; y++) for(let k = 0; k < 64; k++){
      const i = (y*64 + k)*4, b = d[i] < 16 && d[i+1] < 16 && d[i+2] < 16;
      if(b) all++;
      if(y >= 9 && y < 22 && k >= 6 && k < 58){ nmid++; if(b) mid++; }
    }
    return {all: all/2304, mid: mid/nmid};
  }catch(e){ return {all: 0, mid: 0}; }
}
const picBlack = b => b.mid >= 0.9 || b.all >= 0.85;    // a hidden video shows as a black block
const picClear = b => b.mid < 0.5;                       // clearly a normal screen again
// A video smaller than this share of the screen is an inline preview (YouTube's
// home page autoplays the highlighted tile): no label or box over it.
const PREVIEW_MAX = 0.25;
const PROT_SUB = "It is playing on the TV. Fire TV keeps video out of screen captures, so the picture here stops.";
const PROT_PAUSED = "Paused on the TV. Fire TV keeps video out of screen captures, so the picture here stops.";
// While the player is open the Cube's capture of it is wrong: black, old copies
// of the controls that flip (and stay after the TV has hidden them), or a
// frame that never updates again (paused, 11-21), so that area is covered.
function setMask(on){
  $("shot").classList.toggle("masked", !!on);
  if(lv) lv.masked = !!on;
}
function hideProt(){ protSeq++; $("prot").classList.remove("show"); setMask(false); protHw = false; setVbox(null); }
function showProtLabel(p){
  $("prottitle").textContent = "Protected video" + (p.app ? " (" + p.app + ")" : "");
  $("protsub").textContent = p.active ? PROT_SUB : PROT_PAUSED;
  $("prot").classList.add("show");
}
// The video plane: the TV draws a playing video on its own hardware layer,
// which the Cube's capture can't see (black, or an old frozen frame flipping
// with the background, e.g. YouTube Shorts). The server says where it is;
// that area gets a black box with the label on it.
let protHw = false, protRect = null;
function setVbox(r){ protRect = r; placeVbox(); }
function placeVbox(){
  const vb = $("vbox"), pr = $("prot");
  const el = view === "cv" ? $("livecv") : $("shotimg");
  const w = view === "cv" ? el.width : el.naturalWidth, h = view === "cv" ? el.height : el.naturalHeight;
  if(!protRect || !w || !h || view === "ph"){ vb.classList.remove("show"); pr.style.left = pr.style.top = ""; return; }
  const box = $("shot"), W = box.clientWidth, H = box.clientHeight;
  const k = Math.min(W / w, H / h), pw = w * k, ph = h * k, ox = (W - pw) / 2, oy = (H - ph) / 2;
  const r = protRect, x = ox + r[0] * pw, y = oy + r[1] * ph, rw = (r[2] - r[0]) * pw, rh = (r[3] - r[1]) * ph;
  Object.assign(vb.style, {left: x + "px", top: y + "px", width: rw + "px", height: rh + "px"});
  vb.classList.add("show");
  const half = Math.min(pr.offsetWidth || 0, W) / 2, halfH = Math.min(pr.offsetHeight || 0, H) / 2;
  pr.style.left = Math.max(half, Math.min(W - half, x + rw / 2)) + "px";
  pr.style.top = Math.max(halfH, Math.min(H - halfH, y + rh / 2)) + "px";
}
new ResizeObserver(()=>{ if(protRect) placeVbox(); }).observe($("shot"));
// bs = how black the picture was when asked. A player on screen (playing or
// paused) is covered where the server says it is; without a known plane, a
// paused video only gets the label while the picture is black.
// poll: a regular check on a picture that isn't black, only the video plane counts.
async function checkProt(ip, bs, poll){
  if(checkProt.busy === ip) return;
  checkProt.busy = ip;
  const seq = ++protSeq;
  try{
    const p = await api("GET", devUrl(ip, "/playing"));
    if(seq !== protSeq || ip !== focused || scrMode === "off" || view === "ph") return;
    const live = scrMode === "live" && !!lv && lv.ip === ip;
    if(live) lv.vid = p.playing;
    const v = p.playing && p.video, wasHw = protHw;
    const small = !!v && (v[2] - v[0]) * (v[3] - v[1]) < PREVIEW_MAX;
    const r = small ? null : v;
    protHw = !!r;
    if(r){
      const full = (r[2] - r[0]) * (r[3] - r[1]) >= 0.8;     // full screen: cover all of it, as before
      showProtLabel(p);
      setMask(live && full);
      setVbox(live && full ? null : r);
      return;
    }
    setVbox(null);
    if(small){ $("prot").classList.remove("show"); setMask(false); return; }
    if(poll){ if(wasHw){ $("prot").classList.remove("show"); setMask(false); } return; }
    if(!p.playing || (!p.active && bs && bs.all < 0.85)){ $("prot").classList.remove("show"); setMask(false); return; }
    showProtLabel(p);
    setMask(live && p.active);
  }catch(e){}
  finally{ if(checkProt.busy === ip) checkProt.busy = null; }
}

// ---- screenshots ----
// The device whose screen is shown: the selected one, or the only connected Fire TV
// when nothing usable is selected. Null (after saying why) when there is no picture to get.
function pickScreenTarget(forceIp){
  let target = forceIp || focused;
  const f = devices.find(d=>d.ip===target);
  if(f && f.type==="appletv"){ screenFail("Apple TV can't share its screen."); return null; }
  if(lost(f) && f.ip === focused){ powerScreen(); return null; }
  const conn = devices.filter(d=>d.connected && d.type!=="appletv");
  if((!f || !f.connected) && conn.length===1){ target = conn[0].ip; setFocusQuiet(target); }
  if(!target){ screenFail("Connect a device first"); return null; }
  if(napping(devices.find(d=>d.ip===target))){ powerScreen(); return null; }
  return target;
}
let shotBusy = false, shotPending = false, lastShotUrl = null;
async function shot(force){
  const target = pickScreenTarget();
  if(!target) return;
  if(shotBusy){ if(force) shotPending = true; return; }     // never overlap captures
  shotBusy = true;
  const hadImg = view==="img";
  $("shotbadge").classList.add("show");
  if(hadImg) $("shot").classList.add("loading");
  let url = null, shown = false;
  try{
    const r = await fetch(devUrl(target, "/screenshot"), {signal: timeoutSignal(20000)});
    if(!r.ok){ const e=await r.json().catch(()=>({})); throw new Error(e.detail||"Screenshot failed"); }
    const blob = await r.blob();
    url = URL.createObjectURL(blob);
    const pre = new Image();
    await new Promise((res, rej)=>{           // decode off-screen first (no flash)
      pre.onload = res; pre.onerror = ()=>rej(new Error("The screenshot could not be decoded")); pre.src = url;
    });
    if((scrMode!=="shot" && scrMode!=="auto") || target!==focused) return;
    $("shotimg").src = url; shown = true;
    showView("img");
    const bs = blackShare(pre);
    if(picBlack(bs)) checkProt(target, bs);
    else {
      if(picClear(bs) && !protHw) hideProt();
      checkProt(target, bs, true);                // a video on a coloured background isn't black
    }
    if(lastShotUrl) URL.revokeObjectURL(lastShotUrl);
    lastShotUrl = url;
    const d = devices.find(x=>x.ip===target);
    log("Screenshot: "+esc(d ? d.name : target));
  }catch(e){
    const msg = e.name === "TimeoutError" ? "Screenshot took too long" : e.message;
    if(scrMode==="shot" && !hadImg) screenFail(esc(msg));
    else log(esc(msg), true);
  }finally{
    if(url && !shown) URL.revokeObjectURL(url);
    shotBusy = false;
    $("shotbadge").classList.remove("show");
    $("shot").classList.remove("loading");
    if(shotPending){ shotPending = false; if(scrMode==="shot" || scrMode==="auto") shot(); }
  }
}

// ---- live video (WebCodecs H.264 from the scrcpy server) ----
let lv = null;     // {ip, ac, dec, ...} while running
function liveParams(){
  const q = $("livequal").value;
  return q==="low" ? "size=960&bitrate=2000000&fps=60"
       : q==="high" ? "size=1920&bitrate=8000000&fps=60"
       : "size=1280&bitrate=4000000&fps=60";
}
let qualTimer = null;
function liveQualityChanged(){
  $("livequal").blur();
  if(scrMode !== "live") return;
  const ip = lv ? lv.ip : focused;
  stopLive(true);
  $("livebadgetxt").textContent = "Switching quality";
  $("livebadge").classList.add("show");
  clearTimeout(qualTimer);
  qualTimer = setTimeout(()=>{ qualTimer = null; if(scrMode==="live" && !lv) startLive(ip); }, 800);
}
// what the stream really is (size, bitrate, frame rate), shown under the picture
function liveInfo(l){
  if(lv !== l || scrMode !== "live") return;
  const now = performance.now();
  if(l.t && l.w){
    const dt = (now - l.t) / 1000;
    const mbps = (l.bytes - l.b0) * 8 / dt / 1e6, fps = Math.round((l.shown - l.s0) / dt);
    const cv = $("livecv"), pct = cv.clientWidth ? Math.round(cv.clientWidth / l.w * 100) : 100;
    $("scrhint").textContent = "Live: " + l.w + " x " + l.h + ", "
      + (fps ? mbps.toFixed(1) + " Mbps, " + fps + " fps" : protHw ? "protected video on the TV, picture frozen" : "screen not changing")
      + (pct < 98 ? " (shown at " + pct + "% to fit this box)" : "");
  }
  l.t = now; l.b0 = l.bytes; l.s0 = l.shown;
  if(l.shown){
    // black for 2 s: ask if a video is playing (again every 5 s). Hide only
    // after 2 s of a clearly normal picture, so player controls fading in and
    // out don't make the label blink.
    const bs = blackShare($("livecv")), on = $("prot").classList.contains("show");
    l.tick = (l.tick || 0) + 1;
    if(bs.mid >= 0.6 || bs.all >= 0.5) l.watchUntil = now + WATCH_MS;   // mostly black: check frames (paintFrame)
    if(protHw){
      // the video plane is covered: the server says when it stops, not the picture
      if(l.tick % 2 === 0) checkProt(l.ip, bs, true);
    } else if(picBlack(bs)){
      l.dark = (l.dark || 0) + 1; l.lit = 0;
      if(l.dark === 1 || l.dark % (l.masked || on || l.vid ? 2 : 5) === 0) checkProt(l.ip, bs);
    } else {
      l.dark = 0;
      l.lit = picClear(bs) ? (l.lit || 0) + 1 : 0;
      if(on && (l.lit >= 2 || (l.lit && l.masked))) hideProt();
      else if(on && l.tick % (l.masked ? 2 : 5) === 0) checkProt(l.ip, bs);
      // a video on a coloured background (YouTube Shorts) never looks black: ask anyway
      // (a normal picture with nothing shown must still get here, or a frozen frame stays)
      else if(!on && l.tick % (l.vid ? 2 : 4) === 1) checkProt(l.ip, bs, true);
    }
    if(protRect) placeVbox();
  }
}
// While a video plays, the Cube's stream drops single all-black frames in
// between the real ones (its video layer), which made the picture flicker.
// Keep the last real picture; show black only once it lasts 0.6 s.
// The pixel check costs ~8 ms a frame, so it only runs while the picture is
// mostly black (WATCH_MS after liveInfo last saw that) and only on frames that
// really changed: a tiny chunk looks like the frame before it and keeps its result.
const BLANK_HOLD = 600, WATCH_MS = 10000, SAME_BYTES = 300, RECHECK = 10;
function isBlank(fr){
  try{
    const c = isBlank.c || (isBlank.c = document.createElement("canvas"));
    c.width = 32; c.height = 18;
    const x = isBlank.x || (isBlank.x = c.getContext("2d", {willReadFrequently: true}));
    x.drawImage(fr, 0, 0, 32, 18);
    const d = x.getImageData(0, 0, 32, 18).data;
    let lit = 0;
    for(let i = 0; i < d.length; i += 4) if(d[i] >= 16 || d[i+1] >= 16 || d[i+2] >= 16) lit++;
    return lit <= 11;                   // under 2% of the picture
  }catch(e){ return false; }
}
function dropPending(l){
  clearTimeout(l.blankTimer); l.blankTimer = null;
  if(l.pend){ try{ l.pend.close(); }catch(e){} l.pend = null; }
}
function paintFrame(l, cv, ctx, fr){
  if(cv.width !== fr.displayWidth || cv.height !== fr.displayHeight){
    dropPending(l); l.litAt = 0; l.chk = RECHECK;
    cv.width = fr.displayWidth; cv.height = fr.displayHeight;
  }
  l.w = fr.displayWidth; l.h = fr.displayHeight;
  l.shown++;
  const now = performance.now();
  const sz = l.sz ? l.sz.get(fr.timestamp) : undefined;   // chunk bytes, -1 = key frame
  if(l.sz) l.sz.delete(fr.timestamp);
  if(now < (l.watchUntil || 0) && !l.masked){
    l.chk = (l.chk || 0) + 1;
    if(sz === undefined || sz < 0 || sz >= SAME_BYTES || l.chk >= RECHECK){
      l.blank = isBlank(fr); l.chk = 0;
    }
  } else { l.blank = false; l.chk = RECHECK; }
  if(l.blank){
    if(l.litAt && now - l.litAt < BLANK_HOLD){   // hold it back; newest black frame waits
      if(l.pend) l.pend.close();
      l.pend = fr;
      if(!l.blankTimer) l.blankTimer = setTimeout(()=>{
        l.blankTimer = null;
        const p = l.pend; l.pend = null;
        if(p){ if(lv === l) ctx.drawImage(p, 0, 0); p.close(); }   // still black: show it
      }, BLANK_HOLD - (now - l.litAt));
      return;
    }
  } else { dropPending(l); l.litAt = now; }
  ctx.drawImage(fr, 0, 0); fr.close();
  if(l.shown === 1) $("livebadge").classList.remove("show");
}
function stopLive(quiet){
  if(!lv) return;
  const l = lv; lv = null;
  try{ l.ac.abort(); }catch(e){}
  try{ if(l.dec && l.dec.state !== "closed") l.dec.close(); }catch(e){}
  dropPending(l); if(l.sz) l.sz.clear();
  clearInterval(l.infoTimer);
  $("livebadge").classList.remove("show");
  hideProt();
  if(!quiet) log("Live video stopped");
  else if(l.msg && $("log").textContent === l.msg) log("");   // a quiet stop must not leave "Live video: X" up
}
// Picture size, chroma format and bit depths from the SPS (exp-Golomb bit reader;
// emulation prevention bytes removed first). Chromium works the size out itself; the
// Mac's decoder wants it with configure(). High profile avcC records carry the rest.
function spsInfo(sps){
  try{
    const b = [];
    for(let i = 1, z = 0; i < sps.length; i++){
      const v = sps[i];
      if(z >= 2 && v === 3){ z = 0; continue; }
      b.push(v); z = v === 0 ? z + 1 : 0;
    }
    let pos = 0;
    const u = n => { let r = 0; for(let i = 0; i < n; i++, pos++) r = r * 2 + ((b[pos >> 3] >> (7 - (pos & 7))) & 1); return r; };
    const ue = () => { let z = 0; while(!u(1) && z < 32) z++; return 2 ** z - 1 + u(z); };
    const se = () => { const k = ue(); return k & 1 ? (k + 1) / 2 : -k / 2; };
    const profile = u(8); u(16); ue();                       // constraint flags + level, sps id
    let chroma = 1, bdl = 0, bdc = 0;
    if([100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135].includes(profile)){
      chroma = ue(); if(chroma === 3) u(1);
      bdl = ue(); bdc = ue(); u(1);                          // bit depths, transform bypass
      if(u(1)) for(let i = 0; i < (chroma !== 3 ? 8 : 12); i++) if(u(1)){   // scaling lists
        for(let j = 0, last = 8, next = 8; j < (i < 6 ? 16 : 64); j++){
          if(next) next = (last + se() + 256) % 256;
          last = next || last;
        }
      }
    }
    ue(); const poc = ue();
    if(poc === 0) ue();
    else if(poc === 1){ u(1); se(); se(); const n = ue(); for(let i = 0; i < n; i++) se(); }
    ue(); u(1);                                              // ref frames, gaps allowed
    const w = (ue() + 1) * 16, hm = ue() + 1, fmo = u(1);
    if(!fmo) u(1);
    u(1);                                                    // direct 8x8
    let cl = 0, cr = 0, ct = 0, cb = 0;
    if(u(1)){ cl = ue(); cr = ue(); ct = ue(); cb = ue(); }
    const sx = chroma === 1 || chroma === 2 ? 2 : 1, sy = (chroma === 1 ? 2 : 1) * (2 - fmo);
    const W = w - sx * (cl + cr), H = (2 - fmo) * hm * 16 - sy * (ct + cb);
    if(pos > b.length * 8 || W <= 0 || H <= 0 || W > 16384 || H > 16384) return null;
    return {w: W, h: H, profile, chroma: chroma & 3, bdl: bdl & 7, bdc: bdc & 7};
  }catch(e){ return null; }
}
// Can this window decode H.264? (WebCodecs may exist without the codec.)
async function h264ok(){
  if(h264ok.v === undefined){
    try{ h264ok.v = (await VideoDecoder.isConfigSupported({codec: "avc1.42E01E"})).supported !== false; }
    catch(e){ h264ok.v = true; }                 // can't tell: try anyway
  }
  return h264ok.v;
}
async function startLive(forceIp){
  if(typeof VideoDecoder === "undefined" || !(await h264ok())){
    return screenFail("This window can't play live video (no H.264 decoder). Close the app and start it with: __PY__ __SCRIPT__ --browser");
  }
  if(lv || scrMode !== "live") return;           // started twice, or switched off while we checked
  const target = pickScreenTarget(forceIp);
  if(!target) return;
  const ac = new AbortController();
  const form = liveForm;
  const l = lv = {ip:target, ac, dec:null, shown:0, infoTimer:null, bytes:0, w:0, h:0, sz:new Map(), codec:"", form};
  // desynchronized: the frame goes to the screen without waiting for the next
  // compositor turn (one display frame less delay where the browser supports it)
  const cv = $("livecv"), ctx = cv.getContext("2d", {desynchronized: true});
  const starting = ()=>{
    $("scrhint").textContent = SCR_HINT.live;
    ctx.clearRect(0, 0, cv.width, cv.height);
    showView("cv");
    $("livebadgetxt").textContent = "Starting";
    $("livebadge").classList.add("show");
  };
  const waiting = !!bootWait[target];    // still starting up: keep that message up until the picture comes
  if(!waiting) starting();
  try{
    const r = await fetch(devUrl(target, "/stream?" + liveParams()), {signal: ac.signal});
    if(!r.ok){
      const e = await r.json().catch(()=>({}));
      const er = new Error(e.detail || ("failed ("+r.status+")")); er.status = r.status; throw er;
    }
    if(lv !== l) return;
    delete bootWait[target];
    if(waiting) starting();
    const dn = devices.find(x=>x.ip===target);
    l.msg = "Live video: " + (dn ? dn.name : target);
    log(esc(l.msg));
    let buf = new Uint8Array(0), configured = false, needKey = true, spsKey = "", curSps = null, curPps = null;
    const dec = l.dec = new VideoDecoder({
      output: fr => {
        if(!l.shown){
          rememberForm(form);
          if(liveFlipped){ liveFlipped = false; api("POST", "/api/clientlog", {msg: "live video decodes with the " + form + " form | " + navigator.userAgent}).catch(()=>{}); }
        }
        paintFrame(l, cv, ctx, fr);
      },
      error: e => {
        if(lv !== l) return;
        const what = (e && e.message) + " (" + form + " form, " + (l.codec || "no config yet") + ", " + l.shown + " frames decoded)";
        api("POST", "/api/clientlog", {msg: "live video decoder error: " + what + " | " + navigator.userAgent}).catch(()=>{});
        // never showed a picture: this decoder may want the other H.264 form
        if(!l.shown){ rememberForm(form === "avcc" ? "annexb" : "avcc"); liveFlipped = true; }
        // a glitch in the stream: start it again (new key frame) instead of giving up
        const t = Date.now(); liveFails = liveFails.filter(x => t - x < 60000);
        if(liveFails.length >= 3) return screenFail("Live video decoder error: "+esc(what)+". Details are in .firecube-fleet/error.log");
        liveFails.push(t);
        stopLive(true);
        $("livebadgetxt").textContent = "Reconnecting"; $("livebadge").classList.add("show");
        setTimeout(()=>{ if(scrMode === "live" && !lv && focused === l.ip) startLive(l.ip); }, 300);
      }
    });
    const cat = parts => { let n = 0; parts.forEach(p=>n+=p.length); const o = new Uint8Array(n); let k = 0; parts.forEach(p=>{ o.set(p,k); k+=p.length; }); return o; };
    const h = x => x.toString(16).padStart(2,"0");
    // The Cube sends H.264 with start codes (Annex B). WebCodecs decoders everywhere
    // (Chromium and WebKit) take the MP4 form: an avcC description made from the SPS and
    // PPS, and each NAL unit prefixed with its 4-byte length instead of a start code.
    function nals(p){
      const out = []; let s = -1;
      for(let i = 0; i + 2 < p.length; i++){
        if(p[i]===0 && p[i+1]===0 && p[i+2]===1){
          if(s >= 0){ let e = i; while(e > s && p[e-1]===0) e--; out.push(p.subarray(s, e)); }
          s = i + 3; i += 2;
        }
      }
      if(s >= 0 && s < p.length){ let e = p.length; while(e > s + 1 && p[e-1]===0) e--; out.push(p.subarray(s, e)); }
      return out;
    }
    function avcc(list){
      let n = 0; list.forEach(u=>n += 4 + u.length);
      const o = new Uint8Array(n), dv = new DataView(o.buffer); let k = 0;
      list.forEach(u=>{ dv.setUint32(k, u.length); o.set(u, k + 4); k += 4 + u.length; });
      return o;
    }
    function annexb(list){
      let n = 0; list.forEach(u=>n += 4 + u.length);
      const o = new Uint8Array(n); let k = 0;
      list.forEach(u=>{ o.set([0, 0, 0, 1], k); o.set(u, k + 4); k += 4 + u.length; });
      return o;
    }
    // AVCDecoderConfigurationRecord (ISO 14496-15). High profiles carry the chroma format
    // and bit depths as well; Chromium doesn't miss them, Apple's decoder may.
    function avcC(sps, pps, si){
      const ext = [100, 110, 122, 144].includes(sps[1]);
      const o = new Uint8Array(11 + sps.length + pps.length + (ext ? 4 : 0));
      o.set([1, sps[1], sps[2], sps[3], 0xFF, 0xE1, sps.length >> 8, sps.length & 255], 0);
      o.set(sps, 8);
      o.set([1, pps.length >> 8, pps.length & 255], 8 + sps.length);
      o.set(pps, 11 + sps.length);
      if(ext) o.set([0xFC | (si ? si.chroma : 1), 0xF8 | (si ? si.bdl : 0), 0xF8 | (si ? si.bdc : 0), 0], 11 + sps.length + pps.length);
      return o;
    }
    // SPS + PPS become the decoder's description (with the picture size, for the Mac)
    function setConfig(u){
      const sps = u.find(x=>(x[0] & 31) === 7), pps = u.find(x=>(x[0] & 31) === 8);
      if(!sps || !pps) return;
      const k = Array.from(sps).join(",") + "|" + Array.from(pps).join(",");
      if(configured && k === spsKey) return;
      spsKey = k; curSps = sps.slice(); curPps = pps.slice();
      const si = spsInfo(sps);
      const cfg = {codec: "avc1."+h(sps[1])+h(sps[2])+h(sps[3]), optimizeForLatency: true};
      if(form === "avcc") cfg.description = avcC(sps, pps, si);
      if(si){ cfg.codedWidth = si.w; cfg.codedHeight = si.h; }
      l.codec = cfg.codec + (si ? " " + si.w + "x" + si.h : "");
      dec.configure(cfg);
      configured = true; needKey = true;
    }
    // one packet from the Cube: a codec config (SPS + PPS) or one whole frame
    function packet(config, isKey, pts, p){
      const u = nals(p);
      // the Cube repeats the SPS + PPS inside every key frame: a changed one reconfigures
      if(config || (isKey && u.some(x=>(x[0] & 31) === 7))) setConfig(u);
      if(config || !configured) return;
      // Every frame builds on the one before it, so a skipped frame breaks the
      // picture (smears, then "Decoding error"). Only if the decoder is really
      // stuck (over half a second behind), skip ahead to the next key frame.
      if(!isKey && dec.decodeQueueSize > 30) needKey = true;
      if(needKey && !isKey) return;
      // A frame holds only its slices (and SEI): parameter sets, delimiters and filler
      // NAL units are left out (the description, or the key frame's own prefix in the
      // Annex B form, carries the parameter sets). Chromium ignores extra ones; the
      // Mac's VideoToolbox is strict about what a frame may hold.
      const vcl = u.filter(x => { const t = x[0] & 31; return t !== 7 && t !== 8 && t !== 9 && t !== 12; });
      if(!vcl.length) return;
      if(isKey) needKey = false;
      const data = form === "avcc" ? avcc(vcl) : annexb(isKey ? [curSps, curPps, ...vcl] : vcl);
      l.sz.set(pts, isKey ? -1 : data.length);              // paintFrame reads it back
      if(l.sz.size > 90) l.sz.delete(l.sz.keys().next().value);
      dec.decode(new EncodedVideoChunk({type: isKey ? "key" : "delta", timestamp: pts, data}));
    }
    const reader = r.body.getReader();
    l.infoTimer = setInterval(()=>liveInfo(l), 1000);
    for(;;){
      const {value, done} = await reader.read();
      if(done || lv !== l) break;
      l.bytes += value.length;
      buf = buf.length ? cat([buf, value]) : value;
      // 12-byte header before every packet: PTS (µs) with flags in the top bits
      // (bit 63 = new encoder session, bit 62 = codec config, bit 61 = key frame), then the packet size
      let o = 0;
      while(buf.length - o >= 12){
        const dv = new DataView(buf.buffer, buf.byteOffset + o, 12);
        const hi = dv.getUint32(0), lo = dv.getUint32(4), n = dv.getUint32(8);
        if(buf.length - o - 12 < n) break;                   // the rest is still on its way
        if(hi & 0x80000000){ o += 12 + n; needKey = true; continue; }   // session marker, not a frame
        packet(!!(hi & 0x40000000), !!(hi & 0x20000000), (hi & 0x1FFFFFFF) * 4294967296 + lo,
               buf.subarray(o + 12, o + 12 + n));
        o += 12 + n;
      }
      buf = o ? buf.slice(o) : buf;
    }
    if(lv === l) liveGone(l, "Live video ended");
  }catch(e){
    if(lv === l){
      if(e.name === "AbortError") stopLive(true);
      else liveGone(l, esc(e.message), e.status);
    }
  }
}
// Live video stopped by itself. In standby (the app stops it then) wait for
// the device to wake instead of turning the screen view off.
async function liveGone(l, msg, status){
  stopLive(true);
  try{ const list = await fetchDevices(); if(list) applyDevices(list); }catch(e){}
  if(scrMode !== "live" || lv) return;
  const f = focusedDev();
  if(l.ip === focused && (napping(f) || lost(f))){ powerScreen(); return; }
  const again = ()=>setTimeout(()=>{ if(scrMode === "live" && !lv && focused === l.ip && !napIp) startLive(l.ip); }, 5000);
  // Just powered up: it answers on the network about a minute before Android has
  // finished starting. Wait for it (up to 5 minutes), then start the picture.
  if(status === 503 && l.ip === focused && f && f.connected){
    const now = performance.now();
    if(!bootWait[l.ip]) bootWait[l.ip] = now;
    if(now - bootWait[l.ip] < 300000){
      backAt[l.ip] = now; bootTries = 0;          // the retries below count from when it is ready
      showView("ph", ph("wait", f.name + " is still starting up", "It was just switched on. The picture starts by itself when it is ready, usually within a minute."));
      $("scrhint").textContent = "Fleet Remote keeps checking and starts live video as soon as it can.";
      again();
      return;
    }
    delete bootWait[l.ip];
    return screenFail(esc(f.name) + " has not finished starting up after 5 minutes. Restart it, then turn Live video on again.");
  }
  // Just back after a power cut, restart or wake: its video service may still be
  // settling, so try again for a while before giving up.
  if(l.ip === focused && f && f.connected && performance.now() - (backAt[l.ip] || -1e9) < 120000 && bootTries < 6){
    bootTries++;
    showView("ph", ph("wait", "No picture from " + f.name + " yet", "Trying again…"));
    again();
    return;
  }
  screenFail(msg);
}

// ---- keyboard: Type + Send, or Live typing ----
async function sendText(){
  const t = $("typetext").value;
  if(!t) return;
  if(await send("text", t)) $("typetext").value = "";
}
async function clearField(){
  if(await send("clear", "x")) log("Cleared text field");
}
let live = false;        // live typing on?
let liveBuf = "";
let liveTimer = null;
function paintKb(){
  document.querySelectorAll("#kbseg button").forEach(b=>b.classList.toggle("on", b.dataset.k===(live ? "live" : "send")));
  $("typetext").disabled = live;
  $("typetext").placeholder = live ? "Live typing is on: just type, keys go to the TV" : "Type text, press Enter to send";
  $("sendbtn").style.display = live ? "none" : "";
  $("kbhint").textContent = live
    ? "Every key you press goes straight to the TV (letters, Backspace, Enter, arrows). Esc stops live typing."
    : "Select a text box on the TV first (for example Search), then type here and press Enter.";
}
function setKb(m){
  document.activeElement?.blur?.();          // keep Enter/Space from re-hitting the button
  const on = m==="live";
  if(on === live) return;
  live = on;
  paintKb();
  if(!live) flushLive();
  log(live ? "Live typing on: keys go to the TV. Esc to stop." : "Live typing off");
}
let liveIp = null;        // the device the buffered keys were typed for
async function flushLive(){
  if(liveTimer){ clearTimeout(liveTimer); liveTimer = null; }
  if(liveBuf){
    const b = liveBuf, ip = liveIp; liveBuf = "";
    if(await send("text", b, ip)) log('Typed: "' + esc(b) + '"');       // visible confirmation of what was sent
  }
}
function liveChar(ch){
  // batch rapid keystrokes into one `input text` call (smoother than per-key)
  if(liveBuf && liveIp !== focused) flushLive();   // switched device mid-word: finish the old one first
  if(!liveBuf) liveIp = focused;
  liveBuf += ch;
  if(liveTimer) clearTimeout(liveTimer);
  liveTimer = setTimeout(flushLive, 150);
}

$("pairpin").addEventListener("keydown", e=>{ if(e.key==="Enter") pairPin(); });
$("grp-new").addEventListener("keydown", e=>{
  if(e.key==="Enter"){ e.preventDefault(); addGroup(); }
  else if(e.key==="Escape"){ toggleGroups(false); }
});
$("typetext").addEventListener("keydown", e=>{ if(e.key==="Enter"){ e.preventDefault(); sendText(); } });
["add-ip","add-name","add-group"].forEach(id=>$(id).addEventListener("keydown", e=>{
  if(e.key==="Enter"){ e.preventDefault(); addDevice(); }
  else if(e.key==="Escape"){ toggleAdd(false); }
}));

// keyboard shortcuts + live typing (capture phase: sees keys before anything else)
const IS_MAC = /Mac/i.test(navigator.platform || navigator.userAgent);
document.addEventListener("keydown", e=>{
  const ae = document.activeElement;
  // the group dropdowns in Devices keep their arrow keys (the quality one doesn't: arrows drive the remote)
  const inField = !!ae && (["INPUT","TEXTAREA"].includes(ae.tagName) || (ae.tagName==="SELECT" && !!ae.closest("#devcard"))) && !ae.disabled;
  if(live && !inField){
    // Live typing owns the keyboard, unless you're typing in one of the app's own boxes.
    if(e.key==="Escape"){ e.preventDefault(); setKb("send"); return; }
    if(e.isComposing || e.keyCode === 229) return;    // an IME / dead key is still composing
    const altgr = !!(e.getModifierState && e.getModifierState("AltGraph"));
    if(e.key.length===1 && !e.metaKey && (altgr || (!e.ctrlKey && (!e.altKey || IS_MAC)))){
      e.preventDefault(); liveChar(e.key); return; }
    const special={Backspace:"delete",Delete:"delete",Enter:"enter",
      ArrowUp:"up",ArrowDown:"down",ArrowLeft:"left",ArrowRight:"right"};
    if(special[e.key]){ e.preventDefault(); if(e.repeat && e.key==="Enter") return; flushLive().then(()=>sendQ).then(()=>key(special[e.key])); }
    return;
  }
  if(inField) return;
  if(e.ctrlKey || e.metaKey || e.altKey) return;       // leave Ctrl/Cmd/Alt shortcuts to the window
  const m={ArrowUp:"up",ArrowDown:"down",ArrowLeft:"left",ArrowRight:"right",
    Enter:"select"," ":"play_pause",Backspace:"back",Escape:"menu"};
  if(m[e.key]){ e.preventDefault(); if(e.repeat && !e.key.startsWith("Arrow")) return; key(m[e.key]); }
}, true);
// The device list waits with a rebuild while a mouse button is down on it (see render).
document.addEventListener("pointerdown", ()=>{ ptrDown = true; ptrAt = performance.now(); }, true);
for(const ev of ["pointerup", "pointercancel"]) document.addEventListener(ev, ()=>{
  ptrDown = false;
  if(renderLater) setTimeout(()=>{ if(renderLater && !ptrDown && !$("devlist").querySelector("input")){ renderLater = false; render(); } }, 0);
}, true);
// Window hidden (minimised, another tab) for half a minute: stop live video, the Cube
// stops encoding for nobody. It starts again when the window comes back.
let hiddenTimer = null;
document.addEventListener("visibilitychange", ()=>{
  clearTimeout(hiddenTimer); hiddenTimer = null;
  if(document.hidden){
    hiddenTimer = setTimeout(()=>{ hiddenTimer = null; if(document.hidden && lv){ stopLive(true); $("livebadgetxt").textContent = "Paused: window hidden"; $("livebadge").classList.add("show"); } }, 30000);
  } else {
    pollDevices();                       // back in view: a fresh list now, not in up to 5 s
    if(scrMode === "live" && !lv && !napIp && !qualTimer) startLive();
  }
});
// a clicked button lets go of focus, so Space/Enter never re-press it
document.addEventListener("click", e=>{ const b = e.target.closest && e.target.closest("button"); if(b) b.blur(); });

// ---- hover ticks: a short click whenever the pointer lands on a button ----
let ticksOn = (()=>{ try{ return localStorage.getItem("fr_ticks") !== "off"; }catch(e){ return true; } })();
let actx = null, tickBuf = null, tickAt = 0, tickEl = null;
const SPK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>';
const SPK_ON = SPK + '<path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/></svg>';
const SPK_OFF = SPK + '<line x1="22" y1="9" x2="16" y2="15"/><line x1="16" y1="9" x2="22" y2="15"/></svg>';
function audioCtx(){
  if(actx) return actx;
  const AC = window.AudioContext || window.webkitAudioContext; if(!AC) return null;
  actx = new AC();
  tickBuf = actx.createBuffer(1, Math.round(actx.sampleRate * 0.04), actx.sampleRate);   // 40 ms of noise
  const d = tickBuf.getChannelData(0); for(let i=0;i<d.length;i++) d[i] = Math.random()*2-1;
  return actx;
}
function tick(){
  if(!ticksOn) return;
  const ctx = audioCtx(); if(!ctx) return;
  if(ctx.state === "running") return playTick(ctx);
  // browsers let sound start only after a click or key press: from then on it stays allowed
  ctx.resume().then(()=>{ if(ticksOn) playTick(ctx); }).catch(()=>{});
}
function playTick(ctx){
  const now = performance.now(); if(now - tickAt < 40) return; tickAt = now;
  const t = ctx.currentTime, src = ctx.createBufferSource(), f = ctx.createBiquadFilter(), g = ctx.createGain();
  src.buffer = tickBuf; f.type = "bandpass"; f.frequency.value = 2600; f.Q.value = 1.2;
  g.gain.setValueAtTime(0.2, t); g.gain.exponentialRampToValueAtTime(0.001, t + 0.03);
  src.connect(f).connect(g).connect(ctx.destination); src.start(t); src.stop(t + 0.035);
}
function paintTicks(){
  const b = $("tickbtn");
  b.classList.toggle("off", !ticksOn); b.setAttribute("aria-pressed", String(ticksOn));
  b.innerHTML = (ticksOn ? SPK_ON : SPK_OFF) + (ticksOn ? "Ticks on" : "Ticks off");
}
function toggleTicks(){
  ticksOn = !ticksOn;
  try{ localStorage.setItem("fr_ticks", ticksOn ? "on" : "off"); }catch(e){}
  paintTicks(); if(ticksOn) tick();
}
document.addEventListener("pointerover", e=>{
  const el = e.target.closest && e.target.closest("button, .dev, .grp");
  if(!el || el === tickEl) return;                 // still inside the same control
  tickEl = el;
  if(!el.disabled && !el.classList.contains("dis")) tick();
}, true);
document.addEventListener("pointerout", e=>{ if(tickEl && !tickEl.contains(e.relatedTarget)) tickEl = null; }, true);
for(const ev of ["pointerdown", "keydown"]) document.addEventListener(ev, ()=>{
  if(ticksOn){ const c = audioCtx(); if(c && c.state !== "running") c.resume().catch(()=>{}); }
}, true);

// boot
(async ()=>{
  paintKb(); paintScreen(); paintTicks();
  try{ const m = await api("GET","/api/meta"); $("appver").textContent = "v"+m.app_version; }catch(e){}
  // Show a user-supplied logo.png if present; keep text title otherwise.
  const logo = $("logo");
  logo.onload = ()=>{ logo.style.display="block"; $("titletext").style.display="none"; };
  logo.onerror = ()=>{ logo.style.display="none"; };
  logo.src = "/api/logo?" + Date.now();
  try{ await refresh(); }catch(e){ log("Fleet Remote did not answer: " + esc(e.message) + ". Retrying…", true); }
  // every second while the window is in view, every 5 s while it is hidden (minimised, another tab)
  const pollLoop = ()=>{ pollDevices(); setTimeout(pollLoop, document.hidden ? 5000 : 1000); };
  setTimeout(pollLoop, 1000);
  try{ const sh = await api("GET","/api/share"); if(sh.url) log("Sharing: open "+esc(sh.url)+" in another browser, access code "+esc(sh.code)); }catch(e){}
})();
</script>
</body>
</html>
"""


def _report_crash(exc: BaseException):
    """Under pythonw there is no console to print to: log the crash and show a dialog."""
    log = DATA_DIR / "error.log"
    _log_error(traceback.format_exc())
    _alert(f"Fleet Remote failed to start:\n\n{exc}\n\nDetails: {log}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as e:
        _report_crash(e)
        raise
    _fast_exit()

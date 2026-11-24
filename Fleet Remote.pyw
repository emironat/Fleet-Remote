"""
Fleet Remote launcher - double-click this file to start Fleet Remote (Windows and Mac).
The first time, it quietly installs the Python packages Fleet Remote needs (about a
minute) into a private 'venv' folder next to this file (the exact versions in
requirements.txt and constraints.txt; nothing else on the computer is touched) and makes a 'Fleet Remote'
shortcut with the app's icon in this folder (on a Mac 'Fleet Remote.app') to start it
from then on.
It always starts the newest fleet_remote_v<version>.py in this folder, so a new version
copied in is picked up by itself. Flags (--browser, --share) are passed on.
"""
import json
import os
import platform
import plistlib
import re
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERSIONED = re.compile(r"^fleet_remote_v(\d+)\.(\d+)\.py$")
MODULES = ("adb_shell", "aiofiles", "rsa", "fastapi", "uvicorn", "webview", "pyatv", "truststore")
WIN = sys.platform == "win32"
VENV = HERE / "venv"
VENV_PY = VENV / ("Scripts/pythonw.exe" if WIN else "bin/python")
NO_WINDOW = {"creationflags": 0x08000000} if WIN else {}           # CREATE_NO_WINDOW
LOG = Path.home() / ".firecube-fleet" / "setup.log"
LOCK = LOG.with_name("setup.lock")
RUNNING = LOG.with_name("running.json")       # written by a running copy: its port
MAC_APP = HERE / "Fleet Remote.app"
ICON = HERE / "icon.ico"
SHORTCUT = HERE / "Fleet Remote.lnk"
AUMID = "emironat.FleetRemote"            # the app sets the same id, so both share one taskbar button


def message(text: str) -> None:
    try:
        if WIN:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, "Fleet Remote", 0x10)
        elif sys.platform == "darwin":
            q = text.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(["osascript", "-e", f'display alert "Fleet Remote" message "{q}" as critical'])
        else:
            print(text)
    except Exception:
        print(text)


def hide_console() -> None:
    """Some PCs open .pyw files with py.exe instead of pyw.exe, which adds an empty black
    console window. Hide it, but only when nothing else (say a cmd window) shares it."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.GetConsoleWindow.restype = wintypes.HWND
    k32.OpenProcess.restype = wintypes.HANDLE
    hwnd = k32.GetConsoleWindow()
    if not hwnd:
        return
    pids = (wintypes.DWORD * 16)()
    n = k32.GetConsoleProcessList(pids, 16)
    if not 0 < n <= 16:
        return
    for pid in pids[:n]:
        h = k32.OpenProcess(0x1000, False, pid)                  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return
        buf, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        try:
            ok = k32.QueryFullProcessImageNameW(wintypes.HANDLE(h), 0, buf, ctypes.byref(size))
        finally:
            k32.CloseHandle(wintypes.HANDLE(h))
        if not ok or not re.match(r"^(pyw?|pythonw?[\d.]*)\.exe$", Path(buf.value).name, re.I):
            return
    ctypes.windll.user32.ShowWindow(hwnd, 0)                       # SW_HIDE


def newest() -> "Path | None":
    best = None
    for p in HERE.glob("fleet_remote_v*.py"):
        m = VERSIONED.match(p.name)
        if m:
            v = (int(m.group(1)), int(m.group(2)))
            if best is None or v > best[0]:
                best = (v, p)
    return best[1] if best else None


def venv_ok() -> bool:
    """The venv's Python still runs (after a Python upgrade or removal it points at nothing)."""
    exe = VENV / "Scripts/python.exe" if WIN else VENV_PY
    if not exe.is_file() or not VENV_PY.is_file():
        return False
    try:
        return subprocess.run([str(exe), "-c", "import sys"], capture_output=True, timeout=60,
                              **NO_WINDOW).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def make_venv(base: Path) -> bool:
    """A private 'venv' folder next to this file, made from the Python that runs this
    launcher. --clear: rebuild one whose Python is gone (an upgrade)."""
    r = subprocess.run([str(base), "-m", "venv", *(["--clear"] if VENV.exists() else []), str(VENV)],
                       capture_output=True, text=True, **NO_WINDOW)
    if r.returncode != 0:
        try:
            LOG.parent.mkdir(exist_ok=True)
            LOG.write_text("venv: " + (r.stdout or "") + (r.stderr or ""), encoding="utf-8")
        except OSError:
            pass
    return r.returncode == 0 and VENV_PY.is_file()


def python() -> Path:
    """The venv's Python (made here when it is missing or broken). Without a venv module
    the launcher's own Python is used, and pip installs into that as before."""
    if venv_ok() or make_venv(Path(sys.executable)):
        return VENV_PY
    exe = Path(sys.executable)
    if WIN and exe.with_name("pythonw.exe").is_file():
        exe = exe.with_name("pythonw.exe")                                      # no console window
    return exe


def marker_ok(marker: str, sys_platform: str = sys.platform, machine: str = platform.machine()) -> bool:
    """The environment markers constraints.txt uses: sys_platform / platform_machine compared
    with == or !=, joined by 'and' / 'or'. Anything else is left to pip (counts as true)."""
    env = {"sys_platform": sys_platform, "platform_machine": machine}

    def atom(text: str) -> bool:
        m = re.fullmatch(r"\s*(\w+)\s*(==|!=)\s*[\"']([^\"']*)[\"']\s*", text)
        if not m or m.group(1) not in env:
            return True
        return (env[m.group(1)] == m.group(3)) == (m.group(2) == "==")

    return any(all(atom(a) for a in part.split(" and ")) for part in marker.split(" or "))


def pins(sys_platform: str = sys.platform, machine: str = platform.machine()) -> dict:
    """package -> version from the 'name==version' lines of requirements.txt and
    constraints.txt that apply to this platform."""
    out = {}
    for name in ("requirements.txt", "constraints.txt"):
        try:
            lines = (HERE / name).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            m = re.match(r"\s*([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?\s*==\s*([0-9][^\s;#]*)\s*(?:;([^#]*))?", line)
            if m and marker_ok(m.group(3) or "", sys_platform, machine):
                out[m.group(1).lower().replace("_", "-")] = m.group(2)
    return out


def missing(exe: Path) -> bool:
    """A module is absent or a package is not at its pinned version (pip install then sets
    everything right: Fleet Remote uses adb-shell internals, so that one in particular has
    to be the version it was tested with; constraints.txt pins the rest the same way)."""
    check = (
        "import importlib.util, importlib.metadata, re, sys\n"
        f"absent = any(importlib.util.find_spec(m) is None for m in {MODULES!r})\n"
        "def norm(v):\n"
        "    n = [int(x) for x in re.findall(r'\\d+', v or '')]\n"
        "    while n and n[-1] == 0:\n"
        "        n.pop()\n"
        "    return n\n"
        "def ver(p):\n"
        "    try:\n"
        "        return norm(importlib.metadata.version(p))\n"
        "    except importlib.metadata.PackageNotFoundError:\n"
        "        return None\n"
        f"sys.exit(absent or any(ver(p) != norm(v) for p, v in {pins()!r}.items()))\n")
    return subprocess.run([str(exe), "-c", check], **NO_WINDOW).returncode != 0


def pip_command(exe: Path) -> list:
    cmd = [str(exe), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(HERE / "requirements.txt")]
    if (HERE / "constraints.txt").is_file():
        cmd += ["-c", str(HERE / "constraints.txt")]
    return cmd


def pip_install(exe: Path) -> "str | None":
    """Install requirements.txt (every version held by constraints.txt); None when it
    worked, else the end of pip's output."""
    r = subprocess.run(pip_command(exe), capture_output=True, text=True, **NO_WINDOW)
    out = (r.stdout or "") + (r.stderr or "")
    try:
        LOG.parent.mkdir(exist_ok=True)
        LOG.write_text(out, encoding="utf-8")
    except OSError:
        pass
    if r.returncode == 0:
        return None
    return "\n".join(out.strip().splitlines()[-6:]) or f"pip exit code {r.returncode}"


def setup(exe: Path) -> "str | None":
    """First start: install the packages without any window. A second double-click while
    that runs just waits for it instead of starting a second install."""
    LOCK.parent.mkdir(exist_ok=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if time.time() - LOCK.stat().st_mtime < 900:
            while LOCK.exists() and time.time() - LOCK.stat().st_mtime < 900:
                time.sleep(1)
            return None if not missing(exe) else "the other setup did not finish"
        LOCK.unlink(missing_ok=True)                    # left over from a setup that was killed
        return setup(exe)
    os.close(fd)
    try:
        return pip_install(exe)
    finally:
        LOCK.unlink(missing_ok=True)


def running_copy(port: int) -> bool:
    """A Fleet Remote is open on this computer: on the port asked for, or on the one the
    running copy noted. pip can't replace a package file that an open copy has loaded
    (Windows locks it), so no install while one runs: the new start joins the open copy,
    which tells the user to close it and start again."""
    import socket
    import urllib.request
    ports = {port}
    try:
        ports.add(int(json.loads(RUNNING.read_text(encoding="utf-8"))["port"]))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for p in ports:
        with socket.socket() as s:                      # nothing listens: don't wait on a stealth firewall
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", p)) != 0:
                continue
        try:
            with opener.open(f"http://127.0.0.1:{p}/api/meta", timeout=2) as r:
                if "app_version" in json.loads(r.read().decode("utf-8")):
                    return True
        except Exception:
            pass
    return False


def asked_port(argv: list) -> int:
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv) and argv[i + 1].isdigit():
            return int(argv[i + 1])
        if a.startswith("--port=") and a[7:].isdigit():
            return int(a[7:])
    return 8756


def ico_pngs() -> dict:
    """size -> PNG bytes, from the PNG images inside icon.ico."""
    out = {}
    try:
        data = ICON.read_bytes()
        for i in range(struct.unpack_from("<H", data, 4)[0]):
            w, _, _, _, _, _, size, off = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
            png = data[off:off + size]
            if png.startswith(b"\x89PNG"):
                out[w or 256] = png
    except (OSError, struct.error):
        pass
    return out


def mac_icns() -> bytes:
    pngs = ico_pngs()
    parts = b"".join(kind + struct.pack(">I", len(pngs[n]) + 8) + pngs[n]
                     for kind, n in ((b"icp4", 16), (b"icp5", 32), (b"icp6", 64), (b"ic07", 128), (b"ic08", 256))
                     if n in pngs)
    return b"icns" + struct.pack(">I", len(parts) + 8) + parts if parts else b""


SHORTCUT_PS = r"""
$ErrorActionPreference = 'Stop'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:FR_LNK)
$s.TargetPath = $env:FR_EXE
$s.Arguments = '"' + $env:FR_PYW + '"'
$s.WorkingDirectory = $env:FR_DIR
$s.IconLocation = $env:FR_ICO + ',0'
$s.Description = 'Fleet Remote'
$s.Save()
# The part below only gives the shortcut the window's taskbar id (one taskbar button).
# A locked-down PC may block Add-Type: the shortcut still works, so don't fail on it.
try {
Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
[StructLayout(LayoutKind.Sequential, Pack=4)] public struct FrKey { public Guid fmtid; public uint pid; }
[StructLayout(LayoutKind.Explicit, Size=24)] public struct FrVar { [FieldOffset(0)] public ushort vt; [FieldOffset(8)] public IntPtr p; }
[ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IFrStore { void GetCount(out uint c); void GetAt(uint i, out FrKey k); void GetValue(ref FrKey k, out FrVar v); void SetValue(ref FrKey k, ref FrVar v); void Commit(); }
public static class FrAumid {
  [DllImport("shell32.dll", CharSet=CharSet.Unicode, PreserveSig=false)]
  static extern void SHGetPropertyStoreFromParsingName(string path, IntPtr bc, int flags, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out IFrStore ps);
  public static void Set(string path, string id) {
    Guid iid = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"); IFrStore ps;
    SHGetPropertyStoreFromParsingName(path, IntPtr.Zero, 2, ref iid, out ps);
    FrKey k = new FrKey { fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 };
    FrVar v = new FrVar { vt = 31, p = Marshal.StringToCoTaskMemUni(id) };
    try { ps.SetValue(ref k, ref v); ps.Commit(); } finally { Marshal.FreeCoTaskMem(v.p); Marshal.ReleaseComObject(ps); }
  }
}
'@
[FrAumid]::Set($env:FR_LNK, $env:FR_ID)
} catch { }
exit [int](-not (Test-Path -LiteralPath $env:FR_LNK))   # the catch leaves $? false
"""


def win_shortcut(exe: Path) -> None:
    """'Fleet Remote' shortcut next to this file, with the app's icon (a .pyw always shows
    Python's). Pin it to the taskbar or Start, or copy it to the Desktop."""
    if not ICON.is_file():
        return
    stamp = LOG.with_name("shortcut.txt")
    want = f"{exe}|{HERE}|{ICON.stat().st_mtime_ns}|1"
    try:
        if SHORTCUT.is_file() and stamp.read_text(encoding="utf-8") == want:
            return
    except OSError:
        pass
    env = dict(os.environ, FR_LNK=str(SHORTCUT), FR_EXE=str(exe), FR_PYW=str(Path(__file__).resolve()),
               FR_DIR=str(HERE), FR_ICO=str(ICON), FR_ID=AUMID)
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-Command", SHORTCUT_PS], env=env, capture_output=True, text=True,
                           timeout=90, **NO_WINDOW)
        if r.returncode == 0:
            stamp.parent.mkdir(exist_ok=True)
            stamp.write_text(want, encoding="utf-8")
        else:
            LOG.with_name("shortcut.log").write_text((r.stdout or "") + (r.stderr or ""), encoding="utf-8")
    except (OSError, subprocess.SubprocessError):
        pass                                            # no shortcut: the .pyw still works


def mac_native() -> None:
    """macOS opens an app whose executable is a script under Rosetta on an Apple-silicon Mac
    (seen on macOS 15 with 'Fleet Remote.app', LSRequiresNativeExecution or not), and that
    Intel Python can't load the venv's arm64 packages: start this launcher again natively."""
    try:
        translated = subprocess.run(["sysctl", "-n", "sysctl.proc_translated"], capture_output=True,
                                    text=True, timeout=10).stdout.strip() == "1"
        native = ["/usr/bin/arch", "-arm64", sys.executable]
        if translated and subprocess.run([*native, "-c", "pass"], capture_output=True, timeout=60).returncode == 0:
            os.execv(native[0], [*native, str(Path(__file__).resolve()), *sys.argv[1:]])
    except (OSError, subprocess.SubprocessError):
        pass                                            # an Intel-only Python: carry on as before


def mac_app(exe: Path) -> None:
    """Make 'Fleet Remote.app' next to this file: a normal Mac app that runs this launcher
    without opening IDLE or a Terminal window. Falls back from the venv's Python to the
    Python this launcher runs on (the venv's base when started from the .app) and the
    usual python.org / Homebrew paths, for a venv whose Python is gone."""
    base = Path(getattr(sys, "_base_executable", None) or sys.executable).resolve()
    script = f"""#!/bin/bash
HERE="$(cd "$(dirname "$0")/../../.." && pwd)"
alert() {{ osascript -e "display alert \\"Fleet Remote\\" message \\"$1\\" as critical"; exit 1; }}
[ -f "$HERE/Fleet Remote.pyw" ] || alert "Keep Fleet Remote.app in the Fleet Remote folder, next to Fleet Remote.pyw."
PY=""
for P in "$HERE/venv/bin/python" "{base}" /usr/local/bin/python3 /opt/homebrew/bin/python3; do
  if [ -x "$P" ]; then PY="$P"; break; fi
done
[ -n "$PY" ] || alert "Python 3 was not found. Install it from python.org, then open Fleet Remote again."
exec "$PY" "$HERE/Fleet Remote.pyw"
"""
    plist = plistlib.dumps({"CFBundleName": "Fleet Remote", "CFBundleDisplayName": "Fleet Remote",
                            "CFBundleIdentifier": "com.emironat.fleetremote", "CFBundleExecutable": "Fleet Remote",
                            "CFBundleIconFile": "Fleet Remote.icns",
                            "CFBundlePackageType": "APPL", "CFBundleVersion": "1", "LSUIElement": True})
    icns = mac_icns()
    run = MAC_APP / "Contents" / "MacOS" / "Fleet Remote"
    info = MAC_APP / "Contents" / "Info.plist"
    icon = MAC_APP / "Contents" / "Resources" / "Fleet Remote.icns"
    try:
        if (run.is_file() and run.read_text() == script and info.is_file() and info.read_bytes() == plist
                and (not icns or (icon.is_file() and icon.read_bytes() == icns))):
            return
        run.parent.mkdir(parents=True, exist_ok=True)
        info.write_bytes(plist)
        run.write_text(script)
        os.chmod(run, 0o755)
        if icns:
            icon.parent.mkdir(parents=True, exist_ok=True)
            icon.write_bytes(icns)
        os.utime(MAC_APP)                               # Finder shows the new icon
    except OSError:
        pass                                            # read-only folder: the .pyw still works


def _secs(etime: str) -> int:
    """ps etime ([[dd-]hh:]mm:ss) in seconds."""
    days, _, clock = etime.rpartition("-")
    secs = 0
    for part in clock.split(":"):
        secs = secs * 60 + int(part)
    return secs + int(days or 0) * 86400


def _procs() -> list:
    """(pid, seconds running, command) of every process. Not pgrep: it skips its own
    ancestors, and Terminal is one here."""
    out = subprocess.run(["ps", "-axo", "pid=,etime=,comm="], capture_output=True, text=True).stdout
    return [(int(pid), _secs(etime), comm) for pid, etime, comm in
            (line.split(None, 2) for line in out.splitlines())]


TIDY_SCPT = """
tell application "Terminal"
  set ids to get id of every window
  repeat with i from 1 to count of ids
    set wid to item i of ids
    try
      repeat with t in (get every tab of window id wid)
        if tty of t is "%TTY%" then
          if (count of processes of t) > 0 then return "wait"
          close window id wid
          exit repeat
        end if
      end repeat
    end try
  end repeat
  if %QUIT% then
    set ids to get id of every window
    repeat with i from 1 to count of ids
      repeat with t in (get every tab of window id (item i of ids))
        if busy of t then return "done"
      end repeat
    end repeat
    quit
  end if
end tell
return "done"
"""


def mac_tidy_terminal() -> None:
    """Double-clicking the .pyw makes Python Launcher run it in a Terminal window, and
    Terminal, when it was not open, adds its own startup window too. A helper closes that
    window once this script has exited (its shell is finished by then; a window with a live
    shell, say the user's own, is left alone) and quits Terminal when it was opened just
    for this. Python Launcher itself has done its job but would stay in the Dock."""
    if os.environ.get("TERM_PROGRAM") != "Apple_Terminal":
        return
    try:
        tty = os.ttyname(sys.stdin.fileno())
    except OSError:
        return
    quit_all = False
    try:
        procs = _procs()
        mine = next((age for pid, age, _ in procs if pid == os.getpid()), None)
        term = next((age for _, age, c in procs if c.endswith("/Terminal.app/Contents/MacOS/Terminal")), None)
        quit_all = mine is not None and term is not None and term - mine < 30
        for pid, _, c in procs:
            if c.endswith("/Python Launcher.app/Contents/MacOS/Python Launcher"):
                os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass
    scpt = TIDY_SCPT.replace("%TTY%", tty).replace("%QUIT%", "true" if quit_all else "false")
    helper = (f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.2; done\n"
              "for i in 1 2 3 4 5 6 7 8 9 10; do\n"
              f"  [ \"$(osascript -e '{scpt}' 2>/dev/null)\" = wait ] || break\n"
              "  sleep 0.5\ndone\n")
    subprocess.Popen(["/bin/sh", "-c", helper], start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if MAC_APP.is_dir():
        hint = ("display notification \"Fleet Remote.app is ready in this folder: start it from "
                "there from now on (drag it to the Dock).\" with title \"Fleet Remote\"")
        subprocess.Popen(["osascript", "-e", hint], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> None:
    if sys.version_info < (3, 11):                  # the pinned packages have no builds for older Pythons
        message(f"Fleet Remote needs Python 3.11 or newer (this is {sys.version.split()[0]}).\n"
                "Install the newest Python from python.org.")
        sys.exit(1)
    if WIN:
        try:
            hide_console()
        except Exception:
            pass
    elif sys.platform == "darwin":
        mac_native()
    script = newest()
    if script is None:
        message(f"No fleet_remote_v*.py found in\n{HERE}")
        sys.exit(1)
    exe = python()
    if sys.platform == "darwin":
        mac_app(exe)
    if missing(exe) and not running_copy(asked_port(sys.argv[1:])):
        if sys.platform == "darwin":                    # the install is silent: say it has begun
            with_notice = ("display notification \"Setting up Fleet Remote, about a minute. "
                           "The window opens by itself.\" with title \"Fleet Remote\"")
            subprocess.Popen(["osascript", "-e", with_notice], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        err = setup(exe)
        if err:
            message(f"Fleet Remote could not install what it needs.\n\n{err}\n\nFull details: {LOG}")
            sys.exit(1)
    kw = {"cwd": str(HERE), "close_fds": True}
    if not WIN:
        kw.update(start_new_session=True, stdin=subprocess.DEVNULL,       # keeps running when the
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # Mac Terminal window closes
    subprocess.Popen([str(exe), str(script), *sys.argv[1:]], **kw)
    if WIN:
        win_shortcut(exe)                               # after the start, so the window isn't kept waiting
    elif sys.platform == "darwin":
        mac_tidy_terminal()


if __name__ == "__main__":
    main()

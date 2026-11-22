"""
Fleet Remote launcher - double-click this file to start Fleet Remote (Windows and Mac).
The first time, it quietly installs the Python packages Fleet Remote needs (about a
minute) and makes a 'Fleet Remote' shortcut with the app's icon in this folder (on a Mac
'Fleet Remote.app') to start it from then on.
It always starts the newest fleet_remote_v<version>.py in this folder, so a new version
copied in is picked up by itself. Flags (--browser, --share) are passed on.
"""
import os
import plistlib
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERSIONED = re.compile(r"^fleet_remote_v(\d+)\.(\d+)\.py$")
MODULES = ("adb_shell", "aiofiles", "rsa", "fastapi", "uvicorn", "webview", "pyatv", "truststore")
WIN = sys.platform == "win32"
NO_WINDOW = {"creationflags": 0x08000000} if WIN else {}           # CREATE_NO_WINDOW
LOG = Path.home() / ".firecube-fleet" / "setup.log"
LOCK = LOG.with_name("setup.lock")
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


def python() -> Path:
    exe = Path(sys.executable)
    venv = HERE / "venv" / ("Scripts/pythonw.exe" if WIN else "bin/python")   # the README's venv, if made
    if venv.is_file():
        exe = venv
    if WIN and exe.with_name("pythonw.exe").is_file():
        exe = exe.with_name("pythonw.exe")                                      # no console window
    return exe


def missing(exe: Path) -> bool:
    check = ("import importlib.util as u, sys; "
             f"sys.exit(any(u.find_spec(m) is None for m in {MODULES!r}))")
    return subprocess.run([str(exe), "-c", check], **NO_WINDOW).returncode != 0


def pip_install(exe: Path) -> "str | None":
    """Install requirements.txt; None when it worked, else the end of pip's output."""
    r = subprocess.run([str(exe), "-m", "pip", "install", "--disable-pip-version-check",
                        "-r", str(HERE / "requirements.txt")],
                       capture_output=True, text=True, **NO_WINDOW)
    out = (r.stdout or "") + (r.stderr or "")
    try:
        LOG.parent.mkdir(exist_ok=True)
        LOG.write_text(out, encoding="utf-8")
    except OSError:
        pass
    if r.returncode == 0:
        return None
    return "\n".join(out.strip().splitlines()[-6:]) or f"pip exit code {r.returncode}"


def make_venv(exe: Path) -> "Path | None":
    """A Python that refuses installs (Homebrew on a Mac, PEP 668) gets a private
    'venv' folder next to this file instead; python() uses it from then on."""
    base = HERE / "venv"
    # --clear: after a Python upgrade an old venv points at a Python that is gone
    r = subprocess.run([str(exe), "-m", "venv", *(["--clear"] if base.exists() else []), str(base)],
                       capture_output=True, text=True, **NO_WINDOW)
    v = base / ("Scripts/pythonw.exe" if WIN else "bin/python")
    return v if r.returncode == 0 and v.is_file() else None


def _log_text() -> str:
    try:
        return LOG.read_text(encoding="utf-8")
    except OSError:
        return ""


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


def mac_app(exe: Path) -> None:
    """Make 'Fleet Remote.app' next to this file: a normal Mac app that runs this launcher
    without opening IDLE or a Terminal window."""
    script = f"""#!/bin/bash
HERE="$(cd "$(dirname "$0")/../../.." && pwd)"
alert() {{ osascript -e "display alert \\"Fleet Remote\\" message \\"$1\\" as critical"; exit 1; }}
[ -f "$HERE/Fleet Remote.pyw" ] || alert "Keep Fleet Remote.app in the Fleet Remote folder, next to Fleet Remote.pyw."
PY=""
for P in "$HERE/venv/bin/python" "{exe}" /usr/local/bin/python3 /opt/homebrew/bin/python3; do
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


def main() -> None:
    if sys.version_info < (3, 10):
        message(f"Fleet Remote needs Python 3.10 or newer (this is {sys.version.split()[0]}).\n"
                "Install the newest Python from python.org.")
        sys.exit(1)
    if WIN:
        try:
            hide_console()
        except Exception:
            pass
    script = newest()
    if script is None:
        message(f"No fleet_remote_v*.py found in\n{HERE}")
        sys.exit(1)
    exe = python()
    if sys.platform == "darwin":
        mac_app(exe)
    if missing(exe):
        if sys.platform == "darwin":                    # the install is silent: say it has begun
            with_notice = ("display notification \"Setting up Fleet Remote, about a minute. "
                           "The window opens by itself.\" with title \"Fleet Remote\"")
            subprocess.Popen(["osascript", "-e", with_notice], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        err = setup(exe)
        if err and "externally-managed" in err + _log_text():
            venv = make_venv(exe)
            if venv is not None:
                exe = venv
                err = setup(exe) if missing(exe) else None
                if sys.platform == "darwin":
                    mac_app(exe)
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


if __name__ == "__main__":
    main()

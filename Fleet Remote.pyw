"""
Fleet Remote launcher - double-click this file to start Fleet Remote (Windows and Mac).
The first time, it installs the Python packages Fleet Remote needs (about a minute).
It always starts the newest fleet_remote_v<version>.py in this folder, so the app's
Update button can swap the versioned file. Flags (--browser, --share) are passed on.
"""
import re
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERSIONED = re.compile(r"^fleet_remote_v(\d+)\.(\d+)\.py$")
MODULES = ("adb_shell", "aiofiles", "rsa", "fastapi", "uvicorn", "webview", "pyatv")
WIN = sys.platform == "win32"
NO_WINDOW = {"creationflags": 0x08000000} if WIN else {}           # CREATE_NO_WINDOW
LOG = Path.home() / ".firecube-fleet" / "setup.log"


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


def setup(exe: Path) -> "str | None":
    """First start: install the packages, with a small 'please wait' window when Tk is there."""
    result = []
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        return pip_install(exe)
    root = tk.Tk()
    root.title("Fleet Remote")
    root.resizable(False, False)
    tk.Label(root, text="Setting up Fleet Remote (first start only).\n"
                        "Installing what it needs, this takes about a minute...",
             padx=24, pady=14, justify="left").pack()
    bar = ttk.Progressbar(root, mode="indeterminate", length=320)
    bar.pack(padx=24, pady=(0, 18))
    bar.start(12)
    root.protocol("WM_DELETE_WINDOW", lambda: None)               # closing it would not stop pip

    def work():
        result.append(pip_install(exe))
        root.after(0, root.destroy)
    threading.Thread(target=work, daemon=True).start()
    root.mainloop()
    return result[0] if result else "setup was interrupted"


def main() -> None:
    script = newest()
    if script is None:
        message(f"No fleet_remote_v*.py found in\n{HERE}")
        sys.exit(1)
    exe = python()
    if missing(exe):
        err = setup(exe)
        if err:
            message(f"Fleet Remote could not install what it needs.\n\n{err}\n\nFull details: {LOG}")
            sys.exit(1)
    kw = {"cwd": str(HERE), "close_fds": True}
    if not WIN:
        kw.update(start_new_session=True, stdin=subprocess.DEVNULL,       # keeps running when the
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # Mac Terminal window closes
    subprocess.Popen([str(exe), str(script), *sys.argv[1:]], **kw)


if __name__ == "__main__":
    main()

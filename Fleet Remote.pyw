"""
Fleet Remote launcher (Windows)
Double-click this file, or point a shortcut at it. It starts the newest
fleet_remote_v<version>.py in this folder, so the Update button inside the app
can swap the versioned file without breaking your shortcut.
Any flags you give (for example --browser or --share) are passed on.
"""
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERSIONED = re.compile(r"^fleet_remote_v(\d+)\.(\d+)\.py$")


def newest() -> "Path | None":
    best = None
    for p in HERE.glob("fleet_remote_v*.py"):
        m = VERSIONED.match(p.name)
        if m:
            v = (int(m.group(1)), int(m.group(2)))
            if best is None or v > best[0]:
                best = (v, p)
    return best[1] if best else None


def main() -> None:
    script = newest()
    if script is None:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, f"No fleet_remote_v*.py found in\n{HERE}", "Fleet Remote", 0x10)
        sys.exit(1)
    exe = Path(sys.executable)
    venv = HERE / "venv" / "Scripts" / "pythonw.exe"  # the venv from the README, if you made one
    if venv.is_file():
        exe = venv
    pyw = exe.with_name("pythonw.exe")                # no console window
    if pyw.is_file():
        exe = pyw
    subprocess.Popen([str(exe), str(script), *sys.argv[1:]], cwd=str(HERE), close_fds=True)


if __name__ == "__main__":
    main()

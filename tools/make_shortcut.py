"""Make a ShelfLife shortcut (Windows) that opens the app window without a console.

Run from the project root:  python tools/make_shortcut.py [--desktop]

By default the shortcut ShelfLife.lnk is made in the project folder; --desktop puts it on your
Desktop instead. Pin it to the taskbar or Start from there. It runs this same Python with
`-m scripts.app`, so make it with the Python that has the packages installed (py -3.15).
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "shelflife" / "web" / "static" / "shelflife.ico"  # drawn by tools/make_icon.py


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--desktop", action="store_true", help="put it on the Desktop")
    args = parser.parse_args()
    pythonw = Path(sys.executable).with_name("pythonw.exe")  # no console window
    if not pythonw.exists():
        raise SystemExit(f"pythonw.exe not found next to {sys.executable}")
    folder = Path.home() / "Desktop" if args.desktop else ROOT
    target = folder / "ShelfLife.lnk"
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:SL_LINK); "
        "$s.TargetPath = $env:SL_TARGET; $s.Arguments = '-m scripts.app'; $s.WorkingDirectory = $env:SL_DIR; "
        "$s.IconLocation = $env:SL_ICON; $s.Description = 'ShelfLife'; $s.Save()"
    )
    env = {**os.environ, "SL_LINK": str(target), "SL_TARGET": str(pythonw), "SL_DIR": str(ROOT), "SL_ICON": str(ICON)}
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True, env=env)
    print(f"Made {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

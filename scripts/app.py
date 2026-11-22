"""ShelfLife as a desktop window: no command, no browser tab.

Run from the project root:  python -m scripts.app      (or the ShelfLife shortcut, see tools/make_shortcut.py)

It starts the web app in the background on this computer, opens it in its own window (pywebview, using
the Edge WebView2 that Windows already has) and stops everything when you close the window. One window
at a time: starting it again brings the open one to the front. Needs `pip install -r requirements-app.txt`.

Closing the window ends the app. If a Sync or a Find-more-games search is running, it asks first.
The pages and the server are the same as with `python -m scripts.web`.
"""

import argparse
import socket
import threading
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "shelflife" / "web" / "static" / "shelflife.ico"
PROFILE = ROOT / "data" / "window"  # the window's own browser storage: keeps the light/dark choice
INSTANCE_PORT = 47831  # a second launch finds the first one here and asks it to come forward
DEFAULT_PORT = 8000


def free_port(preferred: int) -> int:
    """The preferred port if nothing uses it, else any free one."""
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return s.getsockname()[1]
    raise OSError("no free port")


def already_open() -> bool:
    """Ask a running window to come forward. True if there was one."""
    try:
        with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1) as s:
            s.sendall(b"show")
        return True
    except OSError:
        return False


def listen_for_second_launch(on_show) -> socket.socket | None:
    """Hold the instance port and call `on_show` whenever another launch knocks. None if it is taken."""
    server = socket.socket()
    try:
        server.bind(("127.0.0.1", INSTANCE_PORT))
    except OSError:
        server.close()
        return None
    server.listen()

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:  # closed on exit
                return
            with conn:
                on_show()

    threading.Thread(target=serve, daemon=True).start()
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--hidden", action="store_true", help="open no visible window (for tests)")
    parser.add_argument("--check", action="store_true", help="start, load the home page, report and exit (for tests)")
    args = parser.parse_args()

    if already_open():
        return 0
    try:
        import webview
    except ImportError:
        raise SystemExit("The window needs pywebview: pip install -r requirements-app.txt")

    port = free_port(args.port)
    config = uvicorn.Config("shelflife.web.app:app", host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        if not thread.is_alive():
            raise SystemExit("The ShelfLife server did not start.")
        threading.Event().wait(0.05)

    PROFILE.mkdir(parents=True, exist_ok=True)
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True  # "Steam store page ↗" opens in your browser
    window = webview.create_window(
        "ShelfLife", f"http://127.0.0.1:{port}/", width=1360, height=900, min_size=(720, 520), hidden=args.hidden
    )

    def bring_forward() -> None:
        window.restore()
        window.show()

    def confirm_close() -> bool:
        """Closing stops the app, and with it a running Sync or search: ask before doing that."""
        from shelflife.discover_job import job as discover_job
        from shelflife.sync_job import job as sync_job

        busy = [name for name, j in (("A Sync", sync_job), ("A search for new games", discover_job)) if j.snapshot().running]
        if not busy:
            return True
        return window.create_confirmation_dialog("Close ShelfLife?", f"{' and '.join(busy)} is still running. Closing stops it.")

    window.events.closing += confirm_close
    held = listen_for_second_launch(bring_forward)

    if args.check:
        import urllib.request

        def check() -> None:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=30) as r:
                print("home page", r.status, "title:", window.evaluate_js("document.title"))
            window.destroy()

        webview.start(check, private_mode=False, storage_path=str(PROFILE), icon=str(ICON))
    else:
        webview.start(private_mode=False, storage_path=str(PROFILE), icon=str(ICON))

    server.should_exit = True  # the window is closed: stop the server, then the process ends
    thread.join(timeout=10)
    if held:
        held.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Open the compact Apollo control window without browser tabs or a dashboard.

The local HTML is rendered by Edge/Chrome in ``--app`` mode.  It has no address
bar, tab strip, or dashboard; to the user it is a small desktop popup.  The
window is a companion to Telegram, not a replacement for the run engine.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
from pathlib import Path

from apollo_tool.config import settings

WINDOW_TITLE = "Apollo Email Finder"
API_URL = os.getenv("APOLLO_API_URL", "http://127.0.0.1:8765").strip().rstrip("/")
DESKTOP_URL = f"{API_URL}/desktop"
_WINDOW_LOCK = threading.Lock()
_LAST_LAUNCH = 0.0
_LAUNCH_GRACE_SECONDS = 6.0

EDGE_PATHS = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]
CHROME_PATHS = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]


def _find_window() -> int:
    """Return the popup window handle, matching an exact or suffixed title.

    ``FindWindowW`` only matches the exact title, and Chromium sometimes adds
    its own suffix (for example ``Apollo Email Finder - Profile 1``).  The
    enumeration below is what keeps repeated calls from launching duplicate
    windows when the exact match misses.
    """
    if os.name != "nt":
        return 0
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    handle = user32.FindWindowW(None, WINDOW_TITLE)
    if handle:
        return int(handle)

    exact: list[int] = []
    partial: list[int] = []

    enum_windows_proc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
    )

    @enum_windows_proc
    def enum_proc(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = int(user32.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if title == WINDOW_TITLE:
            exact.append(int(hwnd))
            return False
        if WINDOW_TITLE.casefold() in title.casefold():
            partial.append(int(hwnd))
        return True

    user32.EnumWindows.argtypes = [enum_windows_proc, ctypes.c_void_p]
    user32.EnumWindows(enum_proc, 0)
    handles = exact or partial
    return handles[0] if handles else 0


def _bring_to_front(handle: int) -> None:
    if not handle or os.name != "nt":
        return
    user32 = ctypes.windll.user32
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
    user32.ShowWindow(handle, 9)  # SW_RESTORE
    user32.BringWindowToTop(handle)
    try:
        user32.SetForegroundWindow(handle)
    except Exception:  # noqa: BLE001
        pass


def _browser() -> Path | None:
    for path in [*EDGE_PATHS, *CHROME_PATHS]:
        if path.exists():
            return path
    return None


def _screen_center(size: tuple[int, int]) -> tuple[int, int]:
    if os.name != "nt":
        return (80, 60)
    user32 = ctypes.windll.user32
    width = int(user32.GetSystemMetrics(0) or 1920)
    height = int(user32.GetSystemMetrics(1) or 1080)
    return (max(0, (width - size[0]) // 2), max(0, (height - size[1]) // 2))


def _launch() -> bool:
    browser = _browser()
    if browser is None:
        return False
    profile = settings.root / "desktop_profile"
    profile.mkdir(parents=True, exist_ok=True)
    x, y = _screen_center((560, 780))
    args = [
        str(browser),
        f"--app={DESKTOP_URL}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-extensions",
        "--disable-features=Translate",
        "--window-size=560,780",
        f"--window-position={x},{y}",
    ]
    try:
        subprocess.Popen(
            args,
            cwd=str(settings.root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return False

    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if _find_window():
            return True
        time.sleep(0.25)
    return False


def show_desktop_window() -> bool:
    """Bring the popup forward, launching it if it was closed.

    A launch grace period stops rapid taps (or a monitor plus a button press)
    from spawning two app windows while the first one is still starting.
    """
    global _LAST_LAUNCH
    with _WINDOW_LOCK:
        handle = _find_window()
        if handle:
            _bring_to_front(handle)
            return True
        now = time.monotonic()
        if now - _LAST_LAUNCH < _LAUNCH_GRACE_SECONDS:
            deadline = now + 1.2
            while time.monotonic() < deadline:
                handle = _find_window()
                if handle:
                    _bring_to_front(handle)
                    return True
                time.sleep(0.2)
            # The first launch is still settling; do not stack another one.
            return True
        if _browser() is None:
            return False
        _LAST_LAUNCH = now
        return _launch()


def close_desktop_window() -> None:
    """Used by tests or a future tray action; closes only our titled window."""
    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    handle = _find_window()
    if handle:
        user32.PostMessageW(handle, 0x0010, 0, 0)  # WM_CLOSE


if __name__ == "__main__":
    print("desktop window:", show_desktop_window())

"""Start the local UI and open it in the default browser."""

from __future__ import annotations

import argparse
import socket
import threading
import time
import webbrowser

import uvicorn


def find_port(start: int = 8765) -> int:
    for port in range(start, start + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free local port found")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    port = args.port or find_port()
    url = f"http://127.0.0.1:{port}"
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"\n  Apollo Email Finder is running at {url}\n  Press Ctrl+C to stop.\n", flush=True)
    uvicorn.run("apollo_tool.web:app", host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()

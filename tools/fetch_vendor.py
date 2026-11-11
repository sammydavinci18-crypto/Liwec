"""Download the one browser library the live room needs (Socket.IO client)
into static/vendor/, so the room loads it from YOUR server rather than a
public CDN.

Run once, on any machine with internet access, then deploy:

    python tools/fetch_vendor.py

The room template notices the file and uses it automatically.
"""
import sys
import urllib.request
from pathlib import Path

URL = "https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"
DEST = Path(__file__).resolve().parent.parent / "static" / "vendor" / "socket.io.min.js"


def main():
    DEST.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(URL, timeout=30) as resp:
            data = resp.read()
    except Exception as exc:  # noqa: BLE001
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1
    if b"socket.io" not in data[:400].lower():
        print("Downloaded file doesn't look like socket.io — aborting.", file=sys.stderr)
        return 1
    DEST.write_bytes(data)
    print(f"Saved {len(data)} bytes to {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

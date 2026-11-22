"""Download the browser libraries the live room needs into static/vendor/, so
the room loads them from YOUR server rather than a public CDN.

Run once, on any machine with internet access, then deploy:

    python tools/fetch_vendor.py

The room template notices the files and uses them automatically.
  * socket.io.min.js          always needed
  * livekit-client.umd.min.js only used when LIVEKIT_URL is set (media server)
"""
import sys
import urllib.request
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent / "static" / "vendor"

FILES = [
    # (url, file name, text that must appear near the start, to catch a wrong download)
    ("https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js", "socket.io.min.js", b"socket.io"),
    ("https://cdn.jsdelivr.net/npm/livekit-client@2.15.14/dist/livekit-client.umd.min.js", "livekit-client.umd.min.js", b""),
]


def main():
    VENDOR.mkdir(parents=True, exist_ok=True)
    failed = 0
    for url, name, marker in FILES:
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                data = resp.read()
        except Exception as exc:  # noqa: BLE001
            print(f"{name}: download failed: {exc}", file=sys.stderr)
            failed += 1
            continue
        if marker and marker not in data[:400].lower():
            print(f"{name}: downloaded file doesn't look right, skipping.", file=sys.stderr)
            failed += 1
            continue
        if len(data) < 10_000:
            print(f"{name}: suspiciously small ({len(data)} bytes), skipping.", file=sys.stderr)
            failed += 1
            continue
        (VENDOR / name).write_bytes(data)
        print(f"Saved {name} ({len(data)} bytes)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

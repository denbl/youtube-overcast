#!/usr/bin/python3
"""Ask a running Chrome for its cookies and write them in Netscape format.

Chrome 158 encrypts its cookie store with a scheme yt-dlp cannot read on macOS
("v11"), so reading the files directly is out. Chrome itself hands the cookies
over in plain text through its DevTools protocol, which is what this does.

Usage: dump_cookies.py <profile-dir> <output-file> [port]
"""
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def ws_connect(host, port, path):
    key = base64.b64encode(os.urandom(16)).decode()
    sock = socket.create_connection((host, port), timeout=20)
    sock.sendall(
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n".encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("Chrome closed the connection during the handshake")
        buf += chunk
    status = buf.split(b"\r\n", 1)[0]
    if b" 101 " not in status:
        raise RuntimeError(f"Chrome refused the websocket: {status.decode()}")
    return sock, buf.split(b"\r\n\r\n", 1)[1]


def ws_send(sock, text):
    data = text.encode()
    mask = os.urandom(4)
    header = bytearray([0x81])
    if len(data) < 126:
        header.append(0x80 | len(data))
    elif len(data) < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", len(data))
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", len(data))
    header += mask
    sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


class Frames:
    """Just enough of RFC 6455 to read one reply."""

    def __init__(self, sock, rest=b""):
        self.sock, self.buf = sock, rest

    def _need(self, size):
        while len(self.buf) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("Chrome closed the connection")
            self.buf += chunk

    def _frame(self):
        self._need(2)
        first, second = self.buf[0], self.buf[1]
        fin, opcode, masked, length = first & 0x80, first & 0x0F, second & 0x80, second & 0x7F
        offset = 2
        if length == 126:
            self._need(4)
            length = struct.unpack(">H", self.buf[2:4])[0]
            offset = 4
        elif length == 127:
            self._need(10)
            length = struct.unpack(">Q", self.buf[2:10])[0]
            offset = 10
        mask = b""
        if masked:
            self._need(offset + 4)
            mask = self.buf[offset:offset + 4]
            offset += 4
        self._need(offset + length)
        payload = self.buf[offset:offset + length]
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.buf = self.buf[offset + length:]
        return fin, opcode, payload

    def message(self):
        data = b""
        while True:
            fin, opcode, payload = self._frame()
            if opcode == 0x8:
                raise RuntimeError("Chrome closed the websocket")
            if opcode in (0x9, 0xA):  # ping/pong
                continue
            data += payload
            if fin:
                return json.loads(data.decode())


def start_chrome(profile, port):
    proc = subprocess.Popen(
        [CHROME, f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
         "--headless=new", "--no-first-run", "--no-default-browser-check",
         "--disable-gpu", "--remote-allow-origins=*", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                return proc, json.load(r)["webSocketDebuggerUrl"]
        except Exception:
            time.sleep(0.3)
    proc.terminate()
    raise RuntimeError("Chrome did not open its debugging port in 30 seconds")


def fetch_cookies(ws_url, port):
    path = ws_url.split(f":{port}", 1)[1]
    sock, rest = ws_connect("127.0.0.1", port, path)
    try:
        ws_send(sock, json.dumps({"id": 1, "method": "Storage.getCookies"}))
        frames = Frames(sock, rest)
        while True:
            msg = frames.message()
            if msg.get("id") != 1:
                continue
            if "error" in msg:
                raise RuntimeError(f"Chrome refused the request: {msg['error']}")
            return msg["result"]["cookies"]
    finally:
        sock.close()


def netscape(cookies):
    lines = ["# Netscape HTTP Cookie File", "# Written by dump_cookies.py, do not edit."]
    for c in cookies:
        name, value = c.get("name", ""), c.get("value", "")
        if any(ch in name + value for ch in "\t\r\n"):
            continue  # a value that would break the format is not worth keeping
        expires = int(c.get("expires") or 0)
        lines.append("\t".join([
            c["domain"],
            "TRUE" if c["domain"].startswith(".") else "FALSE",
            c.get("path", "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(expires if expires > 0 else 0),
            name,
            value,
        ]))
    return "\n".join(lines) + "\n", len(lines) - 2


def main(profile, out, port=9222):
    proc, ws_url = start_chrome(profile, int(port))
    try:
        cookies = fetch_cookies(ws_url, int(port))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    text, count = netscape(cookies)
    path = Path(out)
    path.write_text(text)
    path.chmod(0o600)
    print(f"saved {count} cookies to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))

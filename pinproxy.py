#!/usr/bin/env python3
"""Local CONNECT proxy for YouTube when the ISP blocks its usual IPs.

Only the YouTube web hosts are pinned to another Google frontend IP.  TLS is
still end-to-end between yt-dlp and YouTube: this proxy merely chooses which
Google address to connect to.  It listens on localhost only.
"""

from __future__ import annotations

import select
import socket
import ssl
import threading
import time

LISTEN = ("127.0.0.1", 8899)
PIN_HOSTS = {
    "www.youtube.com",
    "youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
FALLBACK_IPS = (
    "142.251.150.4",
    "142.251.151.4",
    "142.251.152.4",
)

_cache = {"ip": "", "at": 0.0}
_cache_lock = threading.Lock()


def candidate_ips() -> list[str]:
    ips: list[str] = []
    try:
        for info in socket.getaddrinfo(
            "music.youtube.com", 443, socket.AF_INET, socket.SOCK_STREAM
        ):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    for ip in FALLBACK_IPS:
        if ip not in ips:
            ips.append(ip)
    return ips


def works_as_youtube(ip: str) -> bool:
    """Check that this IP serves HTTPS for www.youtube.com."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((ip, 443), timeout=5) as raw:
            with ctx.wrap_socket(raw, server_hostname="www.youtube.com") as tls:
                tls.sendall(
                    b"HEAD / HTTP/1.1\r\n"
                    b"Host: www.youtube.com\r\n"
                    b"Connection: close\r\n\r\n"
                )
                data = tls.recv(256)
                return data.startswith(b"HTTP/")
    except (OSError, ssl.SSLError):
        return False


def pin_ip(force: bool = False) -> str:
    now = time.time()
    with _cache_lock:
        if not force and _cache["ip"] and now - _cache["at"] < 600:
            return str(_cache["ip"])
    for ip in candidate_ips():
        if works_as_youtube(ip):
            with _cache_lock:
                _cache.update({"ip": ip, "at": now})
            return ip
    return FALLBACK_IPS[0]


def relay(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            ready, _, _ = select.select([a, b], [], [], 120)
            if not ready:
                return
            for src in ready:
                data = src.recv(65536)
                if not data:
                    return
                (b if src is a else a).sendall(data)
    except OSError:
        pass


def handle(client: socket.socket) -> None:
    upstream: socket.socket | None = None
    try:
        client.settimeout(15)
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            chunk = client.recv(4096)
            if not chunk:
                return
            buf += chunk
        line = buf.split(b"\r\n", 1)[0].decode("latin1")
        parts = line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        host, _, port_text = parts[1].rpartition(":")
        port = int(port_text or "443")
        if not host:
            host = parts[1]
        if host in PIN_HOSTS:
            destinations = [pin_ip()]
            if host == "music.youtube.com":
                # Sometimes music.youtube.com is reachable directly even when
                # www.youtube.com needs a pinned Google frontend.  Keep that
                # direct address as a second chance.
                try:
                    info = socket.getaddrinfo(
                        host, port, socket.AF_INET, socket.SOCK_STREAM
                    )
                    direct = info[0][4][0]
                    if direct not in destinations:
                        destinations.append(direct)
                except OSError:
                    pass
        else:
            info = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
            destinations = [info[0][4][0]]

        last_error: Exception | None = None
        for dest in destinations:
            try:
                upstream = socket.create_connection((dest, port), timeout=15)
                break
            except OSError as exc:
                last_error = exc
                if host in PIN_HOSTS:
                    dest = pin_ip(force=True)
                    try:
                        upstream = socket.create_connection((dest, port), timeout=15)
                        break
                    except OSError as retry_exc:
                        last_error = retry_exc
        if upstream is None:
            raise last_error or OSError("no upstream")

        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        rest = buf.split(b"\r\n\r\n", 1)[1]
        if rest:
            upstream.sendall(rest)
        client.settimeout(300)
        upstream.settimeout(300)
        relay(client, upstream)
    except Exception as exc:
        try:
            client.sendall(f"HTTP/1.1 502 Bad Gateway\r\n\r\n{exc}\r\n".encode())
        except OSError:
            pass
    finally:
        for sock in (upstream, client):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


def main() -> None:
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(LISTEN)
    server.listen(50)
    print(f"pinproxy on {LISTEN[0]}:{LISTEN[1]}", flush=True)
    while True:
        client, _ = server.accept()
        threading.Thread(target=handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()

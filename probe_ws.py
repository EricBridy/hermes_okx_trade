#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WebSocket-only OKX latency probe.
Run on the server with:    /root/.local/bin/python3.11 probe_ws.py

Why a separate WS test:
  REST hits Cloudflare CDN -> origin (~256ms)
  WS holds a persistent connection so each tick only pays one-way wire latency.
  This is the timing budget that an event-driven strategy actually feels.

Tries first the standard `websockets` library; if not installed, falls back
to a hand-rolled WebSocket frame parser using only stdlib so it runs on a
freshly-installed Python.
"""
import asyncio
import json
import socket
import ssl
import statistics
import struct
import sys
import time
import os
import urllib.request

REST_HOST = "www.okx.com"
WS_HOST = "ws.okx.com"
WS_PORT = 8443
WS_PATH = "/ws/v5/public"
SAMPLE_TICKER = "BTC-USDT-SWAP"


def stats_line(name, samples):
    if not samples:
        print(f"  {name:30s}  no samples")
        return
    s = sorted(samples)
    n = len(s)
    p95 = s[max(0, int(n * 0.95) - 1)]
    print(f"  {name:30s}  n={n:3d}  min={s[0]:6.1f}  median={statistics.median(samples):6.1f}  p95={p95:6.1f}  max={s[-1]:6.1f}  ms")


# --- ipify + ipapi geolocation -------------------------------------------
def show_location():
    print("\n[geo] external IP and location")
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as r:
            ip = r.read().decode().strip()
        print(f"  IP: {ip}")
    except Exception as e:
        print(f"  ipify fail: {e}")
        return
    try:
        req = urllib.request.Request(f"https://ipapi.co/{ip}/json/",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.loads(r.read().decode())
        for k in ("country_name", "region", "city", "org"):
            print(f"  {k:13s}: {d.get(k)}")
    except Exception as e:
        print(f"  ipapi fail: {e}")


def show_ws_dns():
    print(f"\n[dns] {WS_HOST}")
    try:
        ips = sorted({a[4][0] for a in socket.getaddrinfo(WS_HOST, WS_PORT)})
        print(f"  resolved: {', '.join(ips)}")
    except Exception as e:
        print(f"  fail: {e}")


# --- Path A: official `websockets` library --------------------------------
async def test_with_websockets_lib(rounds=10):
    import websockets
    print(f"\n[ws] using websockets {websockets.__version__}")
    connect_t = []
    first_msg_t = []
    sub = json.dumps({"op": "subscribe",
                      "args": [{"channel": "tickers", "instId": SAMPLE_TICKER}]})
    for i in range(rounds):
        try:
            t0 = time.perf_counter()
            ws = await asyncio.wait_for(
                websockets.connect(f"wss://{WS_HOST}:{WS_PORT}{WS_PATH}"),
                timeout=10)
            connect_t.append((time.perf_counter() - t0) * 1000)
            t1 = time.perf_counter()
            await ws.send(sub)
            # First frame is usually the subscribe ack {"event":"subscribe", ...}
            ack = await asyncio.wait_for(ws.recv(), timeout=5)
            # Second frame is the first tickers payload
            first_data = await asyncio.wait_for(ws.recv(), timeout=10)
            first_msg_t.append((time.perf_counter() - t1) * 1000)
            await ws.close()
        except Exception as e:
            print(f"  round {i+1} fail: {e}")
        await asyncio.sleep(0.2)
    stats_line("WS connect", connect_t)
    stats_line("WS subscribe + 1st data", first_msg_t)
    return bool(connect_t)


# --- Path B: stdlib-only fallback ------------------------------------------
def _ws_handshake_and_subscribe(sock, sub_payload):
    """Minimal RFC6455 client handshake + send a single text frame."""
    import base64
    import os as _os
    key = base64.b64encode(_os.urandom(16)).decode()
    req = (
        f"GET {WS_PATH} HTTP/1.1\r\n"
        f"Host: {WS_HOST}:{WS_PORT}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    )
    sock.sendall(req.encode())

    # Read until end of headers
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("connection closed during handshake")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    if b"101" not in head.split(b"\r\n", 1)[0]:
        raise RuntimeError(f"handshake failed: {head[:200]!r}")

    # Send a masked text frame with the subscribe JSON
    payload = sub_payload.encode()
    mask = _os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    plen = len(payload)
    if plen < 126:
        hdr = struct.pack("!BB", 0x81, 0x80 | plen)
    elif plen < 65536:
        hdr = struct.pack("!BBH", 0x81, 0x80 | 126, plen)
    else:
        hdr = struct.pack("!BBQ", 0x81, 0x80 | 127, plen)
    sock.sendall(hdr + mask + masked)

    return rest  # any leftover bytes already in buffer


def _ws_recv_text(sock, leftover):
    """Read exactly one text frame. Returns (text, new_leftover)."""
    buf = bytearray(leftover)

    def need(n):
        nonlocal buf
        while len(buf) < n:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("connection closed")
            buf.extend(chunk)

    need(2)
    b1, b2 = buf[0], buf[1]
    masked = bool(b2 & 0x80)
    plen = b2 & 0x7F
    idx = 2
    if plen == 126:
        need(idx + 2)
        plen = struct.unpack("!H", bytes(buf[idx:idx+2]))[0]
        idx += 2
    elif plen == 127:
        need(idx + 8)
        plen = struct.unpack("!Q", bytes(buf[idx:idx+8]))[0]
        idx += 8
    if masked:
        need(idx + 4)
        mask = bytes(buf[idx:idx+4])
        idx += 4
    else:
        mask = None
    need(idx + plen)
    payload = bytes(buf[idx:idx+plen])
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    new_leftover = bytes(buf[idx+plen:])
    return payload.decode("utf-8", errors="replace"), new_leftover


def test_with_stdlib(rounds=8):
    print("\n[ws] stdlib fallback (no websockets lib)")
    connect_t = []
    first_msg_t = []
    sub = json.dumps({"op": "subscribe",
                      "args": [{"channel": "tickers", "instId": SAMPLE_TICKER}]})
    ctx = ssl.create_default_context()
    for i in range(rounds):
        try:
            t0 = time.perf_counter()
            raw = socket.create_connection((WS_HOST, WS_PORT), timeout=10)
            sock = ctx.wrap_socket(raw, server_hostname=WS_HOST)
            sock.settimeout(10)
            connect_t.append((time.perf_counter() - t0) * 1000)
            t1 = time.perf_counter()
            leftover = _ws_handshake_and_subscribe(sock, sub)
            # Drain ack first, then time first data frame
            text1, leftover = _ws_recv_text(sock, leftover)
            text2, leftover = _ws_recv_text(sock, leftover)
            first_msg_t.append((time.perf_counter() - t1) * 1000)
            sock.close()
        except Exception as e:
            print(f"  round {i+1} fail: {e}")
        time.sleep(0.2)
    stats_line("WS connect (stdlib)", connect_t)
    stats_line("WS handshake+1st (stdlib)", first_msg_t)


def main():
    print("=" * 72)
    print(f" OKX WebSocket latency probe   ({time.strftime('%Y-%m-%d %H:%M:%S')})")
    print(f" Python {sys.version.split()[0]}    executable={sys.executable}")
    print("=" * 72)
    show_location()
    show_ws_dns()

    used_lib = False
    try:
        import websockets  # noqa: F401
        used_lib = asyncio.run(test_with_websockets_lib(rounds=10))
    except ImportError:
        print("\n[ws] websockets not installed, using stdlib fallback")
    except Exception as e:
        print(f"\n[ws] websockets attempt errored: {e}")

    if not used_lib:
        test_with_stdlib(rounds=8)

    print("\n" + "=" * 72)
    print(" Reading the numbers")
    print("-" * 72)
    print(" WS connect          ~ matches REST first-byte (TLS + handshake)")
    print(" Subscribe + 1st msg ~ one round-trip to OKX backend")
    print(" If 1st msg <= 130ms : GOOD, sub-second strategies feasible")
    print(" If 1st msg <= 300ms : OK,  30M+ strategies feasible")
    print(" If 1st msg >  500ms : poor, only swing/daily horizons")
    print("=" * 72)


if __name__ == "__main__":
    main()

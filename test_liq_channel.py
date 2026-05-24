#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test which OKX WebSocket endpoint serves liquidation-orders.

Run on the server with:
    /root/.local/bin/python3.11 test_liq_channel.py

Tries 3 endpoints in sequence with the same subscribe payload:
  1. /ws/v5/public        (what we currently use, expected to fail post-2023)
  2. /ws/v5/business      (no auth — expected behaviour for public-readable channels)
  3. /ws/v5/business      (with auth login — fallback for private-only channels)

For each endpoint waits 30 seconds for either:
  - subscribe ack
  - any data event
  - error event

Then reports what happened.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
import ssl
import struct
import time
from datetime import datetime, timezone

WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
WS_BUSINESS = "wss://ws.okx.com:8443/ws/v5/business"
WS_PRIVATE = "wss://ws.okx.com:8443/ws/v5/private"

SUB_PAYLOAD = {
    "op": "subscribe",
    "args": [{"channel": "liquidation-orders", "instType": "SWAP"}],
}

OKX_CONFIG = os.path.expanduser("~/.okx/config.toml")


def load_creds():
    out = {"api_key": "", "secret_key": "", "passphrase": ""}
    try:
        with open(OKX_CONFIG, "r", encoding="utf-8") as f:
            for raw in f:
                ln = raw.strip()
                for k in out:
                    if ln.startswith(k):
                        out[k] = ln.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception as e:
        print(f"!! cred load failed: {e}")
    return out


# --- Minimal RFC6455 client (stdlib only) --------------------------------

async def _ws_handshake(reader, writer, host, port, path):
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    writer.write(req.encode())
    await writer.drain()
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = await asyncio.wait_for(reader.read(4096), timeout=10)
        if not chunk:
            raise RuntimeError("closed during handshake")
        head += chunk
    if b"101" not in head.split(b"\r\n", 1)[0]:
        raise RuntimeError(f"handshake rejected: {head[:200]!r}")


async def _send_text(writer, text):
    payload = text.encode()
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    plen = len(payload)
    if plen < 126:
        hdr = struct.pack("!BB", 0x81, 0x80 | plen)
    elif plen < 65536:
        hdr = struct.pack("!BBH", 0x81, 0x80 | 126, plen)
    else:
        hdr = struct.pack("!BBQ", 0x81, 0x80 | 127, plen)
    writer.write(hdr + mask + masked)
    await writer.drain()


async def _recv_text(reader):
    head = await reader.readexactly(2)
    b1, b2 = head[0], head[1]
    plen = b2 & 0x7F
    if plen == 126:
        plen = struct.unpack("!H", await reader.readexactly(2))[0]
    elif plen == 127:
        plen = struct.unpack("!Q", await reader.readexactly(8))[0]
    payload = await reader.readexactly(plen) if plen else b""
    return payload.decode("utf-8", errors="replace")


async def login(reader, writer, creds):
    """For /business and /private endpoints."""
    ts = str(int(time.time()))
    msg = ts + "GET" + "/users/self/verify"
    sig = base64.b64encode(
        hmac.new(creds["secret_key"].encode(), msg.encode(), "sha256").digest()
    ).decode()
    payload = {
        "op": "login",
        "args": [{
            "apiKey": creds["api_key"],
            "passphrase": creds["passphrase"],
            "timestamp": ts,
            "sign": sig,
        }],
    }
    await _send_text(writer, json.dumps(payload))
    # Wait for login ack
    for _ in range(10):
        text = await asyncio.wait_for(_recv_text(reader), timeout=10)
        if text == "pong":
            continue
        try:
            d = json.loads(text)
        except Exception:
            continue
        if d.get("event") == "login":
            return d.get("code") == "0", d
    return False, {"msg": "login timeout"}


async def test_endpoint(url, do_login=False):
    print("\n" + "=" * 64)
    print(f"  testing: {url}  login={do_login}")
    print("=" * 64)

    host_port, _, path = url.replace("wss://", "").partition("/")
    host, port = host_port.split(":")
    port = int(port)
    path = "/" + path
    ctx = ssl.create_default_context()

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname=host),
            timeout=10,
        )
        await _ws_handshake(reader, writer, host, port, path)
        print("  ✓ handshake OK")

        if do_login:
            creds = load_creds()
            if not creds["api_key"]:
                print("  !! no creds, skip login")
                writer.close()
                return
            ok, resp = await login(reader, writer, creds)
            if not ok:
                print(f"  ✗ login failed: {resp}")
                writer.close()
                return
            print(f"  ✓ login OK")

        await _send_text(writer, json.dumps(SUB_PAYLOAD))
        print(f"  → sent: {json.dumps(SUB_PAYLOAD)}")

        # Wait up to 30s for subscribe ack + first data
        deadline = time.time() + 30
        got_ack = False
        got_data = 0
        while time.time() < deadline:
            try:
                text = await asyncio.wait_for(_recv_text(reader), timeout=deadline - time.time())
            except asyncio.TimeoutError:
                break
            if text == "pong":
                continue
            try:
                msg = json.loads(text)
            except Exception:
                continue
            if msg.get("event") == "subscribe":
                got_ack = True
                print(f"  ✓ subscribe ack: {msg}")
            elif msg.get("event") == "error":
                print(f"  ✗ error: {msg}")
                break
            elif msg.get("data"):
                got_data += 1
                if got_data <= 2:
                    print(f"  ✓ DATA #{got_data}: {json.dumps(msg)[:200]}...")
        print(f"  summary: ack={got_ack} data_msgs={got_data}")

        writer.close()
    except Exception as e:
        print(f"  ✗ exception: {e}")


async def main():
    print("Probing OKX WebSocket endpoints for liquidation-orders channel")
    print(f"now: {datetime.now(timezone.utc).isoformat()}\n")
    await test_endpoint(WS_PUBLIC, do_login=False)
    await test_endpoint(WS_BUSINESS, do_login=False)
    await test_endpoint(WS_BUSINESS, do_login=True)


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hermes v10 — OKX REST + WebSocket client (async)

REST: signed via stdlib ssl + http.client (no requests dep needed, but
      uses requests if available for connection pooling).
WS:   subscribes to liquidation-orders, funding-rate, and per-symbol
      candle/trades. Auto-reconnect with exponential backoff, ping every
      25s (OKX requires <30s pings, server disconnects at 30s idle).

Endpoint references:
  https://www.okx.com/docs-v5/en/  (REST + WS V5 API)
  Public WS:  wss://ws.okx.com:8443/ws/v5/public
  Business:   wss://ws.okx.com:8443/ws/v5/business  (some channels live here)
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
from typing import Any, Awaitable, Callable, Dict, List, Optional

REST_BASE = "https://www.okx.com"
WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
WS_BUSINESS = "wss://ws.okx.com:8443/ws/v5/business"

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def load_creds(path: str = "~/.okx/config.toml") -> Dict[str, str]:
    path = os.path.expanduser(path)
    out = {"api_key": "", "secret_key": "", "passphrase": ""}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                ln = raw.strip()
                for k in out:
                    if ln.startswith(k):
                        out[k] = ln.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


# ---------------------------------------------------------------------------
# Signed REST (synchronous, used in thread pool)
# ---------------------------------------------------------------------------


class OKXRest:
    """
    Tiny signed REST client. Uses `requests` if available (connection pool),
    otherwise stdlib http.client. Methods are synchronous — call from a
    thread pool when used inside async code.
    """

    def __init__(self, creds: Optional[Dict[str, str]] = None):
        self.creds = creds or load_creds()
        try:
            import requests
            self._session = requests.Session()
            self._use_requests = True
        except ImportError:
            self._session = None
            self._use_requests = False

    @staticmethod
    def _ts() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _sign(self, ts: str, method: str, path: str, body: str) -> str:
        msg = ts + method + path + body
        return base64.b64encode(
            hmac.new(self.creds["secret_key"].encode(),
                     msg.encode(), "sha256").digest()
        ).decode()

    def _headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        ts = self._ts()
        return {
            "OK-ACCESS-KEY": self.creds["api_key"],
            "OK-ACCESS-SIGN": self._sign(ts, method, path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.creds["passphrase"],
            "Content-Type": "application/json",
        }

    def _do(self, method: str, path: str, body: Optional[Any] = None,
            signed: bool = False, timeout: float = 10.0) -> Dict[str, Any]:
        body_str = "" if body is None else (
            body if isinstance(body, str) else json.dumps(body)
        )
        headers = self._headers(method, path, body_str) if signed else {
            "Content-Type": "application/json",
        }
        url = REST_BASE + path
        for attempt in range(3):
            try:
                if self._use_requests:
                    if method == "GET":
                        r = self._session.get(url, headers=headers, timeout=timeout)
                    else:
                        r = self._session.post(url, headers=headers, data=body_str,
                                               timeout=timeout)
                    d = r.json()
                else:
                    import http.client as hc
                    conn = hc.HTTPSConnection("www.okx.com", 443, timeout=timeout)
                    conn.request(method, path, body=body_str, headers=headers)
                    resp = conn.getresponse()
                    d = json.loads(resp.read().decode("utf-8"))
                    conn.close()
                if d.get("code") == "50011":  # rate limit, back off
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return d
            except Exception as e:
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return {"code": "-1", "msg": str(e)}
        return {"code": "-1", "msg": "max retries"}

    # ---- Public ----
    def get(self, path: str, signed: bool = False) -> Dict[str, Any]:
        return self._do("GET", path, None, signed=signed)

    def post(self, path: str, body: Any, signed: bool = True) -> Dict[str, Any]:
        return self._do("POST", path, body, signed=signed)

    # ---- Convenience helpers ----
    def instruments_swap(self) -> List[Dict[str, Any]]:
        d = self.get("/api/v5/public/instruments?instType=SWAP")
        return d.get("data") or []

    def tickers_swap(self) -> List[Dict[str, Any]]:
        d = self.get("/api/v5/market/tickers?instType=SWAP")
        return d.get("data") or []

    def candles(self, inst_id: str, bar: str = "30m", limit: int = 100
                ) -> List[List[str]]:
        d = self.get(f"/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}")
        return d.get("data") or []

    def funding_rate(self, inst_id: str) -> Optional[float]:
        d = self.get(f"/api/v5/public/funding-rate?instId={inst_id}")
        rows = d.get("data") or []
        if rows:
            try:
                return float(rows[0]["fundingRate"])
            except (KeyError, TypeError, ValueError):
                pass
        return None

    def funding_rate_full(self, inst_id: str) -> Optional[Dict[str, Any]]:
        """
        Returns the full funding-rate row for `inst_id`, or None.
        Key fields:
          fundingRate        current rate
          nextFundingRate    estimated next rate
          fundingTime        ms timestamp of CURRENT settlement (already paid)
          nextFundingTime    ms timestamp of NEXT settlement (this is what we
                             must avoid holding through)
          minFundingRate     cap floor
          maxFundingRate     cap ceiling
          settState          'processing' / 'settled'
          formulaType        funding rate formula version
          period             settlement period descriptor (some tenants only)
        Each symbol can have its own period (1h / 2h / 4h / 8h), and OKX may
        switch a symbol's period dynamically when the rate hits the cap.
        Always trust nextFundingTime from this endpoint, not a hardcoded
        UTC schedule.
        """
        d = self.get(f"/api/v5/public/funding-rate?instId={inst_id}")
        rows = d.get("data") or []
        return rows[0] if rows else None

    def orderbook(self, inst_id: str, sz: int = 20) -> Optional[Dict[str, Any]]:
        d = self.get(f"/api/v5/market/books?instId={inst_id}&sz={sz}")
        rows = d.get("data") or []
        return rows[0] if rows else None

    def balance_usdt(self) -> float:
        d = self.get("/api/v5/account/balance?ccy=USDT", signed=True)
        try:
            det = d["data"][0]["details"][0]
            return float(det.get("availBal") or det.get("eq") or 0)
        except (KeyError, IndexError, TypeError, ValueError):
            return 0.0

    def positions(self) -> List[Dict[str, Any]]:
        d = self.get("/api/v5/account/positions", signed=True)
        return d.get("data") or []

    def position(self, inst_id: str) -> Optional[Dict[str, Any]]:
        d = self.get(f"/api/v5/account/positions?instId={inst_id}", signed=True)
        for p in d.get("data") or []:
            try:
                if p.get("instId") == inst_id and float(p.get("pos") or 0) != 0:
                    return p
            except (TypeError, ValueError):
                continue
        return None

    def positions_history(self, inst_id: str, limit: int = 5
                          ) -> List[Dict[str, Any]]:
        d = self.get(
            f"/api/v5/account/positions-history?instId={inst_id}&limit={limit}",
            signed=True,
        )
        return d.get("data") or []

    def set_leverage(self, inst_id: str, lever: int) -> Dict[str, Any]:
        return self.post("/api/v5/account/set-leverage", {
            "instId": inst_id, "lever": str(lever), "mgnMode": "cross"
        })

    def order_market(self, inst_id: str, side: str, sz: int,
                     reduce_only: bool = False) -> Dict[str, Any]:
        body = {
            "instId": inst_id, "tdMode": "cross",
            "side": side, "ordType": "market", "sz": str(sz),
        }
        if reduce_only:
            body["reduceOnly"] = True
        return self.post("/api/v5/trade/order", body)

    def order_algo_tp_sl(self, inst_id: str, close_side: str, sz: int,
                         tp_price: str, sl_price: str) -> Dict[str, Any]:
        return self.post("/api/v5/trade/order-algo", {
            "instId": inst_id, "tdMode": "cross",
            "side": close_side, "sz": str(sz),
            "ordType": "oco",
            "tpTriggerPx": tp_price, "tpOrdPx": "-1",
            "slTriggerPx": sl_price, "slOrdPx": "-1",
            "reduceOnly": True,
        })

    def cancel_algo(self, inst_id: str, algo_id: str) -> Dict[str, Any]:
        return self.post("/api/v5/trade/cancel-algos",
                         [{"instId": inst_id, "algoId": algo_id}])

    def pending_algos(self) -> List[Dict[str, Any]]:
        d = self.get("/api/v5/trade/orders-algo-pending?ordType=conditional,oco",
                     signed=True)
        return d.get("data") or []


# ---------------------------------------------------------------------------
# Async WebSocket client (stdlib only, RFC6455 hand-rolled if needed)
# ---------------------------------------------------------------------------


class OKXWebSocket:
    """
    Single asyncio task that maintains a long-lived public WS connection.

    - Subscribes to a configurable list of channels.
    - Auto-reconnects with exponential back-off (capped at 60s).
    - Sends a "ping" text every 25s; OKX times out at 30s.
    - On every received message, calls the user-provided async handler.
    """

    PING_INTERVAL = 25.0
    BACKOFF_INITIAL = 1.0
    BACKOFF_MAX = 60.0

    def __init__(self, url: str, handler: Callable[[Dict[str, Any]], Awaitable[None]],
                 on_log: Optional[Callable[[str], None]] = None) -> None:
        self.url = url
        self.handler = handler
        self.on_log = on_log or (lambda m: print(m, flush=True))
        self._subs: List[Dict[str, Any]] = []
        self._stop = False
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._last_send = 0.0
        self._connected = asyncio.Event()

    async def add_subscriptions(self, args: List[Dict[str, Any]]) -> None:
        # Append, avoiding duplicates
        for a in args:
            if a not in self._subs:
                self._subs.append(a)
        if self._connected.is_set():
            await self._send_json({"op": "subscribe", "args": args})

    async def _connect_once(self) -> None:
        # Parse wss://host:port/path
        host_port, _, path = self.url.replace("wss://", "").partition("/")
        if ":" in host_port:
            host, port_str = host_port.split(":")
            port = int(port_str)
        else:
            host, port = host_port, 443
        path = "/" + path
        ctx = ssl.create_default_context()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname=host),
            timeout=15,
        )
        # WebSocket handshake
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        writer.write(req.encode())
        await writer.drain()
        # Read response headers
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=10)
            if not chunk:
                raise ConnectionError("ws: closed during handshake")
            head += chunk
        if b"101" not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"ws: handshake rejected: {head[:200]!r}")
        self._reader = reader
        self._writer = writer
        self._last_send = time.monotonic()
        self._connected.set()
        self.on_log(f"[ws] connected {self.url}")
        # Resubscribe
        if self._subs:
            await self._send_json({"op": "subscribe", "args": list(self._subs)})

    async def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        if self._writer is None:
            raise ConnectionError("ws: not connected")
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        plen = len(payload)
        if plen < 126:
            hdr = struct.pack("!BB", 0x80 | opcode, 0x80 | plen)
        elif plen < 65536:
            hdr = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, plen)
        else:
            hdr = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, plen)
        self._writer.write(hdr + mask + masked)
        await self._writer.drain()
        self._last_send = time.monotonic()

    async def _send_json(self, obj: Any) -> None:
        await self._send_frame(json.dumps(obj).encode("utf-8"), 0x1)

    async def _send_text(self, s: str) -> None:
        await self._send_frame(s.encode("utf-8"), 0x1)

    async def _read_frame(self) -> Optional[bytes]:
        """Read one frame; returns payload bytes or None for close/control."""
        if self._reader is None:
            raise ConnectionError("ws: not connected")
        head = await self._reader.readexactly(2)
        b1, b2 = head[0], head[1]
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        plen = b2 & 0x7F
        if plen == 126:
            ext = await self._reader.readexactly(2)
            plen = struct.unpack("!H", ext)[0]
        elif plen == 127:
            ext = await self._reader.readexactly(8)
            plen = struct.unpack("!Q", ext)[0]
        if masked:
            mask = await self._reader.readexactly(4)
        payload = await self._reader.readexactly(plen) if plen else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x9:  # ping
            await self._send_frame(payload, 0xA)
            return None
        if opcode == 0xA:  # pong
            return None
        if opcode == 0x8:  # close
            raise ConnectionError("ws: close frame received")
        if opcode in (0x1, 0x2):  # text or binary -> data
            return payload
        return None

    async def _read_loop(self) -> None:
        while not self._stop:
            payload = await self._read_frame()
            if payload is None:
                continue
            text = payload.decode("utf-8", errors="replace").strip()
            if text == "pong":
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            try:
                await self.handler(msg)
            except Exception as e:
                self.on_log(f"[ws] handler error: {e}")

    async def _ping_loop(self) -> None:
        while not self._stop:
            await asyncio.sleep(self.PING_INTERVAL / 4.0)
            if not self._connected.is_set():
                continue
            if time.monotonic() - self._last_send >= self.PING_INTERVAL:
                try:
                    await self._send_text("ping")
                except Exception:
                    return  # let read loop notice the close

    async def run(self) -> None:
        backoff = self.BACKOFF_INITIAL
        while not self._stop:
            self._connected.clear()
            try:
                await self._connect_once()
                backoff = self.BACKOFF_INITIAL
                read_task = asyncio.create_task(self._read_loop())
                ping_task = asyncio.create_task(self._ping_loop())
                done, pending = await asyncio.wait(
                    [read_task, ping_task],
                    return_when=asyncio.FIRST_EXCEPTION,
                )
                for t in pending:
                    t.cancel()
                for t in done:
                    exc = t.exception()
                    if exc:
                        raise exc
            except Exception as e:
                self.on_log(f"[ws] error: {e}; reconnecting in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(self.BACKOFF_MAX, backoff * 2)
            finally:
                if self._writer is not None:
                    try:
                        self._writer.close()
                        await self._writer.wait_closed()
                    except Exception:
                        pass
                self._connected.clear()

    async def stop(self) -> None:
        self._stop = True
        if self._writer is not None:
            try:
                await self._send_frame(b"", 0x8)
            except Exception:
                pass


__all__ = ["OKXRest", "OKXWebSocket", "WS_PUBLIC", "WS_BUSINESS",
           "REST_BASE", "load_creds"]

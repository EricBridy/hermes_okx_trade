#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Zero-dependency OKX latency probe.
Works on Python 3.6+ with stdlib only — no pip needed.

Usage on the server:
    python3 test_okx_latency.py

What it measures:
  1. DNS resolution time
  2. TCP connect time
  3. TCP + TLS handshake time
  4. Full HTTP GET to /api/v5/public/time
  5. Full HTTP GET to /api/v5/market/ticker?instId=BTC-USDT-SWAP
  6. Server's external IP + geolocation (best-effort)
"""

from __future__ import print_function

import json
import socket
import ssl
import statistics
import sys
import time
from urllib.request import urlopen, Request

REST_HOST = "www.okx.com"
PING_ROUNDS = 20
SAMPLE_TICKER = "BTC-USDT-SWAP"


def stats_line(name, samples):
    if not samples:
        print("  {:30s}  no samples".format(name))
        return
    s = sorted(samples)
    n = len(s)
    p95_idx = max(0, int(n * 0.95) - 1)
    print("  {:30s}  n={:3d}  min={:6.1f}  median={:6.1f}  p95={:6.1f}  max={:6.1f}  ms".format(
        name, n, s[0], statistics.median(samples), s[p95_idx], s[-1]
    ))


def resolve_dns():
    print("\n[A] DNS resolution")
    samples = []
    ips = set()
    for _ in range(5):
        t0 = time.perf_counter()
        try:
            ip = socket.gethostbyname(REST_HOST)
            samples.append((time.perf_counter() - t0) * 1000)
            ips.add(ip)
        except Exception as e:
            print("  DNS fail: {}".format(e))
            return [], set()
    stats_line("DNS lookup", samples)
    print("  resolved IPs: {}".format(", ".join(sorted(ips))))
    return samples, ips


def test_tcp_only():
    print("\n[B] Pure TCP connect (no TLS)")
    samples = []
    for _ in range(PING_ROUNDS):
        t0 = time.perf_counter()
        try:
            sock = socket.create_connection((REST_HOST, 443), timeout=5)
            samples.append((time.perf_counter() - t0) * 1000)
            sock.close()
        except Exception as e:
            print("  TCP fail: {}".format(e))
    stats_line("TCP connect", samples)
    return samples


def test_tcp_tls():
    print("\n[C] TCP + TLS handshake")
    samples = []
    ctx = ssl.create_default_context()
    for _ in range(PING_ROUNDS):
        t0 = time.perf_counter()
        try:
            with socket.create_connection((REST_HOST, 443), timeout=5) as raw:
                with ctx.wrap_socket(raw, server_hostname=REST_HOST):
                    pass
            samples.append((time.perf_counter() - t0) * 1000)
        except Exception as e:
            print("  TLS fail: {}".format(e))
    stats_line("TCP+TLS handshake", samples)
    return samples


def test_rest(path, label):
    print("\n[{}] HTTPS GET {}".format(label, path))
    url = "https://{}{}".format(REST_HOST, path)
    samples = []
    for _ in range(PING_ROUNDS):
        t0 = time.perf_counter()
        try:
            req = Request(url, headers={"User-Agent": "okx-latency-probe/1.0"})
            with urlopen(req, timeout=5) as resp:
                resp.read()
            samples.append((time.perf_counter() - t0) * 1000)
        except Exception as e:
            print("  HTTP fail: {}".format(e))
    stats_line("HTTPS round-trip", samples)
    return samples


def get_external_ip_and_geo():
    print("\n[E] External IP + rough geolocation")
    try:
        with urlopen("https://api.ipify.org", timeout=5) as resp:
            ip = resp.read().decode().strip()
        print("  external IP: {}".format(ip))
    except Exception as e:
        print("  could not get IP: {}".format(e))
        return
    try:
        url = "https://ipapi.co/{}/json/".format(ip)
        with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=5) as resp:
            data = json.loads(resp.read().decode())
        print("  country: {}".format(data.get("country_name", "?")))
        print("  region:  {}".format(data.get("region", "?")))
        print("  city:    {}".format(data.get("city", "?")))
        print("  org/ISP: {}".format(data.get("org", "?")))
    except Exception as e:
        print("  geo lookup failed: {}".format(e))


def main():
    print("=" * 70)
    print(" OKX latency probe ({})".format(time.strftime("%Y-%m-%d %H:%M:%S")))
    print(" Python {}".format(sys.version.split()[0]))
    print("=" * 70)

    get_external_ip_and_geo()
    resolve_dns()
    test_tcp_only()
    test_tcp_tls()
    rest_time = test_rest("/api/v5/public/time", "D1")
    test_rest("/api/v5/market/ticker?instId={}".format(SAMPLE_TICKER), "D2")

    print("\n" + "=" * 70)
    print(" Reading the numbers")
    print("-" * 70)
    if rest_time:
        med = statistics.median(rest_time)
        if med <= 50:
            verdict = "EXCELLENT — co-located. Any strategy timeframe works."
        elif med <= 150:
            verdict = "GOOD — fine for >=1m bars, 5M+ very comfortable."
        elif med <= 300:
            verdict = "OK — stick to 15m+ timeframes; avoid sub-minute scalping."
        else:
            verdict = "SLOW — only 1h/daily/swing strategies are realistic."
        print(" REST median = {:.1f}ms  →  {}".format(med, verdict))
    print("=" * 70)


if __name__ == "__main__":
    main()

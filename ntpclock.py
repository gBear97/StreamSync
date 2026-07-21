"""Shared wall clock via SNTP.

Both session ends measure their offset against internet time servers and
timestamp everything in corrected UTC, so a host hint like "position
5017.2 at T" means the same instant on every machine. The system clock is
never changed - we only measure how far off it is.
"""

import socket
import struct
import time

NTP_SERVERS = ["time.windows.com", "pool.ntp.org", "time.google.com"]
NTP_EPOCH_DELTA = 2208988800  # seconds between 1900 (NTP) and 1970 (Unix)


def _query(server, timeout=2.0):
    """One SNTP exchange. Returns (offset_s, roundtrip_s)."""
    packet = b"\x1b" + 47 * b"\x00"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        t0 = time.time()
        s.sendto(packet, (server, 123))
        data, _ = s.recvfrom(512)
        t3 = time.time()
    if len(data) < 48:
        raise OSError("short NTP response")
    # receive (t1) and transmit (t2) timestamps from the server
    sec1, frac1 = struct.unpack("!II", data[32:40])
    sec2, frac2 = struct.unpack("!II", data[40:48])
    t1 = sec1 - NTP_EPOCH_DELTA + frac1 / 2**32
    t2 = sec2 - NTP_EPOCH_DELTA + frac2 / 2**32
    offset = ((t1 - t0) + (t2 - t3)) / 2
    return offset, (t3 - t0) - (t2 - t1)


class SharedClock:
    """UTC with a measured correction. utc() is comparable across machines
    that each ran sync() - typically within a few tens of milliseconds."""

    def __init__(self):
        self.offset = 0.0
        self.synced = False
        self.uncertainty = None

    def sync(self, samples=4):
        results = []
        for server in NTP_SERVERS:
            for _ in range(samples):
                try:
                    results.append(_query(server))
                except OSError:
                    break
            if len(results) >= samples:
                break
        if not results:
            raise OSError("No NTP server reachable - check the connection.")
        # the lowest-roundtrip sample has the least asymmetric-path error
        offset, rtt = min(results, key=lambda r: r[1])
        self.offset = offset
        self.uncertainty = rtt / 2
        self.synced = True
        return offset

    def utc(self):
        return time.time() + self.offset

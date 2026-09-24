"""Shared wall clock via SNTP.

Both session ends measure their offset against internet time servers and
timestamp everything in corrected UTC, so a host hint like "position
5017.2 at T" means the same instant on every machine. The system clock is
never changed - we only measure how far off it is.

The correction is measured against the monotonic clock, not time.time():
the OS's own time service steps the system clock whenever it decides it
has drifted, and a correction measured against the old system time was
wrong by exactly that step for the rest of the session. The monotonic
clock never steps, but its rate is only as good as the machine's crystal
(tens of ppm - a few hundred ms over a two-hour film), so sessions call
resync_every() to re-measure periodically.
"""

import socket
import struct
import threading
import time

NTP_SERVERS = ["time.google.com", "pool.ntp.org", "time.windows.com"]
NTP_EPOCH_DELTA = 2208988800  # seconds between 1900 (NTP) and 1970 (Unix)
RESYNC_INTERVAL = 600.0


def _query(server, timeout=2.0):
    """One SNTP exchange. Returns (offset_s, roundtrip_s), where offset
    maps time.monotonic() to UTC: utc = monotonic() + offset."""
    packet = b"\x1b" + 47 * b"\x00"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        t0 = time.monotonic()
        s.sendto(packet, (server, 123))
        data, _ = s.recvfrom(512)
        t3 = time.monotonic()
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

    def __init__(self, query=_query):
        self._query = query
        self.offset = time.time() - time.monotonic()   # until measured
        self.synced = False
        self.uncertainty = None
        self._stop = threading.Event()

    def sync(self, samples=4):
        results = []
        for server in NTP_SERVERS:
            for _ in range(samples):
                try:
                    results.append(self._query(server))
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

    def resync_every(self, interval=RESYNC_INTERVAL):
        """Re-measure in the background until stop(); a failed attempt
        keeps the last good correction."""
        def loop():
            while not self._stop.wait(interval):
                try:
                    self.sync()
                except OSError:
                    pass
        threading.Thread(target=loop, daemon=True).start()

    def stop(self):
        self._stop.set()

    def utc(self):
        return time.monotonic() + self.offset

    def perf_to_utc(self, perf):
        """UTC of a time.perf_counter() reading - how audio and player
        timestamps, which are perf_counter-based, join the shared clock."""
        return self.utc() - (time.perf_counter() - perf)

    def utc_to_perf(self, utc):
        return time.perf_counter() - (self.utc() - utc)

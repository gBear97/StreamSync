"""Non-invertible binary audio fingerprints for hosted sessions.

Philips-style robust hashing: each frame becomes 16 sign bits derived from
energy differences across neighboring frequency bands and consecutive
frames. The bits survive re-encoding, volume changes and mild EQ, but the
original audio cannot be reconstructed from them - only sign decisions
cross the wire, never spectra or samples. This is what hosts publish for
media verification and what both ends compute for voice-delay measurement.

Frame rate: one 16-bit word per FP_HOP seconds (4 words/second).
A whole 2-hour film fingerprints to ~56 KB.
"""

import numpy as np

from matcher import MatchError
import audio_matcher

FP_HOP = 0.25    # seconds per fingerprint word
FP_WIN = 0.50    # analysis window per frame
FP_BANDS = 17    # 17 bands -> 16 difference bits
F_LO, F_HI = 200.0, 4000.0

# empirically calibrated thresholds (bit error rate at best alignment)
VERIFY_BER = 0.32      # same media across encodes scores well under this
DELAY_BER = 0.42       # voice through a lossy stream is noisier
DELAY_MARGIN = 0.05    # best lag must beat the median by this much


def fingerprint_samples(x, sr):
    """float32 mono samples -> uint16 fingerprint words (one per FP_HOP)."""
    x = np.asarray(x, dtype=np.float32)
    win = int(round(FP_WIN * sr))
    hop = int(round(FP_HOP * sr))
    if x.size < win + hop:
        raise MatchError("Audio too short to fingerprint.")
    frames = np.lib.stride_tricks.sliding_window_view(x, win)[::hop]
    w = np.hanning(win).astype(np.float32)
    nfft = 1 << (win - 1).bit_length()
    edges = np.geomspace(F_LO, F_HI, FP_BANDS + 1)
    freqs = np.fft.rfftfreq(nfft, 1.0 / sr)
    idx = np.searchsorted(freqs, edges)

    energies = []
    for i0 in range(0, len(frames), 2048):
        mag = np.abs(np.fft.rfft(frames[i0:i0 + 2048] * w, n=nfft, axis=1))
        e = np.stack([(mag[:, idx[b]:max(idx[b + 1], idx[b] + 1)] ** 2).mean(axis=1)
                      for b in range(FP_BANDS)], axis=1)
        energies.append(np.log1p(e))
    E = np.concatenate(energies, axis=0)          # (T, FP_BANDS)
    if len(E) < 2:
        raise MatchError("Audio too short to fingerprint.")
    # bit[b] = sign of the band-difference delta between consecutive frames
    d = (E[1:, :-1] - E[1:, 1:]) - (E[:-1, :-1] - E[:-1, 1:])   # (T-1, 16)
    bits = (d > 0).astype(np.uint16)
    words = np.zeros(len(bits), dtype=np.uint16)
    for b in range(16):
        words |= bits[:, b] << b
    return words


def fingerprint_file(path, t0=None, t1=None):
    """Fingerprint a media file's audio track (or a range of it)."""
    duration, _start = audio_matcher.probe(path)
    a = 0.0 if t0 is None else max(0.0, t0)
    b = duration if t1 is None else min(duration, t1)
    x = audio_matcher.decode_audio(path, a, b - a)
    return fingerprint_samples(x, audio_matcher.SR)


_POPCOUNT = np.array([bin(i).count("1") for i in range(65536)], dtype=np.uint8)


def ber_at(a, b):
    """Bit error rate between two equal-length word arrays."""
    n = min(len(a), len(b))
    if n == 0:
        return 1.0
    return float(_POPCOUNT[np.bitwise_xor(a[:n], b[:n])].sum()) / (16.0 * n)


def best_align(ref, probe, max_lag=None, lags=None):
    """Slide `probe` along `ref`; return (lag_words, ber, median_ber).

    lag_words > 0 means probe content appears `lag * FP_HOP` seconds into
    ref. median_ber shows what chance alignment looks like, so callers can
    demand a real margin. `lags` restricts the search to those lags.
    """
    ref = np.asarray(ref, dtype=np.uint16)
    probe = np.asarray(probe, dtype=np.uint16)
    if len(probe) < 8 or len(ref) < len(probe):
        raise MatchError("Not enough fingerprint data to align.")
    n = len(ref) - len(probe) + 1
    if max_lag is not None:
        n = min(n, max_lag)
    bers = _bers(ref, probe, n)
    if lags is not None:
        lags = [k for k in lags if 0 <= k < n]
        if not lags:
            raise MatchError("Not enough fingerprint data to align.")
        k = min(lags, key=lambda i: bers[i])
        return k, float(bers[k]), float(np.median(bers))
    best = int(np.argmin(bers))
    return best, float(bers[best]), float(np.median(bers))


def _bers(ref, probe, n):
    """BER of `probe` at each of the first n lags into `ref`, vectorized."""
    windows = np.lib.stride_tricks.sliding_window_view(ref, len(probe))[:n]
    out = np.empty(n, dtype=np.float32)
    for i0 in range(0, n, 4096):          # bounded memory on whole films
        x = np.bitwise_xor(windows[i0:i0 + 4096], probe)
        out[i0:i0 + 4096] = _POPCOUNT[x].sum(axis=1, dtype=np.uint32)
    return out / (16.0 * len(probe))


FINE_PHASES = 10   # sub-hop steps tried: FP_HOP / 10 = 25 ms


def fine_align(ref, x, sr, phases=FINE_PHASES):
    """Where raw audio `x` starts inside fingerprint `ref`, in seconds.

    Fingerprint words are FP_HOP (250 ms) apart, so aligning words to
    words can only answer in 250 ms steps - measured errors of up to
    +-125 ms on a stream delay. The side holding the raw audio can do
    better without the other side sending anything more: re-fingerprint
    its audio starting at sub-hop offsets, and the offset whose words
    line up best says where between two of ref's words the audio really
    starts. A parabola through the best offset and its neighbours refines
    it further.

    Returns (seconds into ref where x[0] falls, ber there, median_ber).
    """
    hop = FP_HOP
    step = hop / phases
    coarse = fingerprint_samples(x, sr)
    lag0, _, median = best_align(ref, coarse)
    points = {}                         # position (s) -> ber
    for i in range(phases):
        cut = int(round(i * step * sr))
        words = fingerprint_samples(x[cut:], sr)
        # x[cut] sits at ref time lag*hop, so x[0] sits `cut` earlier
        for lag in (lag0 - 1, lag0, lag0 + 1):
            if 0 <= lag <= len(ref) - len(words):
                ber = float(_bers(ref[lag:], words, 1)[0])
                points[round(lag * hop - i * step, 6)] = ber
    pos = sorted(points)
    bers = [points[p] for p in pos]
    k = int(np.argmin(bers))
    best, ber = pos[k], bers[k]
    if 0 < k < len(pos) - 1:
        a, b, c = bers[k - 1], bers[k], bers[k + 1]
        denom = a - 2 * b + c
        if denom > 1e-9:
            best += float(np.clip(0.5 * (a - c) / denom, -1, 1)) * step
    return best, ber, median


def words_to_bytes(words):
    return np.asarray(words, dtype="<u2").tobytes()


def words_from_bytes(data):
    return np.frombuffer(data, dtype="<u2").copy()

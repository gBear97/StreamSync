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
    duration, start = audio_matcher.probe(path)
    a = 0.0 if t0 is None else max(0.0, t0)
    b = duration if t1 is None else min(duration, t1)
    x = audio_matcher.decode_audio(path, start + a, b - a)
    return fingerprint_samples(x, audio_matcher.SR)


_POPCOUNT = np.array([bin(i).count("1") for i in range(65536)], dtype=np.uint8)


def ber_at(a, b):
    """Bit error rate between two equal-length word arrays."""
    n = min(len(a), len(b))
    if n == 0:
        return 1.0
    return float(_POPCOUNT[np.bitwise_xor(a[:n], b[:n])].sum()) / (16.0 * n)


def best_align(ref, probe, max_lag=None):
    """Slide `probe` along `ref`; return (lag_words, ber, median_ber).

    lag_words > 0 means probe content appears `lag * FP_HOP` seconds into
    ref. median_ber shows what chance alignment looks like, so callers can
    demand a real margin.
    """
    ref = np.asarray(ref, dtype=np.uint16)
    probe = np.asarray(probe, dtype=np.uint16)
    if len(probe) < 8 or len(ref) < len(probe):
        raise MatchError("Not enough fingerprint data to align.")
    lags = len(ref) - len(probe) + 1
    if max_lag is not None:
        lags = min(lags, max_lag)
    bers = np.empty(lags, dtype=np.float32)
    for lag in range(lags):
        bers[lag] = ber_at(ref[lag:lag + len(probe)], probe)
    best = int(np.argmin(bers))
    return best, float(bers[best]), float(np.median(bers))


def words_to_bytes(words):
    return np.asarray(words, dtype="<u2").tobytes()


def words_from_bytes(data):
    return np.frombuffer(data, dtype="<u2").copy()

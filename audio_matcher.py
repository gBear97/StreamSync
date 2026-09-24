"""Audio-based matching: find where captured stream audio occurs in a file.

The system-audio capture contains the film's soundtrack with the streamer's
commentary mixed on top. Log band-energy features plus normalized
cross-correlation still peak at the right offset as long as the film audio
is audible under the voice - correlation only needs a fraction of the
spectrum to line up.
"""

import collections
import os
import subprocess
import threading

import numpy as np
import imageio_ffmpeg

from matcher import MatchError, probe, _creationflags

SR = 16000            # analysis sample rate for file audio
WIN_S = 0.064         # STFT window
HOP_S = 0.016         # feature frame step -> 16 ms timing resolution
N_BANDS = 26
F_LO, F_HI = 80.0, 7200.0
CHUNK_S = 900.0       # decode long windows in chunks this big
OVERLAP_S = 8.0

CACHE_CHUNKS = 12     # decoded feature chunks kept (~6 MB each at 900 s)

# gates used by callers to decide whether a peak is trustworthy
Z_OK = 6.0            # peak must stand this many sigmas above the score curve
SCORE_OK = 0.10       # and reach this normalized correlation


def decode_audio(path, t0, dur, sr=SR):
    """Mono float32 PCM of [t0, t0+dur] on the player timeline.

    The player timeline starts at 0 however the container is stamped: an
    .m2ts can begin at 600 s and an MP4 at a few seconds. ffmpeg's input
    -ss is already relative to the container start, so t0 goes in as-is -
    adding the start time here once shifted every match by exactly that
    much, and sent 600-s-start files looking past their own end.

    aresample pins the samples to their timestamps, so an audio track that
    starts late (or has a gap) is padded with silence rather than slid
    earlier - a slide would shift the match by the same amount.
    """
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostats",
           "-ss", f"{max(t0, 0.0):.3f}", "-i", path, "-t", f"{dur:.3f}",
           "-vn", "-sn", "-ac", "1", "-ar", str(sr),
           "-af", "aresample=async=1:first_pts=0", "-f", "f32le", "-"]
    out = subprocess.run(cmd, capture_output=True, creationflags=_creationflags())
    x = np.frombuffer(out.stdout, dtype=np.float32)
    if x.size < sr:
        raise MatchError("Could not decode audio from the file in that range.")
    return x


def features(x, sr):
    """(T, N_BANDS) z-scored log band energies, one row per HOP_S seconds."""
    win = int(round(WIN_S * sr))
    hop = int(round(HOP_S * sr))
    nfft = 1 << (win - 1).bit_length()
    if x.size < win + hop:
        raise MatchError("Audio clip too short to analyze.")
    frames = np.lib.stride_tricks.sliding_window_view(x, win)[::hop]
    w = np.hanning(win).astype(np.float32)
    edges = np.geomspace(F_LO, F_HI, N_BANDS + 1)
    freqs = np.fft.rfftfreq(nfft, 1.0 / sr)
    idx = np.searchsorted(freqs, edges)

    feats = []
    for i0 in range(0, len(frames), 4096):  # chunked to bound FFT memory
        mag = np.abs(np.fft.rfft(frames[i0:i0 + 4096] * w, n=nfft, axis=1))
        bands = np.stack(
            [mag[:, idx[b]:max(idx[b + 1], idx[b] + 1)].mean(axis=1)
             for b in range(N_BANDS)], axis=1)
        feats.append(np.log1p(bands.astype(np.float32)))
    X = np.concatenate(feats, axis=0)
    X -= X.mean(axis=0, keepdims=True)          # level-invariant
    X /= (X.std(axis=0, keepdims=True) + 1e-6)
    return X


def resample(x, sr_in, sr_out=SR):
    """Band-limited resample by FFT truncation - exact for a few seconds
    of audio, and numpy-only."""
    x = np.asarray(x, dtype=np.float32)
    if sr_in == sr_out or x.size == 0:
        return x
    n_out = int(round(x.size * sr_out / sr_in))
    spec = np.fft.rfft(x.astype(np.float64))
    keep = n_out // 2 + 1
    if keep <= spec.size:
        spec = spec[:keep]
    else:
        spec = np.concatenate([spec, np.zeros(keep - spec.size, spec.dtype)])
    return (np.fft.irfft(spec, n_out) * (n_out / x.size)).astype(np.float32)


def prep_capture(samples, sr):
    """Feature block for a loopback recording (any sample rate).

    Resampled to the rate the file is decoded at first, so both sides go
    through identical FFT sizes and band edges: at 48 kHz the lowest bands
    covered different bins than at 16 kHz, a small mismatch in exactly the
    bands a film's score lives in.
    """
    return features(resample(samples, sr, SR), SR)


_cache = collections.OrderedDict()
_cache_lock = threading.Lock()


def _file_features(path, seg0, seg1):
    """features() of the file's audio over [seg0, seg1], cached.

    A weak match is retried with a longer recording over the same window,
    and decoding is most of a search's cost - a whole film takes tens of
    seconds - so the second and third look reuse the first one's decode.
    """
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        stamp = None
    key = (path, stamp, round(seg0, 3), round(seg1, 3))
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    W = features(decode_audio(path, seg0, seg1 - seg0), SR)
    with _cache_lock:
        _cache[key] = W
        while len(_cache) > CACHE_CHUNKS:
            _cache.popitem(last=False)
    return W


def _corr_scores(W, C):
    """Normalized correlation of capture C (L,B) at every lag inside W (T,B).

    Each candidate stretch of W is centered on its own per-band mean
    before it is compared. W is z-scored over the whole search window, so
    a quiet scene sits far below that window's mean; left uncentered, that
    offset inflates the denominator and crushes the score of the right
    answer - measured at 0.10-0.14 (below the trust gates) against
    0.5-0.7 centered, for a quiet scene inside a loud window. C is already
    centered, so the numerator needs no change: a constant offset in W
    correlates with it to exactly zero.
    """
    T, B = W.shape
    L = C.shape[0]
    if T < L + 4:
        raise MatchError("Search window is shorter than the recording.")
    W = W.astype(np.float64)
    n = 1 << int(np.ceil(np.log2(T + L)))
    num = np.zeros(T - L + 1, dtype=np.float64)
    for b in range(B):
        fa = np.fft.rfft(W[:, b], n)
        fb = np.fft.rfft(C[::-1, b], n)
        num += np.fft.irfft(fa * fb, n)[L - 1:T]
    zero = np.zeros((1, B))
    s1 = np.concatenate([zero, np.cumsum(W, axis=0)])
    s2 = np.concatenate([zero, np.cumsum(W * W, axis=0)])
    win_sum = s1[L:] - s1[:-L]                       # (T-L+1, B)
    win_sq = s2[L:] - s2[:-L]
    win_var = np.maximum((win_sq - win_sum * win_sum / L).sum(axis=1), 0.0)
    denom = np.sqrt(win_var * float((C * C).sum())) + 1e-9
    return num / denom


def find_match_audio(path, capture_feats, t0=None, t1=None, progress=None):
    """Locate the recording inside `path`'s audio track.

    Returns (time_on_player_timeline, score, peak_z). Callers should treat
    the result as unreliable when score < SCORE_OK or peak_z < Z_OK.
    """
    progress = progress or (lambda msg: None)
    duration, _start = probe(path)
    t0 = 0.0 if t0 is None else max(0.0, min(t0, duration))
    t1 = duration if t1 is None else max(0.0, min(t1, duration))
    if t1 - t0 < 8.0:
        t0, t1 = max(0.0, t0 - 8.0), min(duration, t1 + 8.0)

    best = None  # (time, score, z)
    seg0 = t0
    while seg0 < t1:
        seg1 = min(seg0 + CHUNK_S, t1)
        if seg1 - seg0 >= 4.0:
            progress(f"Listening through {int(seg0) // 60}:{int(seg0) % 60:02d}"
                     f" - {int(seg1) // 60}:{int(seg1) % 60:02d}...")
            W = _file_features(path, seg0, seg1)
            try:
                scores = _corr_scores(W, capture_feats)
            except MatchError:
                break
            i = int(np.argmax(scores))
            z = float((scores[i] - scores.mean()) / (scores.std() + 1e-9))
            # parabolic interpolation for sub-hop timing
            d = 0.0
            if 0 < i < len(scores) - 1:
                a, b, c = scores[i - 1], scores[i], scores[i + 1]
                denom = a - 2 * b + c
                if abs(denom) > 1e-12:
                    d = float(np.clip(0.5 * (a - c) / denom, -1.0, 1.0))
            t_match = seg0 + (i + d) * HOP_S
            if best is None or scores[i] > best[1]:
                best = (t_match, float(scores[i]), z)
        if seg1 >= t1:
            break
        seg0 = seg1 - OVERLAP_S
    if best is None:
        raise MatchError("Audio scan produced no candidates.")
    return best

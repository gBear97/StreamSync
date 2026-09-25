"""Audio-based matching: find where captured stream audio occurs in a file.

The system-audio capture contains the film's soundtrack with the streamer's
commentary mixed on top. Log band-energy features plus normalized
cross-correlation still peak at the right offset as long as the film audio
is audible under the voice - correlation only needs a fraction of the
spectrum to line up.

When the commentary is loud, most of the capture is voice and the film
survives mainly in the gaps between words and sentences - where a
streamer's sidechain ducking has also let the film back up. Two views of
the capture look for those gaps, and the search adds up their evidence:

  1. voice-activity weighting: every capture frame is weighted by how
     quiet it is next to the capture's quieter frames, and the whole
     capture is compared with a weighted normalized correlation
     (_corr_scores) - one smooth curve that keeps slow structure;
  2. short sub-segments: the capture is cut into 128 ms pieces, each is
     correlated on its own (centered and normalized locally on both
     sides), and at every lag the best half of them are averaged
     (_segment_scores) - a sharp curve that picks up whatever gaps line
     up at that lag, however loud the rest of the capture is.

Each curve is standardized over the search window, the weighted one
counts VOICE_MIX times, and the sum, standardized again, is the score
curve: how many sigmas a lag stands above the search as a whole.
"""

import collections
import math
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
OVERLAP_S = 20.0      # chunks overlap by at least this (see find_match_audio)

# Decoded feature chunks kept (see _file_features): as many as a whole-film
# search of a LONG_FILM_S film cuts, so the weak-match retries over it
# reuse the first look's decode. The search visits its chunks in order,
# so a cache even one chunk short misses on every one of them. A 900 s
# chunk is 56,247 frames x 26 bands of float32, 5.85 MB: 17 of them, about
# 100 MB at most, cover films up to 4 h 9 m.
LONG_FILM_S = 4 * 3600.0
CACHE_CHUNKS = 1 + math.ceil((LONG_FILM_S - CHUNK_S) / (CHUNK_S - OVERLAP_S))

# Voice-activity weights (see voice_weights).
VOICE_REF_PCT = 20.0  # the capture's quietest fifth sets the reference level
VOICE_POW = 2.0       # weight = (reference power / frame power) ** VOICE_POW
VOICE_LEAK_HOPS = 2   # a frame counts as at least VOICE_LEAK x as loud as
VOICE_LEAK = 0.3      # the loudest frame within this many hops of it
SILENT_DB = 70.0      # frames this far under the loudest one carry nothing
W_VAR_FLOOR = 1e-3    # guards the variance of a (digitally) silent stretch of file

# Sub-segments (see _segment_scores).
SEG_FRAMES = 8        # 128 ms pieces of the capture ...
SEG_STEP = 4          # ... starting every 64 ms

VOICE_MIX = 2.0       # weight of the voice-weighted curve in the sum
RUNNER_EXCL = 32      # hops (0.5 s) around the best peak that are its own

# Gates used by callers to decide whether a peak is trustworthy, both in
# sigmas of the combined score curve:
#   z      how far the best peak stands above the search as a whole;
#   score  how far it stands above the best competing peak more than
#          0.5 s away (in this search or another chunk of it) - a clear
#          winner, not one of two look-alikes.
# Tuned on bench_audio.py dev seeds only (11, 21-24: loud and all
# speech-to-film ratios, windowed and whole-film searches, ~1050 trials)
# and on the same captures searched in a window of the same film that
# leaves the true moment out (or the other film, whole). Wrong peaks
# with the truth in the window reached z 4.9; with the truth left out,
# z >= 7 passed 4 of ~1050 searches, all at places where the film's
# soundtrack itself repeats (the previous matcher's gates passed 17).
# Confirmed on held-out seeds 14-16 (864 trials: loud, all ratios, whole
# film): 96.4-98.6% trusted and right, no false accepts, closest wrong
# peak z 5.2.
Z_OK = 7.0
SCORE_OK = 1.0


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


def _band_mags(x, sr):
    """(T, N_BANDS) mean STFT magnitude per band, one row per HOP_S."""
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

    out = []
    for i0 in range(0, len(frames), 4096):  # chunked to bound FFT memory
        mag = np.abs(np.fft.rfft(frames[i0:i0 + 4096] * w, n=nfft, axis=1))
        out.append(np.stack(
            [mag[:, idx[b]:max(idx[b + 1], idx[b] + 1)].mean(axis=1)
             for b in range(N_BANDS)], axis=1).astype(np.float32))
    return np.concatenate(out, axis=0)


def _shape(bm):
    """Log band energies with each frame's own mean across bands removed.

    What is left is the spectrum's shape, not its loudness. A streamer's
    sidechain ducking and bus compression move the whole film up and down
    together many times a second - on the real-audio benchmark
    (bench_audio.py) this took correct, trusted matches from 42% to 61-65%
    of trials with no false accepts, and flattened the broad hills in the
    score curve that made correct peaks look weak.
    """
    X = np.log1p(bm)
    X -= X.mean(axis=1, keepdims=True)
    return X


def features(x, sr):
    """(T, N_BANDS) z-scored log band energies, one row per HOP_S seconds."""
    X = _shape(_band_mags(x, sr))
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


def voice_weights(bm):
    """Per-frame weights (0..1) for a capture's band magnitudes (T, B).

    Commentary is loud and intermittent: while the streamer talks the
    frame is mostly voice (and sidechain ducking pushes the film further
    down), in the gaps between words and sentences the film is back on its
    own. So a frame's weight is the capture's quiet reference level - the
    VOICE_REF_PCT-th percentile of frame power - over the frame's own
    power, raised to VOICE_POW and capped at 1: the quietest frames count
    fully, a frame 10 dB louder than them counts 1%. A quiet frame right
    next to a loud one is judged by VOICE_LEAK of its neighbour's power
    too: the analysis windows overlap, and the codec smears a word's edges
    into the gap. Without commentary the weights just lean toward the
    quieter frames of the film, which match about as well. Frames
    SILENT_DB under the loudest one (dropouts, digital silence) carry no
    film and get weight 0.
    """
    P = (bm.astype(np.float64) ** 2).sum(axis=1)
    top = float(P.max()) if P.size else 0.0
    if top <= 0.0:
        return np.ones(len(P))
    live = P > top * 10.0 ** (-SILENT_DB / 10.0)
    if live.sum() < 8:
        return np.ones(len(P))
    k = VOICE_LEAK_HOPS
    near = np.lib.stride_tricks.sliding_window_view(
        np.pad(P, k, mode="edge"), 2 * k + 1).max(axis=1)
    P = np.maximum(P, VOICE_LEAK * near)
    ref = np.percentile(P[live], VOICE_REF_PCT)
    w = np.minimum(1.0, (ref / np.maximum(P, 1e-30)) ** VOICE_POW)
    w[~live] = 0.0
    return w


class CaptureFeatures:
    """prep_capture's result: capture features (L, N_BANDS), z-scored per
    band under the voice weights, and those per-frame weights (L,)."""

    __slots__ = ("feats", "weights")

    def __init__(self, feats, weights):
        self.feats = feats
        self.weights = weights

    @property
    def shape(self):
        return self.feats.shape

    def __len__(self):
        return len(self.feats)


def prep_capture(samples, sr):
    """Weighted feature block for a loopback recording (any sample rate).

    Resampled to the rate the file is decoded at first, so both sides go
    through identical FFT sizes and band edges: at 48 kHz the lowest bands
    covered different bins than at 16 kHz, a small mismatch in exactly the
    bands a film's score lives in.
    """
    bm = _band_mags(resample(samples, sr, SR), SR)
    w = voice_weights(bm)
    X = _shape(bm).astype(np.float64)
    sw = float(w.sum())
    X -= (w[:, None] * X).sum(axis=0) / sw
    X /= np.sqrt((w[:, None] * X * X).sum(axis=0) / sw) + 1e-6
    return CaptureFeatures(X, w)


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


def _corr_scores(W, C, w=None):
    """Weighted normalized correlation of capture C (L,B) at every lag
    inside W (T,B), with per-frame capture weights w (L,) (default: all 1).

    score(i) = sum_t w_t <C_t, W_i+t - m_i> /
               sqrt(sum_t w_t |W_i+t - m_i|^2 * sum_t w_t |C_t|^2)
    where m_i is the w-weighted per-band mean of the candidate stretch.
    Each candidate stretch is centered on its own mean before it is
    compared: W is z-scored over the whole search window, so a quiet scene
    sits far below that window's mean, and left uncentered that offset
    inflates the denominator and crushes the score of the right answer.
    C is already w-centered, so the numerator needs no change: a constant
    offset in W correlates with it to exactly zero. With all weights 1
    this is the plain local-centered NCC.
    """
    T, B = W.shape
    L = C.shape[0]
    if T < L + 4:
        raise MatchError("Search window is shorter than the recording.")
    w = np.ones(L) if w is None else np.asarray(w, dtype=np.float64)
    sw = float(w.sum())
    W = W.astype(np.float64)
    n = 1 << int(np.ceil(np.log2(T + L)))
    FW = np.fft.rfft(W, n, axis=0)                               # (n/2+1, B)
    FC = np.fft.rfft((C * w[:, None])[::-1], n, axis=0)
    num = np.fft.irfft((FW * FC).sum(axis=1), n)[L - 1:T]
    Fw = np.fft.rfft(w[::-1], n)
    s1 = np.fft.irfft(FW * Fw[:, None], n, axis=0)[L - 1:T]      # sum_t w W
    del FW
    s2 = np.fft.irfft(np.fft.rfft((W * W).sum(axis=1), n) * Fw, n)[L - 1:T]
    var = s2 - (s1 * s1).sum(axis=1) / sw
    var = np.maximum(var, W_VAR_FLOOR * sw * B)
    denom = np.sqrt(var * float((w[:, None] * C * C).sum())) + 1e-9
    return num / denom


def _segment_scores(W, C):
    """Robust sub-segment score over every lag of capture C (L,B) in W (T,B).

    Each SEG_FRAMES piece of C (every SEG_STEP frames) is centered per band
    and correlated against every same-length stretch of W, which is
    centered and normalized on its own - one matmul against W's sliding
    windows. The per-piece curves are aligned by the capture's start lag
    and standardized over the search; at every lag the best half of them
    are averaged. A piece the voice swamped is noise at the true lag and
    falls into the discarded half there instead of pulling the average
    down. Variances are floored a hair above zero so digital silence reads
    as no match instead of 0/0.
    """
    T, B = W.shape
    L = C.shape[0]
    S = min(SEG_FRAMES, L)
    if T < L + 4:
        raise MatchError("Search window is shorter than the recording.")
    starts = np.arange(0, L - S + 1, SEG_STEP)
    K = len(starts)
    segs = np.stack([C[s:s + S].T for s in starts]).astype(np.float64)
    segs -= segs.mean(axis=2, keepdims=True)                     # (K, B, S)
    norms = np.sqrt((segs * segs).sum(axis=(1, 2)))
    norms = np.maximum(norms, 1e-3 * np.median(norms) + 1e-6)
    tmpl = (segs / norms[:, None, None]).reshape(K, B * S).astype(np.float32)
    Wf = np.ascontiguousarray(W, dtype=np.float32)
    V = np.lib.stride_tricks.sliding_window_view(Wf, S, axis=0)  # (P, B, S)
    P = T - S + 1
    num = np.empty((K, P), dtype=np.float32)
    for i0 in range(0, P, 16384):                                # bound memory
        blk = V[i0:i0 + 16384].reshape(-1, B * S)
        num[:, i0:i0 + blk.shape[0]] = tmpl @ blk.T
    W64 = W.astype(np.float64)
    zero = np.zeros((1, B))
    s1 = np.concatenate([zero, np.cumsum(W64, axis=0)])
    s2 = np.concatenate([zero, np.cumsum(W64 * W64, axis=0)])
    ws = s1[S:] - s1[:-S]
    wvar = np.maximum(((s2[S:] - s2[:-S]) - ws * ws / S).sum(axis=1), 0.0)
    wvar = np.maximum(wvar, 1e-3 * np.median(wvar) + 1e-6 * S * B)
    num *= (1.0 / np.sqrt(wvar)).astype(np.float32)
    Tl = T - L + 1
    for k, s in enumerate(starts):       # align by the capture's start lag,
        num[k, :Tl] = num[k, s:s + Tl]   # in place to spare a second copy
    R = num[:, :Tl]                                              # (K, Tl)
    R -= R.mean(axis=1, keepdims=True)
    R /= R.std(axis=1, keepdims=True) + 1e-9
    m = (K + 1) // 2
    if m < K:
        R.partition(K - m, axis=0)
    return R[K - m:].mean(axis=0, dtype=np.float64)


def _std(x):
    return (x - x.mean()) / (x.std() + 1e-9)


def _score_curve(W, C, w):
    """Combined score curve (in sigmas over the search) at every lag."""
    return _std(_std(_segment_scores(W, C))
                + VOICE_MIX * _std(_corr_scores(W, C, w)))


def find_match_audio(path, capture_feats, t0=None, t1=None, progress=None):
    """Locate the recording inside `path`'s audio track.

    `capture_feats` is prep_capture's result (a plain (L, N_BANDS) feature
    array is also accepted and weighted uniformly). Returns
    (time_on_player_timeline, score, peak_z): peak_z is how far the peak
    stands above the combined score curve, score how far above the best
    competing peak (both in sigmas of that curve). Callers should treat
    the result as unreliable when score < SCORE_OK or peak_z < Z_OK.
    """
    C = np.asarray(getattr(capture_feats, "feats", capture_feats),
                   dtype=np.float64)
    w = getattr(capture_feats, "weights", None)
    progress = progress or (lambda msg: None)
    duration, _start = probe(path)
    t0 = 0.0 if t0 is None else max(0.0, min(t0, duration))
    t1 = duration if t1 is None else max(0.0, min(t1, duration))
    if t1 - t0 < 8.0:
        t0, t1 = max(0.0, t0 - 8.0), min(duration, t1 + 8.0)

    # A capture is only found where it fits entirely inside one chunk: a
    # chunk covers starts up to its end minus the capture's length, so the
    # next chunk has to begin at least that far back, or every boundary
    # hides a band no chunk can reach - and the search answers from
    # somewhere else entirely. A fixed 8 s left 10 s of every boundary of a
    # whole-film search blind to the 18 s retry listen, the one a first
    # sync applies even when weak. OVERLAP_S covers every listen the
    # controller makes, so its 6/12/18 s looks cut the same chunks and
    # reuse one decode (_file_features); only a longer capture widens it
    # (to at most half a chunk, so the scan always moves on).
    overlap = min(max(OVERLAP_S, len(C) * HOP_S + 1.0), 0.5 * CHUNK_S)

    cands = []  # (time, peak z, best z more than RUNNER_EXCL away) per chunk
    seg0 = t0
    while seg0 < t1:
        seg1 = min(seg0 + CHUNK_S, t1)
        if seg1 - seg0 >= 4.0:
            progress(f"Listening through {int(seg0) // 60}:{int(seg0) % 60:02d}"
                     f" - {int(seg1) // 60}:{int(seg1) % 60:02d}...")
            W = _file_features(path, seg0, seg1)
            try:
                scores = _score_curve(W, C, w)
            except MatchError:
                break
            i = int(np.argmax(scores))
            lo, hi = max(0, i - RUNNER_EXCL), i + RUNNER_EXCL + 1
            rest = np.concatenate([scores[:lo], scores[hi:]])
            runner = float(rest.max()) if rest.size else float(scores.min())
            # parabolic interpolation for sub-hop timing
            d = 0.0
            if 0 < i < len(scores) - 1:
                a, b, c = scores[i - 1], scores[i], scores[i + 1]
                denom = a - 2 * b + c
                if abs(denom) > 1e-12:
                    d = float(np.clip(0.5 * (a - c) / denom, -1.0, 1.0))
            cands.append((seg0 + (i + d) * HOP_S, float(scores[i]), runner))
        if seg1 >= t1:
            break
        seg0 = seg1 - overlap
    if not cands:
        raise MatchError("Audio scan produced no candidates.")
    best = max(cands, key=lambda c: c[1])
    t_match, z, runner = best
    for c in cands:  # other chunks compete too (their own peak, unless it
        if c is not best:  # is this same moment seen from the overlap)
            far = abs(c[0] - t_match) > RUNNER_EXCL * HOP_S
            runner = max(runner, c[1] if far else c[2])
    return t_match, z - runner, z

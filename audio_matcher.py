"""Audio-based matching: find where captured stream audio occurs in a file.

The system-audio capture contains the film's soundtrack with the streamer's
commentary mixed on top. Log band-energy features plus normalized
cross-correlation still peak at the right offset as long as the film audio
is audible under the voice - correlation only needs a fraction of the
spectrum to line up.
"""

import subprocess

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

# gates used by callers to decide whether a peak is trustworthy
Z_OK = 6.0            # peak must stand this many sigmas above the score curve
SCORE_OK = 0.10       # and reach this normalized correlation


def decode_audio(path, t0_abs, dur, sr=SR):
    """Mono float32 PCM of [t0_abs, t0_abs+dur] (absolute source seconds)."""
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostats",
           "-ss", f"{max(t0_abs, 0.0):.3f}", "-i", path, "-t", f"{dur:.3f}",
           "-vn", "-sn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
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


def prep_capture(samples, sr):
    """Feature block for a loopback recording (any sample rate)."""
    return features(np.asarray(samples, dtype=np.float32), sr)


def _corr_scores(W, C):
    """Normalized correlation of capture C (L,B) at every lag inside W (T,B)."""
    T, B = W.shape
    L = C.shape[0]
    if T < L + 4:
        raise MatchError("Search window is shorter than the recording.")
    n = 1 << int(np.ceil(np.log2(T + L)))
    num = np.zeros(T - L + 1, dtype=np.float64)
    for b in range(B):
        fa = np.fft.rfft(W[:, b], n)
        fb = np.fft.rfft(C[::-1, b], n)
        num += np.fft.irfft(fa * fb, n)[L - 1:T]
    energy = np.concatenate([[0.0], np.cumsum((W * W).sum(axis=1))])
    win_energy = energy[L:] - energy[:-L]
    denom = np.sqrt(win_energy * float((C * C).sum())) + 1e-9
    return num / denom


def find_match_audio(path, capture_feats, t0=None, t1=None, progress=None):
    """Locate the recording inside `path`'s audio track.

    Returns (time_on_player_timeline, score, peak_z). Callers should treat
    the result as unreliable when score < SCORE_OK or peak_z < Z_OK.
    """
    progress = progress or (lambda msg: None)
    duration, start = probe(path)
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
            x = decode_audio(path, start + seg0, seg1 - seg0)
            W = features(x, SR)
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

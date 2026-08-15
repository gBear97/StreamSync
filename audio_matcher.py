"""Audio-based matching: find where captured stream audio occurs in a file.

The system-audio capture contains the film's soundtrack with the streamer's
commentary mixed on top. Log band-energy features plus normalized
cross-correlation still peak at the right offset as long as the film audio
is audible under the voice - correlation only needs a fraction of the
spectrum to line up.
"""

import collections
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

# ...and a gate for the failure those two cannot see. z measures the peak
# against the MEAN of the score curve, so when a film repeats itself - a
# recurring musical theme, a returning location - several positions score
# nearly the same, the winner still towers over the average, and a coin
# flip is reported as a confident match. Measured on In the Mood for
# Love: audio taken from the film itself at 25:00 lost to 43:36, 0.81 vs
# 0.76, at z 16.4. Compare the winner with its best well-separated rival
# instead: that ratio is what says "I know where this is".
# Measured on 11 real films plus negative controls: at 0.80 no correct
# answer that used to pass is refused (0/408 on the different-cut corpus),
# wrong-film false accepts drop from 16 to 10, and real positives lose 2
# points of recall. 0.85 was a guess; this is not.
RIVAL_RATIO = 0.80    # rival/best above this = the answer is a guess
# Two peaks closer than this are the same place, not rivals. Swept 2-20s
# on a self-similar film: identical results throughout, because the
# per-chunk peak budget fills with the winner's own neighbourhood long
# before a closer twin could be reported. Keep the wide value; splitting
# one peak's lobe into false rivals is the worse failure.
PEAK_SEP_S = 20.0
# a proximity hint resolves a tie only if it puts one candidate this much
# nearer than the next one; otherwise the hint is not evidence
NEAR_MARGIN_S = 10.0
# and the candidate proximity picks must be this good relative to the best
# peak before its verdict is trusted enough to clear the flag
NEAR_TRUST_RATIO = 0.90

Match = collections.namedtuple(
    "Match", "t score z rival_score rival_t ambiguous candidates")


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


def _chunk_peaks(scores, seg0, max_n=6):
    """The top few well-separated peaks in one chunk's score curve.

    Sub-hop timing comes from parabolic interpolation around each peak.
    """
    out = []
    order = np.argsort(scores)[::-1]
    for j in order:
        i = int(j)
        t = seg0 + i * HOP_S
        if any(abs(t - p[0]) < PEAK_SEP_S for p in out):
            continue
        d = 0.0
        if 0 < i < len(scores) - 1:
            a, b, c = scores[i - 1], scores[i], scores[i + 1]
            denom = a - 2 * b + c
            if abs(denom) > 1e-12:
                d = float(np.clip(0.5 * (a - c) / denom, -1.0, 1.0))
        out.append((seg0 + (i + d) * HOP_S, float(scores[i])))
        if len(out) >= max_n:
            break
    return out


def find_match_audio_ex(path, capture_feats, t0=None, t1=None, progress=None,
                        cancel=None, near=None):
    """Locate the recording inside `path`'s audio track, honestly.

    Returns a Match. `ambiguous` is set when the best peak has a rival
    that scores nearly as well somewhere else entirely - the film sounds
    the same in both places and this recording cannot tell them apart.

    `near` (seconds on the player timeline) breaks such ties by
    proximity: when several places match equally well, the one closest to
    where the film already is wins. That is the difference between a
    re-sync nudging a few seconds and a re-sync teleporting 18 minutes.
    """
    progress = progress or (lambda msg: None)
    duration, start = probe(path)
    t0 = 0.0 if t0 is None else max(0.0, min(t0, duration))
    t1 = duration if t1 is None else max(0.0, min(t1, duration))
    if t1 - t0 < 8.0:
        t0, t1 = max(0.0, t0 - 8.0), min(duration, t1 + 8.0)

    # A capture is only findable where it fits ENTIRELY inside one chunk,
    # so a chunk covers starts up to its end minus the capture length,
    # while the next chunk begins one overlap earlier. Any capture longer
    # than the overlap therefore leaves a band at every boundary that no
    # chunk can match - and the scan answers from somewhere else entirely
    # rather than reporting that it looked nowhere. A fixed 8s overlap was
    # safe only while every capture was 6s: measured on a 30s listen, a
    # truth at 883s came back as 295s.
    cap_s = capture_feats.shape[0] * HOP_S
    overlap = min(max(OVERLAP_S, cap_s + 1.0), 0.5 * CHUNK_S)

    cands = []       # [(t, score)] across every chunk
    z_of = {}        # peak time -> z within its own chunk
    seg0 = t0
    while seg0 < t1:
        if cancel is not None and cancel():
            raise MatchError("search stopped")
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
            mean, std = scores.mean(), scores.std()
            for t_peak, sc in _chunk_peaks(scores, seg0):
                cands.append((t_peak, sc))
                z_of[t_peak] = float((sc - mean) / (std + 1e-9))
        if seg1 >= t1:
            break
        seg0 = seg1 - overlap
    if not cands:
        raise MatchError("Audio scan produced no candidates.")

    # merge across the chunk overlap, best first
    cands.sort(key=lambda c: -c[1])
    merged = []
    for t, sc in cands:
        if all(abs(t - m[0]) >= PEAK_SEP_S for m in merged):
            merged.append((t, sc))

    top_t, top_score = merged[0]
    # the winner plus everyone arguably as good as it
    tied = [(t, sc) for t, sc in merged if sc >= RIVAL_RATIO * top_score]
    ambiguous = len(tied) > 1

    t_best, score_best = top_t, top_score
    if ambiguous and near is not None and len(tied) > 1:
        by_near = sorted(tied, key=lambda c: abs(c[0] - near))
        # Proximity only settles anything when it actually separates the
        # candidates. Clearing the flag merely because a hint was supplied
        # would disable the gate for every caller that supplies one - and
        # both of ours do.
        separated = (abs(by_near[1][0] - near) - abs(by_near[0][0] - near)
                     >= NEAR_MARGIN_S)
        if separated:
            t_best, score_best = by_near[0]
            # ...and take the hint as CONFIRMING the evidence only when it
            # agrees with it. Merged peaks are >= PEAK_SEP_S apart, so on
            # material with no correct answer at all - the wrong episode,
            # a scene the user's cut does not contain - separation is
            # satisfied by geometry and the nearest spurious peak was
            # being promoted to a confident answer. If proximity overrules
            # a materially better peak, we still take the nearer position
            # (it is usually what a resync wants) but we keep saying we
            # are unsure.
            if score_best >= NEAR_TRUST_RATIO * top_score:
                ambiguous = False

    # describe the rival to whatever we actually chose, not to the global
    # winner - otherwise the diagnostics discuss a different answer
    others = [(t, sc) for t, sc in merged if t != t_best]
    rival_t, rival_score = (others[0] if others else (None, 0.0))

    return Match(t=t_best, score=score_best, z=z_of.get(t_best, 0.0),
                 rival_score=rival_score, rival_t=rival_t,
                 ambiguous=ambiguous, candidates=merged[:5])


def self_similarity(path, probes=24, clip_s=6.0, seed=1234, progress=None,
                    cancel=None):
    """How much does this film sound like itself somewhere else?

    Takes `probes` random clips from the film's own audio and asks, for
    each, how well the best OTHER place matches it - the rival ratio.
    The median across probes predicts measured sync failure per film
    (Spearman 0.81 over 11 real films): a film whose median rival ratio
    exceeds ~0.6 produces ambiguous and wrong syncs several times more
    often than a distinctive one.

    One decode pass over the film; the probes ride along. Returns
    {"median_ratio", "ratios", "positions", "duration"} or raises
    MatchError. `cancel` (callable -> bool) aborts between chunks.
    """
    progress = progress or (lambda msg: None)
    duration, start = probe(path)
    margin = 60.0 + clip_s
    if duration < 2 * margin + 60.0:
        raise MatchError("Film too short for a self-similarity pass.")
    rng = np.random.default_rng(seed)
    positions = np.sort(rng.uniform(margin, duration - margin, probes))

    clips = []
    for t in positions:
        if cancel is not None and cancel():
            raise MatchError("analysis stopped")
        clips.append(features(decode_audio(path, start + t, clip_s), SR))

    # every probe is correlated against every chunk in one pass over the
    # film; overlap follows the clip so chunk boundaries hide nothing
    cands = [[] for _ in clips]          # per probe: [(t_peak, score)]
    selfs = [0.0] * len(clips)           # correlation AT each own position
    overlap = min(max(OVERLAP_S, clip_s + 1.0), 0.5 * CHUNK_S)
    seg0 = 0.0
    while seg0 < duration:
        if cancel is not None and cancel():
            raise MatchError("analysis stopped")
        seg1 = min(seg0 + CHUNK_S, duration)
        if seg1 - seg0 >= 4.0:
            progress(f"Analyzing {int(seg0) // 60}:{int(seg0) % 60:02d}"
                     f" - {int(seg1) // 60}:{int(seg1) % 60:02d}...")
            W = features(decode_audio(path, start + seg0, seg1 - seg0), SR)
            for i, C in enumerate(clips):
                try:
                    scores = _corr_scores(W, C)
                except MatchError:
                    continue
                cands[i].extend(_chunk_peaks(scores, seg0))
                # Read the probe's own position DIRECTLY - its index is
                # known exactly. Recovering it from _chunk_peaks instead
                # would make it compete for a 6-peak budget it can lose
                # (chunk-wide z-scoring makes a quiet passage correlate
                # weakly against a loud chunk), and a peak exactly
                # PEAK_SEP_S away can suppress it and then fail the
                # strict self test. Those losses are one-sided - every
                # one would have scored above 1.0 - so dropping them
                # deflates the median precisely on the self-similar films
                # this statistic exists to flag.
                mid = int(round((positions[i] - seg0) / HOP_S))
                for j in (mid - 1, mid, mid + 1):
                    if 0 <= j < len(scores):
                        selfs[i] = max(selfs[i], float(scores[j]))
        if seg1 >= duration:
            break
        seg0 = seg1 - overlap

    ratios, skipped = [], 0
    for t, plist, self_score in zip(positions, cands, selfs):
        plist.sort(key=lambda c: -c[1])
        merged = []
        for tp, sc in plist:
            if all(abs(tp - m[0]) >= PEAK_SEP_S for m in merged):
                merged.append((tp, sc))
        rival = max((sc for tp, sc in merged
                     if abs(tp - t) >= PEAK_SEP_S), default=0.0)
        if self_score <= 0:
            skipped += 1                 # position never covered by a chunk
            continue
        ratios.append(rival / self_score)
    if not ratios:
        raise MatchError("Self-similarity pass produced no usable probes.")
    return {"median_ratio": float(np.median(ratios)),
            "ratios": [round(r, 4) for r in ratios],
            "positions": [round(float(t), 1) for t in positions],
            "n_probes": len(ratios), "n_skipped": skipped,
            "duration": duration}


def find_match_audio(path, capture_feats, t0=None, t1=None, progress=None,
                     cancel=None, near=None):
    """Locate the recording inside `path`'s audio track.

    Returns (time_on_player_timeline, score, peak_z). Callers should treat
    the result as unreliable when score < SCORE_OK or peak_z < Z_OK - and
    see find_match_audio_ex for the ambiguity those two cannot detect.
    `cancel` (callable -> bool) aborts between decode chunks.
    """
    m = find_match_audio_ex(path, capture_feats, t0, t1, progress, cancel,
                            near)
    return m.t, m.score, m.z

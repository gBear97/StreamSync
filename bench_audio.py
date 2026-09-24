"""Accuracy benchmark for the audio matcher on real film audio and speech.

test_audio_matcher.py checks the plumbing with synthetic tones and white
noise; it passes easily and says nothing about how often the matcher finds
the right moment in a real stream. This script measures that. Each trial
picks a random moment in a real film, builds what the app's loopback
capture would record while a streamer plays that moment, and asks
find_match_audio to find it in the user's local copy:

  film soundtrack - an independent encode of the same film when one is
    given (aligned to the local copy's timeline), else the local copy
  + streamer commentary - real read speech, one speaker per trial, in
    1-5 s talk bursts and short pauses, at a speech-to-film ratio (voice
    active level vs the film's median level), with sidechain ducking that
    pulls the film down 6-15 dB while the streamer talks
  -> optional bus compression, peak limiter
  -> streaming codec round trip through ffmpeg (AAC or Opus)
  -> viewer volume (-30..0 dB) -> 48 kHz capture, 4 s (auto) or 6 s (manual)

Outcomes, per trial and matcher:
  ok   the match is within 0.1 s of the truth and passes the matcher's own
       SCORE_OK / Z_OK gates
  FA   false accept: passes the gates somewhere else - the dangerous case,
       the app would seek there
  rej  fails the gates (or the capture is silent) - the app says "weak match"
  raw  the best peak is within 0.1 s, gates or not (what better gates
       could at most recover)
  med/p90  |error| of the ok trials, ms

Results are broken down by speech-to-film ratio, film loudness at that
moment (segment RMS vs the film's median), capture length and the stream
path settings, for one or more matcher versions side by side (--matcher is
repeatable; a matcher file outside the repo still imports this repo's
matcher.py). Every trial is planned up front from --seed, so a run is
reproducible and identical for every matcher and any --jobs.

Media (fetched once into ~/.cache/streamsync-bench by --fetch, ~70 MB):
  Sintel and Tears of Steel, (c) Blender Foundation, CC BY 3.0, audio
  tracks from the Shaka Player demo assets: the 128k AAC track becomes the
  local copy (.m4a), the 128k Opus track the stream's source.
  LibriSpeech test-other, 10 speakers x 10 utterances (CC BY 4.0, Vassil
  Panayotov et al.), from the Resemblyzer repository's sample data.

Usage:
  python bench_audio.py --fetch
  python bench_audio.py --n 240 --jobs 4
  git show HEAD:audio_matcher.py > /tmp/head_audio_matcher.py
  python bench_audio.py --matcher /tmp/head_audio_matcher.py \\
      --matcher audio_matcher.py --csv /tmp/bench.csv
  python bench_audio.py --whole-file --n 96
"""

import argparse
import concurrent.futures
import csv
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.request

import numpy as np
import imageio_ffmpeg

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CACHE = os.path.join(os.path.expanduser("~"), ".cache",
                             "streamsync-bench")
SIM_SR = 48000        # the app records loopback audio at 48 kHz
PAD_S = 1.0           # codec warm-up margin kept around every capture
TOL_S = 0.1           # |error| under this is the right moment
BLOCK_S = 0.010       # envelope resolution for ducking and compression
LEVEL_BLOCK_S = 0.5   # blocks for the film's median ("typical") level
SILENT_PEAK = 1e-4    # record_loopback rejects captures quieter than this
GAIN_DB = (-30.0, 0.0)
DUCK_DB = (6.0, 15.0)
AUDIO_EXTS = (".flac", ".wav", ".mp3", ".ogg", ".opus", ".m4a", ".aac",
              ".webm")

SHAKA = "https://storage.googleapis.com/shaka-demo-assets"
FILM_URLS = {  # name: (AAC track -> local copy, Opus track -> stream source)
    "sintel": (f"{SHAKA}/sintel-mp4-only/a-eng-0128k-aac.mp4",
               f"{SHAKA}/sintel/a-eng-0128k-libopus.webm"),
    "tears_of_steel": (f"{SHAKA}/tos-ttml/a-eng-0128k-aac.mp4",
                       f"{SHAKA}/tos-ttml/a-eng-0128k-libopus.webm"),
}
LIBRI_URL = ("https://raw.githubusercontent.com/resemble-ai/Resemblyzer/"
             "15d828edebe06bc72b9cabc8ef8ca5ab2cb457ce/audio_data/"
             "librispeech_test-other")
LIBRI_CHAPTERS = ["1688-142285", "1998-15444", "2033-164914", "2414-128291",
                  "2609-156975", "3005-163389", "3080-5032", "3331-159605",
                  "367-130732", "533-1066"]  # utterances 0000-0009 of each


# --------------------------------------------------------------------------
# media
# --------------------------------------------------------------------------

def _download(url, dest):
    if os.path.exists(dest):
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    print(f"  {url}")
    tmp = dest + ".part"
    urllib.request.urlretrieve(url, tmp)
    os.replace(tmp, dest)


def fetch(cache):
    """Download the benchmark media into `cache`."""
    films = os.path.join(cache, "films")
    for name, (aac_url, opus_url) in FILM_URLS.items():
        local = os.path.join(films, f"{name}.m4a")
        if not os.path.exists(local):
            raw = os.path.join(films, f"{name}.aac.mp4")
            _download(aac_url, raw)
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner",
                            "-loglevel", "error", "-y", "-i", raw, "-map",
                            "0:a", "-c", "copy", local], check=True)
            os.remove(raw)
        _download(opus_url, os.path.join(films, f"{name}.stream.webm"))
    speech = os.path.join(cache, "speech", "librispeech_test-other")
    for name in ("LICENSE.TXT", "SPEAKERS.TXT"):
        _download(f"{LIBRI_URL}/{name}", os.path.join(speech, name))
    for chapter in LIBRI_CHAPTERS:
        spk = chapter.split("-")[0]
        for u in range(10):
            fn = f"{chapter}-{u:04d}.flac"
            _download(f"{LIBRI_URL}/{spk}/{fn}", os.path.join(speech, spk, fn))
    print(f"media ready in {cache}")


def decode_pcm(path, sr=SIM_SR):
    """Whole file as mono float32 PCM (sample 0 = file time 0)."""
    out = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostats",
         "-i", path, "-vn", "-sn", "-ac", "1", "-ar", str(sr),
         "-f", "f32le", "-"], capture_output=True)
    x = np.frombuffer(out.stdout, dtype=np.float32)
    if x.size == 0:
        raise SystemExit(f"could not decode audio from {path}")
    return x


def _cache_file(cache, label, paths, ext):
    h = hashlib.sha1(str(SIM_SR).encode())
    for p in paths:
        st = os.stat(p)
        h.update(f"|{os.path.abspath(p)}|{st.st_size}|{st.st_mtime_ns}"
                 .encode())
    return os.path.join(cache, "pcm", f"{label}.{h.hexdigest()[:12]}{ext}")


def cached_pcm(path, cache):
    """decode_pcm through an on-disk cache, memory-mapped so worker
    processes share one copy."""
    npy = _cache_file(cache, os.path.basename(path), [path], ".npy")
    if not os.path.exists(npy):
        os.makedirs(os.path.dirname(npy), exist_ok=True)
        np.save(npy + ".tmp.npy", decode_pcm(path))
        os.replace(npy + ".tmp.npy", npy)
    return np.load(npy, mmap_mode="r")


def load_speech(root, cache):
    """All speech under `root` as one cached PCM file plus speaker ranges.

    Returns (npy_path, [(speaker, start, end), ...]). LibriSpeech names
    (SPEAKER-CHAPTER-UTT) give the speaker; any other file is a speaker of
    its own. A speaker's files are concatenated in name order, so reading
    through a range walks through their talk utterance by utterance.
    """
    files = [root] if os.path.isfile(root) else sorted(
        os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs
        if f.lower().endswith(AUDIO_EXTS))
    if not files:
        raise SystemExit(f"no speech audio under {root} (run --fetch)")
    groups = {}
    for f in files:
        stem = os.path.splitext(os.path.basename(f))[0]
        parts = stem.split("-")
        spk = parts[0] if len(parts) == 3 and all(
            p.isdigit() for p in parts) else stem
        groups.setdefault(spk, []).append(f)
    npy = _cache_file(cache, "speech", files, ".npy")
    index = npy[:-4] + ".json"
    if not (os.path.exists(npy) and os.path.exists(index)):
        chunks, ranges, pos = [], [], 0
        for spk in sorted(groups):
            start = pos
            for f in groups[spk]:
                chunks.append(decode_pcm(f))
                pos += chunks[-1].size
            ranges.append((spk, start, pos))
        os.makedirs(os.path.dirname(npy), exist_ok=True)
        np.save(npy + ".tmp.npy", np.concatenate(chunks))
        os.replace(npy + ".tmp.npy", npy)
        with open(index, "w") as fh:
            json.dump(ranges, fh)
    with open(index) as fh:
        return npy, [tuple(r) for r in json.load(fh)]


def xcorr_lag(a, b, m):
    """Lag in [-m, m] where b (= len(a) + 2m samples, centered) best matches
    a: b[m + lag + i] ~ a[i]. Returns (lag, normalized correlation)."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    nfft = 1 << int(np.ceil(np.log2(a.size + b.size)))
    c = np.fft.irfft(np.fft.rfft(b, nfft) * np.conj(np.fft.rfft(a, nfft)),
                     nfft)[:2 * m + 1]
    k = int(np.argmax(c))
    seg = b[k:k + a.size]
    ncc = c[k] / (np.sqrt(np.dot(a, a) * np.dot(seg, seg)) + 1e-12)
    return k - m, float(ncc)


def measure_lag(ref, other):
    """Samples by which `other` is shifted against `ref`: other[i + lag]
    ~ ref[i].

    Two encodes of one master differ by codec priming (1024 samples for an
    AAC track without an edit list); measured at five points that must
    agree, so a mismatched pair of files fails loudly.
    """
    n, m = 5 * SIM_SR, SIM_SR // 2
    usable = min(ref.size, other.size) - n - 2 * m
    found = []
    for frac in (0.1, 0.3, 0.5, 0.7, 0.9):
        s = m + int(frac * usable)
        found.append(xcorr_lag(ref[s:s + n], other[s - m:s + n + m], m))
    lags = np.array([lag for lag, ncc in found if ncc > 0.8])
    if lags.size < 3 or np.sum(np.abs(lags - np.median(lags)) <= 2) < 3:
        raise SystemExit(f"stream source does not line up with the local "
                         f"copy (lag, ncc at 5 points: {found})")
    return int(np.median(lags))


def block_rms(x, n):
    """RMS of consecutive n-sample blocks (a partial tail block dropped)."""
    k = x.size // n
    b = np.asarray(x[:k * n], dtype=np.float32).reshape(k, n)
    return np.sqrt((b * b).mean(axis=1, dtype=np.float64))


def load_film(local, source, cache):
    """Film record: the local copy, where its stream source lines up, and
    its typical level (median RMS of 0.5 s blocks, digital silence
    ignored)."""
    pcm = cached_pcm(local, cache)
    lag = 0
    if source:
        lag = measure_lag(pcm, cached_pcm(source, cache))
    rms = block_rms(pcm, int(LEVEL_BLOCK_S * SIM_SR))
    return {"name": os.path.splitext(os.path.basename(local))[0],
            "path": os.path.abspath(local), "source": source, "lag": lag,
            "dur": pcm.size / SIM_SR,
            "typical_rms": float(np.median(rms[rms > 1e-5]))}


def default_films(cache):
    """(local, stream source or None) for every films/*.m4a in the cache;
    the source is the NAME.stream.* file next to NAME.m4a."""
    films = os.path.join(cache, "films")
    out = []
    for f in sorted(os.listdir(films)) if os.path.isdir(films) else []:
        if f.endswith(".m4a"):
            stem = f[:-4]
            src = [g for g in sorted(os.listdir(films))
                   if g.startswith(stem + ".stream.")]
            out.append((os.path.join(films, f),
                        os.path.join(films, src[0]) if src else None))
    return out


# --------------------------------------------------------------------------
# stream simulation
# --------------------------------------------------------------------------

def padded_slice(x, i0, n):
    """x[i0:i0+n] as float64, zero-filled where it runs off either end."""
    out = np.zeros(n)
    lo, hi = max(i0, 0), min(i0 + n, x.size)
    if hi > lo:
        out[lo - i0:hi - i0] = x[lo:hi]
    return out


def smooth_db(target_db, attack_s, release_s):
    """One-pole smoothing of a per-block gain curve in dB: gain reduction
    sets in with `attack_s` and recovers with `release_s`."""
    a_att = np.exp(-BLOCK_S / max(attack_s, 1e-4))
    a_rel = np.exp(-BLOCK_S / max(release_s, 1e-4))
    out = np.empty_like(target_db)
    g = 0.0
    for i, t in enumerate(target_db):
        a = a_att if t < g else a_rel
        g = a * g + (1.0 - a) * t
        out[i] = g
    return out


def block_gain(g_db, n):
    """Per-block gain curve (dB) -> per-sample linear gain for n samples."""
    nb = int(round(BLOCK_S * SIM_SR))
    centers = (np.arange(g_db.size) + 0.5) * nb
    return 10.0 ** (np.interp(np.arange(n), centers, g_db) / 20.0)


def active_rms(x):
    """RMS over the 10 ms blocks within 30 dB of the loudest - a voice's
    level while it is actually talking."""
    rms = block_rms(x, int(round(BLOCK_S * SIM_SR)))
    if rms.size == 0 or rms.max() <= 0:
        return 0.0
    act = rms[rms > rms.max() * 10 ** (-30 / 20)]
    return float(np.sqrt(np.mean(act * act)))


def commentary(rng, speech, span, n):
    """n samples of one speaker talking in bursts, scaled to unit active RMS.

    Bursts of 1-5 s of continuous read speech (with its own micro-pauses)
    alternate with 0.2-1.5 s silences from a random phase - roughly how a
    streamer talks over a scene.
    """
    a, b = span
    total = b - a
    out = np.zeros(n)
    fade = int(0.01 * SIM_SR)
    ramp = np.linspace(0.0, 1.0, fade)
    read = int(rng.integers(total))
    pos = -int(rng.uniform(0.0, 3.0) * SIM_SR)
    talking = bool(rng.random() < 0.75)
    while pos < n:
        dur = int((rng.uniform(1.0, 5.0) if talking
                   else rng.uniform(0.2, 1.5)) * SIM_SR)
        if talking:
            burst = np.asarray(speech[a + (read + np.arange(dur)) % total],
                               dtype=np.float64)
            burst[:fade] *= ramp
            burst[-fade:] *= ramp[::-1]
            lo, hi = max(pos, 0), min(pos + dur, n)
            if hi > lo:
                out[lo:hi] = burst[lo - pos:hi - pos]
            read = (read + dur) % total
        pos += dur
        talking = not talking
    level = active_rms(out)
    return out / level if level > 0 else out


def duck_gain(voice, depth_db, attack_s, release_s):
    """Sidechain ducking: the film's gain, dipping depth_db while the voice
    is active (within 30 dB of its loudest 10 ms)."""
    rms = block_rms(voice, int(round(BLOCK_S * SIM_SR)))
    key = rms > rms.max() * 10 ** (-30 / 20)
    g = smooth_db(np.where(key, -depth_db, 0.0), attack_s, release_s)
    return block_gain(g, voice.size)


def compress(x, thr_rms, ratio, attack_s, release_s):
    """Feed-forward bus compressor on 10 ms RMS, no makeup gain."""
    env_db = 20 * np.log10(block_rms(x, int(round(BLOCK_S * SIM_SR))) + 1e-9)
    over = np.maximum(env_db - 20 * np.log10(thr_rms), 0.0)
    g = smooth_db(-over * (1.0 - 1.0 / ratio), attack_s, release_s)
    return x * block_gain(g, x.size)


def soft_limit(x, knee=0.8):
    """Peaks beyond `knee` bend smoothly toward full scale."""
    a = np.abs(x)
    over = a > knee
    y = x.copy()
    y[over] = np.sign(x[over]) * (
        knee + (1 - knee) * np.tanh((a[over] - knee) / (1 - knee)))
    return y


def codec_roundtrip(x, codec, kbps):
    """Encode + decode through ffmpeg, aligned back onto x's samples.

    The simulation is mono, so a `kbps` stereo stream is encoded as mono at
    half that rate. Encoder priming is measured and removed: it is a fixed
    stream latency, not something the matcher has to find.
    """
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    rate = f"{max(kbps // 2, 16)}k"
    enc = (["-c:a", "aac", "-b:a", rate, "-f", "adts"] if codec == "aac" else
           ["-c:a", "libopus", "-b:a", rate, "-f", "ogg"])
    coded = subprocess.run(
        [ff, "-hide_banner", "-loglevel", "error", "-f", "f32le", "-ar",
         str(SIM_SR), "-ac", "1", "-i", "pipe:0", *enc, "pipe:1"],
        input=x.astype(np.float32).tobytes(), capture_output=True,
        check=True).stdout
    y = np.frombuffer(subprocess.run(
        [ff, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", "f32le", "-ar", str(SIM_SR), "-ac", "1", "pipe:1"],
        input=coded, capture_output=True, check=True).stdout,
        dtype=np.float32)
    m = SIM_SR // 10
    lag, _ = xcorr_lag(x[m:x.size - m], padded_slice(y, 0, x.size), m)
    return padded_slice(y, lag, x.size)


def build_capture(trial, film, pcm, speech, span):
    """The float32 48 kHz samples the app would record for this trial."""
    rng = np.random.default_rng(trial["seed"])
    n = int(round(trial["length"] * SIM_SR))
    pad = int(PAD_S * SIM_SR)
    x = padded_slice(pcm, trial["start"] - pad + film["lag"], n + 2 * pad)
    if trial["sfr"] is not None:
        voice = commentary(rng, speech, span, x.size)
        voice *= film["typical_rms"] * 10 ** (trial["sfr"] / 20)
        if trial["duck_db"] > 0:
            x *= duck_gain(voice, trial["duck_db"], trial["duck_attack"],
                           trial["duck_release"])
        x += voice
    if trial["comp"]:
        x = compress(x, film["typical_rms"] * 10 ** (trial["comp_thr"] / 20),
                     trial["comp_ratio"], 0.005, trial["comp_release"])
    x = codec_roundtrip(soft_limit(x), trial["codec"], trial["kbps"])
    return (x[pad:pad + n] * 10 ** (trial["gain_db"] / 20)).astype(np.float32)


# --------------------------------------------------------------------------
# matchers and trials
# --------------------------------------------------------------------------

def load_matcher(path, idx):
    """Import an audio_matcher implementation from any file path."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)  # for its `from matcher import ...`
    spec = importlib.util.spec_from_file_location(
        f"_bench_matcher_{idx}", os.path.abspath(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def plan_trials(args, films, locals_, n_speakers):
    """Every trial's settings, drawn up front from --seed.

    Capture length x speech-to-film ratio cells are cycled so each gets an
    equal share; everything else is drawn at random.
    """
    rng = np.random.default_rng(args.seed)
    cells = [(L, s) for L in args.lengths for s in args.sfr]
    trials = []
    for i in range(args.n):
        length, sfr = cells[i % len(cells)]
        f = int(rng.integers(len(films)))
        film = films[f]
        lo = PAD_S + 1.0
        hi = film["dur"] - length - PAD_S - 1.0
        start = int(round(rng.uniform(lo, hi) * SIM_SR))
        hint = start / SIM_SR + rng.uniform(-0.5, 0.5) * args.window
        codec, kbps = args.codecs[int(rng.integers(len(args.codecs)))]
        duck = rng.uniform(*DUCK_DB)
        ducked = rng.random() < args.duck_prob and sfr is not None
        n = int(round(length * SIM_SR))
        seg = np.asarray(locals_[f][start:start + n], dtype=np.float64)
        level = np.sqrt(np.mean(seg * seg)) / film["typical_rms"]
        trials.append({
            "i": i, "film": f, "length": length, "sfr": sfr,
            "start": start, "truth": start / SIM_SR,
            "t0": None if args.whole_file else hint - args.window,
            "t1": None if args.whole_file else hint + args.window,
            "level_db": float(20 * np.log10(level + 1e-9)),
            "gain_db": float(rng.uniform(*GAIN_DB)),
            "codec": codec, "kbps": kbps,
            "duck_db": float(duck) if ducked else 0.0,
            "duck_attack": float(rng.uniform(0.005, 0.05)),
            "duck_release": float(rng.uniform(0.15, 0.6)),
            "comp": bool(rng.random() < args.comp_prob),
            "comp_thr": float(rng.uniform(-6.0, 6.0)),
            "comp_ratio": float(rng.uniform(2.0, 6.0)),
            "comp_release": float(rng.uniform(0.05, 0.3)),
            "speaker": int(rng.integers(n_speakers)),
            "seed": int(rng.integers(2 ** 31)),
        })
    return trials


_W = {}  # per-process state: films, their stream PCM, speech, matchers


def init_worker(films, cache, speech_npy, speakers, matcher_paths):
    _W["films"] = films
    _W["pcm"] = [cached_pcm(f["source"] or f["path"], cache) for f in films]
    _W["speech"] = np.load(speech_npy, mmap_mode="r")
    _W["spans"] = [(a, b) for _, a, b in speakers]
    _W["matchers"] = [load_matcher(p, i) for i, p in enumerate(matcher_paths)]


def run_trial(trial):
    """[(time or None, score, z, passed gates), ...] - one per matcher."""
    f = trial["film"]
    film = _W["films"][f]
    x = build_capture(trial, film, _W["pcm"][f], _W["speech"],
                      _W["spans"][trial["speaker"]])
    silent = float(np.abs(x).max()) < SILENT_PEAK  # the app calls it silence
    results = []
    for mod in _W["matchers"]:
        if silent:
            results.append((None, 0.0, 0.0, False))
            continue
        try:
            feats = mod.prep_capture(x, SIM_SR)
            t, score, z = mod.find_match_audio(film["path"], feats,
                                               trial["t0"], trial["t1"])
        except RuntimeError:  # MatchError included
            results.append((None, 0.0, 0.0, False))
            continue
        passed = score >= mod.SCORE_OK and z >= mod.Z_OK
        results.append((float(t), float(score), float(z), bool(passed)))
    return results


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def _level_bin(t):
    edges = [(-10, "< -10 dB"), (0, "-10..0 dB"), (10, "0..+10 dB")]
    for i, (edge, label) in enumerate(edges):
        if t["level_db"] < edge:
            return i, label
    return 3, "> +10 dB"


def _snr_bin(t):
    if t["sfr"] is None:
        return 0, "no voice"
    snr = t["sfr"] - t["level_db"]
    for i, edge in enumerate((0, 10, 20, 30)):
        if snr < edge:
            return i + 1, (f"< {edge} dB" if i == 0
                           else f"{edge - 10}..{edge} dB")
    return 5, "> 30 dB"


DIMENSIONS = [
    ("speech-to-film ratio (voice vs film's median level)",
     lambda t: ((-99, "no voice") if t["sfr"] is None
                else (t["sfr"], f"{t['sfr']:+g} dB"))),
    ("film level here vs its median", _level_bin),
    ("voice vs film at this moment, before ducking", _snr_bin),
    ("capture length", lambda t: (t["length"], f"{t['length']:g} s")),
    ("ducking", lambda t: ((0, "none") if t["duck_db"] == 0 else
                           (1, "6-10 dB") if t["duck_db"] < 10 else
                           (2, "10-15 dB"))),
    ("codec (stereo-equivalent rate)",
     lambda t: ((t["codec"], t["kbps"]), f"{t['codec']} {t['kbps']}k")),
    ("viewer volume", lambda t: ((0, "-30..-20 dB") if t["gain_db"] < -20 else
                                 (1, "-20..-10 dB") if t["gain_db"] < -10 else
                                 (2, "-10..0 dB"))),
    ("bus compression", lambda t: (int(t["comp"]), "on" if t["comp"]
                                   else "off")),
]


def _cells(trials, res):
    """ok/FA/rej/raw percentages and ok-error quantiles for one group."""
    n = len(trials)
    ok = fa = raw = 0
    errs = []
    for t, (tm, _score, _z, passed) in zip(trials, res):
        hit = tm is not None and abs(tm - t["truth"]) < TOL_S
        raw += hit
        if passed and hit:
            ok += 1
            errs.append(abs(tm - t["truth"]) * 1000)
        elif passed:
            fa += 1
    pct = [f"{100 * v / n:4.0f}" for v in (ok, fa, n - ok - fa, raw)]
    q = ([f"{np.median(errs):4.0f}", f"{np.percentile(errs, 90):4.0f}"]
         if errs else ["   -", "   -"])
    return " ".join(pct + q)


def report(trials, rows, labels, films):
    width = 29
    head = "".join(f" | {lab[:width]:<{width}}" for lab in labels)
    cols = "".join(f" | {' ok%  FA% rej% raw%  med  p90':<{width}}"
                   for _ in labels)
    groups = [("all trials", lambda t: (0, "all"))] + DIMENSIONS + [
        ("film", lambda t: (t["film"], films[t["film"]]["name"]))]
    for title, key in groups:
        print(f"\n{title}")
        print(f"{'':<16}{'n':>4}{head}")
        print(f"{'':<20}{cols}")
        bins = {}
        for t, r in zip(trials, rows):
            k = key(t)
            bins.setdefault(k, ([], []))
            bins[k][0].append(t)
            bins[k][1].append(r)
        for (_, label), (ts, rs) in sorted(bins.items()):
            line = "".join(
                f" | {_cells(ts, [r[m] for r in rs]):<{width}}"
                for m in range(len(labels)))
            print(f"{label[:16]:<16}{len(ts):>4}{line}")


def write_csv(path, trials, rows, labels, films):
    keys = ["i", "length", "sfr", "truth", "t0", "t1", "level_db", "gain_db",
            "codec", "kbps", "duck_db", "comp", "speaker"]
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["film"] + keys + [f"{lab}_{c}" for lab in labels
                                      for c in ("t", "score", "z", "pass")])
        for t, r in zip(trials, rows):
            w.writerow([films[t["film"]]["name"]] + [t[k] for k in keys]
                       + [v for res in r for v in res])


# --------------------------------------------------------------------------

def parse_args():
    ap = argparse.ArgumentParser(
        description="Accuracy benchmark for audio_matcher on real film "
                    "audio with speech commentary through a stream codec.")
    ap.add_argument("--fetch", action="store_true",
                    help="download the benchmark media and exit")
    ap.add_argument("--cache", default=DEFAULT_CACHE,
                    help="media and decode cache (default %(default)s)")
    ap.add_argument("--film", nargs="+", action="append", metavar="PATH",
                    help="LOCAL_COPY [STREAM_SOURCE]; repeatable (default: "
                         "CACHE/films/*.m4a with NAME.stream.* as source)")
    ap.add_argument("--speech", help="speech file or directory "
                                     "(default CACHE/speech)")
    ap.add_argument("--matcher", action="append", metavar="PATH",
                    help="audio_matcher implementation; repeat to compare "
                         "(default: this repo's audio_matcher.py)")
    ap.add_argument("--n", type=int, default=240, help="trials")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--window", type=float, default=120.0,
                    help="search half-width around the hint (s); the truth "
                         "lies within +-window/2 of the hint")
    ap.add_argument("--whole-file", action="store_true",
                    help="search the whole file instead of a window")
    ap.add_argument("--lengths", default="6,4",
                    help="capture lengths in s (default %(default)s)")
    ap.add_argument("--sfr", default="off,-6,0,6,12,18",
                    help="speech-to-film ratios in dB, 'off' = no voice")
    ap.add_argument("--codecs", default="aac:128,opus:96,opus:128,opus:160",
                    help="CODEC:KBPS stream settings, codec aac or opus")
    ap.add_argument("--duck-prob", type=float, default=0.8,
                    help="share of voiced trials with ducking")
    ap.add_argument("--comp-prob", type=float, default=0.5,
                    help="share of trials with bus compression")
    ap.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    ap.add_argument("--csv", help="write per-trial results here")
    args = ap.parse_args()
    args.lengths = [float(v) for v in args.lengths.split(",")]
    args.sfr = [None if v.strip() == "off" else float(v)
                for v in args.sfr.split(",")]
    args.codecs = [(c.split(":")[0], int(c.split(":")[1]))
                   for c in args.codecs.split(",")]
    if any(c not in ("aac", "opus") for c, _ in args.codecs):
        ap.error("codecs must be aac or opus")
    if any(len(f) > 2 for f in args.film or []):
        ap.error("--film takes LOCAL_COPY [STREAM_SOURCE]")
    return args


def main():
    args = parse_args()
    if args.fetch:
        fetch(args.cache)
        return
    encoders = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),
                               "-hide_banner", "-encoders"],
                              capture_output=True, text=True).stdout
    if "libopus" not in encoders:
        print("ffmpeg has no libopus - dropping Opus stream settings")
        args.codecs = [c for c in args.codecs if c[0] != "opus"]

    specs = ([(f[0], f[1] if len(f) > 1 else None) for f in args.film]
             if args.film else default_films(args.cache))
    if not specs:
        raise SystemExit("no films (run --fetch or pass --film)")
    films = [load_film(local, src, args.cache) for local, src in specs]
    locals_ = [cached_pcm(f["path"], args.cache) for f in films]
    speech_npy, speakers = load_speech(
        args.speech or os.path.join(args.cache, "speech"), args.cache)
    paths = args.matcher or [os.path.join(HERE, "audio_matcher.py")]
    mods = [load_matcher(p, i) for i, p in enumerate(paths)]
    labels = [os.path.splitext(os.path.basename(p))[0] for p in paths]
    labels = [lab if labels.count(lab) == 1 else f"{lab}#{i}"
              for i, lab in enumerate(labels)]
    trials = plan_trials(args, films, locals_, len(speakers))

    print(f"{args.n} trials, seed {args.seed}, "
          + ("whole-file search" if args.whole_file
             else f"search window +-{args.window:g} s"))
    for f in films:
        print(f"  film {f['name']}: {f['dur']:.0f} s, stream source "
              f"{os.path.basename(f['source']) if f['source'] else 'local copy'}"
              f" (lag {f['lag']} samples), median level "
              f"{20 * np.log10(f['typical_rms']):.1f} dBFS")
    print(f"  speech: {len(speakers)} speakers")
    for lab, mod, p in zip(labels, mods, paths):
        print(f"  matcher {lab}: {os.path.abspath(p)} "
              f"(gates score >= {mod.SCORE_OK}, z >= {mod.Z_OK})")

    t_start = time.time()

    def collect(results):
        rows = []
        for r in results:
            rows.append(r)
            if len(rows) % max(1, args.n // 10) == 0:
                print(f"  {len(rows)}/{args.n} trials "
                      f"({time.time() - t_start:.0f} s)", file=sys.stderr)
        return rows

    init = (films, args.cache, speech_npy, speakers, paths)
    if args.jobs <= 1:
        init_worker(*init)
        rows = collect(map(run_trial, trials))
    else:
        with concurrent.futures.ProcessPoolExecutor(
                args.jobs, initializer=init_worker, initargs=init) as pool:
            rows = collect(pool.map(run_trial, trials))

    report(trials, rows, labels, films)
    print(f"\n{args.n} trials x {len(paths)} matchers in "
          f"{time.time() - t_start:.0f} s")
    if args.csv:
        write_csv(args.csv, trials, rows, labels, films)
        print(f"per-trial results: {args.csv}")


if __name__ == "__main__":
    main()

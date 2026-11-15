"""End-to-end check of the audio matcher.

Renders 3 minutes of film-like audio (random note segments plus
envelope-modulated noise -- rich temporal structure, like music and
effects), AAC-encodes it, simulates a loopback capture at a known moment
with louder 'commentary' noise mixed over it, and checks that
find_match_audio recovers the timestamp. Also covers:

- a capture recorded at 48 kHz (the loopback rate) against the 16 kHz
  file path;
- the same film remuxed with container timestamps starting at 5 s and at
  600 s (.m2ts-style), which once shifted every match by the start time;
- a quiet scene inside a loud search window, where uncentered scoring once
  pushed the right answer under the trust gates;
- an 18 s capture swept across a chunk boundary of a whole-film search,
  which once hid a 10 s band before every boundary, the retry ladder's
  6/12/18 s looks reusing one decode, and a 25 s capture, which widens
  the overlap to fit;
- the same ladder over a film nearly 3 h and one 4 h long (decoding
  stubbed), whose retries once decoded the whole film again because the
  decode cache was a chunk short;
- a DTS track, decoded from its core alone, and a DTS stream with no
  core, which has to fall back to a full decode.

bench_audio.py measures real-world accuracy; this only guards regressions.
"""

import os
import subprocess
import tempfile
import wave

import numpy as np
import imageio_ffmpeg

import audio_matcher
import controller
import matcher

TRUTH = 97.4
CAPTURE_S = 6.0
SR = 16000
DUR = 180.0


def render_clip(path):
    rng = np.random.default_rng(3)
    n = int(DUR * SR)
    x = np.zeros(n, dtype=np.float64)

    # "music": two random tones per quarter-second segment
    seg = int(0.25 * SR)
    tt = np.arange(seg) / SR
    ramp = np.minimum(1.0, np.minimum(np.arange(seg), np.arange(seg)[::-1]) / (0.01 * SR))
    for s in range(n // seg):
        freqs = np.exp(rng.uniform(np.log(120.0), np.log(5500.0), size=2))
        tone = sum(np.sin(2 * np.pi * f * tt) for f in freqs)
        x[s * seg:(s + 1) * seg] += 0.35 * tone * ramp

    # "effects": noise with a randomly varying loudness envelope
    env_pts = rng.uniform(0.0, 1.0, size=int(DUR * 20)) ** 2
    env = np.interp(np.arange(n), np.linspace(0, n, env_pts.size), env_pts)
    x += 0.4 * rng.standard_normal(n) * env

    x *= 0.7 / np.abs(x).max()
    wav = path + ".wav"
    with wave.open(wav, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes((x * 32767).astype(np.int16).tobytes())
    subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-y",
        "-i", wav, "-c:a", "aac", "-b:a", "128k", path,
    ], check=True, capture_output=True)
    os.remove(wav)


def main():
    tmp = tempfile.mkdtemp()
    clip = os.path.join(tmp, "clip.m4a")
    print("rendering synthetic film-audio clip...")
    render_clip(clip)
    _, start = matcher.probe(clip)

    # simulate the loopback capture: film audio + louder 'commentary' noise
    x = audio_matcher.decode_audio(clip, start + TRUTH, CAPTURE_S)
    rng = np.random.default_rng(7)
    noise = np.convolve(rng.standard_normal(x.size), np.hamming(65), "same")
    noise *= 1.2 * np.sqrt((x * x).mean()) / (np.sqrt((noise * noise).mean()) + 1e-12)
    feats = audio_matcher.prep_capture(x + noise.astype(np.float32), audio_matcher.SR)

    cases = {
        "narrow window": (TRUTH - 30, TRUTH + 30),
        "whole file": (None, None),
    }
    for label, (a, b) in cases.items():
        t, score, z = audio_matcher.find_match_audio(
            clip, feats, a, b, progress=lambda m: print("   ", m))
        err = abs(t - TRUTH)
        print(f"{label}: matched {t:.3f}s (truth {TRUTH}s, "
              f"err {err * 1000:.0f} ms, score {score:.3f}, z {z:.1f})")
        assert err < 0.12, f"match error too large: {err:.3f}s"
        assert score >= audio_matcher.SCORE_OK, f"score too low: {score:.3f}"
        assert z >= audio_matcher.Z_OK, f"peak z too low: {z:.1f}"
    # the loopback records at 48 kHz, not the file path's 16 kHz
    x48 = audio_matcher.resample(x + noise.astype(np.float32), audio_matcher.SR, 48000)
    t, score, z = audio_matcher.find_match_audio(
        clip, audio_matcher.prep_capture(x48, 48000), TRUTH - 30, TRUTH + 30)
    print(f"48 kHz capture: matched {t:.3f}s (err {abs(t - TRUTH) * 1000:.0f} ms, "
          f"score {score:.3f}, z {z:.1f})")
    assert abs(t - TRUTH) < 0.12 and score >= audio_matcher.SCORE_OK

    # container timestamps that do not start at zero
    for name, off in (("offset5.mp4", 5), ("offset600.mkv", 600)):
        shifted = os.path.join(tmp, name)
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-y",
                        "-i", clip, "-c", "copy", "-output_ts_offset", str(off),
                        shifted], check=True, capture_output=True)
        t, score, z = audio_matcher.find_match_audio(shifted, feats,
                                                     TRUTH - 30, TRUTH + 30)
        err = abs(t - TRUTH)
        print(f"{name}: start {matcher.probe(shifted)[1]:.2f}s, matched {t:.3f}s "
              f"(err {err * 1000:.0f} ms)")
        # AAC priming puts the container start ~64 ms before the audio
        assert err < 0.12, f"{name}: start time mishandled, err {err:.3f}s"

    # refine_match: the matcher's answer made exact, from the recording
    # itself - a 48 kHz loopback with a louder voice over it
    rec = audio_matcher.decode_audio(clip, start + TRUTH, CAPTURE_S)
    voice = np.convolve(rng.standard_normal(rec.size), np.hamming(65), "same")
    voice *= 1.2 * np.sqrt((rec * rec).mean()) / (np.sqrt((voice * voice).mean()) + 1e-12)
    rec48 = audio_matcher.resample(rec + voice.astype(np.float32), audio_matcher.SR, 48000)
    for off in (0.011, -0.007, 0.0):
        fine = audio_matcher.refine_match(rec48, 48000, clip, TRUTH + off)
        err = None if fine is None else (fine - TRUTH) * 1000
        print(f"refine from {off * 1000:+.0f} ms out: "
              + ("nothing" if fine is None else f"{err:+.2f} ms"))
        assert fine is not None and abs(err) < 0.5, f"refined to {err} ms"
    noise_only = rng.standard_normal(rec48.size).astype(np.float32)
    assert audio_matcher.refine_match(noise_only, 48000, clip, TRUTH) is None, \
        "a recording with no film in it was refined"
    assert audio_matcher.refine_match(rec48[:48000], 48000, clip, TRUTH) is None, \
        "one second is too little to refine"
    print("refine: within half a millisecond; nothing from noise or too little")

    presence(tmp, clip)
    centre(tmp)
    survey(tmp)
    quiet_scene(tmp)
    dts_core(tmp, clip, feats)
    chunk_boundary(clip)
    long_film_retries()
    print("AUDIO MATCHER TEST PASSED")


def presence(tmp, clip):
    """film_presence, the look Pause with the stream takes several times a
    second: heard where the stream carries the film (with how far off the
    film placed it), not heard over noise or silence, and "quiet" where the
    film's own track has nothing to hear."""
    rng = np.random.default_rng(11)
    at = 61.3
    look = audio_matcher.decode_audio(clip, at, 2.0)[:int(0.6 * SR)]   # it decodes 1 s or more
    voice = np.convolve(rng.standard_normal(look.size), np.hamming(65), "same")
    voice *= 0.5 * np.sqrt((look * look).mean()) / (np.sqrt((voice * voice).mean()) + 1e-12)
    stream = audio_matcher.resample(look + voice.astype(np.float32), SR, 48000)
    for placed, want in ((at, 0.0), (at - 0.25, 0.25), (at + 0.3, -0.3)):
        p = audio_matcher.film_presence(clip, stream, 48000, placed)
        print(f"presence, film placed {want:+.2f} s off: {p.state}, shift {p.shift:+.3f} s, "
              f"ncc {p.ncc:.2f}, clear {p.clear:.2f}")
        assert p.state == "heard" and abs(p.shift - want) < 0.03, p
        # the film's sound makes one sharp peak, standing clear of the rest
        assert p.clear >= 0.3, p
    noise = (0.1 * rng.standard_normal(stream.size)).astype(np.float32)
    p = audio_matcher.film_presence(clip, noise, 48000, at)
    assert p.state != "heard", f"noise was heard as the film: {p}"
    assert p.clear < 0.3, f"noise stood clear as the film does: {p}"
    p = audio_matcher.film_presence(clip, np.zeros_like(stream), 48000, at)
    assert p.state == "missing", f"digital silence: {p}"
    # a film with a silent stretch: nothing to listen for there
    x = np.zeros(int(40 * SR))
    x[:] = 0.3 * rng.standard_normal(x.size)
    x[int(15 * SR):int(25 * SR)] = 0.0
    wav = os.path.join(tmp, "gap.wav")
    with wave.open(wav, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes((x * 32767).astype(np.int16).tobytes())
    p = audio_matcher.film_presence(wav, noise, 48000, 19.0)
    assert p.state == "quiet", f"the film's silent stretch was judged: {p}"
    print("presence: heard and placed within 30 ms, standing clear; noise not heard "
          "nor clear; silence missing; a silent stretch of film quiet")


def _speech(rng, n, sr):
    """Speech-like sound: syllables of a voiced harmonic stack at a random
    pitch with a breath of band noise, words and pauses between them."""
    x = np.zeros(n)
    i = 0
    while i < n:
        i += int(rng.uniform(0.05, 0.3) * sr) if rng.random() < 0.8 else int(rng.uniform(0.5, 1.5) * sr)
        m = int(rng.uniform(0.15, 0.4) * sr)
        if i + m > n:
            break
        t = np.arange(m) / sr
        f0 = rng.uniform(100.0, 220.0)
        tone = sum(np.sin(2 * np.pi * f0 * h * t + rng.uniform(0, 6.3)) / h
                   for h in range(1, int(3000.0 / f0)))
        breath = np.convolve(rng.standard_normal(m), np.hanning(9), "same") * 0.3
        env = np.sin(np.pi * np.arange(m) / m) ** 0.7
        x[i:i + m] += (tone + breath) * env
        i += m
    return x / (np.abs(x).max() + 1e-12)


def _music(rng, n, sr):
    """Music-like sound: two random tones every quarter second, as render_clip."""
    x = np.zeros(n)
    seg = int(0.25 * sr)
    tt = np.arange(seg) / sr
    ramp = np.minimum(1.0, np.minimum(np.arange(seg), np.arange(seg)[::-1]) / (0.01 * sr))
    for s in range(n // seg):
        for f in np.exp(rng.uniform(np.log(120.0), np.log(5500.0), size=2)):
            x[s * seg:(s + 1) * seg] += np.sin(2 * np.pi * f * tt) * ramp
    return x / (np.abs(x).max() + 1e-12)


def _write_wav(path, chans, sr):
    x = np.stack(chans, axis=1)
    with wave.open(path, "wb") as f:
        f.setnchannels(x.shape[1])
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())


def centre(tmp):
    """A 5.1 film is heard by its centre where the stream's copy has its
    music elsewhere: dialogue in the centre, music in front left and right,
    and a stream that carries the same dialogue over the music moved 7 s.
    decode_presence gives the mix exactly as decode_audio does, and the
    centre on its own; a stereo or mono file has no centre to give."""
    sr = 48000
    rng = np.random.default_rng(21)
    n = int(90 * sr)
    talk, music, amb = _speech(rng, n, sr), _music(rng, n, sr), rng.standard_normal(n)
    FL = 0.35 * music + 0.02 * amb
    FR = 0.35 * np.roll(music, int(0.013 * sr)) + 0.02 * np.roll(amb, 97)
    FC = 0.5 * talk
    SL, SR_ = 0.05 * np.roll(amb, 1013), 0.05 * np.roll(amb, 2029)
    wav = os.path.join(tmp, "six.wav")
    _write_wav(wav, [FL, FR, FC, np.zeros(n), SL, SR_], sr)
    film = os.path.join(tmp, "six.mkv")
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run([ff, "-hide_banner", "-y", "-i", wav, "-af", "aformat=channel_layouts=5.1(side)",
                    "-c:a", "flac", film], check=True, capture_output=True)
    stereo, mono = os.path.join(tmp, "two.mkv"), os.path.join(tmp, "one.mkv")
    for out, ac in ((stereo, "2"), (mono, "1")):
        subprocess.run([ff, "-hide_banner", "-y", "-i", film, "-ac", ac, "-c:a", "flac", out],
                       check=True, capture_output=True)
    os.remove(wav)

    mix, cen = audio_matcher.decode_presence(film, 20.0, 30.0)
    assert np.array_equal(mix, audio_matcher.decode_audio(film, 20.0, 30.0)), \
        "decode_presence's mix differs from decode_audio's"
    want = audio_matcher.resample(FC[20 * sr:50 * sr], sr, SR)
    k = min(want.size, cen.size)
    r = float(np.corrcoef(cen[:k], want[:k])[0, 1])
    r_mix = float(np.corrcoef(cen[:k], mix[:k])[0, 1])
    print(f"centre: decoded centre against the film's FC r {r:.3f}, against the mix {r_mix:.3f}")
    assert r > 0.99 and r_mix < 0.9, (r, r_mix)
    # a lossy 5.1 track whose centre is empty decodes it to faint dither: no centre either
    hollow_wav, hollow = os.path.join(tmp, "hollow.wav"), os.path.join(tmp, "hollow.ac3")
    _write_wav(hollow_wav, [FL, FR, np.zeros(n), np.zeros(n), SL, SR_], sr)
    subprocess.run([ff, "-hide_banner", "-y", "-i", hollow_wav, "-af",
                    "aformat=channel_layouts=5.1(side)", "-c:a", "ac3", "-b:a", "448k", hollow],
                   check=True, capture_output=True)
    os.remove(hollow_wav)
    for path in (stereo, mono, hollow):
        m, c = audio_matcher.decode_presence(path, 20.0, 30.0)
        assert c is None, f"{os.path.basename(path)} gave a centre"
        assert np.array_equal(m, audio_matcher.decode_audio(path, 20.0, 30.0))
    # a track the two-way decode can't give is decoded as before, the mix alone
    real_both = audio_matcher._decode_both
    audio_matcher._decode_both = lambda *a: np.zeros((0, 2), np.float32)
    try:
        m, c = audio_matcher.decode_presence(film, 20.0, 30.0)
    finally:
        audio_matcher._decode_both = real_both
    assert c is None and np.array_equal(m, mix), "no fallback to the mix alone"

    # the stream: the film's dialogue as it is, its music 7 s later
    moved = np.roll(music, int(7.0 * sr))
    stream_full = FC + 0.707 * 0.35 * (moved + np.roll(moved, int(0.013 * sr)))
    same_full = FC + 0.707 * (FL + FR) + 0.5 * (SL + SR_)
    # syllables to listen at: 0.6 s looks whose centre has speech
    looks, t = [], 15.0
    while len(looks) < 12 and t < 80.0:
        seg = talk[int(t * sr):int((t + 0.6) * sr)]
        if np.mean(np.abs(seg) > 0.05) > 0.5:
            looks.append(t)
            t += 1.3
        t += 0.1
    assert len(looks) >= 8, looks
    got = {"centre": 0, "mix": 0}
    heard_51 = heard_two = 0
    ncc_51, ncc_two = [], []
    for at in looks:
        cap = stream_full[int(at * sr):int((at + 0.6) * sr)].astype(np.float32)
        p = audio_matcher.film_presence(film, cap, sr, at)
        q = audio_matcher.film_presence(stereo, cap, sr, at)
        got[p.by] += 1
        heard_51 += p.state == "heard"
        heard_two += q.state == "heard"
        ncc_51.append(p.ncc)
        ncc_two.append(q.ncc)
        assert p.centre is not None and q.centre is None and q.by == "mix", (p, q)
        assert p.ncc >= q.ncc - 0.02, (p, q)
        if p.state == "heard":
            assert abs(p.shift) < 0.03, p
    m51, m2 = float(np.median(ncc_51)), float(np.median(ncc_two))
    print(f"centre: music moved 7 s - the 5.1 file heard {heard_51} of {len(looks)} looks "
          f"(by centre {got['centre']}, mix {got['mix']}; median ncc {m51:.2f}), "
          f"its stereo copy {heard_two} (median ncc {m2:.2f})")
    assert heard_51 >= len(looks) - 1 and got["centre"] >= len(looks) - 2, (heard_51, got)
    assert heard_two < heard_51 and m51 - m2 > 0.15, (heard_two, m51, m2)
    # where the stream's copy is the file's own, the mix answers as well as ever
    p = audio_matcher.film_presence(film, same_full[int(looks[0] * sr):int((looks[0] + 0.6) * sr)]
                                    .astype(np.float32), sr, looks[0])
    assert p.state == "heard" and p.ncc > 0.7 and abs(p.shift) < 0.03, p
    noise = (0.1 * rng.standard_normal(int(0.6 * sr))).astype(np.float32)
    p = audio_matcher.film_presence(film, noise, sr, looks[0])
    assert p.state != "heard" and p.clear < 0.3, f"noise was heard as the film: {p}"
    # centre=False: the mix alone, exactly as a film without a centre
    cap = stream_full[int(looks[0] * sr):int((looks[0] + 0.6) * sr)].astype(np.float32)
    p, q = (audio_matcher.film_presence(film, cap, sr, looks[0], centre=False),
            audio_matcher.film_presence(film, cap, sr, looks[0]))
    assert p.by == "mix" and p.centre is None and q.centre is not None, (p, q)
    assert abs(q.ncc - max(p.ncc, q.centre.ncc)) < 1e-9, (p, q)    # p.ncc is the mix's
    # ...and so film_presence answers: its look's mix and centre stood in for
    real_look, L = audio_matcher._look, audio_matcher.Look
    for mix_ncc, cen_ncc, want in ((0.10, 0.20, "missing"), (0.10, 0.31, "heard"),
                                   (0.20, 0.10, "unsure")):
        answers = [L(mix_ncc, 0.01, 0.5), L(cen_ncc, 0.02, 0.5)]
        audio_matcher._look = lambda W, C, s_lo: answers.pop(0)
        try:
            p = audio_matcher.film_presence(film, cap, sr, looks[0])
        finally:
            audio_matcher._look = real_look
        assert p.state == want and p.mix.ncc == mix_ncc and p.centre.ncc == cen_ncc, (p, want)
        assert p.by == ("centre" if cen_ncc > mix_ncc else "mix")
        m = audio_matcher.mix_only(p)
        assert m.ncc == mix_ncc and m.centre is None and m.by == "mix", m
    # a DTS track with no core: the two-way decode is tried again in full
    real_both, tried = audio_matcher._decode_both, []

    def both(path, t0, dur, sr_, opts):
        tried.append(tuple(opts))
        return np.zeros((0, 2), np.float32) if opts else real_both(path, t0, dur, sr_, opts)
    audio_matcher._decode_both = both
    try:
        m, c = audio_matcher.decode_presence(film, 20.0, 30.0)
    finally:
        audio_matcher._decode_both = real_both
    assert tried == [audio_matcher.DTS_CORE_ONLY, ()] and c is not None and np.array_equal(m, mix), tried
    # missing stays the mix's call unless the centre hears the film
    L, st = audio_matcher.Look, audio_matcher._presence_state
    for mix_ncc, cen_ncc, want in ((0.10, None, "missing"), (0.20, None, "unsure"),
                                   (0.35, None, "heard"), (0.10, 0.20, "missing"),
                                   (0.10, 0.31, "heard"), (0.20, 0.10, "unsure"),
                                   (0.31, 0.05, "heard")):
        got_state = st(L(mix_ncc, 0.0, 0.5), None if cen_ncc is None else L(cen_ncc, 0.0, 0.5),
                       0.30, 0.15)
        assert got_state == want, (mix_ncc, cen_ncc, got_state, want)
    print("centre: heard by its centre through a different mix; the file's own mix still "
          "heard; noise not; stereo and mono files have no centre; missing is the mix's call")


def survey(tmp):
    """survey_typical: a film whose opening minute is 30 dB under the rest
    takes its typical level from the rest once surveyed - judged on its
    first block alone, nearly all of it would pass for loud."""
    rng = np.random.default_rng(5)
    x = 0.3 * rng.standard_normal(int(200 * SR))
    x[:int(60 * SR)] *= 10 ** (-30 / 20)
    wav = os.path.join(tmp, "quiet-opening.wav")
    with wave.open(wav, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
    audio_matcher.film_block(wav, 0.0)
    first = audio_matcher.typical_level(wav)
    audio_matcher.survey_typical(wav)
    surveyed = audio_matcher.typical_level(wav)
    loud = audio_matcher.film_block(wav, 90.0)
    lv = loud.level_db
    rest = float(np.median(lv[lv > lv.max() - audio_matcher.PRESENCE_LIVE_DB]))
    print(f"survey: typical level {first:.1f} dB from the first block, {surveyed:.1f} dB "
          f"surveyed (the film's loud part: {rest:.1f} dB)")
    assert rest - first > 25, (first, rest)
    assert abs(surveyed - rest) < 3, (surveyed, rest)


def quiet_scene(tmp):
    """A capture from a quiet stretch, searched inside a loud window."""
    rng = np.random.default_rng(3)
    n = int(DUR * SR)
    x = np.zeros(n)
    seg = int(0.25 * SR)
    tt = np.arange(seg) / SR
    for s in range(n // seg):
        for f in np.exp(rng.uniform(np.log(120.0), np.log(5500.0), size=2)):
            x[s * seg:(s + 1) * seg] += 0.35 * np.sin(2 * np.pi * f * tt)
    env = rng.uniform(0.0, 1.0, size=int(DUR * 20)) ** 2
    x += 0.4 * rng.standard_normal(n) * np.interp(
        np.arange(n), np.linspace(0, n, env.size), env)
    x[int(60 * SR):int(150 * SR)] *= 0.03          # a -30 dB scene
    x *= 0.7 / np.abs(x).max()
    wav, clip = os.path.join(tmp, "quiet.wav"), os.path.join(tmp, "quiet.m4a")
    with wave.open(wav, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes((x * 32767).astype(np.int16).tobytes())
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-y",
                    "-i", wav, "-c:a", "aac", "-b:a", "128k", clip],
                   check=True, capture_output=True)
    for truth in (80.0, 100.0, 120.0):
        c = audio_matcher.decode_audio(clip, truth, CAPTURE_S)
        noise = np.convolve(np.random.default_rng(int(truth)).standard_normal(c.size),
                            np.hamming(65), "same")
        noise *= 3.0 * np.sqrt((c * c).mean()) / np.sqrt((noise * noise).mean())
        feats = audio_matcher.prep_capture((c + noise).astype(np.float32),
                                           audio_matcher.SR)
        t, score, z = audio_matcher.find_match_audio(clip, feats, None, None)
        print(f"quiet scene @{truth:.0f}s: matched {t:.3f}s "
              f"(score {score:.3f}, z {z:.1f})")
        assert abs(t - truth) < 0.12, f"quiet scene: wrong match {t:.3f}s"
        assert score >= audio_matcher.SCORE_OK and z >= audio_matcher.Z_OK, \
            f"quiet scene: correct match not trusted (score {score:.3f}, z {z:.1f})"


def dts_core(tmp, clip, feats):
    """A DTS soundtrack is decoded from its core alone, and still found.

    DTS-HD - most Blu-ray remuxes - decodes several times faster without
    its lossless extension. ffmpeg cannot write DTS-HD, so this checks
    what it can: a plain DTS track decodes to exactly the samples a full
    decode gives, and the capture is found in it. A DTS stream with no
    core (DTS Express) decodes to nothing core-only, so decode_audio must
    try again in full - stubbed, as ffmpeg cannot write that either.
    """
    dts = os.path.join(tmp, "clip_dts.mkv")
    made = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-y",
                           "-i", clip, "-ar", "48000", "-ac", "2",
                           "-c:a", "dca", "-strict", "-2", dts],
                          capture_output=True)
    if made.returncode:
        print("DTS track: this ffmpeg cannot write DTS - skipped")
    else:
        core = audio_matcher.decode_audio(dts, 60.0, 30.0)
        full = audio_matcher._decode(dts, 60.0, 30.0, audio_matcher.SR, ())
        assert np.array_equal(core, full), \
            "core-only decoding changed a plain DTS track"
        t, score, z = audio_matcher.find_match_audio(dts, feats,
                                                     TRUTH - 30, TRUTH + 30)
        print(f"DTS track: matched {t:.3f}s (err {abs(t - TRUTH) * 1000:.0f} ms, "
              f"score {score:.3f}, z {z:.1f})")
        assert abs(t - TRUTH) < 0.12, f"DTS track: wrong match {t:.3f}s"
        assert score >= audio_matcher.SCORE_OK and z >= audio_matcher.Z_OK, \
            f"DTS track: match not trusted (score {score:.3f}, z {z:.1f})"

    real, tried = audio_matcher._decode, []

    def decode(path, t0, dur, sr, opts):
        tried.append(tuple(opts))
        return np.zeros(0, np.float32) if opts else real(path, t0, dur, sr, opts)

    audio_matcher._decode = decode
    try:
        x = audio_matcher.decode_audio(clip, 60.0, 10.0)
    finally:
        audio_matcher._decode = real
    print(f"no DTS core: {x.size / audio_matcher.SR:.1f} s decoded in full "
          "after an empty core-only decode")
    assert tried == [audio_matcher.DTS_CORE_ONLY, ()], f"decodes tried: {tried}"
    assert abs(x.size - 10 * audio_matcher.SR) < audio_matcher.SR // 10, \
        f"fallback decoded {x.size} samples"


def chunk_boundary(clip):
    """Long listens across the chunk boundaries of a whole-film search.

    A search longer than CHUNK_S is cut into chunks, and a capture is only
    found where it fits entirely inside one. With an 8 s overlap, the 18 s
    retry listen could not be found in the 10 s before every boundary - it
    was answered from somewhere else. A capture too long for the overlap
    widens it, or the same would happen to it. The chunks are shrunk to
    60 s here so the 3-minute clip has boundaries to sweep across.
    """
    ladder = (controller.AUDIO_SYNC_SECONDS,) + controller.AUDIO_RETRY_SECONDS
    saved = audio_matcher.CHUNK_S
    audio_matcher.CHUNK_S = 60.0
    try:
        def look(truth, seconds):
            c = audio_matcher.decode_audio(clip, truth, seconds)
            noise = np.convolve(np.random.default_rng(int(truth * 10)).standard_normal(c.size),
                                np.hamming(65), "same")
            noise *= 1.2 * np.sqrt((c * c).mean()) / np.sqrt((noise * noise).mean())
            feats = audio_matcher.prep_capture((c + noise).astype(np.float32),
                                               audio_matcher.SR)
            return audio_matcher.find_match_audio(clip, feats, None, None)

        # the controller's weak-match ladder listens longer over the same
        # window; every look must cut the same chunks, so the retries reuse
        # the first look's decode instead of decoding the film again
        audio_matcher._cache.clear()
        for seconds in ladder:
            look(100.3, seconds)
            if seconds == ladder[0]:
                chunks = set(audio_matcher._cache)
            assert set(audio_matcher._cache) == chunks, \
                f"a {seconds:.0f} s look re-decoded the film in new chunks"
        print(f"chunk cache: {len(chunks)} chunks decoded once for "
              f"{'/'.join(f'{s:.0f}' for s in ladder)} s looks")

        def sweep(seconds, truths):
            missed = []
            for truth in truths:
                t, score, z = look(truth, seconds)
                if not (abs(t - truth) < 0.12 and score >= audio_matcher.SCORE_OK
                        and z >= audio_matcher.Z_OK):
                    missed.append(f"{truth:.1f}s -> {t:.1f}s (score {score:.1f}, "
                                  f"z {z:.1f})")
            print(f"{seconds:.0f} s capture swept across a chunk boundary: "
                  f"{len(missed)} missed")
            assert not missed, (f"blind band at a chunk boundary ({seconds:.0f} s "
                                "capture): " + ", ".join(missed))

        sweep(max(ladder), np.arange(38.3, 57.0, 1.0))   # across the one at 60 s
        # a capture too long for OVERLAP_S widens the overlap to its own
        # length plus a second; at a fixed 20 s, a 25 s one could not be
        # found starting 35-40 s, before the boundary at 60 s
        sweep(25.0, np.arange(34.3, 41.0, 1.0))
    finally:
        audio_matcher.CHUNK_S = saved


def long_film_retries():
    """The retry ladder over a long film reuses the first look's decode.

    A whole-film search visits its chunks in order, so a decode cache one
    chunk short of the film misses on every one of them and the 12 s and
    18 s looks decode the whole film again. The 20 s overlap once took a
    2 h 57 m film from 12 chunks to 13, one more than the cache kept.
    Decoding and scoring are stubbed: only which chunks get decoded
    matters here, and a real film that long would take minutes.
    """
    ladder = (controller.AUDIO_SYNC_SECONDS,) + controller.AUDIO_RETRY_SECONDS
    saved = (audio_matcher.probe, audio_matcher.decode_audio,
             audio_matcher.features, audio_matcher._score_curve)
    decoded = []

    def decode(path, t0, dur, sr=audio_matcher.SR):
        decoded.append((path, t0, dur))
        return np.zeros(sr, np.float32)

    audio_matcher.decode_audio = decode
    audio_matcher.features = lambda x, sr: np.zeros((1, audio_matcher.N_BANDS))
    audio_matcher._score_curve = lambda W, C, w: np.arange(8.0)
    try:
        for duration in (10650.0, 4 * 3600.0):
            audio_matcher.probe = lambda path, d=duration: (d, 0.0)
            audio_matcher._cache.clear()
            path = f"film-{duration:.0f}s.mkv"
            new = []
            for seconds in ladder:
                n = len(decoded)
                capture = np.zeros((int(seconds / audio_matcher.HOP_S),
                                    audio_matcher.N_BANDS))
                audio_matcher.find_match_audio(path, capture, None, None)
                new.append(len(decoded) - n)
            print(f"{duration:.0f} s film, whole-film looks: "
                  + ", ".join(f"{s:.0f} s decoded {k} chunks"
                              for s, k in zip(ladder, new)))
            assert new[0] > 0 and not any(new[1:]), \
                f"{duration:.0f} s film: the retries decoded the film again {new}"
    finally:
        (audio_matcher.probe, audio_matcher.decode_audio,
         audio_matcher.features, audio_matcher._score_curve) = saved
        audio_matcher._cache.clear()


if __name__ == "__main__":
    main()

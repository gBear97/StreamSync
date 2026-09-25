"""Fingerprint + session-math tests, no network or audio devices needed.

Covers: hash robustness across re-encodes, rejection of wrong media,
verify_media offset detection, stream-delay measurement from chunked
voice fingerprints, the viewer's measure loop, and the listen-mode
host's pause bookkeeping.

Delays and offsets are deliberately NOT multiples of the 250 ms
fingerprint hop: the first version of this test used 7.25 s, which the
hop-quantized alignment of the time happened to hit exactly, and so it
never saw errors of up to 125 ms everywhere else.
"""

import os
import queue
import subprocess
import tempfile
import time
import wave

import numpy as np
import imageio_ffmpeg

import audio_matcher
import fingerprint
import matcher
import session

SR = 16000


def synth(dur, seed):
    """Film-like audio: random tone segments + enveloped noise."""
    rng = np.random.default_rng(seed)
    n = int(dur * SR)
    x = np.zeros(n)
    seg = SR // 4
    tt = np.arange(seg) / SR
    ramp = np.minimum(1.0, np.minimum(np.arange(seg), np.arange(seg)[::-1]) / (0.01 * SR))
    for s in range(n // seg):
        for f in np.exp(rng.uniform(np.log(150), np.log(3800), 2)):
            x[s * seg:(s + 1) * seg] += 0.3 * np.sin(2 * np.pi * f * tt) * ramp
    env = rng.uniform(0, 1, int(dur * 20)) ** 2
    x += 0.35 * rng.standard_normal(n) * np.interp(
        np.arange(n), np.linspace(0, n, env.size), env)
    return (0.7 * x / np.abs(x).max()).astype(np.float32)


def encode(x, path, bitrate):
    wav = path + ".wav"
    with wave.open(wav, "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(SR)
        f.writeframes((x * 32767).astype(np.int16).tobytes())
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-y",
                    "-i", wav, "-c:a", "aac", "-b:a", bitrate, path],
                   check=True, capture_output=True)
    os.remove(wav)


def main():
    tmp = tempfile.mkdtemp()
    film = synth(240, seed=11)

    print("encoding two 'releases' of the same film audio...")
    rel_a = os.path.join(tmp, "release_a.m4a")   # the host's copy
    rel_b = os.path.join(tmp, "release_b.m4a")   # the viewer's copy:
    encode(film, rel_a, "128k")                  # different bitrate, gain,
    encode((film * 0.82), rel_b, "96k")          # same content

    host_words = fingerprint.fingerprint_file(rel_a)
    print(f"host fingerprint: {len(host_words)} words "
          f"({len(host_words) * 2 // 1024} KB for {240 / 60:.0f} min)")

    # 1. same media verifies, offset ~0
    delta, ber = session.verify_media(host_words, rel_b)
    print(f"same media: verified, delta {delta:+.3f}s, worst ber {ber:.3f}")
    assert abs(delta) < 0.05, delta
    assert ber < fingerprint.VERIFY_BER

    # 1b. a release with 1.37 s of extra lead-in (another studio logo)
    shifted = os.path.join(tmp, "release_c.m4a")
    lead = np.zeros(int(1.37 * SR), np.float32)
    encode(np.concatenate([lead, film * 0.9]), shifted, "96k")
    delta, ber = session.verify_media(host_words, shifted)
    print(f"shifted release: delta {delta:+.3f}s (true +1.370s), ber {ber:.3f}")
    assert abs(delta - 1.37) < 0.04, delta

    # 2. wrong media is rejected
    other = os.path.join(tmp, "other.m4a")
    encode(synth(240, seed=99), other, "128k")
    try:
        session.verify_media(host_words, other)
        raise AssertionError("wrong media was NOT rejected")
    except matcher.MatchError as e:
        print(f"wrong media: correctly rejected ({str(e)[:60]}...)")

    # 3. voice delay measurement: host words cut on the UTC grid, as
    # HostSession sends them
    voice = synth(120, seed=42)
    base_utc = 1_000_000.0                  # a multiple of the hop
    n = int(round(session.VOICE_CHUNK / fingerprint.FP_HOP))
    hop = int(fingerprint.FP_HOP * SR)
    span = int((session.VOICE_CHUNK + fingerprint.FP_WIN) * SR)
    # a block reaches a viewer this long after it starts: the host sends
    # it once its span is in, then fingerprinting and the relay hop
    arrives = session.VOICE_CHUNK + fingerprint.FP_WIN + 0.5

    def held(by=None):
        """The voice buffer a viewer holds at UTC `by` (None: all of it)."""
        b = session.VoiceBuffer()
        for i in range(0, len(voice) - span, n * hop):
            if by is not None and base_utc + i / SR + arrives > by:
                break
            words = fingerprint.fingerprint_samples(voice[i:i + span], SR)[:n]
            b.add(base_utc + i / SR, words)
        return b

    rng = np.random.default_rng(5)

    def heard(start):
        """MEASURE_SECONDS of the host's voice from `start`, as a viewer's
        stream delivers it."""
        seg = voice[int(start * SR):int((start + session.MEASURE_SECONDS) * SR)]
        return np.clip(seg * 0.9 + 0.15 * np.convolve(
            rng.standard_normal(seg.size), np.hamming(33), "same").astype(np.float32),
            -1, 1)

    buf = held()
    # late enough that the 90 s correlation window is inside the buffer
    # (in production the voice buffer runs continuously, so this is the
    # normal situation after ~90 s of session)
    errs = []
    for true_delay in (7.25, 7.10, 7.375, 7.40, 7.61, 12.93):
        start = 95.0 - (true_delay - 7.25)      # keep the probe in range
        probe_t0 = base_utc + start + true_delay       # heard this late
        d = session.measure_delay(buf, heard(start), SR, probe_t0)
        assert d is not None, f"delay {true_delay}: inconclusive"
        errs.append(d - true_delay)
        print(f"delay measurement: true {true_delay:.3f}s, measured {d:.3f}s "
              f"(err {1000 * (d - true_delay):+.0f} ms)")
    assert max(abs(e) for e in errs) < 0.04, errs

    # 3a. short delays. A probe heard D late holds host voice up to D
    # before its own end, so a reference stopping 5 s into a 10 s probe
    # could never align a stream under 5 s behind - those viewers sat on
    # the host's default_delay hint all party. A stream with next to no
    # delay can also align a little under 0 (clock sync, device latency);
    # that reads as 0. At -0.8 s the probe's words (9.5 s of a 10 s probe)
    # end 0.3 s past the probe, so they fit only because the reference
    # runs MEASURE_EARLY past its end.
    for true_delay in (0.0, 0.96, 2.07, 3.96, -0.37, -0.8):
        probe_t0 = base_utc + 95.0 + true_delay
        d = session.measure_delay(buf, heard(95.0), SR, probe_t0)
        assert d is not None, f"delay {true_delay}: inconclusive"
        print(f"short delay: true {true_delay:.3f}s, measured {d:.3f}s")
        assert abs(d - max(0.0, true_delay)) < 0.04, (true_delay, d)

    # 3b. when the probe ends, the host is still recording the voice it
    # ends on. VOICE_SETTLE later, wherever the probe falls against the
    # host's blocks, a viewer holds all of that voice (and MEASURE_EARLY
    # past it), and one with no delay at all measures.
    for phase in (0.0, 1.1, 2.3, 3.6):
        probe_t0 = base_utc + 95.0 + phase
        probe_end = probe_t0 + session.MEASURE_SECONDS
        have = held(by=probe_end + session.VOICE_SETTLE)
        cov = have.timeline(probe_t0, probe_end + session.MEASURE_EARLY)[2]
        assert cov == 1.0, (phase, cov)
        d = session.measure_delay(have, heard(95.0 + phase), SR, probe_t0)
        assert d is not None and d < 0.04, (phase, d)
    print("short delay: the voice a probe ends on has arrived when it is measured")

    # 3c. a young session: the viewer holds only the voice sent since it
    # joined (at base_utc here). Coverage scored over the whole 90 s
    # look-back kept every viewer on the host's hint for its first ~80 s.
    for start, true_delay in ((4.0, 2.07), (8.5, 3.96), (13.0, 0.96)):
        probe_t0 = base_utc + start
        at = probe_t0 + session.MEASURE_SECONDS + session.VOICE_SETTLE
        d = session.measure_delay(held(by=at), heard(start - true_delay), SR,
                                  probe_t0)
        assert d is not None, f"{at - base_utc:.1f} s in: inconclusive"
        print(f"young session: {at - base_utc:.1f} s in, true {true_delay:.3f}s, "
              f"measured {d:.3f}s")
        assert abs(d - true_delay) < 0.04, (start, true_delay, d)

    # 3d. ...while a real gap in the host's voice still fails the coverage
    # gate: the host's mic died for 40 s of the look-back, even though the
    # voice this probe heard came through
    probe = heard(95.0 - 7.25)
    d = session.measure_delay(buf, probe, SR, base_utc + 95.0)
    assert d is not None and abs(d - 7.25) < 0.04, d
    holed = held()
    with holed.lock:
        holed.blocks = [b for b in holed.blocks
                        if not base_utc + 40 <= b[0] < base_utc + 80]
    d = session.measure_delay(holed, probe, SR, base_utc + 95.0)
    assert d is None, f"measured {d} across a 40 s voice dropout"
    print("coverage gate: a 40 s voice dropout is still rejected")

    # 4. timeline math: delayed rendering delays pauses too
    tl = session.StateTimeline()
    tl.add({"pos": 100.0, "utc": 1000.0, "playing": True, "default_delay": 8})
    tl.add({"pos": 130.0, "utc": 1030.0, "playing": False})
    pos, playing = tl.at(1030.0 - 8.0)   # viewer 8s behind at host pause time
    assert playing and abs(pos - 122.0) < 0.01, (pos, playing)
    pos, playing = tl.at(1039.0)         # 9s later the pause reaches them
    assert not playing and abs(pos - 130.0) < 0.01, (pos, playing)
    print("timeline math: delayed pause lands correctly")

    listen_mode_pause()
    measure_loop()

    print("FINGERPRINT TEST PASSED")


def listen_mode_pause():
    """A listen-mode host pauses at utc 1012 (film 112) after a good match
    at utc 1000 (film 100); checks run every 4 s.

    The old code relabelled the 1000/100 anchor "paused", so viewers were
    sent back to 100 and paused ~12 s early. Now: one miss is not a pause,
    two are, the pause lands where the audio stopped, and the resume is
    dated to when playback restarted.
    """
    tr = session.ListenTracker()
    tl = session.StateTimeline()

    def publish(state):
        if state is not None:
            pos, utc, playing = state
            tl.add({"pos": pos, "utc": utc, "playing": playing,
                    "default_delay": 10})

    publish(tr.hit(100.0, 1000.0))
    publish(tr.hit(104.0, 1004.0))
    publish(tr.hit(108.0, 1008.0))
    # the 1012 recording is silent from its start; so is the 1016 one
    publish(tr.miss(1012.0, stopped_utc=1012.0))
    assert tr.anchor[2], "one miss must not count as a pause"
    publish(tr.miss(1016.0, stopped_utc=1012.0))
    pos, at, playing = tr.anchor
    assert not playing and abs(pos - 112.0) < 1e-6 and at == 1012.0, tr.anchor
    for wall, want_pos, want_play in ((1018.0, 108.0, True), (1021.9, 111.9, True),
                                      (1024.0, 112.0, False)):
        p, pl = tl.at(wall - 10.0)
        assert pl == want_play and abs(p - want_pos) < 1e-6, (wall, p, pl)
    print("listen-mode pause: viewers pause at 112 when it reaches them, "
          "not back at 100")

    # host resumes at utc 1030; the 1032 recording finds film 114
    publish(tr.hit(114.0, 1032.0))
    pos, at, playing = tr.anchor
    assert playing and abs(pos - 112.0) < 1e-6 and abs(at - 1030.0) < 1e-6, tr.anchor
    p, pl = tl.at(1031.0)
    assert pl and abs(p - 113.0) < 1e-6, (p, pl)
    print("listen-mode resume: dated to when playback restarted (1030), "
          "not when it was heard (1032)")

    # a host who jumps elsewhere while paused is not back-dated
    tr2 = session.ListenTracker()
    tr2.hit(100.0, 1000.0)
    tr2.miss(1004.0, 1004.0)
    tr2.miss(1008.0, 1004.0)
    assert tr2.hit(900.0, 1020.0) == (900.0, 1020.0, True)


def measure_loop():
    """ViewerSession._measure_loop, fed by a fake loopback monitor.

    A probe ends while the host is still recording the voice it ends on,
    and the host sends that voice only once its block is whole: measured
    straight away, the reference is missing the probe's tail and a short
    delay cannot align (3b). The loop has to wait VOICE_SETTLE first -
    and, if the viewer leaves meanwhile, not measure at all.

    A young viewer's look-back stops at the voice it holds (3c), so a
    stream further behind than that can only align by chance - and does,
    now and then. Such a measurement is used only once a second agrees.
    """
    settle = 0.3
    saved = {k: getattr(session, k, None) for k in
             ("VOICE_SETTLE", "MEASURE_INTERVAL", "measure_delay")}
    saved_monitor = session.audio_capture.AudioMonitor

    class UtcClock:
        def perf_to_utc(self, perf):
            return perf         # the fake probes are dated in UTC already

    def run(probes, leave=None, results=(), held_from=None):
        """A viewer's measure loop over probes heard from each UTC in
        `probes`, measure_delay returning `results` in turn (then None).
        The viewer holds host voice from UTC `held_from` on, and leaves as
        probe `leave` ends. Returns, per measurement: seconds after its
        probe ended ("dt"), the probe's UTC ("t0"), the viewer's delay
        after it ("delay"), and whether the next probe went straight on
        from the audio after it, with no pause ("follow")."""
        viewer = session.ViewerSession("ws://unused", "ROOM", None, None,
                                       queue.Queue())
        viewer.clock = UtcClock()
        if held_from is not None:
            viewer.voice.add(held_from, np.zeros(16, np.uint16))
        probes, results = list(probes), list(results)
        ended, measured = [], []
        paused = [False]
        settle_or_pause = viewer.stop_flag.wait

        def wait(timeout=None):
            paused[0] = True        # only the pause comes after a measurement
            return settle_or_pause(timeout)

        viewer.stop_flag.wait = wait

        class Monitor:
            end = None              # frame the last probe ended at

            def mark(self):
                return "now"

            def capture_span(self, seconds, start=None):
                if measured:
                    measured[-1]["delay"] = viewer.delay
                    measured[-1]["follow"] = (start == self.end
                                              and not paused[0])
                if not probes:
                    viewer.stop_flag.set()
                    raise RuntimeError("no more probes")
                if len(ended) == leave:
                    viewer.stop_flag.set()
                ended.append(time.perf_counter())
                t0 = probes.pop(0)
                frame = int(round((t0 - 1000.0) * SR))
                samples = np.zeros(int(seconds * SR), np.float32)
                self.end = frame + samples.size
                return samples, SR, t0, frame

            def stop(self):
                pass

        def measure(voice_buf, samples, sr, probe_t0):
            measured.append({"dt": time.perf_counter() - ended[-1],
                             "t0": probe_t0})
            paused[0] = False
            return results.pop(0) if results else None

        session.audio_capture.AudioMonitor = lambda *a, **kw: Monitor()
        session.measure_delay = measure
        viewer._measure_loop()
        return measured

    try:
        session.VOICE_SETTLE = settle
        # no pause between probes beyond the settle wait
        session.MEASURE_INTERVAL = session.MEASURE_SECONDS + settle
        measured = run([1020.0, 1045.0, 1070.0])
        assert [m["t0"] for m in measured] == [1020.0, 1045.0, 1070.0], measured
        dt = min(m["dt"] for m in measured)
        assert dt > 0.8 * settle, measured
        print(f"measure loop: measures {dt:.2f} s after each probe ends "
              f"(VOICE_SETTLE {settle} s here)")
        measured = run([1020.0, 1045.0], leave=1)
        assert [m["t0"] for m in measured] == [1020.0], measured
        print("measure loop: a viewer who leaves while the voice settles "
              "does not measure")

        # A viewer who joined at UTC 1000, 45 s behind the host, holds
        # none of the voice its probes heard. A chance alignment (0.6 s)
        # is not used: the next probe follows straight on, and its own
        # measurement disagrees - and so waits for a third that agrees.
        # (Used outright, 0.6 s would have taken seven more 25 s rounds
        # of the moving average to get within 5 s of the truth.)
        m = run([1020.0, 1030.0, 1040.0], results=[0.6, 45.0, 45.02],
                held_from=1000.0)
        assert [x["delay"] for x in m] == [None, None, 45.02], m
        assert [x["follow"] for x in m] == [True, True, False], m
        print("measure loop: a young viewer's chance alignment is not used; "
              "two measurements that agree are")

        # a short delay is used as soon as the probe after it agrees, and
        # a chance alignment after that does not drag it off
        m = run([1020.0, 1030.0, 1055.0, 1065.0],
                results=[2.07, 2.09, 38.4, 2.1], held_from=1000.0)
        assert [x["delay"] for x in m] == [
            None, 2.09, 2.09, 0.7 * 2.09 + 0.3 * 2.1], m
        assert [x["follow"] for x in m] == [True, False, True, False], m
        print("measure loop: a young viewer's delay is used from the second "
              "probe, and a later chance alignment is not averaged in")

        # once the voice held spans the whole look-back, a measurement is
        # used outright, as in v1.0.7 - whatever came before it. (Joined
        # at 999: the look-back from 1080 starts before that, from 1090
        # it does not.)
        m = run([1080.0, 1090.0], results=[30.0, 7.25], held_from=999.0)
        assert [x["delay"] for x in m] == [None, 7.25], m
        print("measure loop: with the full look-back held, a delay is used "
              "at once")
    finally:
        for k, v in saved.items():
            setattr(session, k, v)
        session.audio_capture.AudioMonitor = saved_monitor


if __name__ == "__main__":
    main()

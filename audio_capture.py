"""System-audio capture.

Windows: WASAPI loopback via the `soundcard` package - records whatever is
playing on a speaker/headphone device, no setup needed.

macOS: there is no OS loopback API, so the stream's audio is picked up
through the BlackHole virtual audio device. The user routes sound to a
Multi-Output Device (real speakers + BlackHole) and we record from
BlackHole like a normal microphone. All the same functions apply; a
"speaker name" on macOS is really an input-device name.
"""

import sys
import time

import numpy as np

CAPTURE_SR = 48000  # native rate on virtually all devices
IS_MAC = sys.platform == "darwin"


def list_speakers():
    """Names of capturable sources (Windows: speakers; macOS: inputs)."""
    import soundcard as sc
    if IS_MAC:
        names = [m.name for m in sc.all_microphones()]
        # BlackHole is the loopback carrier on macOS - surface it first
        names.sort(key=lambda n: 0 if "blackhole" in n.lower() else 1)
        return names
    return [s.name for s in sc.all_speakers()]


def default_speaker_name():
    import soundcard as sc
    if IS_MAC:
        for name in list_speakers():
            if "blackhole" in name.lower():
                return name
        try:
            return sc.default_microphone().name
        except Exception:
            return ""
    return sc.default_speaker().name


def _pick_mac_source(sc, speaker_name):
    mics = sc.all_microphones()
    if speaker_name:
        for m in mics:
            if speaker_name.lower() in m.name.lower():
                return m
    for m in mics:
        if "blackhole" in m.name.lower():
            return m
    return sc.default_microphone()


def list_microphones():
    """Real input devices (the host's mic for voice fingerprinting)."""
    import soundcard as sc
    return [m.name for m in sc.all_microphones()]


PRIME_S = 0.05   # discarded lead-in that drains the device's first buffers


def _timed_record(mic, seconds, sr, cancel=None):
    """Record `seconds` from `mic`, returning (samples, perf_counter_at_start).

    `mic.record()` would open and start the device *inside* the call, so a
    timestamp taken before it runs is early by the open latency (measured
    ~140-280 ms on macOS/CoreAudio) - and sync_seek treats that error as
    elapsed stream time, landing every seek that far ahead. Opening the
    recorder explicitly, then discarding a short priming read, means t0 is
    stamped with the stream already flowing and its first buffers drained.

    Reads in quarter-second blocks so a 30 s listen can be abandoned:
    `cancel` (a callable -> bool) is checked between blocks and raises.
    """
    with mic.recorder(samplerate=sr) as rec:
        rec.record(numframes=int(PRIME_S * sr))
        t0 = time.perf_counter()
        chunks = []
        remaining = int(seconds * sr)
        block = int(0.25 * sr)
        while remaining > 0:
            if cancel is not None and cancel():
                raise RuntimeError("capture stopped")
            n = min(block, remaining)
            chunks.append(rec.record(numframes=n))
            remaining -= n
        data = np.concatenate(chunks)
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data.astype(np.float32), t0


def record_mic(seconds, mic_name=None, sr=CAPTURE_SR):
    """Record from a microphone. Returns (mono float32, sr, perf_t0).

    Unlike record_loopback, silence is fine here - a streamer pausing
    between sentences is normal, not an error.
    """
    import soundcard as sc
    mic = None
    if mic_name:
        for m in sc.all_microphones():
            if mic_name.lower() in m.name.lower():
                mic = m
                break
    if mic is None:
        mic = sc.default_microphone()
    data, t0 = _timed_record(mic, seconds, sr)
    return data, sr, t0


def record_loopback(seconds, speaker_name=None, sr=CAPTURE_SR, cancel=None):
    """Record `seconds` of the stream's audio.

    Returns (mono float32 samples, sample_rate, perf_counter_at_start).
    `cancel` (callable -> bool) aborts between blocks with a RuntimeError.
    """
    import soundcard as sc
    if IS_MAC:
        mic = _pick_mac_source(sc, speaker_name)
        silence_hint = (
            f"Captured only silence from '{mic.name}'. On macOS the stream "
            "must be routed through BlackHole: install BlackHole, create a "
            "Multi-Output Device (your speakers + BlackHole) in Audio MIDI "
            "Setup, select it as the system output, and pick BlackHole "
            "under 'Listen on'.")
    else:
        spk = None
        if speaker_name:
            for s in sc.all_speakers():
                if speaker_name.lower() in s.name.lower():
                    spk = s
                    break
        if spk is None:
            spk = sc.default_speaker()
        mic = sc.get_microphone(spk.name, include_loopback=True)
        silence_hint = (
            f"Captured only silence from '{spk.name}' - is the stream "
            "audible on that device?")

    data, t0 = _timed_record(mic, seconds, sr, cancel=cancel)
    if float(np.abs(data).max(initial=0.0)) < 1e-4:
        raise RuntimeError(silence_hint)
    return data, sr, t0

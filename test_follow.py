"""Watch-party following, without a relay: what the host sends when its
film plays, pauses or jumps, and what a viewer's player is told.

Found on 2026-10-22, with the PC hosting through a Cloudflare tunnel and
the Mac following:
- the host sent its state only on the 2 s heartbeat, so a viewer played
  on ~2 s past every pause and stepped back, and a jump landed as late;
- a viewer's loop woke once a second, adding up to a second more;
- a jump the host made while paused reached no viewer until it played;
- a film that would not start (2.0.6 could not restart one that had
  played to its end) left the viewer silent, "Following the session"
  while it stood still.

Still open after 2.0.8, and checked here with the real EmbeddedPlayer over
test_players' fake of libvlc 3 as measured:
- a host whose film had not played yet sent no state at all, so viewers
  got nothing until the host's first Play;
- a viewer whose film had not played showed 0:00 for a paused host, and
  for every jump it made while paused: libvlc 3 ignores a seek until a
  film has played.

The host's _state_loop and the viewer's _follow_loop run in threads over
fakes on this machine's clock (UTC = perf_counter here).
"""

import queue
import threading
import time

import players
import session
import test_players


class Clock:
    """One machine and no NTP: UTC is perf_counter."""
    def perf_to_utc(self, perf):
        return perf

    def utc(self):
        return time.perf_counter()


class Source:
    """A host's player, as clock_state reads it: (pos, playing, perf)."""
    def __init__(self, pos=100.0):
        self.lock = threading.Lock()
        self.pos, self.at, self.playing = pos, time.perf_counter(), True

    def __call__(self):
        now = time.perf_counter()
        with self.lock:
            return self.pos + (now - self.at if self.playing else 0.0), self.playing, now

    def _set(self, pos=None, playing=None):
        now = time.perf_counter()
        with self.lock:
            here = self.pos + (now - self.at if self.playing else 0.0)
            self.pos, self.at = (here if pos is None else pos), now
            if playing is not None:
                self.playing = playing
        return now

    def pause(self):
        return self._set(playing=False)

    def play(self):
        return self._set(playing=True)

    def seek(self, pos):
        return self._set(pos=pos)


class Link:
    """What the host sends, and when."""
    def __init__(self):
        self.sent = []
        self.lock = threading.Lock()

    def send(self, obj):
        with self.lock:
            self.sent.append((time.perf_counter(), obj))

    def states(self):
        with self.lock:
            return [(t, m) for t, m in self.sent if m.get("type") == "state"]

    def first_after(self, since, pred, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for t, m in self.states():
                if t >= since and pred(m):
                    return t, m
            time.sleep(0.01)
        raise AssertionError("the host never sent it")


class VLCError(RuntimeError):
    pass


class Player:
    """A viewer's player: does what it is told and notes when."""
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []           # (perf, name, arg)
        self.anchor = None        # (pos, perf)
        self.playing = False
        self.fail = None          # raised by sync_seek while set

    def _note(self, name, arg=None):
        with self.lock:
            self.calls.append((time.perf_counter(), name, arg))

    def pos_at(self, perf):
        with self.lock:
            if self.anchor is None:
                return None
            p, t = self.anchor
            return p + (perf - t if self.playing else 0.0)

    def clock(self):
        now = time.perf_counter()
        return (self.pos_at(now), now) if self.playing and self.anchor else None

    def sync_seek(self, match_t, t0, offset, waiting=None):
        self._note("sync_seek", match_t)
        if self.fail is not None:
            raise self.fail
        now = time.perf_counter()
        with self.lock:
            self.anchor, self.playing = (match_t + (now - t0) + offset, now), True

    def pause_at(self, t, waiting=None):
        self._note("pause_at", t)
        with self.lock:
            self.anchor, self.playing = (t, time.perf_counter()), False

    def called(self, name):
        with self.lock:
            return [(t, a) for t, n, a in self.calls if n == name]


def wait(cond, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def viewer(delay, *states, player=None):
    """A viewer `delay` behind, holding `states` already, its receive and
    follow loops running."""
    v = session.ViewerSession("ws://unused", "ROOM", None, player or Player(),
                              queue.Queue())
    v.clock, v.delay = Clock(), delay
    for s in states:
        v.timeline.add(s)
    for loop in (v._recv_loop, v._follow_loop):
        threading.Thread(target=loop, daemon=True).start()
    return v


def arrives(v, pos, utc, playing):
    """A host state, as the relay hands it to the viewer."""
    v.inbox.put({"type": "state", "pos": pos, "utc": utc, "playing": playing})


def end(v):
    v.stop_flag.set()
    v._wake.set()


def said(v, seen=None):
    seen = [] if seen is None else seen
    while not v.events.empty():
        kind, text = v.events.get()
        if kind == "session":
            seen.append(text)
    return seen


def changed():
    """state_changed: a flip of playing, or a position off the last line."""
    f = session.state_changed
    assert f(None, (5.0, 0.0, True))
    assert not f((100.0, 10.0, True), (102.0, 12.0, True)), "steady play is no change"
    assert not f((100.0, 10.0, True), (102.1, 12.0, True)), "0.1 s of wobble is no change"
    assert f((100.0, 10.0, True), (102.0, 12.0, False)), "a pause"
    assert f((100.0, 10.0, False), (100.0, 12.0, True)), "a play"
    assert f((100.0, 10.0, True), (400.0, 12.0, True)), "a jump while playing"
    assert f((100.0, 10.0, False), (40.0, 12.0, False)), "a jump while paused"
    assert not f((100.0, 10.0, False), (100.0, 99.0, False)), "a long pause is no change"
    tl = session.StateTimeline()
    tl.add({"pos": 1.0, "utc": 10.0, "playing": True})
    tl.add({"pos": 5.0, "utc": 14.0, "playing": False})
    assert (tl.next_after(9.0), tl.next_after(10.0), tl.next_after(14.0)) == (10.0, 14.0, None)
    print("state_changed: plays, pauses and jumps are changes; steady play, "
          "wobble and a long pause are not")


def host_sends_changes_at_once():
    src = Source(100.0)
    host = session.HostSession("ws://unused", None, queue.Queue(), position_source=src)
    host.clock = Clock()
    host.link = link = Link()
    threading.Thread(target=host._state_loop, daemon=True).start()
    try:
        link.first_after(0, lambda m: True)
        time.sleep(2.5)                     # playing steadily: the heartbeat only
        n = len(link.states())
        assert 2 <= n <= 3, f"{n} states in 2.5 s of steady play"

        t = src.pause()
        at, m = link.first_after(t, lambda m: not m["playing"])
        print(f"host: a pause went out after {(at - t) * 1000:.0f} ms")
        assert at - t < 0.4, at - t
        assert abs(m["pos"] - src()[0]) < 0.01, m

        t = src.seek(500.0)                 # a jump while paused
        at, m = link.first_after(t, lambda m: abs(m["pos"] - 500.0) < 0.01)
        assert at - t < 0.4 and not m["playing"], (at - t, m)

        t = src.play()
        at, m = link.first_after(t, lambda m: m["playing"])
        assert at - t < 0.4, at - t

        t = src.seek(20.0)                  # a jump while playing
        at, m = link.first_after(t, lambda m: m["pos"] < 21.0)
        assert at - t < 0.4, at - t
        print("host: a pause, a play and jumps go out within "
              f"{session.STATE_CHECK:.1f} s, not on the next heartbeat")
    finally:
        host.stop_flag.set()


def pause_lands_when_it_reaches_the_viewer():
    """A viewer 1.3 s behind: the old loop came round once a second and
    applied a pause up to a second after it reached them - here, about
    0.4 s."""
    start = time.perf_counter()
    v = viewer(1.3, {"pos": 100.0, "utc": start - 5.0, "playing": True})
    try:
        wait(lambda: v.player.called("sync_seek"), 2, "the film to play")
        pause_utc = time.perf_counter() + 0.3
        arrives(v, 100.0 + (pause_utc - (start - 5.0)), pause_utc, False)
        wait(lambda: v.player.called("pause_at"), 4, "the pause")
        at, frame = v.player.called("pause_at")[0]
        late = at - (pause_utc + 1.3)
        print(f"viewer: paused {late * 1000:+.0f} ms from when it reached them")
        assert -0.05 < late < 0.15, late
        assert abs(frame - (pause_utc - start + 105.0)) < 1e-6, frame
    finally:
        end(v)


def paused_jump_reaches_the_viewer():
    now = time.perf_counter()
    v = viewer(0.5, {"pos": 100.0, "utc": now - 5.0, "playing": False})
    try:
        wait(lambda: v.player.called("pause_at"), 2, "the paused frame")
        # this viewer's own nudge: the player has made it, and the loop
        # leaves it be - the host has not moved
        v.offset = 0.5
        time.sleep(1.2)
        assert [a for _, a in v.player.called("pause_at")] == [100.0], v.player.calls

        jump = time.perf_counter()
        arrives(v, 500.0, jump, False)
        wait(lambda: len(v.player.called("pause_at")) == 2, 3, "the jump")
        at, to = v.player.called("pause_at")[1]
        assert abs(to - 500.5) < 1e-6, to
        assert abs(at - (jump + 0.5)) < 0.15, at - jump
        assert not v.player.called("sync_seek"), "it played the film to move it"
        print("viewer: a host's jump while paused moves the paused film, "
              "nudge kept, as it reaches them")
    finally:
        end(v)


def libvlc3_film(**kw):
    """The real EmbeddedPlayer over test_players' libvlc 3, a film loaded
    and not played. players.vlc must be the fake's while it is used."""
    mp = test_players.Libvlc3MP(**kw)
    film = players.EmbeddedPlayer._around(None, mp)
    film.has_media = True
    return film, mp


def host_sends_its_start_before_play():
    """A host whose film has not played yet: libvlc has no time for it,
    and the host sent viewers nothing at all until its first Play. The
    film is at its start, paused, and that goes out at once; the Play
    goes out as it lands."""
    saved = players.vlc
    players.vlc = test_players.FAKE_VLC
    film, _mp = libvlc3_film(opens_after=0.1)
    host = session.HostSession("ws://unused", None, queue.Queue(),
                               position_source=film.clock_state)
    host.clock = Clock()
    host.link = link = Link()
    began = time.perf_counter()
    threading.Thread(target=host._state_loop, daemon=True).start()
    try:
        first, m = link.first_after(0, lambda m: True)
        assert (m["pos"], m["playing"]) == (0.0, False), m
        assert first - began < 0.3, first - began
        t = time.perf_counter()
        film.ensure_playing()
        _at, m = link.first_after(t, lambda m: m["playing"])
        assert 0.0 <= m["pos"] < 0.5, m
        print(f"host: a film not played yet goes out at 0:00, paused, "
              f"{(first - began) * 1000:.0f} ms in; its first Play as it lands")
    finally:
        host.stop_flag.set()
        players.vlc = saved


def unplayed_viewer_meets_a_paused_host():
    """A viewer whose film had never played showed 0:00 for a paused host,
    and for each jump the host made while paused, until the host played:
    libvlc 3 ignores a seek until a film has played. The film now shows
    the host's paused frame - started for a moment to get there, muted -
    and a paused jump moves it."""
    S = test_players.FAKE_VLC.State
    saved = players.vlc
    players.vlc = test_players.FAKE_VLC
    film, mp = libvlc3_film(opens_after=0.15)
    film.set_mute(False)                 # this viewer has the film's sound on
    v = viewer(0.5, {"pos": 100.0, "utc": time.perf_counter() - 5.0, "playing": False},
               player=film)
    try:
        wait(lambda: mp.get_time() == 100000, 3, "the host's paused frame")
        assert mp.get_state() == S.Paused and not mp.heard(), mp.log
        arrives(v, 500.0, time.perf_counter(), False)
        wait(lambda: mp.get_time() == 500000, 3, "the paused jump")
        assert mp.get_state() == S.Paused and mp.muted is False, mp.log
        assert mp.calls("play") == [("play", S.NothingSpecial)], mp.log
        assert said(v) == [], "the viewer reported trouble"
        print("viewer: a film that never played shows a paused host's frame and "
              "its paused jumps, started once, muted till paused")
    finally:
        end(v)
        players.vlc = saved


def failures_are_said_once():
    logged = []
    real_log = session.diagnostics.log
    session.diagnostics.log = lambda message, echo=True: logged.append(message)
    v = None
    try:
        why = ("VLC could not play the film - is it still where it was? "
               "Open it again to check.")
        player = Player()
        player.fail = VLCError(why)
        v = viewer(0.2, {"pos": 100.0, "utc": time.perf_counter() - 5.0,
                         "playing": True}, player=player)
        wait(lambda: len(v.player.called("sync_seek")) >= 3, 5, "three tries")
        seen = said(v)
        assert seen == [f"Could not follow the host: {why}"], seen

        v.player.fail = None
        wait(lambda: "Following the session again." in said(v, seen), 3, "the recovery")
        assert seen[-1] == "Following the session again." and v.player.playing, seen
        assert len(logged) == 2 and "could not follow" in logged[0], logged
        print("viewer: a film that will not start is said once, tried again, "
              "and its recovery said")
    finally:
        session.diagnostics.log = real_log
        if v is not None:
            end(v)


def main():
    changed()
    host_sends_changes_at_once()
    host_sends_its_start_before_play()
    pause_lands_when_it_reaches_the_viewer()
    paused_jump_reaches_the_viewer()
    unplayed_viewer_meets_a_paused_host()
    failures_are_said_once()
    print("FOLLOW TEST PASSED")


if __name__ == "__main__":
    main()

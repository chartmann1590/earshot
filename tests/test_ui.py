# SPDX-License-Identifier: GPL-3.0-or-later
"""Drives the real Earshot window inside the Luma runtime, offline (needs a display; run with xvfb-run).

    xvfb-run flatpak run --command=python3 org.projectluma.Sdk//44 tests/test_ui.py

Episodes are short WAV files written here, played through a fake audio sink, so playback, resuming,
Up next and "played" are exercised for real without a network or speakers.
"""
import os
import struct
import sys
import tempfile
import time
import traceback
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
TMP = tempfile.mkdtemp()
os.environ["XDG_DATA_HOME"] = os.path.join(TMP, "data")
os.environ["XDG_CACHE_HOME"] = os.path.join(TMP, "cache")
os.environ["EARSHOT_AUDIO_SINK"] = "fakesink"

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib  # noqa: E402

from earshot import feeds  # noqa: E402

RESULTS = []


def tone(path, seconds):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(struct.pack("<h", (i % 40 - 20) * 300) for i in range(8000 * seconds)))
    return path


def pump(seconds=0.3):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.005)


def wait_for(cond, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pump(0.05)
        if cond():
            return True
    return False


def feed_xml(files):
    items = "".join(f"""<item><title>Episode {i}</title><guid>g{i}</guid>
        <pubDate>Mon, {10 + i:02d} Nov 2026 08:00:00 GMT</pubDate>
        <enclosure url="https://example.invalid/{i}.wav" type="audio/wav" length="1"/>
        <itunes:duration>{secs}</itunes:duration>
        <description>&lt;p&gt;Jump to 0:02 for the good bit.&lt;/p&gt;</description></item>"""
                    for i, (_p, secs) in enumerate(files))
    return f"""<?xml version="1.0"?><rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
        <channel><title>Test Show</title><itunes:author>Tester</itunes:author>{items}</channel></rss>""".encode()


def check(name, fn):
    try:
        fn()
        RESULTS.append((name, None))
    except Exception:
        RESULTS.append((name, traceback.format_exc()))


def run(w):
    url = "https://example.invalid/feed.xml"
    files = [(tone(os.path.join(TMP, f"{i}.wav"), secs), secs) for i, secs in enumerate((4, 3, 30))]
    show = feeds.parse(feed_xml(files), url)
    w.store.subscribe(url, show, fresh=3)
    eps = {e["title"]: e for e in w.store.episodes(url)}
    for i, (path, _s) in enumerate(files):
        w.store.set_file(eps[f"Episode {i}"]["id"], path)   # "downloaded": plays from disk
    w._changed()

    def pages():
        for view in ("fresh", "queue", "downloads", "shows", "discover"):
            w.go(view)
            pump(0.2)
            assert w.trail.current.view == view
        w.go("show", "Test Show", url)
        pump(0.2)
        w.open_episode(w.store.episodes(url)[0])
        pump(0.2)
        assert w.trail.current.view == "episode"
        assert w.trail.back() and w.trail.current.view == "show"
        assert w.store.counts()["fresh"] == 3 and w.store.counts()["downloads"] == 3

    def queue_and_play():
        e0, e1, e2 = (w.store.episode(eps[f"Episode {i}"]["id"]) for i in range(3))
        w.store.enqueue(e1["id"])
        w.play(e0)
        assert wait_for(lambda: w.player.playing), "never started playing"
        assert w.store.counts()["fresh"] == 2           # playing it takes it out of New episodes
        assert w.mini.get_parent() is not None          # the mini player is in the bar
        w.open_now()
        pump(0.3)
        assert w.trail.current.view == "now" and "transport" in w._now_widgets
        # the end of Episode 0 marks it played and Up next carries on to Episode 1
        assert wait_for(lambda: w.current and w.current["id"] == e1["id"], 20), "didn't move on to Up next"
        assert w.store.episode(e0["id"])["played"] == 1
        assert w.store.queue() == []
        # pause part-way through a long one: the place is kept, and resumes from there
        w.play(e2)
        assert wait_for(lambda: w.player.playing)
        w.seek(12)
        pump(0.5)
        w.pause()
        pump(0.3)
        pos = w.store.episode(e2["id"])["position"]
        assert pos >= 11, pos
        assert w.store.in_progress()[0]["id"] == e2["id"]
        w.player.stop()
        w.loaded = False
        w.play()                                       # resume the current episode
        assert wait_for(lambda: w.player.playing)
        assert w.position() >= 11, w.position()
        w.pause()

    def speed_and_sleep():
        w.set_speed(1.5)
        assert w.player.rate == 1.5 and w.speed() == 1.5
        assert w.store.podcast(url)["speed"] == 1.5      # remembered for the show
        w.set_sleep(15)
        assert w.sleep_mode == 15 and "Sleep in" in w._sleep_words()
        w.set_sleep(0)
        assert w.sleep_mode == 0

    def notes_links():
        e = w.store.episode(eps["Episode 2"]["id"])
        assert w._notes_link("seek:2", e) is True
        pump(0.5)
        assert w.current["id"] == e["id"]
        w.pause()

    def menus_and_opml():
        e = w.store.episode(eps["Episode 1"]["id"])
        w._enqueue(e, front=True)
        assert w.store.queued(e["id"])
        w._dequeue(e)
        assert not w.store.queued(e["id"])
        w._played(e, False)
        assert w.store.episode(e["id"])["played"] == 0
        from earshot import opml
        data = opml.write(w.store.podcasts())
        assert opml.read(data)[0]["url"] == url
        w.center.grow("settings", w._settings_panel)
        pump(0.2)
        w.center.fold()

    def unfollow():
        w._unfollow(url, quiet=True)
        assert w.store.counts()["shows"] == 0 and w.current is None

    for name, fn in (("pages", pages), ("queue and playback", queue_and_play), ("speed and sleep", speed_and_sleep),
                     ("show-note times", notes_links), ("menus and OPML", menus_and_opml), ("unfollow", unfollow)):
        check(name, fn)


class TestApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.charleshartmann.EarshotTest")
        self.window = None
        self._held = False

    def do_activate(self):
        from earshot import app as A
        self.window = A.Window(self)
        self.window.present()
        pump(0.5)
        try:
            run(self.window)
        finally:
            self.window.player.stop()
            self.quit()


TestApp().run([])
bad = 0
for name, err in RESULTS:
    print(("ok   " if err is None else "FAIL ") + name)
    if err:
        bad += 1
        print(err)
print(f"{len(RESULTS) - bad} passed, {bad} failed")
sys.exit(1 if bad else 0)

# SPDX-License-Identifier: GPL-3.0-or-later
"""The audio player: one GStreamer playbin on the GLib main loop.

Speed changes keep the voice's pitch (scaletempo). The platform's audio sink is left to playbin, as Luma's
own players do; EARSHOT_AUDIO_SINK=fakesink is a test-only escape hatch for machines without sound.
"""
from __future__ import annotations

import os
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from .net import USER_AGENT  # noqa: E402

TICK_MS = 500


class Player:
    """Callbacks (all on the main loop): on_state(playing: bool), on_position(seconds), on_duration(seconds),
    on_eos(), on_error(message)."""

    def __init__(self):
        Gst.init(None)
        self.on_state: Callable[[bool], None] = lambda _p: None
        self.on_position: Callable[[float], None] = lambda _s: None
        self.on_duration: Callable[[float], None] = lambda _s: None
        self.on_eos: Callable[[], None] = lambda: None
        self.on_error: Callable[[str], None] = lambda _m: None
        self.pipe = Gst.ElementFactory.make("playbin3", "earshot") or Gst.ElementFactory.make("playbin", "earshot")
        if self.pipe is None:
            raise RuntimeError("GStreamer's playbin is missing")
        # audio only: never decode a video track some feeds ship
        try:
            flags = self.pipe.get_property("flags")
            self.pipe.set_property("flags", (int(flags) & ~0x1 & ~0x4) | 0x2)  # -video -text +audio
        except (TypeError, ValueError):
            pass
        tempo = Gst.ElementFactory.make("scaletempo", "tempo")
        if tempo is not None:
            self.pipe.set_property("audio-filter", tempo)
        sink = os.environ.get("EARSHOT_AUDIO_SINK")
        if sink:
            element = Gst.ElementFactory.make(sink, "sink")
            if element.find_property("sync") is not None:
                element.set_property("sync", True)  # play in real time, as a sound card would
            self.pipe.set_property("audio-sink", element)
        self.pipe.connect("source-setup", self._source_setup)
        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._message)
        self.uri = ""
        self.rate = 1.0
        self.playing = False
        self.duration = 0.0
        self._pending_seek: float | None = None
        self._want_play = False
        self._ready = False
        self._tick = 0
        self._last_pos = 0.0

    # ---------------------------------------------------------------- control
    def load(self, uri: str, *, position: float = 0.0, play: bool = True, duration: float = 0.0) -> None:
        self.pipe.set_state(Gst.State.NULL)
        self.uri, self.duration, self._ready = uri, max(0.0, duration), False
        self._last_pos = max(0.0, position)
        self._pending_seek = position if position > 1 else None
        self._want_play = play
        self.pipe.set_property("uri", uri)
        if self.pipe.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
            self.on_error("This episode can't be played.")
            return
        self._start_tick()

    def play(self) -> None:
        if not self.uri:
            return
        self._want_play = True
        if self._ready:
            self.pipe.set_state(Gst.State.PLAYING)

    def pause(self) -> None:
        self._want_play = False
        if self.uri:
            self.pipe.set_state(Gst.State.PAUSED)

    def toggle(self) -> None:
        self.pause() if self._want_play else self.play()

    def stop(self) -> None:
        self._want_play = False
        self.pipe.set_state(Gst.State.NULL)
        self.uri, self._ready, self.playing = "", False, False
        self._stop_tick()
        self.on_state(False)

    @property
    def wants_play(self) -> bool:
        return self._want_play

    def position(self) -> float:
        if not self._ready:
            return self._pending_seek if self._pending_seek is not None else self._last_pos
        ok, pos = self.pipe.query_position(Gst.Format.TIME)
        if ok and pos >= 0:
            self._last_pos = pos / Gst.SECOND
        return self._last_pos

    def seek(self, seconds: float) -> None:
        seconds = max(0.0, seconds)
        if self.duration:
            seconds = min(seconds, max(0.0, self.duration - 0.5))
        self._last_pos = seconds
        if not self._ready:
            self._pending_seek = seconds
            return
        self._do_seek(seconds)
        self.on_position(seconds)

    def skip(self, delta: float) -> None:
        self.seek(self.position() + delta)

    def set_rate(self, rate: float) -> None:
        self.rate = max(0.5, min(3.0, float(rate)))
        if self._ready:
            self._do_seek(self.position())

    def set_volume(self, volume: float) -> None:
        self.pipe.set_property("volume", max(0.0, min(1.0, volume)))

    def _do_seek(self, seconds: float) -> None:
        self.pipe.seek(self.rate, Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                       Gst.SeekType.SET, int(seconds * Gst.SECOND), Gst.SeekType.NONE, -1)

    # ---------------------------------------------------------------- GStreamer
    def _source_setup(self, _pipe, source):
        if source.find_property("user-agent") is not None:
            source.set_property("user-agent", USER_AGENT)

    def _message(self, _bus, msg):
        t = msg.type
        if t == Gst.MessageType.ASYNC_DONE:
            first = not self._ready
            self._ready = True
            if first:
                self._query_duration()
                if self._pending_seek is not None or self.rate != 1.0:
                    target = self._pending_seek if self._pending_seek is not None else 0.0
                    self._pending_seek = None
                    self._do_seek(target)
                    return  # the seek's own ASYNC_DONE continues below
                if self._want_play:
                    self.pipe.set_state(Gst.State.PLAYING)
            elif self._want_play and not self.playing:
                self.pipe.set_state(Gst.State.PLAYING)
        elif t == Gst.MessageType.STATE_CHANGED and msg.src == self.pipe:
            _old, new, _pending = msg.parse_state_changed()
            playing = new == Gst.State.PLAYING
            if playing != self.playing:
                self.playing = playing
                self.on_state(playing)
        elif t == Gst.MessageType.DURATION_CHANGED:
            self._query_duration()
        elif t == Gst.MessageType.EOS:
            self._want_play = False
            self.pipe.set_state(Gst.State.PAUSED)
            self.on_eos()
        elif t == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            self._want_play = False
            self.pipe.set_state(Gst.State.NULL)
            self._ready = False
            self.on_error(_friendly(err, debug))
        elif t == Gst.MessageType.BUFFERING:
            percent = msg.parse_buffering()
            if self.uri.startswith(("http://", "https://")) and self._want_play and self._ready:
                self.pipe.set_state(Gst.State.PAUSED if percent < 100 else Gst.State.PLAYING)

    def _query_duration(self):
        ok, dur = self.pipe.query_duration(Gst.Format.TIME)
        if ok and dur > 0:
            seconds = dur / Gst.SECOND
            if abs(seconds - self.duration) > 0.5:
                self.duration = seconds
                self.on_duration(seconds)

    def _start_tick(self):
        if not self._tick:
            self._tick = GLib.timeout_add(TICK_MS, self._ticked)

    def _stop_tick(self):
        if self._tick:
            GLib.source_remove(self._tick)
            self._tick = 0

    def _ticked(self):
        if self._ready and self.playing:
            self.on_position(self.position())
            if not self.duration:
                self._query_duration()
        return True


def _friendly(err, debug) -> str:
    text = (getattr(err, "message", "") or "").lower()
    if "not found" in text or "404" in (debug or ""):
        return "The episode's file isn't there any more."
    if "resolve" in text or "connect" in text or "network" in text:
        return "Couldn't reach the episode. Check your connection."
    if "decoder" in text or "codec" in text or "type" in text:
        return "This episode is in a format that can't be played."
    return "The episode stopped playing because of an error."

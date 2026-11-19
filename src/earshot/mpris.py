# SPDX-License-Identifier: GPL-3.0-or-later
"""MPRIS: the shell's media controls, the lock screen and headphone keys drive Earshot's player.

The interface description follows the MPRIS 2.2 specification (and the shape Luma's Tide uses).
"""
from __future__ import annotations

from typing import Callable

from gi.repository import Gio, GLib

PATH = "/org/mpris/MediaPlayer2"
NAME = "org.mpris.MediaPlayer2.Earshot"
XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/><method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/><method name="Previous"/><method name="Pause"/>
    <method name="PlayPause"/><method name="Stop"/><method name="Play"/>
    <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
    <method name="SetPosition"><arg direction="in" name="TrackId" type="o"/><arg direction="in" name="Position" type="x"/></method>
    <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""


class Mpris:
    """`app` provides: raise_window(), quit(), toggle(), play(), pause(), stop(), next(), back(), skip(seconds),
    seek(seconds), set_rate(rate), set_volume(v) and state() → dict(status, episode, position, duration, rate,
    volume, art, can_next)."""

    def __init__(self, app, app_id: str):
        self.app, self.app_id = app, app_id
        self.conn: Gio.DBusConnection | None = None
        self.ids: list[int] = []
        self._last: dict = {}
        self.owner = Gio.bus_own_name(Gio.BusType.SESSION, NAME, Gio.BusNameOwnerFlags.NONE,
                                      self._acquired, None, None)

    def close(self):
        if self.conn:
            for i in self.ids:
                self.conn.unregister_object(i)
        Gio.bus_unown_name(self.owner)

    def _acquired(self, conn, _name):
        self.conn = conn
        info = Gio.DBusNodeInfo.new_for_xml(XML)
        for iface in info.interfaces:
            self.ids.append(conn.register_object(PATH, iface, self._call, self._get, self._set))

    # ---------------------------------------------------------------- calls
    def _call(self, _conn, _sender, _path, _iface, method, params, invocation):
        a = self.app
        try:
            if method == "Raise":
                a.raise_window()
            elif method == "Quit":
                a.quit()
            elif method == "PlayPause":
                a.toggle()
            elif method == "Play":
                a.play()
            elif method == "Pause":
                a.pause()
            elif method == "Stop":
                a.pause()
            elif method == "Next":
                a.skip(a.skip_forward())
            elif method == "Previous":
                a.skip(-a.skip_back())
            elif method == "Seek":
                a.skip(params.unpack()[0] / 1_000_000)
            elif method == "SetPosition":
                track, pos = params.unpack()
                if track == self._track_id():
                    a.seek(pos / 1_000_000)
            elif method == "OpenUri":
                pass
        finally:
            invocation.return_value(None)

    def _track_id(self) -> str:
        ep = self.app.state().get("episode")
        return f"/com/charleshartmann/Earshot/episode/e{ep['id']}" if ep else "/org/mpris/MediaPlayer2/TrackList/NoTrack"

    def _props(self) -> dict:
        s = self.app.state()
        ep = s.get("episode")
        meta = {"mpris:trackid": GLib.Variant("o", self._track_id())}
        if ep:
            meta["xesam:title"] = GLib.Variant("s", ep["title"])
            meta["xesam:artist"] = GLib.Variant("as", [ep.get("show") or ""])
            meta["xesam:album"] = GLib.Variant("s", ep.get("show") or "")
            meta["xesam:genre"] = GLib.Variant("as", ["Podcast"])
            if s.get("duration"):
                meta["mpris:length"] = GLib.Variant("x", int(s["duration"] * 1_000_000))
            if s.get("art"):
                meta["mpris:artUrl"] = GLib.Variant("s", GLib.filename_to_uri(s["art"], None))
        return {
            "org.mpris.MediaPlayer2": {
                "CanQuit": GLib.Variant("b", True), "CanRaise": GLib.Variant("b", True),
                "HasTrackList": GLib.Variant("b", False), "Identity": GLib.Variant("s", "Earshot"),
                "DesktopEntry": GLib.Variant("s", self.app_id),
                "SupportedUriSchemes": GLib.Variant("as", []), "SupportedMimeTypes": GLib.Variant("as", []),
            },
            "org.mpris.MediaPlayer2.Player": {
                "PlaybackStatus": GLib.Variant("s", s.get("status", "Stopped")),
                "LoopStatus": GLib.Variant("s", "None"), "Rate": GLib.Variant("d", s.get("rate", 1.0)),
                "Shuffle": GLib.Variant("b", False), "Metadata": GLib.Variant("a{sv}", meta),
                "Volume": GLib.Variant("d", s.get("volume", 1.0)),
                "Position": GLib.Variant("x", int(s.get("position", 0) * 1_000_000)),
                "MinimumRate": GLib.Variant("d", 0.5), "MaximumRate": GLib.Variant("d", 3.0),
                "CanGoNext": GLib.Variant("b", ep is not None), "CanGoPrevious": GLib.Variant("b", ep is not None),
                "CanPlay": GLib.Variant("b", ep is not None), "CanPause": GLib.Variant("b", ep is not None),
                "CanSeek": GLib.Variant("b", ep is not None), "CanControl": GLib.Variant("b", True),
            },
        }

    def _get(self, _conn, _sender, _path, iface, prop):
        return self._props().get(iface, {}).get(prop)

    def _set(self, _conn, _sender, _path, _iface, prop, value):
        if prop == "Rate":
            self.app.set_rate(value.unpack())
        elif prop == "Volume":
            self.app.set_volume(value.unpack())
        return True

    # ---------------------------------------------------------------- changes
    def changed(self) -> None:
        """Tell listeners what changed since the last call (Position is never pushed, per the spec)."""
        if not self.conn:
            return
        props = self._props()["org.mpris.MediaPlayer2.Player"]
        props.pop("Position")
        diff = {k: v for k, v in props.items() if self._last.get(k) is None or not self._last[k].equal(v)}
        self._last = props
        if diff:
            self.conn.emit_signal(None, PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                                  GLib.Variant("(sa{sv}as)", ("org.mpris.MediaPlayer2.Player", diff, [])))

    def seeked(self, seconds: float) -> None:
        if self.conn:
            self.conn.emit_signal(None, PATH, "org.mpris.MediaPlayer2.Player", "Seeked",
                                  GLib.Variant("(x)", (int(seconds * 1_000_000),)))

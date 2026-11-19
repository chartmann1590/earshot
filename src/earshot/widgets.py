# SPDX-License-Identifier: GPL-3.0-or-later
"""Earshot's own widgets: cover art at any size, the chapter scrub, episode rows and show tiles."""
from __future__ import annotations

import colorsys
import hashlib
import math
import threading
import time
from collections import OrderedDict

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Gdk, GLib, GObject, Graphene, Gsk, Gtk, Pango  # noqa: E402

from luma_appkit import ProgressLine, TypeLabel, icons  # noqa: E402

# ---------------------------------------------------------------- pictures
_FALLBACK: OrderedDict = OrderedDict()   # path → Gdk.Texture decoded by GTK itself
_FALLBACK_BYTES = [0]
_FALLBACK_BUDGET = 160 * 1024 * 1024
_TEX_LOCK = threading.Lock()
_INFLIGHT: dict = {}                      # path → callbacks waiting for the fallback decode
_QUEUE: "queue.LifoQueue" = None          # newest first: what just scrolled into view decodes next
_WORKERS: list = []


def _ensure_workers():
    global _QUEUE
    import queue
    with _TEX_LOCK:
        if _QUEUE is None:
            _QUEUE = queue.LifoQueue()
        if _WORKERS:
            return
        for _ in range(2):
            t = threading.Thread(target=_fallback_worker, daemon=True, name="earshot-art")
            t.start()
            _WORKERS.append(t)


def _fallback_worker():
    while True:
        path = _QUEUE.get()
        try:
            t = Gdk.Texture.new_from_filename(path)
        except Exception:
            t = None
        with _TEX_LOCK:
            if t is not None:
                _FALLBACK[path] = t
                _FALLBACK_BYTES[0] += t.get_width() * t.get_height() * 4
                while _FALLBACK_BYTES[0] > _FALLBACK_BUDGET and len(_FALLBACK) > 1:
                    _p, old = _FALLBACK.popitem(last=False)
                    _FALLBACK_BYTES[0] -= old.get_width() * old.get_height() * 4
            callbacks = _INFLIGHT.pop(path, [])
        GLib.idle_add(lambda cbs=callbacks, t=t: ([cb(t) for cb in cbs], False)[1])


def load_texture(path: str, px: int, done) -> None:
    """A texture for `path` sized for `px`, delivered to `done(texture | None)` on the main loop.

    Luma's shared TextureLoader decodes scaled copies off the main loop and keeps them on disk. Where its
    image loader can't run, GTK's own decoder is the fallback, within a memory budget."""
    try:
        from luma_appkit.media_style import TextureLoader
        loader = TextureLoader.shared()
    except Exception:
        loader = None

    def fallback():
        with _TEX_LOCK:
            tex = _FALLBACK.get(path)
            if tex is not None:
                _FALLBACK.move_to_end(path)
            waiting = _INFLIGHT.get(path)
            if tex is None:
                if waiting is not None:
                    waiting.append(done)
                    return
                _INFLIGHT[path] = [done]
        if tex is not None:
            done(tex)
            return
        _ensure_workers()
        _QUEUE.put(path)

    if loader is None:
        fallback()
        return

    def got(texture):
        if texture is not None:
            done(texture)
        else:
            fallback()
    try:
        loader.request(path, px, got)
    except Exception:
        fallback()


def hue_for(text: str) -> float:
    return int(hashlib.md5(text.encode()).hexdigest()[:4], 16) / 65535


class Art(Gtk.Widget):
    """A show's or episode's picture, square with round corners. Until (or unless) the picture loads it is a
    colour from the title with the show's initials, so a list never jumps or shows holes."""

    __gtype_name__ = "EarshotArt"

    def __init__(self, title: str = "", size: int = 56, radius: float | None = None):
        super().__init__(accessible_role=Gtk.AccessibleRole.IMG, halign=Gtk.Align.START, valign=Gtk.Align.START)
        self.size = size
        self.radius = radius if radius is not None else max(6.0, size * 0.12)
        self.title = title
        self.texture = None
        self._want = ""
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"{title} artwork" if title else "Artwork"])
        h = hue_for(title or "?")
        r1, g1, b1 = colorsys.hls_to_rgb(h, 0.55, 0.55)
        r2, g2, b2 = colorsys.hls_to_rgb((h + 0.08) % 1, 0.32, 0.6)
        self._c1, self._c2 = Gdk.RGBA(red=r1, green=g1, blue=b1, alpha=1), Gdk.RGBA(red=r2, green=g2, blue=b2, alpha=1)
        words = [w for w in "".join(c if c.isalnum() or c.isspace() else " " for c in title).split() if w]
        skip = {"the", "a", "an", "podcast", "show", "with", "and", "of"}
        letters = [w[0] for w in words if w.lower() not in skip] or [w[0] for w in words]
        self._initials = "".join(letters[:2]).upper()

    def set_size(self, size: int):
        self.size = size
        self.radius = max(6.0, size * 0.12)
        self.queue_resize()

    def set_path(self, path: str):
        if not path or path == self._want:
            return
        self._want = path
        load_texture(path, int(self.size * (self.get_scale_factor() or 1)), self._loaded)

    def _loaded(self, texture):
        if texture is not None:
            self.texture = texture
            self.queue_draw()

    def do_measure(self, orientation, for_size):
        return self.size, self.size, -1, -1

    def do_snapshot(self, s):
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        rect = Graphene.Rect().init(0, 0, w, h)
        rr = Gsk.RoundedRect()
        rr.init_from_rect(rect, self.radius)
        s.push_rounded_clip(rr)
        if self.texture is not None:
            s.append_scaled_texture(self.texture, Gsk.ScalingFilter.TRILINEAR, rect)
        else:
            stops = [Gsk.ColorStop(), Gsk.ColorStop()]
            stops[0].offset, stops[0].color = 0.0, self._c1
            stops[1].offset, stops[1].color = 1.0, self._c2
            s.append_linear_gradient(rect, Graphene.Point().init(0, 0), Graphene.Point().init(w, h), stops)
            if self._initials and w >= 24:
                layout = self.create_pango_layout(self._initials)
                fd = Pango.FontDescription.from_string("sans bold")
                fd.set_absolute_size(w * 0.34 * Pango.SCALE)
                layout.set_font_description(fd)
                lw, lh = layout.get_pixel_size()
                s.save()
                s.translate(Graphene.Point().init((w - lw) / 2, (h - lh) / 2))
                s.append_layout(layout, Gdk.RGBA(red=1, green=1, blue=1, alpha=0.92))
                s.restore()
        s.pop()
        # a hairline so white artwork doesn't melt into a white page
        border = Gsk.RoundedRect()
        border.init_from_rect(rect, self.radius)
        line = Gdk.RGBA(red=0, green=0, blue=0, alpha=0.08)
        s.append_border(border, [0.5] * 4, [line] * 4)


# ---------------------------------------------------------------- the scrub
class ChapterScrub(Gtk.Box):
    """The Now playing position: a range with a mark at each chapter. MediaTransport connects its `seek`
    signal and calls set_position as playback moves."""

    __gtype_name__ = "EarshotChapterScrub"
    __gsignals__ = {"seek": (GObject.SignalFlags.RUN_FIRST, None, (float,))}

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.add_css_class("earshot-scrub")
        self.scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, draw_value=False)
        self.scale.set_range(0, 1)
        self.scale.update_property([Gtk.AccessibleProperty.LABEL], ["Position"])
        self.scale.connect("change-value", self._changed)
        self.append(self.scale)
        self.duration = 0.0
        self._held = 0.0
        self._pending = None
        self._timer = 0

    def set_kind(self, _kind):  # MediaTransport asks every waveform for its kind
        pass

    def set_duration(self, seconds: float, chapters=()):
        self.duration = max(0.0, seconds)
        self.scale.clear_marks()
        self.scale.set_range(0, max(1.0, self.duration))
        self.scale.set_increments(15, 60)
        for c in chapters or ():
            if 0 < c["start"] < self.duration:
                self.scale.add_mark(c["start"], Gtk.PositionType.BOTTOM, None)

    def set_position(self, seconds: float):
        if time.monotonic() < self._held:
            return
        self.scale.set_value(max(0.0, seconds))

    def _changed(self, _scale, _scroll, value):
        self._held = time.monotonic() + 1.2
        self._pending = max(0.0, min(value, self.duration or value))
        if not self._timer:
            self._timer = GLib.timeout_add(150, self._emit)
        return False

    def _emit(self):
        self._timer = 0
        if self._pending is not None:
            value, self._pending = self._pending, None
            self.emit("seek", value)
        return False


# ---------------------------------------------------------------- formatting
def clock(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def length_words(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    if seconds <= 0:
        return ""
    if minutes < 1:
        return "Under a minute"
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} hr {m} min"
    return f"{h} hr" if h else f"{m} min"


def date_words(t: float) -> str:
    if not t:
        return ""
    now = time.time()
    d = now - t
    lt, nt = time.localtime(t), time.localtime(now)
    if d < 86400 and lt.tm_yday == nt.tm_yday:
        return "Today"
    if d < 2 * 86400 and (nt.tm_yday - lt.tm_yday) % 366 == 1:
        return "Yesterday"
    if d < 6 * 86400:
        return time.strftime("%A", lt)
    return time.strftime("%b %-d" if lt.tm_year == nt.tm_year else "%b %-d, %Y", lt)


def status_words(ep: dict) -> str:
    """"45 min", "12 min left" or "Played"."""
    if ep.get("played"):
        return "Played"
    dur, pos = ep.get("duration") or 0, ep.get("position") or 0
    if pos > 0 and dur > 0:
        return length_words(dur - pos).replace("Under a minute", "Less than a minute") + " left"
    return length_words(dur)


# ---------------------------------------------------------------- rows and tiles
def caption(text: str, *, ellipsize: bool = True) -> Gtk.Widget:
    """A muted caption line that shrinks to fit (ellipsized), so a long show name never widens a phone page."""
    t = TypeLabel(text, role="caption")
    t.set_halign(Gtk.Align.FILL)
    t.set_hexpand(True)
    if ellipsize:
        t.label.set_ellipsize(Pango.EllipsizeMode.END)
        t.label.set_max_width_chars(1)
        t.label.set_hexpand(True)
    return t


def link_button(text: str, callback, *, center: bool = False) -> Gtk.Button:
    """Words that open something (a show's name over its episode): wraps instead of widening the page."""
    label = Gtk.Label(label=text, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                      justify=Gtk.Justification.CENTER if center else Gtk.Justification.LEFT, xalign=0.5 if center else 0)
    b = Gtk.Button(child=label, halign=Gtk.Align.CENTER if center else Gtk.Align.START)
    b.add_css_class("flat")
    b.add_css_class("earshot-link")
    b.connect("clicked", lambda _b: callback())
    return b


def _icon_button(icon: str, label: str, callback, *, css: tuple = ()) -> Gtk.Button:
    b = Gtk.Button(child=icons.image(icon), valign=Gtk.Align.CENTER, tooltip_text=label)
    b.update_property([Gtk.AccessibleProperty.LABEL], [label])
    b.add_css_class("flat")
    b.add_css_class("circular")
    for c in css:
        b.add_css_class(c)
    b.connect("clicked", lambda _b: callback())
    return b


class EpisodeRow(Gtk.Box):
    """One episode in a list: picture, show and date, title, how long or how much is left, Play and ⋯.
    Tapping the row opens the episode; `on_play(ep)` and `on_more(ep, anchor)` are its keys."""

    __gtype_name__ = "EarshotEpisodeRow"

    def __init__(self, ep: dict, *, art_path: str = "", on_open=None, on_play=None, on_more=None,
                 show_name: bool = True, playing: bool = False, download: float | None = None, queued_index=None):
        super().__init__(spacing=12, accessible_role=Gtk.AccessibleRole.GROUP)
        self.add_css_class("earshot-episode")
        self.ep = ep
        if ep.get("played"):
            self.add_css_class("played")
        self.art = Art(ep.get("show") or ep.get("title") or "", 56)
        self.art.set_path(art_path)
        self.append(self.art)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        text.set_size_request(1, -1)
        meta = [ep.get("show")] if show_name and ep.get("show") else []
        meta.append(date_words(ep.get("published") or 0))
        text.append(caption(" · ".join(m for m in meta if m)))
        title = Gtk.Label(label=ep.get("title") or "", xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                          lines=2, ellipsize=Pango.EllipsizeMode.END)
        title.add_css_class("earshot-episode-title")
        text.append(title)
        bottom = Gtk.Box(spacing=6)
        if playing:
            bottom.append(icons.image("headphones", pixel_size=12))
        if ep.get("file"):
            bottom.append(icons.image("download", pixel_size=12))
        words = ("Now playing · " if playing else "") + status_words(ep)
        if queued_index is not None:
            words += f" · #{queued_index + 1} in Up next"
        bottom.append(caption(words))
        text.append(bottom)
        dur, pos = ep.get("duration") or 0, ep.get("position") or 0
        if download is not None:
            line = ProgressLine(download, label="Downloading")
            text.append(line)
            self.download_line = line
        elif pos > 0 and dur > 0 and not ep.get("played"):
            text.append(ProgressLine(min(1.0, pos / dur), label="Listened"))
        self.append(text)

        if on_play is not None:
            label = "Pause" if playing else ("Resume" if pos > 0 and not ep.get("played") else "Play")
            self.play = _icon_button("pause" if playing else "play", f"{label} {ep.get('title', '')}",
                                     lambda: on_play(ep), css=("earshot-play",))
            self.append(self.play)
        if on_more is not None:
            more = _icon_button("ellipsis", "More", lambda: on_more(ep, more))
            self.append(more)
        if on_open is not None:
            # The words are one button (remote OK, Enter, a tap or a click opens the episode), so a TV remote,
            # a keyboard and a screen reader reach it as easily as a finger does.
            self.remove(text)
            opener = Gtk.Button(child=text, hexpand=True)
            opener.add_css_class("flat")
            opener.add_css_class("earshot-row-open")
            opener.update_property([Gtk.AccessibleProperty.LABEL], [f"Open {ep.get('title', '')}"])
            opener.connect("clicked", lambda _b: on_open(ep))
            self.insert_child_after(opener, self.art)
            art_click = Gtk.GestureClick()
            art_click.connect("released", lambda *_: on_open(ep))
            self.art.add_controller(art_click)
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"{ep.get('title', '')}, {status_words(ep)}"])

    def set_download(self, fraction: float):
        line = getattr(self, "download_line", None)
        if line is not None:
            line.set_fraction(fraction)


class ShowTile(Gtk.Button):
    """A show in a grid: its picture, title and who makes it."""

    __gtype_name__ = "EarshotShowTile"

    def __init__(self, title: str, author: str, *, art_path: str = "", size: int = 148, badge: int = 0, on_open=None):
        super().__init__(halign=Gtk.Align.START, valign=Gtk.Align.START)
        self.add_css_class("flat")
        self.add_css_class("earshot-tile")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        over = Gtk.Overlay()
        self.art = Art(title, size)
        self.art.set_path(art_path)
        over.set_child(self.art)
        if badge:
            dot = Gtk.Label(label=str(badge) if badge < 100 else "99+", halign=Gtk.Align.END, valign=Gtk.Align.START,
                            margin_top=6, margin_end=6)
            dot.add_css_class("earshot-badge")
            over.add_overlay(dot)
        box.append(over)
        t = Gtk.Label(label=title, xalign=0, wrap=True, lines=2, ellipsize=Pango.EllipsizeMode.END, max_width_chars=1)
        t.set_size_request(size, -1)
        t.add_css_class("earshot-tile-title")
        box.append(t)
        if author:
            a = Gtk.Label(label=author, xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=1)
            a.set_size_request(size, -1)
            a.add_css_class("dim-label")
            a.add_css_class("caption")
            box.append(a)
        self.set_child(box)
        label = f"{title}, by {author}" if author else title
        if badge:
            label += f", {badge} new"
        self.update_property([Gtk.AccessibleProperty.LABEL], [label])
        if on_open is not None:
            self.connect("clicked", lambda _b: on_open())


def tile_grid(tiles: list[Gtk.Widget], *, min_size: int = 148) -> Gtk.FlowBox:
    flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, column_spacing=10, row_spacing=18,
                       min_children_per_line=1, max_children_per_line=8, valign=Gtk.Align.START)
    flow.add_css_class("earshot-grid")
    for t in tiles:
        flow.append(t)
    return flow


def episode_list(rows: list[Gtk.Widget]) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.add_css_class("earshot-list")
    for i, r in enumerate(rows):
        if i:
            sep = Gtk.Separator()
            sep.add_css_class("earshot-sep")
            box.append(sep)
        box.append(r)
    return box


def heading(text: str, *, action: tuple[str, object] | None = None) -> Gtk.Widget:
    row = Gtk.Box(spacing=8, margin_top=18, margin_bottom=6)
    t = TypeLabel(text, role="section-title")
    t.set_hexpand(True)
    t.set_halign(Gtk.Align.START)
    row.append(t)
    if action is not None:
        from luma_appkit import TextButton
        row.append(TextButton(action[0], on_click=action[1], small=True))
    return row

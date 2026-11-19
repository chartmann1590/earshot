# SPDX-License-Identifier: GPL-3.0-or-later
"""Earshot: podcasts for Luma.

Shape: places and content (Tide, Tasks). The sidebar holds the places (New episodes, Up next, Downloads,
Shows, Discover) and the shows you follow; the island holds the page; the bar holds what you do here, with
the mini player above it whenever an episode is loaded.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from luma_appkit import (  # noqa: E402
    ActionCenter, AppWindow, BarAction, BarSearch, Command, CommandGroup, CommandRegistry, DestructiveDialog,
    EmptyState, FileRequest, Island, ListEmptyState, MediaMiniPlayer, MediaTransport, NavigationSidebar,
    NavigationTrail, PageHeader, PanelHeading, PanelRow, Place, RowLead, SidebarFoot, SidebarRow, SidebarToggle,
    TextButton, TitleIsland, Toast, ToastHost, TypeLabel, ask_for_file, icons,
)
from luma_appkit.action_bubble import FloatingMenu, MenuItem  # noqa: E402

from . import net, notes, opml  # noqa: E402
from .hsbilling import Billing  # noqa: E402
from .hsupdates import Updates  # noqa: E402
from .updates_ui import Updater  # noqa: E402
from .paywall import Paywall, badge  # noqa: E402
from .player import Player  # noqa: E402
from .store import Store  # noqa: E402
from .widgets import (  # noqa: E402
    Art, ChapterScrub, EpisodeRow, ShowTile, caption, clock, link_button, date_words, episode_list, heading, length_words, status_words,
    tile_grid,
)

APP_ID = "com.charleshartmann.Earshot"
APP_NAME = "Earshot"
VERSION = "1.2.1"
PLACES = (("fresh", "New episodes", "inbox"), ("queue", "Up next", "list-ordered"),
          ("downloads", "Downloads", "download"), ("shows", "Shows", "library"), ("discover", "Discover", "compass"))
SAVE_EVERY = 10.0
PLUS = "Earshot Plus"
PLUS_PRODUCTS = ("earshot_lifetime", "earshot_plus:yearly", "earshot_plus:monthly")  # best first
PLUS_FEATURES = (
    ("volume-2", "Voice boost", "Quiet voices lifted and loud ones tamed, so every show sounds clear."),
    ("rotate-cw", "Skip intros and outros", "Set it once per show and the theme music is skipped for you."),
    ("download", "Automatic downloads", "New episodes from the shows you choose are ready offline."),
    ("chart-column", "Listening stats", "How much you listen, your top shows, and the time speed saved you."),
)
SLEEP_CHOICES = ((0, "Off"), (5, "5 minutes"), (15, "15 minutes"), (30, "30 minutes"), (45, "45 minutes"),
                 (60, "1 hour"), (-1, "End of episode"), (-2, "End of chapter"))


def data_dir() -> str:
    return os.path.join(GLib.get_user_data_dir(), "earshot")


class Window(AppWindow):
    def __init__(self, app):
        self.app_ref = app
        self.store = Store(os.path.join(data_dir(), "library.sqlite3"))
        self.billing = Billing("earshot", PLUS_PRODUCTS, os.path.join(data_dir(), "license.json"))
        self._listen_mark = None                # (monotonic time, position) of the last listening tick
        self._listen_pending: dict = {}         # podcast url → [seconds, saved] not yet written
        self.pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="earshot")
        self.art: dict[str, str] = {}           # image url → cached path ("" while fetching)
        self._art_waiters: dict[str, list] = {}
        self.downloads: dict[str, dict] = {}    # episode id → {"cancel", "fraction", "rows"}
        self.refreshing = False
        self.directory_cache: dict[str, list] = {}
        self.preview: dict[str, dict] = {}      # feed url → fetched show not (yet) followed
        self.search_text = ""
        self.genre = ""
        self.current: dict | None = None        # the loaded episode
        self.loaded = False                     # pipeline holds it
        self.sleep_mode = 0
        self.sleep_until = 0.0
        self._sleep_timer = 0
        self._last_save = 0.0
        self._now_widgets: dict = {}
        self._rows: dict = {}
        self._place_rows: dict = {}
        self._populating = False

        commands = CommandRegistry([
            CommandGroup("Playback", (
                Command("play.toggle", "Play or pause", self.toggle, icon="play", shortcut=("Ctrl", "space"),
                        enabled=lambda: self.current is not None),
                Command("play.back", "Back", lambda: self.skip(-self.skip_back()), icon="rotate-ccw",
                        shortcut=("Ctrl", "Left"), enabled=lambda: self.current is not None),
                Command("play.forward", "Forward", lambda: self.skip(self.skip_forward()), icon="rotate-cw",
                        shortcut=("Ctrl", "Right"), enabled=lambda: self.current is not None),
                Command("play.faster", "Faster", lambda: self.nudge_speed(1), icon="chevron-up",
                        shortcut=("Ctrl", "bracketright"), enabled=lambda: self.current is not None),
                Command("play.slower", "Slower", lambda: self.nudge_speed(-1), icon="chevron-down",
                        shortcut=("Ctrl", "bracketleft"), enabled=lambda: self.current is not None),
                Command("play.now", "Now playing", self.open_now, icon="headphones", shortcut=("Ctrl", "L"),
                        enabled=lambda: self.current is not None),
                Command("play.sleep", "Sleep timer…", self.ask_sleep, icon="moon", enabled=lambda: self.current is not None),
                Command("play.boost", "Voice boost", lambda: self.set_voice_boost(not self.voice_boost_on()),
                        icon="volume-2"),
            )),
            CommandGroup("App", (
                Command("app.updates", "Check for updates…", lambda: self.updater.open(), icon="refresh-cw"),
            )),
            CommandGroup(PLUS, (
                Command("plus.open", f"{PLUS}…", self.open_plus, icon="sparkles"),
                Command("plus.stats", "Listening stats", self.open_stats, icon="chart-column"),
            )),
            CommandGroup("Shows", (
                Command("shows.refresh", "Refresh", self.refresh_all, icon="refresh-cw", shortcut=("Ctrl", "R")),
                Command("shows.add", "Add a show by address…", self.ask_address, icon="link", shortcut=("Ctrl", "N")),
                Command("shows.import", "Import subscriptions…", self.import_opml, icon="upload", shortcut=("Ctrl", "O")),
                Command("shows.export", "Export subscriptions…", self.export_opml, icon="download",
                        shortcut=("Ctrl", "Shift", "S"), enabled=lambda: self.store.counts()["shows"] > 0),
                Command("shows.search", "Search podcasts", self.focus_search, icon="search", shortcut=("Ctrl", "F")),
            )),
        ])
        super().__init__(application=app, app_id=APP_ID, title=APP_NAME, icon_name=APP_ID, commands=commands,
                         default_width=1180, default_height=800, minimum_width=360, minimum_height=294)
        self.commands_registry = commands
        self._css()

        self.sidebar = NavigationSidebar(variant="files")
        self.sidebar.append_footer(SidebarFoot(search="Search podcasts", on_search=self._sidebar_search,
                                               add=("Add a show", "plus", self.ask_address)))
        self.sidebar.list.connect("row-activated", self._row_activated)

        self.island = Island()
        self.stack = Gtk.Stack(hexpand=True, vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        from luma_appkit.lumaui import reduced_motion
        self.stack.set_transition_duration(0 if reduced_motion() else 140)
        self.island.append(self.stack)
        self.host = ToastHost(self.island)
        body = Gtk.Box()
        body.append(self.sidebar)
        main = Gtk.Box(hexpand=True)
        main.append(self.host)
        body.append(main)
        self.title_island = TitleIsland(APP_NAME, "", lead="menu")
        self.title_island.add_trailing(TitleIsland.button("search", "Search podcasts", self.focus_search))
        self.title_island.add_trailing(TitleIsland.button("ellipsis", "More", self._island_more))
        self.sidebar_toggle = SidebarToggle(self.sidebar, island=self.title_island)
        self.set_leading(self.sidebar_toggle)
        self.set_body(body)
        self.title_island.float_over(body)
        self.center = ActionCenter().attach(self.host)

        self.mini = MediaMiniPlayer(on_open=self.open_now, on_play=self.toggle,
                                    on_next=lambda: self.skip(self.skip_forward()))
        self.mini.next.set_child(icons.image("rotate-cw"))
        self._label(self.mini.next, f"Forward {self.skip_forward()} seconds")
        self.mini_art = None

        self.player = Player()
        self.player.on_state = self._player_state
        self.player.on_position = self._player_position
        self.player.on_duration = self._player_duration
        self.player.on_eos = self._player_eos
        self.player.on_error = self._player_error
        self.player.set_volume(float(self.store.get("volume") or 1.0))
        self.player.voice_boost = self.voice_boost_on()
        self.paywall = Paywall(self, self.billing, PLUS, PLUS_FEATURES, open_uri=self.open_uri,
                               toast=lambda t, k="done": self.toast(t, k), changed=self._plus_changed)
        if self.billing.needs_revalidation():
            threading.Thread(target=self.billing.revalidate, daemon=True).start()
        self.updates = Updates(APP_ID, "chartmann1590/earshot", VERSION, os.path.join(data_dir(), "updates.json"),
                               os.path.join(GLib.get_user_cache_dir(), "earshot", "updates"))
        self.updater = Updater(self, self.updates, APP_NAME, grow=lambda k, w: self.center.grow(k, w, wide=True),
                               fold=lambda: self.center.fold(),
                               toast=lambda t, k="done", action=None: self.toast(t, k, **({"action": action} if action else {})))
        GLib.timeout_add_seconds(8, self.updater.start)
        self.mpris = None
        try:
            from .mpris import Mpris
            self.mpris = Mpris(self, APP_ID)
        except Exception as ex:  # no session bus (tests): playback still works
            print("earshot: media controls unavailable:", ex, file=sys.stderr)

        self.trail = NavigationTrail(Place("fresh", "New episodes"), on_change=lambda place, _d: self._show(place))
        self.tier_watch.connect("tier-changed", lambda *_: self._tiered())
        self.connect("close-request", self._closing)

        cur = self.store.get("current")
        if cur:
            self.current = self.store.episode(cur)
        self.refresh_sidebar()
        start = "fresh" if self.store.counts()["shows"] else "discover"
        self.go(start)
        self._tiered()
        self._update_mini()
        GLib.timeout_add_seconds(2, self._maybe_refresh)
        GLib.timeout_add_seconds(15 * 60, self._maybe_refresh)

    # ================================================================ helpers
    def _css(self):
        from luma_appkit import add_style_sheet
        path = os.path.join(os.path.dirname(__file__), "earshot.css")
        if os.path.exists(path):
            add_style_sheet(path)

    @staticmethod
    def _label(widget, text):
        widget.set_tooltip_text(text)
        widget.update_property([Gtk.AccessibleProperty.LABEL], [text])

    def toast(self, text, kind="done", **kw):
        Toast.show(self.host, text, kind=kind, **kw)

    def run(self, work, done=None, fail=None):
        """`work()` on a worker thread; `done(result)` or `fail(message)` back on the main loop."""
        def wrapped():
            try:
                result = work()
            except net.NetError as ex:
                msg = str(ex)
                GLib.idle_add(lambda: (fail(msg) if fail else self.toast(msg, "error"), False)[1])
                return
            except Exception as ex:  # never lose a worker silently
                import traceback
                traceback.print_exc()
                msg = f"Something went wrong: {ex}"
                GLib.idle_add(lambda: (fail(msg) if fail else self.toast(msg, "error"), False)[1])
                return
            if done is not None:
                GLib.idle_add(lambda: (done(result), False)[1])
        self.pool.submit(wrapped)

    def art_for(self, url: str, widget: Art | None = None, then=None) -> str:
        """The cached path for an image URL, fetching it once in the background; `widget` gets it when ready."""
        if not url:
            return ""
        path = self.art.get(url)
        if path:
            if widget is not None:
                widget.set_path(path)
            return path
        cached = net.artwork_path(url)
        if os.path.exists(cached) and os.path.getsize(cached) > 0:
            self.art[url] = cached
            if widget is not None:
                widget.set_path(cached)
            return cached
        waiters = self._art_waiters.setdefault(url, [])
        if widget is not None:
            waiters.append(widget)
        if then is not None:
            waiters.append(then)
        if len(waiters) == (1 if (widget or then) else 0) and url not in self.art:
            self.art[url] = ""
            self.run(lambda: net.fetch_artwork(url), lambda p: self._art_ready(url, p), lambda _m: None)
        return ""

    def _art_ready(self, url, path):
        self.art[url] = path
        for w in self._art_waiters.pop(url, []):
            if path:
                w(path) if callable(w) and not isinstance(w, Art) else w.set_path(path)

    # ---------------------------------------------------------------- Earshot Plus
    @property
    def plus(self) -> bool:
        return self.billing.owns()

    def open_uri(self, url: str):
        Gtk.UriLauncher.new(url).launch(self, None, None, None)

    def open_plus(self):
        self.center.grow("plus", self.paywall.panel, wide=True)

    def need_plus(self, what: str) -> bool:
        """True when Plus is owned; otherwise opens the upgrade panel (once) and returns False."""
        if self.plus:
            return True
        self.toast(f"{what} comes with {PLUS}", "done")
        self.open_plus()
        return False

    def _plus_changed(self):
        if not self.plus and self.player.voice_boost:
            self.set_voice_boost(False, quiet=True)
        self.show_bar(force=False)       # the upgrade panel stays open to say thank you
        if self.trail.current.view in ("show", "now", "stats"):
            self._show(self.trail.current)

    def voice_boost_on(self) -> bool:
        return bool(self.store.get("voice_boost")) and self.plus

    def set_voice_boost(self, on: bool, quiet: bool = False):
        if on and not self.need_plus("Voice boost"):
            return
        self.store.set("voice_boost", bool(on))
        if self.player.set_voice_boost(on) and self.loaded and self.current is not None:
            pos, playing = self.position(), self.player.wants_play
            self._load(self.current, play=playing, position=pos)
        if not quiet:
            self.toast("Voice boost on" if on else "Voice boost off", "done")
        b = self._now_widgets.get("boost")
        if b is not None:
            for c in ("raised",):
                (b.add_css_class if on else b.remove_css_class)(c)

    def show_options(self, url: str):
        if not self.need_plus("Show settings"):
            return
        p = self.store.podcast(url) or {}
        intro, outro, auto = int(p.get("skip_intro") or 0), int(p.get("skip_outro") or 0), bool(p.get("auto_download"))

        def secs(n):
            return "Off" if not n else (f"{n} s" if n < 60 else f"{n // 60} min" + (f" {n % 60} s" if n % 60 else ""))

        def cycle(option, choices, cur):
            nxt = choices[(choices.index(cur) + 1) % len(choices)] if cur in choices else choices[0]
            self.store.set_show_option(url, option, nxt)
            self.center.fold()
            self.show_options(url)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(PanelHeading(p.get("title") or "This show"))
        box.append(PanelRow("Skip the intro", icon="skip-forward", detail=secs(intro), closes=False,
                            on_activate=lambda: cycle("skip_intro", (0, 15, 30, 45, 60, 90, 120), intro)))
        box.append(PanelRow("Skip the outro", icon="skip-back", detail=secs(outro), closes=False,
                            on_activate=lambda: cycle("skip_outro", (0, 15, 30, 60, 90, 120), outro)))
        box.append(PanelRow("Download new episodes automatically", icon="download", toggle=auto,
                            on_toggle=lambda on: self.store.set_show_option(url, "auto_download", 1 if on else 0)))
        self.center.grow("show-options", box)

    def open_stats(self):
        if not self.need_plus("Listening stats"):
            return
        if self.trail.current.view != "stats":
            self.open_page("stats", "Listening stats", None)

    def _track_listening(self, seconds: float):
        """Count listening between ticks: wall time, and the time speed saved (Earshot Plus stats are kept
        for everyone, so they are all there the day someone upgrades)."""
        now = time.monotonic()
        mark, self._listen_mark = self._listen_mark, (now, seconds)
        if mark is None or self.current is None or not self.player.playing:
            return
        wall, content = now - mark[0], seconds - mark[1]
        if not (0 < wall < 5) or not (0 < content < 20):
            return  # a seek or a pause, not listening
        pend = self._listen_pending.setdefault(self.current.get("podcast") or "", [0.0, 0.0])
        pend[0] += wall
        pend[1] += max(0.0, content - wall)

    def _flush_listening(self):
        for url, (wall, saved) in self._listen_pending.items():
            if url and self.store.subscribed(url):
                self.store.add_listening(url, wall, saved)
        self._listen_pending = {}

    def skip_back(self) -> int:
        return int(self.store.get("skip_back") or 15)

    def skip_forward(self) -> int:
        return int(self.store.get("skip_forward") or 30)

    # ================================================================ navigation
    def go(self, view: str, title: str | None = None, subject=None):
        names = dict((k, t) for k, t, _ in PLACES)
        self.trail.start(Place(view, title or names.get(view, view), subject))
        if self.trail.current.view == view and self.trail.current.subject == subject:
            self._show(self.trail.current)

    def open_page(self, view: str, title: str, subject=None):
        self.trail.open(Place(view, title, subject))

    def _show(self, place: Place):
        builders = {"fresh": self._page_fresh, "queue": self._page_queue, "downloads": self._page_downloads,
                    "shows": self._page_shows, "discover": self._page_discover, "show": self._page_show,
                    "episode": self._page_episode, "now": self._page_now, "search": self._page_search,
                    "stats": self._page_stats}
        try:
            page = builders[place.view](place)
        except Exception as ex:
            import traceback
            traceback.print_exc()
            page = EmptyState("Couldn't show this page", str(ex), "lumaui-circle-alert-symbolic")
        old = self.stack.get_visible_child()
        name = f"p{time.monotonic_ns()}"
        self.stack.add_named(page, name)
        self.stack.set_visible_child(page)
        if old is not None:
            GLib.timeout_add(400, lambda: (old.get_parent() is self.stack and self.stack.remove(old), False)[1])
        if place.view != "now":
            self._now_widgets = {}
        self._select_rows()
        self.show_bar(force=not getattr(self, "_quiet", False))
        self.title_island.set_title(place.title, "")

    def _tile(self) -> int:
        return 128 if self.tier == "phone" else 148

    def _tiered(self):
        self.stack.set_margin_top(64 if self.tier == "phone" else 0)
        if getattr(self, "_last_tier", None) not in (None, self.tier):
            self._show(self.trail.current)   # tiles, headers and margins are sized per tier
        self._last_tier = self.tier

    def _island_more(self):
        from luma_appkit import MenuDrawer
        MenuDrawer.present(self, self.commands_registry)

    def page(self, place: Place, content: list[Gtk.Widget], *, meta=(), header: bool = True, wide: int = 880):
        """A scrolling page: the trail header, then the content, clamped to a readable width."""
        side = 12 if self.tier == "phone" else 20
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_start=side, margin_end=side,
                      margin_top=14, margin_bottom=140)
        if header == "back":
            if self.trail.can_go_back:
                from luma_appkit import NavigationTrailBar
                bar = NavigationTrailBar(self.trail, title=False)
                bar.set_halign(Gtk.Align.START)
                box.append(bar)
        elif header:
            box.append(PageHeader(self.trail, meta=list(meta)))
        for w in content:
            box.append(w)
        clamp = Adw.Clamp(maximum_size=wide, tightening_threshold=wide - 100, child=box)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, hexpand=True, child=clamp)
        return scroller

    # ================================================================ sidebar
    def refresh_sidebar(self):
        self._populating = True
        self.sidebar.clear()
        self._rows, self._place_rows = {}, {}
        counts = self.store.counts()
        trail = {"fresh": counts["fresh"], "queue": counts["queue"], "downloads": counts["downloads"] + len(self.downloads)}
        for key, title, icon in PLACES:
            row = SidebarRow(title, lead=RowLead.icon(icon), trail=trail.get(key) or None)
            row.place = key
            self._place_rows[key] = row
            self.sidebar.append_row(row)
        shows = self.store.podcasts()
        if shows:
            self.sidebar.append_section("Following")
            for p in shows:
                art = Art(p["title"], 26, radius=6)
                self.art_for(p["image"], art)
                row = SidebarRow(p["title"], lead=art, trail=p["fresh"] or None, subtitle=p["author"] or "")
                row.show_url = p["url"]
                self._rows[p["url"]] = row
                self.sidebar.append_row(row)
        self._select_rows()
        self._populating = False

    def _select_rows(self):
        if not self._place_rows:
            return
        cur = self.trail.current
        root = self.trail.places[0]
        lst = self.sidebar.list
        target = None
        if cur.view == "show":
            target = self._rows.get(cur.subject)
        if target is not None:
            lst.select_row(target)
        else:
            lst.unselect_all()
        for key, row in self._place_rows.items():
            on = key == root.view and target is None
            row.set_state_flags(Gtk.StateFlags.CHECKED, False) if on else row.unset_state_flags(Gtk.StateFlags.CHECKED)

    def _row_activated(self, _list, row):
        if self._populating:
            return
        if getattr(row, "place", None):
            self.go(row.place)
        elif getattr(row, "show_url", None):
            p = self.store.podcast(row.show_url)
            self.go("show", p["title"] if p else "Show", row.show_url)
        if self.tier == "phone" and hasattr(self.sidebar_toggle, "set_active"):
            self.sidebar_toggle.set_active(False)

    def _sidebar_search(self, text: str):
        if text.strip():
            self.search(text)

    # ================================================================ the bar
    def show_bar(self, force: bool = True):
        """Rebuild the bar. A background update (force=False) never folds a panel someone has open: it waits
        until the panel closes."""
        if not force and self.center.grown is not None:
            if not getattr(self, "_bar_pending", False):
                self._bar_pending = True
                GLib.timeout_add(400, self._bar_when_folded)
            return
        view = self.trail.current.view
        items = []
        if view == "fresh" and self.store.counts()["fresh"]:
            items.append(BarAction("check-check", "Clear", tooltip="Take everything out of New episodes",
                                   on_activate=self.clear_fresh))
        elif view == "queue" and self.store.counts()["queue"]:
            items.append(BarAction("trash-2", "Clear", tooltip="Empty Up next", on_activate=self.clear_queue))
        elif view == "downloads" and self.store.downloaded():
            items.append(BarAction("trash-2", "Remove played", tooltip="Remove downloads you've finished",
                                   on_activate=self.remove_played_downloads))
        elif view == "discover":
            items.append(BarAction("link", "Add by address", on_activate=self.ask_address))
        elif view == "show":
            url = self.trail.current.subject
            if self.store.subscribed(url):
                items.append(BarAction("refresh-cw", tooltip="Refresh this show", on_activate=lambda: self.refresh_show(url)))
            else:
                items.append(BarAction("plus", "Follow", primary=True, on_activate=lambda: self.follow(url)))
        items.append(BarSearch("Search", label="Search podcasts", collapsed=True, on_activate=self.search))
        items.append(BarAction("refresh-cw", tooltip="Refresh all shows", on_activate=self.refresh_all,
                               sensitive=not self.refreshing) if view != "show" else
                     BarAction("moon", tooltip="Sleep timer", on_activate=self.ask_sleep,
                               active=bool(self.sleep_mode), sensitive=self.current is not None))
        items.append(BarAction("settings", tooltip="Settings", panel=self._settings_panel, key="settings"))
        more = [BarAction("link", "Add a show by address…", on_activate=self.ask_address),
                BarAction("upload", "Import subscriptions…", on_activate=self.import_opml),
                BarAction("download", "Export subscriptions…", on_activate=self.export_opml),
                BarAction("moon", "Sleep timer…", on_activate=self.ask_sleep),
                BarAction("chart-column", "Listening stats", on_activate=self.open_stats),
                BarAction("sparkles", f"{PLUS}…", on_activate=self.open_plus),
                BarAction("download", "Check for updates…", on_activate=lambda: self.updater.open())]
        head = self.mini if self.current is not None and view != "now" else None
        self.center.show_bar(items, more=more, head=head, fill=True, head_inset=False)
        self.search_item = next((i for i in items if isinstance(i, BarSearch)), None)

    def _bar_when_folded(self):
        if self.center.grown is not None:
            return True
        self._bar_pending = False
        self.show_bar()
        return False

    def focus_search(self):
        if self.search_item is not None:
            try:
                self.center.open_search(self.search_item)
                return
            except Exception:
                pass
        self.go("discover")

    def _settings_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(PanelHeading(PLUS))
        if self.plus:
            box.append(PanelRow(f"{PLUS} is on", icon="sparkles", detail="Manage", on_activate=self.open_plus))
        else:
            box.append(PanelRow(f"Get {PLUS}", icon="sparkles", subtitle="Voice boost, skips, auto-downloads and stats",
                                on_activate=self.open_plus))
        box.append(PanelRow("Voice boost", icon="volume-2", toggle=self.voice_boost_on(),
                            on_toggle=lambda on: self.set_voice_boost(on)))
        box.append(PanelRow("Listening stats", icon="chart-column", on_activate=self.open_stats))
        box.append(PanelHeading("Playback"))
        box.append(PanelRow("Play the next episode in Up next", icon="list-ordered",
                            toggle=bool(self.store.get("autoplay_next")),
                            on_toggle=lambda on: self.store.set("autoplay_next", on)))
        box.append(PanelRow("Skip back", icon="rotate-ccw", detail=f"{self.skip_back()} s", closes=False,
                            on_activate=lambda: self._cycle_skip("skip_back", (10, 15, 30))))
        box.append(PanelRow("Skip forward", icon="rotate-cw", detail=f"{self.skip_forward()} s", closes=False,
                            on_activate=lambda: self._cycle_skip("skip_forward", (15, 30, 45, 60))))
        box.append(PanelHeading("Downloads"))
        box.append(PanelRow("Remove downloads after playing", icon="trash-2",
                            toggle=bool(self.store.get("remove_played_downloads")),
                            on_toggle=lambda on: self.store.set("remove_played_downloads", on)))
        box.append(PanelHeading("Updates"))
        box.append(PanelRow(f"Check for {APP_NAME} updates automatically", icon="download", toggle=self.updates.automatic,
                            on_toggle=self.updates.set_automatic))
        box.append(PanelRow("Check now", icon="refresh-cw", detail=VERSION, on_activate=lambda: self.updater.check(manual=True)))
        box.append(PanelHeading("Refresh"))
        minutes = int(self.store.get("refresh_minutes") or 60)
        words = {0: "Only when I ask", 30: "Every 30 minutes", 60: "Every hour", 360: "Every 6 hours"}
        box.append(PanelRow("Check for new episodes", icon="refresh-cw", detail=words.get(minutes, f"{minutes} min"),
                            closes=False, on_activate=lambda: self._cycle_refresh()))
        return box

    def _cycle_skip(self, key, choices):
        cur = int(self.store.get(key) or choices[0])
        nxt = choices[(choices.index(cur) + 1) % len(choices)] if cur in choices else choices[0]
        self.store.set(key, nxt)
        self._label(self.mini.next, f"Forward {self.skip_forward()} seconds")
        self.center.fold()
        self.center.grow("settings", self._settings_panel)
        if self.trail.current.view == "now":
            self._show(self.trail.current)

    def _cycle_refresh(self):
        choices = (30, 60, 360, 0)
        cur = int(self.store.get("refresh_minutes") or 60)
        self.store.set("refresh_minutes", choices[(choices.index(cur) + 1) % len(choices)] if cur in choices else 60)
        self.center.fold()
        self.center.grow("settings", self._settings_panel)

    # ================================================================ pages
    def _row(self, ep, *, show_name=True, queued_index=None):
        playing = self.current is not None and self.current["id"] == ep["id"] and self.player.playing
        dl = self.downloads.get(ep["id"])
        row = EpisodeRow(ep, art_path=self.art_for(ep.get("image") or ep.get("show_image") or ""),
                         on_open=self.open_episode, on_play=self.play_or_pause, on_more=self.episode_menu,
                         show_name=show_name, playing=playing, download=dl["fraction"] if dl else None,
                         queued_index=queued_index)
        if not row.art.texture:
            self.art_for(ep.get("image") or ep.get("show_image") or "", row.art)
        if dl is not None:
            dl["rows"].append(row)
        return row

    def _page_fresh(self, place):
        fresh = self.store.fresh(120)
        going = [e for e in self.store.in_progress(6) if not (self.current and e["id"] == self.current["id"]
                                                               and self.player.playing)]
        content = []
        if going:
            content.append(heading("Keep listening"))
            content.append(episode_list([self._row(e) for e in going[:4]]))
        if fresh:
            if going:
                content.append(heading(f"New · {len(fresh)}"))
            content.append(episode_list([self._row(e) for e in fresh]))
        if not fresh and not going:
            if self.store.counts()["shows"]:
                content.append(EmptyState("You're all caught up", "New episodes from the shows you follow land here.",
                                          "lumaui-inbox-symbolic", primary=("Discover shows", lambda: self.go("discover")),
                                          secondary=("Refresh", self.refresh_all)))
            else:
                return self._welcome()
        return self.page(place, content, meta=[] if fresh else ["Nothing new"])

    def _welcome(self):
        box = EmptyState("Find something to listen to",
                         "Follow shows from Discover, or bring your subscriptions from another app.",
                         "lumaui-headphones-symbolic", primary=("Discover shows", lambda: self.go("discover")),
                         secondary=("Import subscriptions…", self.import_opml))
        return box

    def _page_queue(self, place):
        q = self.store.queue()
        if not q:
            return EmptyState("Up next is empty", "Add episodes with ⋯ › Play next, or Add to Up next.",
                              "lumaui-list-ordered-symbolic", primary=("New episodes", lambda: self.go("fresh")))
        total = sum(max(0, (e["duration"] or 0) - (e["position"] or 0)) for e in q)
        rows = [self._row(e, queued_index=i) for i, e in enumerate(q)]
        meta = [f"{len(q)} episode" + ("" if len(q) == 1 else "s")]
        if total:
            meta.append(length_words(total))
        return self.page(place, [episode_list(rows)], meta=meta)

    def _page_downloads(self, place):
        eps = self.store.downloaded()
        active = [self.store.episode(i) for i in self.downloads]
        active = [e for e in active if e and not e.get("file")]
        if not eps and not active:
            return EmptyState("No downloads", "Download episodes to listen without a connection.",
                              "lumaui-download-symbolic", primary=("New episodes", lambda: self.go("fresh")))
        content = []
        if active:
            content.append(heading("Downloading"))
            content.append(episode_list([self._row(e) for e in active]))
        if eps:
            if active:
                content.append(heading("Ready to play"))
            content.append(episode_list([self._row(e) for e in eps]))
        size = 0
        for e in eps:
            try:
                size += os.path.getsize(e["file"])
            except OSError:
                pass
        return self.page(place, content, meta=[f"{len(eps)} episode" + ("" if len(eps) == 1 else "s"),
                                               GLib.format_size(size)])

    def _page_shows(self, place):
        shows = self.store.podcasts()
        if not shows:
            return self._welcome()
        tiles = []
        for p in shows:
            t = ShowTile(p["title"], p["author"], badge=p["fresh"], size=self._tile(),
                         on_open=lambda p=p: self.open_page("show", p["title"], p["url"]))
            self.art_for(p["image"], t.art)
            tiles.append(t)
        return self.page(place, [tile_grid(tiles)], meta=[f"{len(shows)} show" + ("" if len(shows) == 1 else "s")])

    def _page_discover(self, place):
        chips = Gtk.Box(spacing=6)
        for gid, name in net.GENRES:
            b = Gtk.ToggleButton(label=name, active=gid == self.genre)
            b.add_css_class("earshot-chip")
            b.connect("clicked", lambda _b, g=gid: self._pick_genre(g))
            chips.append(b)
        chip_scroll = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, child=chips, margin_bottom=8)
        chip_scroll.set_propagate_natural_height(True)
        self._discover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content = [chip_scroll, self._discover_box]
        cached = self.directory_cache.get(self.genre)
        if cached is not None:
            self._fill_directory(cached)
        else:
            spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, margin_top=60)
            spinner.set_size_request(32, 32)
            self._discover_box.append(spinner)
            genre = self.genre
            self.run(lambda: net.top(genre), lambda r: self._directory_loaded(genre, r),
                     lambda m: self._directory_failed(m))
        hint = TypeLabel("Search and charts come from the Apple Podcasts directory. Any show with a public feed "
                         "can be added by its address.", role="caption", wrap=True)
        hint.set_margin_top(28)
        content.append(hint)
        return self.page(place, content, meta=[dict(net.GENRES).get(self.genre, "Top shows")])

    def _pick_genre(self, gid):
        if gid != self.genre:
            self.genre = gid
            self._show(self.trail.current)

    def _directory_loaded(self, genre, results):
        self.directory_cache[genre] = results
        if self.trail.current.view == "discover" and genre == self.genre:
            self._fill_directory(results)

    def _directory_failed(self, message):
        if self.trail.current.view == "discover":
            self._clear(self._discover_box)
            self._discover_box.append(EmptyState("Couldn't load the charts", message, "lumaui-circle-alert-symbolic",
                                                 primary=("Try again", lambda: self._show(self.trail.current))))

    @staticmethod
    def _clear(box):
        child = box.get_first_child()
        while child is not None:
            box.remove(child)
            child = box.get_first_child()

    def _fill_directory(self, results):
        self._clear(self._discover_box)
        if not results:
            self._discover_box.append(ListEmptyState("Nothing here right now."))
            return
        self._discover_box.append(tile_grid([self._directory_tile(r) for r in results]))

    def _directory_tile(self, r):
        followed = self.store.subscribed(r["feed"])
        t = ShowTile(r["title"], r["author"] + (" · Following" if followed else ""), size=self._tile(),
                     on_open=lambda: self.open_directory(r))
        self.art_for(r["image"], t.art)
        return t

    def search(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        self.search_text = text
        if text.lower().startswith(("http://", "https://")):
            self.add_address(text)
            return
        if self.trail.current.view == "search":
            self.trail.replace(Place("search", f"“{text}”", text))
            self._show(self.trail.current)
        else:
            self.open_page("search", f"“{text}”", text)

    def _page_search(self, place):
        text = place.subject
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        mine = self.store.search(text, 30)
        if mine:
            box.append(heading("In your shows"))
            box.append(episode_list([self._row(e) for e in mine[:8]]))
        box.append(heading("Shows"))
        results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, margin_top=40)
        spinner.set_size_request(32, 32)
        results.append(spinner)
        box.append(results)

        def found(rs):
            if self.trail.current.view != "search" or self.trail.current.subject != text:
                return
            self._clear(results)
            if rs:
                results.append(tile_grid([self._directory_tile(r) for r in rs]))
            else:
                results.append(ListEmptyState(f"No shows match “{text}”."))

        def failed(msg):
            self._clear(results)
            results.append(ListEmptyState(msg))
        self.run(lambda: net.search(text), found, failed)
        return self.page(place, [box], meta=["Search"])

    # ---------------------------------------------------------------- a show
    def open_directory(self, r):
        self.preview.setdefault(r["feed"], {"directory": r})
        self.open_page("show", r["title"], r["feed"])

    def _page_show(self, place):
        url = place.subject
        p = self.store.podcast(url)
        if p is None:
            pv = self.preview.get(url, {})
            show = pv.get("show")
            if show is None:
                r = pv.get("directory", {"title": place.title, "author": "", "image": ""})
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                box.append(self._show_header(r["title"], r.get("author", ""), r.get("image", ""), "", url, False))
                spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, margin_top=40)
                spinner.set_size_request(32, 32)
                box.append(spinner)
                self.run(lambda: net.fetch_feed(url), lambda res: self._preview_loaded(url, res),
                         lambda m: self._preview_failed(url, m, box, spinner))
                return self.page(place, [box], header="back")
            eps = [dict(e, show=show["title"], show_image=show["image"], position=0, played=0, file="", podcast=url)
                   for e in show["episodes"]]
            header = self._show_header(show["title"], show.get("author", ""), show.get("image", ""),
                                       show.get("description", ""), url, False, count=len(eps))
            listing = self._paged_episodes(lambda offset, n: eps[offset:offset + n], len(eps))
            return self.page(place, [header, listing], header="back")
        total = self.store.counts_for(url)
        header = self._show_header(p["title"], p["author"], p["image"], p["description"], url, True, count=total,
                                   error=p.get("error") or "")
        content = [header]
        if total:
            content.append(self._paged_episodes(lambda offset, n: self.store.episodes(url, limit=n, offset=offset), total))
        else:
            content.append(ListEmptyState("No episodes yet."))
        return self.page(place, content, header="back")

    PAGE = 40

    def _paged_episodes(self, fetch, total, *, show_name=False):
        """An episode list that shows PAGE at a time, with Show more under it."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        shown = [0]
        more = TextButton("Show more", icon="chevron-down", on_click=lambda: add())
        more.set_halign(Gtk.Align.CENTER)
        more.set_margin_top(12)

        def add():
            eps = fetch(shown[0], self.PAGE)
            if eps:
                box.append(episode_list([self._row(e, show_name=show_name) for e in eps]))
            shown[0] += len(eps)
            more.set_visible(shown[0] < total and len(eps) == self.PAGE)
            if more.get_parent() is box:
                box.remove(more)
            box.append(more)
        add()
        return box

    def _preview_loaded(self, url, res):
        show = res["show"]
        if show is None:
            return
        self.preview.setdefault(url, {})["show"] = show
        if self.trail.current.view == "show" and self.trail.current.subject == url:
            self._show(self.trail.current)

    def _preview_failed(self, url, message, box, spinner):
        if spinner.get_parent() is box:
            box.remove(spinner)
            box.append(EmptyState("Couldn't open this show", message, "lumaui-circle-alert-symbolic",
                                  primary=("Try again", lambda: self._show(self.trail.current))))

    def _show_header(self, title, author, image, description, url, followed, *, count=0, error=""):
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_bottom=8)
        phone = self.tier == "phone"
        top = Gtk.Box(spacing=18, orientation=Gtk.Orientation.VERTICAL if phone else Gtk.Orientation.HORIZONTAL)
        art = Art(title, 148 if phone else 176)
        art.set_halign(Gtk.Align.CENTER if phone else Gtk.Align.START)
        self.art_for(image, art)
        top.append(art)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER, hexpand=True)
        t = Gtk.Label(label=title, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        t.add_css_class("earshot-show-title")
        info.append(t)
        if author:
            info.append(TypeLabel(author, role="lead", wrap=True))
        if count:
            info.append(TypeLabel(f"{count} episode" + ("" if count == 1 else "s"), role="caption"))
        keys = Gtk.Box(spacing=8, margin_top=6)
        if followed:
            keys.append(TextButton("Following", icon="check", style="raised", on_click=lambda: self.ask_unfollow(url)))
            keys.append(TextButton("Speed", icon="gauge", on_click=lambda: self.ask_show_speed(url)))
            opts = TextButton("Skips & downloads", icon="settings", on_click=lambda: self.show_options(url))
            keys.append(opts)
            if not self.plus:
                keys.append(badge())
        else:
            keys.append(TextButton("Follow", icon="plus", style="fill", on_click=lambda: self.follow(url)))
        info.append(keys)
        top.append(info)
        wrap.append(top)
        if error:
            err = TypeLabel(f"Last refresh failed: {error}", role="caption", wrap=True)
            err.add_css_class("error")
            wrap.append(err)
        plain = notes.to_plain(description, 1200)
        if plain:
            d = Gtk.Label(label=plain, xalign=0, wrap=True, lines=3, ellipsize=Pango.EllipsizeMode.END,
                          selectable=False, margin_top=4)
            d.add_css_class("earshot-description")
            more = TextButton("More", small=True, inline=True)
            def expand(_b=None):
                d.set_lines(-1)
                d.set_ellipsize(Pango.EllipsizeMode.NONE)
                more.set_visible(False)
            more.connect("clicked", expand)
            more.set_halign(Gtk.Align.START)
            wrap.append(d)
            if len(plain) > 220:
                wrap.append(more)
        return wrap

    # ---------------------------------------------------------------- an episode
    def open_episode(self, ep):
        self.open_page("episode", ep.get("title") or "Episode", ep)

    def _episode_now(self, ep):
        fresh = self.store.episode(ep["id"])
        return fresh or ep

    def _page_episode(self, place):
        ep = self._episode_now(place.subject)
        known = self.store.episode(ep["id"]) is not None
        phone = self.tier == "phone"
        top = Gtk.Box(spacing=18, orientation=Gtk.Orientation.VERTICAL if phone else Gtk.Orientation.HORIZONTAL)
        art = Art(ep.get("show") or ep["title"], 148 if phone else 160)
        self.art_for(ep.get("image") or ep.get("show_image") or "", art)
        top.append(art)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER, hexpand=True)
        info.append(link_button(ep.get("show") or "", lambda: self.open_page("show", ep.get("show") or "Show", ep["podcast"])))
        t = Gtk.Label(label=ep["title"], xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True)
        t.add_css_class("earshot-episode-heading")
        info.append(t)
        meta = [date_words(ep.get("published") or 0), status_words(ep)]
        if ep.get("season") or ep.get("number"):
            meta.insert(0, (f"Season {ep['season']} · " if ep.get("season") else "") +
                        (f"Episode {ep['number']}" if ep.get("number") else ""))
        info.append(TypeLabel(" · ".join(m for m in meta if m), role="caption", wrap=True))
        keys = Gtk.Box(spacing=8, margin_top=8)
        playing_this = self.current is not None and self.current["id"] == ep["id"] and self.player.playing
        word = "Pause" if playing_this else ("Resume" if (ep.get("position") or 0) > 0 and not ep.get("played") else "Play")
        keys.append(TextButton(word, icon="pause" if playing_this else "play", style="fill",
                               on_click=lambda: self.play_or_pause(ep)))
        if known:
            q = self.store.queued(ep["id"])
            keys.append(TextButton("In Up next" if q else "Up next", icon="check" if q else "list-plus",
                                   style="raised" if q else "plain",
                                   on_click=lambda: (self.store.dequeue(ep["id"]) if q else self.store.enqueue(ep["id"]),
                                                     self._changed())))
            if ep.get("file"):
                keys.append(TextButton("Downloaded", icon="check", on_click=lambda: self.ask_remove_download(ep)))
            elif ep["id"] in self.downloads:
                keys.append(TextButton("Cancel download", icon="x", on_click=lambda: self.cancel_download(ep["id"])))
            else:
                keys.append(TextButton("Download", icon="download", on_click=lambda: self.download(ep)))
        if phone:
            flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=2, min_children_per_line=2,
                               column_spacing=8, row_spacing=8, homogeneous=True)
            child = keys.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                keys.remove(child)
                flow.append(child)
                child = nxt
            info.append(flow)
        else:
            info.append(keys)
        top.append(info)
        content = [top]
        if ep.get("chapters"):
            content.append(heading("Chapters"))
            content.append(self._chapter_list(ep, False))
        md = notes.to_markup(ep.get("description") or "", ep.get("duration") or 0)
        if md:
            content.append(heading("Show notes"))
            content.append(self._notes_label(md, ep))
        if not known:
            content.append(TypeLabel("Follow the show to keep your place, queue and download its episodes.",
                                     role="caption", wrap=True))
        return self.page(place, content, header="back")

    def _notes_label(self, markup, ep):
        label = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True, use_markup=True)
        try:
            label.set_markup(markup)
        except Exception:
            label.set_text(notes.to_plain(markup, 100000))
        label.add_css_class("earshot-notes")
        label.connect("activate-link", lambda _l, uri: self._notes_link(uri, ep))
        return label

    def _notes_link(self, uri, ep):
        if uri.startswith("seek:"):
            try:
                seconds = float(uri[5:])
            except ValueError:
                return True
            if self.current is None or self.current["id"] != ep["id"]:
                self.play(ep, position=seconds)
            else:
                self.seek(seconds)
                self.play()
            self.toast(f"Playing from {clock(seconds)}", "done")
            return True
        Gtk.UriLauncher.new(uri).launch(self, None, None, None)
        return True

    def _chapter_list(self, ep, live: bool):
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("earshot-chapters")
        rows = []
        for i, c in enumerate(ep.get("chapters") or []):
            row = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=6, margin_end=6)
            at = Gtk.Label(label=clock(c["start"]), xalign=1, width_chars=7)
            at.add_css_class("earshot-chapter-time")
            row.append(at)
            name = Gtk.Label(label=c["title"] or f"Chapter {i + 1}", xalign=0, hexpand=True, wrap=True)
            row.append(name)
            lr = Gtk.ListBoxRow(child=row, activatable=True)
            lr.start = c["start"]
            lr.update_property([Gtk.AccessibleProperty.LABEL], [f"{name.get_label()}, at {clock(c['start'])}"])
            box.append(lr)
            rows.append(lr)
        box.connect("row-activated", lambda _b, r: self._notes_link(f"seek:{r.start}", ep))
        if live:
            self._now_widgets["chapter_rows"] = rows
        return box

    # ---------------------------------------------------------------- now playing
    def open_now(self):
        if self.current is None:
            return
        if self.trail.current.view == "now":
            return
        self.open_page("now", "Now playing", None)

    def _page_now(self, place):
        ep = self.current and self.store.episode(self.current["id"]) or self.current
        if ep is None:
            return EmptyState("Nothing playing", "Pick an episode to play.", "lumaui-headphones-symbolic",
                              primary=("New episodes", lambda: self.go("fresh")))
        self.current = ep
        phone = self.tier == "phone"
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, halign=Gtk.Align.FILL)
        art = Art(ep.get("show") or ep["title"], 260 if phone else 300)
        art.set_halign(Gtk.Align.CENTER)
        art.set_margin_top(6)
        self.art_for(ep.get("image") or ep.get("show_image") or "", art)
        col.append(art)
        chapter = Gtk.Label(xalign=0.5, wrap=True, visible=False)
        chapter.add_css_class("earshot-now-chapter")
        col.append(chapter)
        title = Gtk.Label(label=ep["title"], wrap=True, justify=Gtk.Justification.CENTER,
                          wrap_mode=Pango.WrapMode.WORD_CHAR, margin_top=4)
        title.add_css_class("earshot-now-title")
        col.append(title)
        col.append(link_button(ep.get("show") or "", lambda: self.open_page("show", ep.get("show") or "Show", ep["podcast"]),
                               center=True))
        scrub = ChapterScrub()
        duration = self.player.duration or ep.get("duration") or 0
        scrub.set_duration(duration, ep.get("chapters"))
        transport = MediaTransport("waveform", duration=duration, position=self.position(), playing=self.player.playing,
                                   speed=self.speed(), waveform=scrub, label="Playback",
                                   speeds=(0.8, 1.0, 1.1, 1.2, 1.3, 1.5, 1.75, 2.0, 2.5, 3.0),
                                   on_play=lambda want: self.play() if want else self.pause(),
                                   on_seek=self.seek, on_skip=lambda d: self.skip(-self.skip_back() if d < 0 else self.skip_forward()),
                                   on_speed=self.set_speed)
        for key, word in (("back", f"Back {self.skip_back()} seconds"), ("forward", f"Forward {self.skip_forward()} seconds")):
            k = transport.key(key)
            if k is not None:
                self._label(k, word)
        transport.set_margin_top(10)
        col.append(transport)
        extras = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=4)
        sleep = TextButton(self._sleep_words(), icon="moon", style="raised" if self.sleep_mode else "plain",
                           on_click=self.ask_sleep)
        extras.append(sleep)
        boost = TextButton("Voice boost", icon="volume-2", style="raised" if self.voice_boost_on() else "plain",
                           on_click=lambda: self.set_voice_boost(not self.voice_boost_on()))
        extras.append(boost)
        extras.append(TextButton("Up next", icon="list-ordered", on_click=lambda: self.go("queue")))
        extras.append(TextButton("Notes", icon="file-text", on_click=lambda: self.open_page("episode", ep["title"], ep)))
        col.append(extras)
        self._now_widgets = {"transport": transport, "scrub": scrub, "chapter": chapter, "sleep": sleep, "boost": boost}
        content = [col]
        if ep.get("chapters"):
            content.append(heading("Chapters"))
            content.append(self._chapter_list(ep, True))
        nxt = self.store.next_in_queue(after=ep["id"])
        if nxt:
            content.append(heading("Up next"))
            content.append(episode_list([self._row(nxt)]))
        self._update_chapter(self.position())
        page = self.page(place, content, wide=620)
        transport.attach_shortcuts(page)  # Space, ← and → while you're on Now playing
        return page

    def _page_stats(self, place):
        if not self.plus:
            return EmptyState(f"Listening stats come with {PLUS}", "See how much you listen, your top shows, and the "
                              "time your speed settings save you.", "lumaui-chart-column-symbolic",
                              primary=(f"Get {PLUS}", self.open_plus))
        self._flush_listening()
        week = time.strftime("%Y-%m-%d", time.localtime(time.time() - 6 * 86400))
        w, total = self.store.stats(week), self.store.stats()

        def hours(s):
            s = int(s)
            return f"{s // 3600} h {s % 3600 // 60} min" if s >= 3600 else f"{s // 60} min"

        tiles = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, min_children_per_line=1, max_children_per_line=4,
                            column_spacing=10, row_spacing=10, homogeneous=True)
        for value, label in ((hours(w["seconds"]), "Listened this week"), (hours(w["saved"]), "Saved by speed this week"),
                             (hours(total["seconds"]), "Listened in all"), (str(total["finished"]), "Episodes finished")):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            card.add_css_class("earshot-stat")
            v = Gtk.Label(label=value, xalign=0)
            v.add_css_class("earshot-stat-value")
            card.append(v)
            card.append(TypeLabel(label, role="caption"))
            tiles.append(card)
        content = [tiles]
        # the last seven days as bars
        days = dict(w["days"])
        top = max([s for _d, s in w["days"]] + [1])
        bars = Gtk.Box(spacing=8, homogeneous=True, margin_top=8)
        for i in range(6, -1, -1):
            t = time.localtime(time.time() - i * 86400)
            key = time.strftime("%Y-%m-%d", t)
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            from luma_appkit import ProgressLine
            line = ProgressLine(days.get(key, 0) / top, label=f"{time.strftime('%A', t)}: {hours(days.get(key, 0))}")
            col.append(line)
            col.append(TypeLabel(time.strftime("%a", t), role="caption"))
            bars.append(col)
        content += [heading("This week"), bars]
        if total["shows"]:
            content.append(heading("Top shows"))
            rows = []
            best = total["shows"][0]["seconds"] or 1
            for s in total["shows"]:
                row = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6)
                art = Art(s["title"], 40)
                self.art_for(s.get("image") or "", art)
                row.append(art)
                col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
                col.append(caption(f"{s['title']} · {hours(s['seconds'])}"))
                from luma_appkit import ProgressLine
                col.append(ProgressLine(s["seconds"] / best, label=s["title"]))
                row.append(col)
                rows.append(row)
            content.append(episode_list(rows))
        else:
            content.append(TypeLabel("Listen to something and your stats start here.", role="caption", wrap=True))
        return self.page(place, content, meta=["Last 7 days and all time"])

    def _sleep_words(self):
        if self.sleep_mode == -1:
            return "Sleep: end of episode"
        if self.sleep_mode == -2:
            return "Sleep: end of chapter"
        if self.sleep_mode > 0:
            left = max(0, int(self.sleep_until - time.monotonic()))
            return f"Sleep in {left // 60 + (1 if left % 60 else 0)} min"
        return "Sleep timer"

    def _chapter_at(self, seconds):
        chs = (self.current or {}).get("chapters") or []
        idx = -1
        for i, c in enumerate(chs):
            if c["start"] <= seconds + 0.25:
                idx = i
        return idx

    def _update_chapter(self, seconds):
        label = self._now_widgets.get("chapter")
        idx = self._chapter_at(seconds)
        chs = (self.current or {}).get("chapters") or []
        if label is not None:
            label.set_visible(idx >= 0)
            if idx >= 0:
                label.set_label(chs[idx]["title"] or f"Chapter {idx + 1}")
        for i, r in enumerate(self._now_widgets.get("chapter_rows", [])):
            (r.add_css_class if i == idx else r.remove_css_class)("current")

    # ================================================================ playback
    def speed(self, ep=None) -> float:
        ep = ep or self.current
        if ep:
            p = self.store.podcast(ep["podcast"])
            if p and p.get("speed"):
                return float(p["speed"])
        return float(self.store.get("speed") or 1.0)

    def set_speed(self, rate: float):
        rate = round(max(0.5, min(3.0, float(rate))), 2)
        if self.current:
            self.store.set_speed(self.current["podcast"], rate)
        self.store.set("speed", rate)
        self.player.set_rate(rate)
        t = self._now_widgets.get("transport")
        if t is not None:
            t.set_speed(rate)
        self._mpris()

    def nudge_speed(self, direction: int):
        steps = (0.8, 1.0, 1.1, 1.2, 1.3, 1.5, 1.75, 2.0, 2.5, 3.0)
        cur = self.speed()
        i = min(range(len(steps)), key=lambda k: abs(steps[k] - cur))
        self.set_speed(steps[max(0, min(len(steps) - 1, i + direction))])
        self.toast(f"Speed {self.speed():g}×", "done")

    def ask_show_speed(self, url):
        p = self.store.podcast(url)
        cur = (p or {}).get("speed")
        rows = ["Speed for this show"]
        rows.append(MenuItem("Same as other shows", selected=not cur, on_activate=lambda: self._show_speed(url, None)))
        for s in (0.8, 1.0, 1.1, 1.2, 1.3, 1.5, 1.75, 2.0, 2.5):
            rows.append(MenuItem(f"{s:g}×", selected=cur == s, on_activate=lambda s=s: self._show_speed(url, s)))
        FloatingMenu(rows, title="Speed").popup(self.stack, align="center")

    def _show_speed(self, url, s):
        self.store.set_speed(url, s)
        if self.current and self.current["podcast"] == url:
            self.player.set_rate(self.speed())
        self.toast(f"{(self.store.podcast(url) or {}).get('title', 'This show')} plays at {s:g}×" if s else
                   "This show plays at your usual speed", "done")

    def position(self) -> float:
        if self.current is None:
            return 0.0
        if self.loaded:
            return self.player.position()
        return float(self.current.get("position") or 0)

    def play_or_pause(self, ep):
        if self.current is not None and self.current["id"] == ep["id"] and self.loaded:
            self.toggle()
        else:
            self.play(ep)

    def play(self, ep=None, position: float | None = None):
        if ep is None:
            if self.current is None:
                return
            if not self.loaded:
                self._load(self.current, play=True)
            else:
                self.player.play()
            return
        known = self.store.episode(ep["id"])
        if known is None:
            # an episode of a show you don't follow: play it, but there's nowhere to keep your place
            known = dict(ep)
        self._save_position()
        self.current = known
        self.store.set("current", known["id"] if self.store.episode(known["id"]) else "")
        if self.store.episode(known["id"]):
            self.store.dequeue(known["id"])
            self.store.dismiss(known["id"])
            if known.get("played"):
                self.store.mark_played(known["id"], False)
                known["position"] = 0
        start = position if position is not None else (0 if known.get("played") else float(known.get("position") or 0))
        if position is None and self.plus:
            intro = int((self.store.podcast(known.get("podcast") or "") or {}).get("skip_intro") or 0)
            if intro and start < intro:
                start = float(intro)   # skip the intro
        self._load(known, play=True, position=start)
        self._fetch_chapters(known)
        self._changed()

    def _load(self, ep, *, play: bool, position: float | None = None):
        path = ep.get("file") or ""
        uri = GLib.filename_to_uri(path, None) if path and os.path.exists(path) else ep["url"]
        if position is None:
            position = float(ep.get("position") or 0)
        self.player.set_rate(self.speed(ep))
        self.player.load(uri, position=position, play=play, duration=float(ep.get("duration") or 0))
        self.loaded = True
        self._update_mini()
        self._mpris()

    def _fetch_chapters(self, ep):
        if ep.get("chapters") or not ep.get("chapters_url"):
            return
        eid, url = ep["id"], ep["chapters_url"]

        def got(chs):
            if not chs:
                return
            if self.store.episode(eid):
                self.store.set_chapters(eid, chs)
            if self.current and self.current["id"] == eid:
                self.current["chapters"] = chs
                if self.trail.current.view == "now":
                    self._show(self.trail.current)
        self.run(lambda: net.fetch_chapters(url), got, lambda _m: None)

    def toggle(self):
        if self.current is None:
            return
        if self.loaded and self.player.wants_play:
            self.pause()
        else:
            self.play()

    def pause(self):
        self.player.pause()
        self._save_position()

    def seek(self, seconds: float):
        if self.current is None:
            return
        if not self.loaded:
            self._load(self.current, play=False, position=seconds)
        self.player.seek(seconds)
        self._save_position(seconds)
        if self.mpris:
            self.mpris.seeked(seconds)

    def skip(self, delta: float):
        if self.current is None:
            return
        self.seek(max(0.0, self.position() + delta))

    def set_volume(self, v):
        self.player.set_volume(v)
        self.store.set("volume", v)

    def raise_window(self):
        self.set_visible(True)
        self.present()

    def quit(self):
        self._save_position()
        self.app_ref.quit()

    def state(self) -> dict:
        ep = self.current
        return {"status": ("Playing" if self.player.playing else "Paused") if ep else "Stopped", "episode": ep,
                "position": self.position(), "duration": self.player.duration or (ep or {}).get("duration") or 0,
                "rate": self.player.rate, "volume": float(self.store.get("volume") or 1.0),
                "art": self.art.get((ep or {}).get("image") or (ep or {}).get("show_image") or "", "")}

    def _mpris(self):
        if self.mpris:
            self.mpris.changed()

    def _save_position(self, seconds: float | None = None):
        if self.current is None or not self.store.episode(self.current["id"]):
            return
        pos = self.position() if seconds is None else seconds
        if pos <= 0 and not self.loaded:
            return
        self._flush_listening()
        done = self.store.set_position(self.current["id"], pos, self.player.duration or None)
        self.current["position"] = pos
        self._last_save = time.monotonic()
        return done

    def _player_state(self, playing: bool):
        if not playing:
            self._save_position()
        self._update_mini()
        t = self._now_widgets.get("transport")
        if t is not None:
            t.set_playing(playing)
        self._mpris()
        app = self.app_ref
        if playing and not getattr(app, "_held", False):
            app.hold()
            app._held = True
        elif not playing and getattr(app, "_held", False) and self.get_visible():
            app.release()
            app._held = False
        self._refresh_rows_playing()

    def _player_position(self, seconds: float):
        self._track_listening(seconds)
        if self.plus and self.current is not None:
            p = self.store.podcast(self.current.get("podcast") or "") or {}
            outro, dur = int(p.get("skip_outro") or 0), self.player.duration
            if outro and dur > outro * 2 and seconds >= dur - outro:
                self._player_eos()   # skip the outro: count it as finished and move on
                return
        t = self._now_widgets.get("transport")
        if t is not None:
            t.set_position(seconds)
        self._update_chapter(seconds)
        self._update_mini(seconds)
        if time.monotonic() - self._last_save > SAVE_EVERY:
            self._save_position(seconds)
        if self.sleep_mode == -2:
            idx = self._chapter_at(seconds)
            if getattr(self, "_sleep_chapter", None) is None:
                self._sleep_chapter = idx
            elif idx != self._sleep_chapter:
                self._sleep_now()

    def _player_duration(self, seconds: float):
        t = self._now_widgets.get("transport")
        if t is not None:
            t.set_duration(seconds)
        s = self._now_widgets.get("scrub")
        if s is not None:
            s.set_duration(seconds, (self.current or {}).get("chapters"))
        if self.current and self.store.episode(self.current["id"]):
            self.store.set_position(self.current["id"], self.position(), seconds)
        self._mpris()

    def _player_eos(self):
        ep = self.current
        if ep is None:
            return
        if self.store.episode(ep["id"]):
            self.store.mark_played(ep["id"])
            if self.store.get("remove_played_downloads") and ep.get("file"):
                self._delete_download(ep["id"], quiet=True)
        nxt = self.store.next_in_queue(after=ep["id"])
        if self.sleep_mode == -1:
            self._sleep_now()
            nxt = None
        if nxt and self.store.get("autoplay_next"):
            self.play(nxt)
            self.toast(f"Up next: {nxt['title']}", "done")
        else:
            self.loaded = False
            self.current = self.store.episode(ep["id"]) or ep
            self.player.stop()
            self._changed()

    def _player_error(self, message: str):
        self.loaded = False
        self.toast(message, "error", action=("Try again", lambda: self.play()))
        self._update_mini()
        self._mpris()

    def _update_mini(self, seconds: float | None = None):
        ep = self.current
        if ep is None:
            return
        art_url = ep.get("image") or ep.get("show_image") or ""
        if getattr(self, "_mini_for", None) != ep["id"]:
            self._mini_for = ep["id"]
            self.mini_art = Art(ep.get("show") or ep["title"], 44, radius=8)
            self.art_for(art_url, self.mini_art, then=lambda _p: self._mpris())
            self.mini.set_artwork(self.mini_art)
        dur = self.player.duration or ep.get("duration") or 0
        pos = self.position() if seconds is None else seconds
        detail = ep.get("show") or ""
        if dur:
            detail = f"{detail} · {clock(max(0, dur - pos))} left" if detail else f"{clock(max(0, dur - pos))} left"
        self.mini.update(ep["title"], detail, playing=self.player.playing, fraction=(pos / dur) if dur else 0)

    def _refresh_rows_playing(self):
        # Rows show "Now playing"; rebuilding list pages is cheap and keeps them honest.
        v = self.trail.current.view
        if v in ("fresh", "queue", "downloads", "show", "episode"):
            if not getattr(self, "_rebuild_pending", False):
                self._rebuild_pending = True
                GLib.timeout_add(120, self._rebuild_now)

    def _rebuild_now(self):
        self._rebuild_pending = False
        adj = self._scroll_value()
        self._quiet = True               # a background refresh of the page: don't fold an open panel
        try:
            self._show(self.trail.current)
        finally:
            self._quiet = False
        GLib.idle_add(lambda: (self._set_scroll(adj), False)[1])
        return False

    def _scroller(self):
        page = self.stack.get_visible_child()
        return page if isinstance(page, Gtk.ScrolledWindow) else None

    def _scroll_value(self):
        s = self._scroller()
        return s.get_vadjustment().get_value() if s else 0

    def _set_scroll(self, value):
        s = self._scroller()
        if s:
            s.get_vadjustment().set_value(value)

    def _changed(self):
        """Something in the library changed: the sidebar counts, the bar and the page follow."""
        self.refresh_sidebar()
        if self.current is not None and self.center is not None:
            pass
        self._rebuild_now()

    # ---------------------------------------------------------------- sleep timer
    def ask_sleep(self):
        rows = ["Sleep timer"]
        for value, words in SLEEP_CHOICES:
            if value == -2 and not (self.current or {}).get("chapters"):
                continue
            rows.append(MenuItem(words, icon="moon" if value else "x", selected=value == self.sleep_mode,
                                 on_activate=lambda v=value: self.set_sleep(v)))
        anchor = self._now_widgets.get("sleep") or self.stack
        FloatingMenu(rows, title="Sleep timer").popup(anchor, align="center")

    def set_sleep(self, value: int):
        if self._sleep_timer:
            GLib.source_remove(self._sleep_timer)
            self._sleep_timer = 0
        self.sleep_mode = value
        self._sleep_chapter = None
        if value > 0:
            self.sleep_until = time.monotonic() + value * 60
            self._sleep_timer = GLib.timeout_add_seconds(15, self._sleep_tick)
            self.toast(f"Stopping in {value} minutes", "done")
        elif value == -1:
            self.toast("Stopping at the end of this episode", "done")
        elif value == -2:
            self.toast("Stopping at the end of this chapter", "done")
        else:
            self.toast("Sleep timer off", "done")
        self._sync_sleep()

    def _sync_sleep(self):
        b = self._now_widgets.get("sleep")
        if b is not None:
            b.set_label(self._sleep_words()) if hasattr(b, "set_label") else None
        self.show_bar()

    def _sleep_tick(self):
        if self.sleep_mode <= 0:
            self._sleep_timer = 0
            return False
        left = self.sleep_until - time.monotonic()
        if left <= 0:
            self._sleep_timer = 0
            self._sleep_now()
            return False
        if left < 30:  # fade out the last seconds instead of cutting someone off mid-sentence
            self.player.set_volume(float(self.store.get("volume") or 1.0) * max(0.1, left / 30))
        self._sync_sleep()
        return True

    def _sleep_now(self):
        self.pause()
        self.player.set_volume(float(self.store.get("volume") or 1.0))
        self.sleep_mode = 0
        if self._sleep_timer:
            GLib.source_remove(self._sleep_timer)
            self._sleep_timer = 0
        self._sync_sleep()
        self.toast("Sleep timer: paused", "paused")

    # ================================================================ episode actions
    def episode_menu(self, ep, anchor):
        known = self.store.episode(ep["id"])
        rows = []
        if known:
            q = self.store.queued(ep["id"])
            rows.append(MenuItem("Play next", icon="list-plus",
                                 on_activate=lambda: self._enqueue(ep, front=True)))
            if q:
                rows.append(MenuItem("Remove from Up next", icon="x", on_activate=lambda: self._dequeue(ep)))
                qi = [e["id"] for e in self.store.queue()]
                i = qi.index(ep["id"])
                if i > 0:
                    rows.append(MenuItem("Move up", icon="chevron-up", on_activate=lambda: self._move(ep, i - 1)))
                if i < len(qi) - 1:
                    rows.append(MenuItem("Move down", icon="chevron-down", on_activate=lambda: self._move(ep, i + 1)))
            else:
                rows.append(MenuItem("Add to Up next", icon="list-ordered", on_activate=lambda: self._enqueue(ep)))
            if known.get("file"):
                rows.append(MenuItem("Remove download", icon="trash-2", on_activate=lambda: self._delete_download(ep["id"])))
            elif ep["id"] in self.downloads:
                rows.append(MenuItem("Cancel download", icon="x", on_activate=lambda: self.cancel_download(ep["id"])))
            else:
                rows.append(MenuItem("Download", icon="download", on_activate=lambda: self.download(ep)))
            if known.get("played"):
                rows.append(MenuItem("Mark as not played", icon="circle-check", on_activate=lambda: self._played(ep, False)))
            else:
                rows.append(MenuItem("Mark as played", icon="check", on_activate=lambda: self._played(ep, True)))
            if known.get("new"):
                rows.append(MenuItem("Remove from New episodes", icon="inbox", on_activate=lambda: self._dismiss(ep)))
            rows.append(None)
            rows.append(MenuItem("Go to show", icon="library",
                                 on_activate=lambda: self.open_page("show", ep.get("show") or "Show", ep["podcast"])))
        rows.append(MenuItem("Show notes", icon="file-text", on_activate=lambda: self.open_episode(ep)))
        if ep.get("link"):
            rows.append(MenuItem("Open episode web page", icon="globe",
                                 on_activate=lambda: Gtk.UriLauncher.new(ep["link"]).launch(self, None, None, None)))
        rows.append(MenuItem("Copy link to audio", icon="link", on_activate=lambda: self._copy(ep["url"])))
        FloatingMenu(rows, label="Episode", title=ep.get("title")).popup(anchor)

    def _copy(self, text):
        self.get_clipboard().set(text)
        self.toast("Link copied", "copied")

    def _enqueue(self, ep, front=False):
        self.store.enqueue(ep["id"], front=front)
        self.toast("Playing next" if front else "Added to Up next", "added",
                   undo=lambda: (self.store.dequeue(ep["id"]), self._changed()))
        self._changed()

    def _dequeue(self, ep):
        self.store.dequeue(ep["id"])
        self.toast("Removed from Up next", "deleted", undo=lambda: (self.store.enqueue(ep["id"]), self._changed()))
        self._changed()

    def _move(self, ep, index):
        self.store.move(ep["id"], index)
        self._changed()

    def _played(self, ep, played):
        before = self.store.episode(ep["id"])
        self.store.mark_played(ep["id"], played)
        if played and self.store.get("remove_played_downloads") and before and before.get("file"):
            self._delete_download(ep["id"], quiet=True)

        def undo():
            self.store.mark_played(ep["id"], not played)
            if before:
                self.store.set_position(ep["id"], before["position"] or 0)
            self._changed()
        self.toast("Marked as played" if played else "Marked as not played", "done", undo=undo)
        self._changed()

    def _dismiss(self, ep):
        self.store.dismiss(ep["id"])
        self.toast("Removed from New episodes", "archived",
                   undo=lambda: (self.store.db.execute("UPDATE episodes SET new = 1 WHERE id = ?", (ep["id"],)),
                                 self.store.db.commit(), self._changed()))
        self._changed()

    def clear_fresh(self):
        ids = [e["id"] for e in self.store.fresh()]
        self.store.dismiss_all()

        def undo():
            self.store.db.executemany("UPDATE episodes SET new = 1 WHERE id = ?", [(i,) for i in ids])
            self.store.db.commit()
            self._changed()
        self.toast(f"Cleared {len(ids)} new episode" + ("" if len(ids) == 1 else "s"), "archived", undo=undo)
        self._changed()

    def clear_queue(self):
        ids = [e["id"] for e in self.store.queue()]
        self.store.clear_queue()

        def undo():
            for i in ids:
                self.store.enqueue(i)
            self._changed()
        self.toast("Up next is empty", "deleted", undo=undo)
        self._changed()

    # ---------------------------------------------------------------- downloads
    def download(self, ep):
        eid = ep["id"]
        if eid in self.downloads or not self.store.episode(eid):
            return
        state = {"cancel": threading.Event(), "fraction": 0.0, "rows": []}
        self.downloads[eid] = state
        full = self.store.episode(eid)
        last = [0.0]

        def progress(done, total):
            if total:
                f = min(1.0, done / total)
                if f - last[0] >= 0.01 or f >= 1:
                    last[0] = f
                    GLib.idle_add(lambda f=f: (self._download_progress(eid, f), False)[1])

        def work():
            return net.download(full, progress, state["cancel"])

        def done(path):
            self.downloads.pop(eid, None)
            if path:
                self.store.set_file(eid, path)
                self.toast(f"Downloaded “{full['title']}”", "downloaded")
                if self.current and self.current["id"] == eid:
                    self.current["file"] = path
            self._changed()

        def failed(msg):
            self.downloads.pop(eid, None)
            self.toast(msg, "error", action=("Try again", lambda: self.download(ep)))
            self._changed()
        self.run(work, done, failed)
        self.toast("Downloading…", "downloaded")
        self._changed()

    def _download_progress(self, eid, f):
        st = self.downloads.get(eid)
        if not st:
            return
        st["fraction"] = f
        for r in st["rows"]:
            r.set_download(f)

    def cancel_download(self, eid):
        st = self.downloads.get(eid)
        if st:
            st["cancel"].set()
        self.toast("Download cancelled", "deleted")

    def _delete_download(self, eid, quiet=False):
        ep = self.store.episode(eid)
        if not ep or not ep.get("file"):
            return
        path = ep["file"]
        if self.current and self.current["id"] == eid and self.loaded and self.player.uri.startswith("file:"):
            # switch the stream under the player rather than pull the file out from under it
            pos, playing = self.position(), self.player.wants_play
            self.store.set_file(eid, "")
            self.current["file"] = ""
            self._load(self.current, play=playing, position=pos)
        try:
            os.remove(path)
        except OSError:
            pass
        self.store.set_file(eid, "")
        if not quiet:
            self.toast("Download removed", "deleted")
            self._changed()

    def ask_remove_download(self, ep):
        self._delete_download(ep["id"])

    def remove_played_downloads(self):
        n = 0
        for ep in self.store.downloaded():
            if ep.get("played"):
                self._delete_download(ep["id"], quiet=True)
                n += 1
        self.toast(f"Removed {n} played download" + ("" if n == 1 else "s") if n else "No played downloads to remove",
                   "deleted" if n else "done")
        self._changed()

    # ---------------------------------------------------------------- following
    def follow(self, url, *, quiet=False, then=None):
        if self.store.subscribed(url):
            return
        pv = self.preview.get(url, {})
        directory_id = (pv.get("directory") or {}).get("directory_id", "")

        def subscribed(res):
            final = res.get("moved_to") or url
            show = res["show"]
            if show is None:
                return
            if not self.store.subscribed(final):
                self.store.subscribe(final, show, etag=res.get("etag", ""), modified=res.get("modified", ""),
                                     directory_id=directory_id)
            self.preview.pop(url, None)
            self.art_for(show.get("image", ""))
            if not quiet:
                self.toast(f"Following {show['title']}", "added", undo=lambda: self._unfollow(final, quiet=True))
            if self.trail.current.view == "show" and self.trail.current.subject == url and final != url:
                self.trail.replace(Place("show", show["title"], final))
            self._changed()
            if then:
                then()

        if pv.get("show") is not None:
            subscribed({"show": pv["show"], "moved_to": ""})
        else:
            self.run(lambda: net.fetch_feed(url), subscribed,
                     lambda m: self.toast(f"Couldn't follow that show: {m}", "error"))

    def ask_unfollow(self, url):
        p = self.store.podcast(url)
        if p is None:
            return
        DestructiveDialog.in_bar(self.center, title=f"Stop following {p['title']}?",
                                 body="Its episodes, downloads and your place in them are removed.",
                                 action="Unfollow", on_confirm=lambda: self._unfollow(url))

    def _unfollow(self, url, quiet=False):
        p = self.store.podcast(url)
        if self.current and self.current.get("podcast") == url:
            self._save_position()
            self.player.stop()
            self.loaded = False
            self.current = None
            self.store.set("current", "")
            self.mini_art = None
            self._mini_for = None
        for f in self.store.unsubscribe(url):
            try:
                os.remove(f)
            except OSError:
                pass
        if not quiet and p:
            self.toast(f"Unfollowed {p['title']}", "deleted")
        if self.trail.current.view == "show" and self.trail.current.subject == url:
            self._changed()
        else:
            self._changed()

    def ask_address(self):
        from luma_appkit import TextField
        def add(_text=None):
            self.center.fold()
            self.add_address(field.text)
        field = TextField("Feed or website address", placeholder="https://", purpose="url", on_activate=add)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=4, margin_end=4)
        box.append(PanelHeading("Add a show by address"))
        box.append(field)
        hint = TypeLabel("Paste the show's RSS feed, or its website if it links to one.", role="caption", wrap=True)
        box.append(hint)
        box.append(TextButton("Add show", icon="plus", style="fill", on_click=add))
        self.center.grow("address", box)
        GLib.idle_add(lambda: (field.grab_focus(), False)[1])

    def add_address(self, text):
        url = (text or "").strip()
        if not url:
            return
        if "://" not in url:
            url = "https://" + url
        if url.lower().startswith("feed://"):
            url = "https://" + url[7:]
        if not url.lower().startswith(("http://", "https://")):
            self.toast("That isn't a web address.", "error")
            return
        if self.store.subscribed(url):
            p = self.store.podcast(url)
            self.go("show", p["title"], url)
            return
        self.toast("Looking for the show…", "done")

        def found(res):
            final = res.get("moved_to") or url
            self.preview[final] = {"show": res["show"]}
            self.open_page("show", res["show"]["title"], final)
        self.run(lambda: net.fetch_feed(url), found, lambda m: self.toast(m, "error"))

    # ---------------------------------------------------------------- refresh
    def _maybe_refresh(self):
        minutes = int(self.store.get("refresh_minutes") or 0)
        last = float(self.store.get("last_refresh") or 0)
        if minutes and time.time() - last > minutes * 60 and self.store.counts()["shows"]:
            self.refresh_all(quiet=True)
        return True

    def refresh_show(self, url):
        self._refresh([self.store.podcast(url)], quiet=False)

    def refresh_all(self, quiet=False):
        if self.refreshing:
            return
        shows = self.store.podcasts()
        if not shows:
            if not quiet:
                self.toast("Follow a show first", "done")
            return
        self._refresh(shows, quiet=quiet)

    def _refresh(self, shows, quiet):
        shows = [p for p in shows if p]
        self.refreshing = True
        self.show_bar(force=False)
        pending = [len(shows)]
        fresh_all: list[str] = []
        failures = [0]
        if not quiet:
            self.toast(f"Checking {len(shows)} show" + ("" if len(shows) == 1 else "s") + "…", "done")

        def one(p):
            url = p["url"]

            def done(res):
                target = url
                if res.get("moved_to") and self.store.move_feed(url, res["moved_to"]):
                    target = res["moved_to"]
                fresh_all.extend(self.store.refreshed(target, res["show"], etag=res.get("etag", ""),
                                                      modified=res.get("modified", "")))
                finish()

            def fail(msg):
                failures[0] += 1
                self.store.refreshed(url, None, error=msg)
                finish()
            self.run(lambda: net.fetch_feed(url, p.get("etag") or "", p.get("modified") or ""), done, fail)

        def finish():
            pending[0] -= 1
            if pending[0] > 0:
                return
            self.refreshing = False
            self.store.set("last_refresh", time.time())
            n = len(fresh_all)
            if n:
                self._notify_new(fresh_all)
                if self.plus:
                    for eid in fresh_all:
                        ep = self.store.episode(eid)
                        if ep and (self.store.podcast(ep["podcast"]) or {}).get("auto_download") and not ep.get("file"):
                            self.download(ep)
            if not quiet or n:
                msg = f"{n} new episode" + ("" if n == 1 else "s") if n else "No new episodes"
                if failures[0]:
                    msg += f" · {failures[0]} show" + (" couldn't" if failures[0] == 1 else "s couldn't") + " be checked"
                self.toast(msg, "done" if not failures[0] else "warning")
            self._changed()

        for p in shows:
            one(p)

    def _notify_new(self, ids):
        if self.is_active():
            return
        eps = [e for e in (self.store.episode(i) for i in ids[:3]) if e]
        if not eps:
            return
        n = Gio.Notification.new(f"{len(ids)} new episode" + ("" if len(ids) == 1 else "s"))
        n.set_body("\n".join(f"{e['show']}: {e['title']}" for e in eps))
        n.set_default_action("app.activate")
        try:
            self.app_ref.send_notification("new-episodes", n)
        except Exception:
            pass

    # ---------------------------------------------------------------- OPML
    def import_opml(self):
        def picked(files):
            if not files:
                return
            f = files[0] if isinstance(files, (list, tuple)) else files
            try:
                ok, data, _etag = f.load_contents(None)
                feeds = opml.read(data)
            except (GLib.Error, ValueError) as ex:
                self.toast(f"Couldn't read that file: {getattr(ex, 'message', ex)}", "error")
                return
            new = [x for x in feeds if not self.store.subscribed(x["url"])]
            if not new:
                self.toast("You already follow every show in that file", "done")
                return
            self.toast(f"Adding {len(new)} show" + ("" if len(new) == 1 else "s") + "…", "added")
            left, added, failed = [len(new)], [0], [0]

            def step(ok):
                left[0] -= 1
                added[0] += ok
                failed[0] += 0 if ok else 1
                if left[0] == 0:
                    msg = f"Now following {added[0]} more show" + ("" if added[0] == 1 else "s")
                    if failed[0]:
                        msg += f" · {failed[0]} couldn't be added"
                    self.toast(msg, "added" if not failed[0] else "warning")
                    self._changed()
            for x in new:
                u = x["url"]

                def got(res, u=u):
                    final = res.get("moved_to") or u
                    if res["show"] is not None and not self.store.subscribed(final):
                        self.store.subscribe(final, res["show"], etag=res.get("etag", ""),
                                             modified=res.get("modified", ""))
                    step(1)
                self.run(lambda u=u: net.fetch_feed(u), got, lambda _m: step(0))
        ask_for_file(self, FileRequest("Import subscriptions from an OPML file", start="downloads"), picked)

    def export_opml(self):
        shows = self.store.podcasts()
        if not shows:
            self.toast("You don't follow any shows yet", "done")
            return
        data = opml.write(shows)

        def done(files):
            if not files:
                return
            f = files[0] if isinstance(files, (list, tuple)) else files
            try:
                f.replace_contents(data, None, False, Gio.FileCreateFlags.REPLACE_DESTINATION, None)
            except GLib.Error as ex:
                self.toast(f"Couldn't save: {ex.message}", "error")
                return
            self.toast(f"Exported {len(shows)} show" + ("" if len(shows) == 1 else "s"), "saved")
        ask_for_file(self, FileRequest("Export subscriptions", mode="save", name="earshot-subscriptions.opml"), done)

    # ---------------------------------------------------------------- closing
    def _closing(self, *_):
        self._save_position()
        if self.player.playing:
            # keep playing with the window closed; the shell's media controls still reach us
            self.set_visible(False)
            return True
        self.player.stop()
        if self.mpris:
            self.mpris.close()
        self.pool.shutdown(wait=False, cancel_futures=True)
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window = None
        self._held = False
        GLib.set_application_name(APP_NAME)

    def do_activate(self):
        if self.window is None:
            self.window = Window(self)
        self.window.set_visible(True)
        self.window.present()


def main() -> int:
    return App().run(sys.argv)

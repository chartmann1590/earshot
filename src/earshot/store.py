# SPDX-License-Identifier: GPL-3.0-or-later
"""Earshot's library: shows, episodes, where you are in each, the Up next queue and settings, in SQLite.

No GTK here. The app uses one Store on the main thread; network work hands parsed feeds back to it.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS podcasts (
  url TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT DEFAULT '', description TEXT DEFAULT '',
  image TEXT DEFAULT '', link TEXT DEFAULT '', added REAL, refreshed REAL DEFAULT 0, etag TEXT DEFAULT '',
  modified TEXT DEFAULT '', speed REAL, error TEXT DEFAULT '', directory_id TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS episodes (
  id TEXT PRIMARY KEY, podcast TEXT NOT NULL REFERENCES podcasts(url) ON DELETE CASCADE, guid TEXT,
  title TEXT, published REAL DEFAULT 0, duration REAL DEFAULT 0, url TEXT, mime TEXT DEFAULT '', size INTEGER DEFAULT 0,
  description TEXT DEFAULT '', image TEXT DEFAULT '', link TEXT DEFAULT '', chapters_url TEXT DEFAULT '',
  chapters TEXT DEFAULT '[]', season INTEGER DEFAULT 0, number INTEGER DEFAULT 0, kind TEXT DEFAULT 'full',
  position REAL DEFAULT 0, played INTEGER DEFAULT 0, played_at REAL DEFAULT 0, new INTEGER DEFAULT 0,
  file TEXT DEFAULT '', listened REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS episodes_podcast ON episodes(podcast, published DESC);
CREATE INDEX IF NOT EXISTS episodes_new ON episodes(new, published DESC);
CREATE TABLE IF NOT EXISTS queue (id TEXT PRIMARY KEY REFERENCES episodes(id) ON DELETE CASCADE, pos REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS listening (day TEXT NOT NULL, podcast TEXT NOT NULL, seconds REAL DEFAULT 0,
  saved REAL DEFAULT 0, PRIMARY KEY (day, podcast));
"""
# columns added after 1.0 (Earshot Plus): ALTER TABLE adds them to existing libraries
MIGRATIONS = (("podcasts", "skip_intro", "INTEGER DEFAULT 0"), ("podcasts", "skip_outro", "INTEGER DEFAULT 0"),
              ("podcasts", "auto_download", "INTEGER DEFAULT 0"))

DEFAULTS = {
    "speed": 1.0,
    "skip_back": 15,
    "skip_forward": 30,
    "autoplay_next": True,
    "remove_played_downloads": True,
    "refresh_minutes": 60,
    "current": "",
    "volume": 1.0,
    "voice_boost": False,
}
PLAYED_MARGIN = 30.0   # within this many seconds of the end counts as played

FEED_FIELDS = ("guid", "title", "published", "duration", "url", "mime", "size", "description", "image", "link",
               "chapters_url", "season", "number", "kind")


def _dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    if "chapters" in d:
        try:
            d["chapters"] = json.loads(d["chapters"] or "[]")
        except ValueError:
            d["chapters"] = []
    return d


class Store:
    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL") if path != ":memory:" else None
        self.db.executescript(SCHEMA)
        for table, column, decl in MIGRATIONS:
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        self.db.commit()

    def close(self):
        self.db.close()

    # ---------------------------------------------------------------- settings
    def get(self, key: str):
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return DEFAULTS.get(key)
        try:
            return json.loads(row[0])
        except ValueError:
            return DEFAULTS.get(key)

    def set(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))
        self.db.commit()

    # ---------------------------------------------------------------- shows
    def podcasts(self) -> list[dict]:
        """Subscribed shows, the one with the newest episode first, each with its count of new episodes."""
        rows = self.db.execute("""
            SELECT p.*, (SELECT COUNT(*) FROM episodes e WHERE e.podcast = p.url AND e.new = 1 AND e.played = 0) AS fresh,
                   (SELECT MAX(published) FROM episodes e WHERE e.podcast = p.url) AS latest,
                   (SELECT COUNT(*) FROM episodes e WHERE e.podcast = p.url) AS total
            FROM podcasts p ORDER BY latest DESC, p.title COLLATE NOCASE""").fetchall()
        return [dict(r) for r in rows]

    def podcast(self, url: str) -> dict | None:
        return _dict(self.db.execute("SELECT * FROM podcasts WHERE url = ?", (url,)).fetchone())

    def subscribed(self, url: str) -> bool:
        return self.db.execute("SELECT 1 FROM podcasts WHERE url = ?", (url,)).fetchone() is not None

    def subscribe(self, url: str, show: dict, *, etag: str = "", modified: str = "", directory_id: str = "",
                  fresh: int = 1) -> int:
        """Add a show and its episodes; its newest `fresh` episodes land in New episodes. Returns that count."""
        now = time.time()
        self.db.execute("""INSERT OR IGNORE INTO podcasts (url, title, author, description, image, link, added, refreshed,
                           etag, modified, directory_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (url, show["title"], show.get("author", ""), show.get("description", ""), show.get("image", ""),
                         show.get("link", ""), now, now, etag, modified, directory_id))
        self._merge(url, show["episodes"], mark_new=False)
        newest = [e["id"] for e in show["episodes"][:max(0, fresh)]]
        self.db.executemany("UPDATE episodes SET new = 1 WHERE id = ? AND played = 0", [(i,) for i in newest])
        self.db.commit()
        return len(newest)

    def unsubscribe(self, url: str) -> list[str]:
        """Remove a show and its episodes. Returns the downloaded files the caller should delete."""
        files = [r[0] for r in self.db.execute("SELECT file FROM episodes WHERE podcast = ? AND file != ''", (url,))]
        self.db.execute("DELETE FROM podcasts WHERE url = ?", (url,))
        self.db.commit()
        return files

    def refreshed(self, url: str, show: dict | None, *, etag: str = "", modified: str = "", error: str = "") -> list[str]:
        """Merge a refreshed feed (None: unchanged, 304). Returns the ids of episodes that are new to us."""
        if not self.subscribed(url):
            return []
        fresh: list[str] = []
        if show is not None:
            self.db.execute("""UPDATE podcasts SET title = ?, author = ?, description = ?, image = ?, link = ?
                               WHERE url = ?""", (show["title"], show.get("author", ""), show.get("description", ""),
                                                  show.get("image", ""), show.get("link", ""), url))
            fresh = self._merge(url, show["episodes"], mark_new=True)
        self.db.execute("UPDATE podcasts SET refreshed = ?, error = ?, etag = COALESCE(NULLIF(?, ''), etag),"
                        " modified = COALESCE(NULLIF(?, ''), modified) WHERE url = ?",
                        (time.time(), error, etag, modified, url))
        self.db.commit()
        return fresh

    def _merge(self, url: str, episodes: list[dict], *, mark_new: bool) -> list[str]:
        known = {r[0] for r in self.db.execute("SELECT id FROM episodes WHERE podcast = ?", (url,))}
        # Only episodes at least as recent as what we had are "new": a show re-publishing its back catalogue
        # must not flood New episodes. (A day of slack for feeds with sloppy dates.)
        newest = self.db.execute("SELECT MAX(published) FROM episodes WHERE podcast = ?", (url,)).fetchone()[0] or 0
        fresh = []
        for e in episodes:
            values = [e.get(f, "" if f in ("guid", "title", "url", "mime", "description", "image", "link",
                                          "chapters_url", "kind") else 0) for f in FEED_FIELDS]
            if e["id"] in known:
                sets = ", ".join(f"{f} = ?" for f in FEED_FIELDS)
                self.db.execute(f"UPDATE episodes SET {sets} WHERE id = ?", values + [e["id"]])
                if e.get("chapters"):
                    self.db.execute("UPDATE episodes SET chapters = ? WHERE id = ? AND chapters = '[]'",
                                    (json.dumps(e["chapters"]), e["id"]))
            else:
                is_new = mark_new and (e.get("published") or 0) >= newest - 86400
                self.db.execute(f"INSERT OR IGNORE INTO episodes (id, podcast, {', '.join(FEED_FIELDS)}, chapters, new) "
                                f"VALUES (?, ?, {', '.join('?' * len(FEED_FIELDS))}, ?, ?)",
                                [e["id"], url] + values + [json.dumps(e.get("chapters") or []), 1 if is_new else 0])
                if is_new:
                    fresh.append(e["id"])
        return fresh

    def set_speed(self, url: str, speed: float | None) -> None:
        self.db.execute("UPDATE podcasts SET speed = ? WHERE url = ?", (speed, url))
        self.db.commit()

    def set_show_option(self, url: str, option: str, value: int) -> None:
        """Earshot Plus per-show options: skip_intro, skip_outro (seconds) and auto_download (0/1)."""
        if option not in ("skip_intro", "skip_outro", "auto_download"):
            raise ValueError(option)
        self.db.execute(f"UPDATE podcasts SET {option} = ? WHERE url = ?", (int(value), url))
        self.db.commit()

    # ---------------------------------------------------------------- listening stats (Earshot Plus)
    def add_listening(self, podcast: str, seconds: float, saved: float, day: str | None = None) -> None:
        """Count `seconds` of wall-clock listening to `podcast` today, of which speed saved `saved`."""
        if seconds <= 0:
            return
        day = day or time.strftime("%Y-%m-%d")
        self.db.execute("""INSERT INTO listening (day, podcast, seconds, saved) VALUES (?, ?, ?, ?)
                           ON CONFLICT(day, podcast) DO UPDATE SET seconds = seconds + excluded.seconds,
                           saved = saved + excluded.saved""", (day, podcast, seconds, max(0.0, saved)))
        self.db.commit()

    def stats(self, since: str = "0000-00-00") -> dict:
        """Totals since a day (YYYY-MM-DD): seconds, saved, finished episodes, and shows by time."""
        tot = self.db.execute("SELECT COALESCE(SUM(seconds), 0), COALESCE(SUM(saved), 0) FROM listening WHERE day >= ?",
                              (since,)).fetchone()
        since_t = time.mktime(time.strptime(since, "%Y-%m-%d")) if since > "1970" else 0
        finished = self.db.execute("SELECT COUNT(*) FROM episodes WHERE played = 1 AND played_at >= ?",
                                   (since_t,)).fetchone()[0]
        shows = self.db.execute("""SELECT l.podcast, COALESCE(p.title, l.podcast) AS title, p.image AS image,
                                   SUM(l.seconds) AS seconds FROM listening l LEFT JOIN podcasts p ON p.url = l.podcast
                                   WHERE l.day >= ? GROUP BY l.podcast ORDER BY seconds DESC LIMIT 10""",
                                (since,)).fetchall()
        days = self.db.execute("SELECT day, SUM(seconds) FROM listening WHERE day >= ? GROUP BY day ORDER BY day",
                               (since,)).fetchall()
        return {"seconds": tot[0], "saved": tot[1], "finished": finished,
                "shows": [dict(r) for r in shows], "days": [(d, s) for d, s in days]}

    def move_feed(self, old: str, new: str) -> bool:
        """A show moved (itunes:new-feed-url or a permanent redirect): keep its episodes and history."""
        if old == new or not self.subscribed(old) or self.subscribed(new):
            return False
        self.db.execute("PRAGMA foreign_keys = OFF")
        self.db.execute("UPDATE podcasts SET url = ? WHERE url = ?", (new, old))
        self.db.execute("UPDATE episodes SET podcast = ? WHERE podcast = ?", (new, old))
        self.db.commit()
        self.db.execute("PRAGMA foreign_keys = ON")
        return True

    # ---------------------------------------------------------------- episodes
    _JOIN = ("SELECT e.*, p.title AS show, p.image AS show_image, p.author AS author "
             "FROM episodes e JOIN podcasts p ON p.url = e.podcast")

    def episode(self, eid: str) -> dict | None:
        return _dict(self.db.execute(f"{self._JOIN} WHERE e.id = ?", (eid,)).fetchone())

    def episodes(self, podcast: str, *, limit: int = 500, offset: int = 0) -> list[dict]:
        rows = self.db.execute(f"{self._JOIN} WHERE e.podcast = ? ORDER BY e.published DESC LIMIT ? OFFSET ?",
                               (podcast, limit, offset)).fetchall()
        return [_dict(r) for r in rows]

    def counts_for(self, podcast: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM episodes WHERE podcast = ?", (podcast,)).fetchone()[0]

    def fresh(self, limit: int = 300) -> list[dict]:
        """New episodes: arrived since you subscribed, not played or dismissed."""
        rows = self.db.execute(f"{self._JOIN} WHERE e.new = 1 AND e.played = 0 ORDER BY e.published DESC LIMIT ?",
                               (limit,)).fetchall()
        return [_dict(r) for r in rows]

    def in_progress(self, limit: int = 20) -> list[dict]:
        rows = self.db.execute(f"{self._JOIN} WHERE e.position > 0 AND e.played = 0 ORDER BY e.listened DESC LIMIT ?",
                               (limit,)).fetchall()
        return [_dict(r) for r in rows]

    def downloaded(self) -> list[dict]:
        rows = self.db.execute(f"{self._JOIN} WHERE e.file != '' ORDER BY e.published DESC").fetchall()
        return [_dict(r) for r in rows]

    def history(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute(f"{self._JOIN} WHERE e.played = 1 ORDER BY e.played_at DESC LIMIT ?", (limit,)).fetchall()
        return [_dict(r) for r in rows]

    def search(self, text: str, limit: int = 100) -> list[dict]:
        like = f"%{text.strip()}%"
        rows = self.db.execute(f"{self._JOIN} WHERE e.title LIKE ? OR p.title LIKE ? ORDER BY e.published DESC LIMIT ?",
                               (like, like, limit)).fetchall()
        return [_dict(r) for r in rows]

    def set_position(self, eid: str, seconds: float, duration: float | None = None) -> bool:
        """Remember where you are. Returns True when that's close enough to the end to count as played."""
        ep = self.episode(eid)
        if ep is None:
            return False
        if duration and duration > 0 and abs(duration - (ep["duration"] or 0)) > 1:
            self.db.execute("UPDATE episodes SET duration = ? WHERE id = ?", (duration, eid))
            ep["duration"] = duration
        length = ep["duration"] or 0
        done = length > 0 and seconds >= length - min(PLAYED_MARGIN, length * 0.05 + 5)
        self.db.execute("UPDATE episodes SET position = ?, listened = ?, new = CASE WHEN ? > 0 THEN 0 ELSE new END "
                        "WHERE id = ?", (max(0.0, seconds), time.time(), seconds, eid))
        self.db.commit()
        return done

    def mark_played(self, eid: str, played: bool = True) -> None:
        if played:
            self.db.execute("UPDATE episodes SET played = 1, played_at = ?, position = 0, new = 0 WHERE id = ?",
                            (time.time(), eid))
            self.db.execute("DELETE FROM queue WHERE id = ?", (eid,))
        else:
            self.db.execute("UPDATE episodes SET played = 0, played_at = 0 WHERE id = ?", (eid,))
        self.db.commit()

    def dismiss(self, eid: str) -> None:
        """Take an episode out of New episodes without marking it played."""
        self.db.execute("UPDATE episodes SET new = 0 WHERE id = ?", (eid,))
        self.db.commit()

    def dismiss_all(self) -> int:
        n = self.db.execute("UPDATE episodes SET new = 0 WHERE new = 1").rowcount
        self.db.commit()
        return n

    def set_file(self, eid: str, path: str) -> None:
        self.db.execute("UPDATE episodes SET file = ? WHERE id = ?", (path, eid))
        self.db.commit()

    def set_chapters(self, eid: str, chapters: list[dict]) -> None:
        self.db.execute("UPDATE episodes SET chapters = ? WHERE id = ?", (json.dumps(chapters), eid))
        self.db.commit()

    # ---------------------------------------------------------------- Up next
    def queue(self) -> list[dict]:
        rows = self.db.execute(f"{self._JOIN} JOIN queue q ON q.id = e.id ORDER BY q.pos").fetchall()
        return [_dict(r) for r in rows]

    def queued(self, eid: str) -> bool:
        return self.db.execute("SELECT 1 FROM queue WHERE id = ?", (eid,)).fetchone() is not None

    def enqueue(self, eid: str, *, front: bool = False) -> None:
        """Add to Up next: at the end, or first (`front`). An episode already queued moves."""
        agg = "MIN" if front else "MAX"
        edge = self.db.execute(f"SELECT {agg}(pos) FROM queue WHERE id != ?", (eid,)).fetchone()[0]
        pos = (edge - 1 if front else edge + 1) if edge is not None else 0
        self.db.execute("INSERT OR REPLACE INTO queue VALUES (?, ?)", (eid, pos))
        self.db.commit()

    def dequeue(self, eid: str) -> None:
        self.db.execute("DELETE FROM queue WHERE id = ?", (eid,))
        self.db.commit()

    def move(self, eid: str, index: int) -> None:
        """Put a queued episode at `index` in Up next."""
        ids = [e["id"] for e in self.queue() if e["id"] != eid]
        index = max(0, min(index, len(ids)))
        ids.insert(index, eid)
        self.db.executemany("UPDATE queue SET pos = ? WHERE id = ?", [(i, x) for i, x in enumerate(ids)])
        self.db.commit()

    def clear_queue(self) -> None:
        self.db.execute("DELETE FROM queue")
        self.db.commit()

    def next_in_queue(self, after: str | None = None) -> dict | None:
        q = [e for e in self.queue() if e["id"] != after and not e["played"]]
        return q[0] if q else None

    # ---------------------------------------------------------------- counts
    def counts(self) -> dict:
        one = lambda sql: self.db.execute(sql).fetchone()[0]
        return {"fresh": one("SELECT COUNT(*) FROM episodes WHERE new = 1 AND played = 0"),
                "queue": one("SELECT COUNT(*) FROM queue"),
                "downloads": one("SELECT COUNT(*) FROM episodes WHERE file != ''"),
                "shows": one("SELECT COUNT(*) FROM podcasts"),
                "progress": one("SELECT COUNT(*) FROM episodes WHERE position > 0 AND played = 0")}

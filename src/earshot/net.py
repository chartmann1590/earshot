# SPDX-License-Identifier: GPL-3.0-or-later
"""Everything that touches the network, as plain blocking functions the app runs on worker threads.

TLS certificates are always checked (urllib's default context). Every read is bounded. The only
services contacted are the feeds you follow, the files they point to, and Apple's public podcast
directory for search and charts (nothing about you is sent beyond the search words and your region).
"""
from __future__ import annotations

import hashlib
import json
import os
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request

from . import feeds

USER_AGENT = "Earshot/1.0 (Luma podcast app; +https://github.com/chartmann1590/earshot)"
TIMEOUT = 25
FEED_LIMIT = 25 * 1024 * 1024
JSON_LIMIT = 5 * 1024 * 1024
IMAGE_LIMIT = 8 * 1024 * 1024
_CTX = ssl.create_default_context()


class NetError(Exception):
    """A network failure; the message is safe to show."""


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        super().__init__()
        self.permanent = None

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and code in (301, 308) and self.permanent is not False:
            self.permanent = new.full_url
        elif new is not None:
            self.permanent = False  # a temporary hop anywhere in the chain: keep the address we have
        return new


def _open(url: str, headers: dict | None = None, timeout: float = TIMEOUT):
    if not url.lower().startswith(("https://", "http://")):
        raise NetError("Only web addresses can be opened.")
    redirects = _Redirects()
    opener = urllib.request.build_opener(redirects, urllib.request.HTTPSHandler(context=_CTX))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity", **(headers or {})})
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as ex:
        if ex.code == 304:
            return None, redirects
        raise NetError(_http_message(ex.code)) from None
    except urllib.error.URLError as ex:
        reason = getattr(ex, "reason", ex)
        if isinstance(reason, ssl.SSLError):
            raise NetError("The site's security certificate couldn't be checked.") from None
        raise NetError("Couldn't reach the site. Check your connection.") from None
    except (TimeoutError, OSError):
        raise NetError("The site took too long to answer.") from None
    return resp, redirects


def _http_message(code: int) -> str:
    return {404: "The feed isn't there any more (404).", 410: "The show has been taken down (410).",
            401: "That feed needs a password.", 403: "The site refused (403)."}.get(
        code, f"The site answered with an error ({code}).")


def _read(resp, limit: int) -> bytes:
    data = resp.read(limit + 1)
    if len(data) > limit:
        raise NetError("That file is too large.")
    return data


def get_bytes(url: str, limit: int = JSON_LIMIT, headers: dict | None = None) -> bytes:
    resp, _ = _open(url, headers)
    if resp is None:
        return b""
    with resp:
        return _read(resp, limit)


def fetch_feed(url: str, etag: str = "", modified: str = "") -> dict:
    """Fetch and parse a feed. {"show": dict | None (None: unchanged), "etag", "modified", "moved_to"}.

    A web page that advertises a feed is followed to it, so a show's home page works as its address.
    """
    headers = {"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"}
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified
    resp, redirects = _open(url, headers)
    if resp is None:
        return {"show": None, "etag": etag, "modified": modified, "moved_to": ""}
    with resp:
        data = _read(resp, FEED_LIMIT)
        final = resp.geturl()
        info = resp.headers
    moved = redirects.permanent if redirects.permanent and redirects.permanent != url else ""
    try:
        show = feeds.parse(data, url)
    except feeds.FeedError as err:
        link = feeds.discover_feed_link(data, final)
        if link and link != url:
            found = fetch_feed(link)
            found["moved_to"] = link
            return found
        raise NetError(str(err)) from None
    if show.get("new_url") and show["new_url"] != url:
        moved = show["new_url"]
    return {"show": show, "etag": info.get("ETag", "") or "", "modified": info.get("Last-Modified", "") or "",
            "moved_to": moved}


def fetch_chapters(url: str) -> list[dict]:
    try:
        return feeds.parse_chapters_json(get_bytes(url, JSON_LIMIT, {"Accept": "application/json+chapters, application/json"}))
    except NetError:
        return []


# ---------------------------------------------------------------- directory (Apple Podcasts' public API)
GENRES = (("", "Top shows"), ("1489", "News"), ("1303", "Comedy"), ("1488", "True crime"), ("1318", "Technology"),
          ("1321", "Business"), ("1545", "Sports"), ("1324", "Society & culture"), ("1512", "Health & fitness"),
          ("1304", "Education"), ("1487", "History"), ("1533", "Science"), ("1305", "Kids & family"),
          ("1309", "TV & film"), ("1310", "Music"), ("1301", "Arts"))


def region() -> str:
    """Two-letter store region from the locale (en_GB → gb); us when unknown."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "")
        if "_" in value:
            code = value.split("_", 1)[1][:2].lower()
            if code.isalpha() and len(code) == 2:
                return code
    return "us"


def _result(r: dict) -> dict | None:
    feed = r.get("feedUrl") or ""
    if not feed.lower().startswith(("http://", "https://")):
        return None
    return {"title": r.get("collectionName") or r.get("trackName") or "", "author": r.get("artistName") or "",
            "feed": feed, "image": r.get("artworkUrl600") or r.get("artworkUrl100") or "",
            "genre": r.get("primaryGenreName") or "", "count": int(r.get("trackCount") or 0),
            "directory_id": str(r.get("collectionId") or "")}


def search(term: str, limit: int = 40) -> list[dict]:
    q = urllib.parse.urlencode({"media": "podcast", "entity": "podcast", "term": term.strip(), "limit": limit,
                                "country": region()})
    data = json.loads(get_bytes(f"https://itunes.apple.com/search?{q}").decode("utf-8", "replace") or "{}")
    return [x for x in (_result(r) for r in data.get("results", [])) if x]


def top(genre: str = "", limit: int = 50) -> list[dict]:
    """A region's chart, optionally one genre's. Charts don't carry feed addresses, so they're looked up."""
    g = f"/genre={genre}" if genre else ""
    url = f"https://itunes.apple.com/{region()}/rss/toppodcasts/limit={limit}{g}/json"
    data = json.loads(get_bytes(url).decode("utf-8", "replace") or "{}")
    entries = data.get("feed", {}).get("entry", [])
    if isinstance(entries, dict):
        entries = [entries]
    ids = [e.get("id", {}).get("attributes", {}).get("im:id") for e in entries]
    ids = [i for i in ids if i and i.isdigit()]
    found: dict[str, dict] = {}
    for start in range(0, len(ids), 50):
        chunk = ",".join(ids[start:start + 50])
        look = json.loads(get_bytes(f"https://itunes.apple.com/lookup?id={chunk}&entity=podcast&country={region()}")
                          .decode("utf-8", "replace") or "{}")
        for r in look.get("results", []):
            x = _result(r)
            if x:
                found[x["directory_id"]] = x
    return [found[i] for i in ids if i in found]


# ---------------------------------------------------------------- artwork
def cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "earshot")


def artwork_path(url: str) -> str:
    return os.path.join(cache_dir(), "art", hashlib.sha1(url.encode()).hexdigest()[:24] + ".img")


def fetch_artwork(url: str) -> str:
    """Download a picture once into the cache; returns its path, or "" when it can't be had."""
    if not url:
        return ""
    path = artwork_path(url)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    try:
        data = get_bytes(url, IMAGE_LIMIT, {"Accept": "image/*"})
    except NetError:
        return ""
    if not data or not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or data[:4] == b"RIFF"
                        or data[:6] in (b"GIF87a", b"GIF89a")):
        return ""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------- downloads
EXT_BY_MIME = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
               "audio/ogg": ".ogg", "audio/opus": ".opus", "video/mp4": ".mp4", "audio/x-m4b": ".m4b", "audio/flac": ".flac",
               "audio/wav": ".wav", "audio/webm": ".webm"}


def download_dir() -> str:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "earshot", "episodes")


def download_target(episode: dict) -> str:
    ext = EXT_BY_MIME.get((episode.get("mime") or "").split(";")[0].strip())
    if not ext:
        path = urllib.parse.urlsplit(episode["url"]).path.lower()
        ext = next((e for e in feeds.AUDIO_EXT if path.endswith(e)), ".mp3")
    return os.path.join(download_dir(), episode["id"] + ext)


def download(episode: dict, progress, cancel: threading.Event) -> str:
    """Download an episode's audio. `progress(done, total)` is called as it goes (from this thread).
    Returns the file's path; raises NetError, or returns "" when cancelled."""
    target = download_target(episode)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".part"
    resp, _ = _open(episode["url"], timeout=60)
    if resp is None:
        raise NetError("The file couldn't be downloaded.")
    try:
        total = int(resp.headers.get("Content-Length") or 0) or int(episode.get("size") or 0)
        done = 0
        with open(tmp, "wb") as f:
            while True:
                if cancel.is_set():
                    break
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                progress(done, total)
    except (OSError, TimeoutError) as ex:
        _unlink(tmp)
        if isinstance(ex, OSError) and ex.errno == 28:
            raise NetError("There isn't enough space to download it.") from None
        raise NetError("The download stopped. Check your connection.") from None
    finally:
        resp.close()
    if cancel.is_set():
        _unlink(tmp)
        return ""
    if done == 0:
        _unlink(tmp)
        raise NetError("The file was empty.")
    os.replace(tmp, target)
    return target


def _unlink(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass

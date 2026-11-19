# SPDX-License-Identifier: GPL-3.0-or-later
"""Podcast feeds: RSS 2.0 (with iTunes, Podcasting 2.0 and Podlove tags) and Atom, read into plain dicts.

No GTK here, so it is tested anywhere. Feeds come from strangers: everything is optional, sizes are bounded,
and a broken item is skipped instead of failing the whole feed.
"""
from __future__ import annotations

import email.utils
import hashlib
import json
import re
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "podcast": "https://podcastindex.org/namespace/1.0",
    "psc": "http://podlove.org/simple-chapters",
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "googleplay": "http://www.google.com/schemas/play-podcasts/1.0",
}
AUDIO_EXT = (".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".mp4", ".m4b", ".wav", ".flac", ".webm", ".m4v", ".mov")
MAX_TEXT = 200_000      # show notes longer than this are cut
MAX_EPISODES = 3000


class FeedError(Exception):
    """A feed that can't be read; the message is safe to show."""


def episode_id(feed_url: str, guid: str) -> str:
    return hashlib.sha1(f"{feed_url}\n{guid}".encode()).hexdigest()[:20]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(el, path: str, default: str = "") -> str:
    if el is None:
        return default
    found = el.find(path, NS)
    if found is None:
        return default
    return "".join(found.itertext()).strip() or default


def _attr(el, path: str, name: str, default: str = "") -> str:
    found = el.find(path, NS) if el is not None else None
    return (found.get(name) or default).strip() if found is not None else default


def parse_duration(value: str | None) -> float:
    """`itunes:duration` is seconds ("3600", "3600.5") or a clock ("1:02:03", "62:03")."""
    if not value:
        return 0.0
    value = value.strip()
    try:
        if ":" in value:
            total = 0.0
            for part in value.split(":"):
                total = total * 60 + float(part or 0)
            return max(0.0, total)
        return max(0.0, float(value))
    except ValueError:
        return 0.0


def parse_date(value: str | None) -> float:
    """RFC 822 (RSS) or ISO 8601 (Atom) to a Unix time; 0 when unreadable."""
    if not value:
        return 0.0
    value = value.strip()
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_clock(value: str) -> float | None:
    """A chapter start: "01:02:03.500", "62:03" or "75.5"."""
    try:
        return parse_duration(value) if value else None
    except Exception:
        return None


def _is_audio(url: str, mime: str) -> bool:
    if mime.startswith(("audio/", "video/")):
        return True
    path = url.split("?", 1)[0].lower()
    return path.endswith(AUDIO_EXT)


def _image(el) -> str:
    return (_attr(el, "itunes:image", "href") or _text(el, "image/url") or _attr(el, "media:thumbnail", "url")
            or _attr(el, "googleplay:image", "href"))


def _clean_url(url: str) -> str:
    url = (url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else ""


def parse(data: bytes, feed_url: str) -> dict:
    """Read a feed into {"title", "author", "description", "image", "link", "episodes": [...]}, newest first."""
    if not data or not data.strip():
        raise FeedError("The feed is empty.")
    head = data[:2048].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        raise FeedError("That address is a web page, not a podcast feed.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as ex:
        raise FeedError(f"The feed isn't valid XML ({ex}).") from None
    tag = _local(root.tag)
    if tag == "rss" or tag == "RDF":
        channel = root.find("channel")
        if channel is None:
            raise FeedError("The feed has no channel.")
        return _rss(channel, feed_url)
    if tag == "feed":
        return _atom(root, feed_url)
    raise FeedError("That address isn't a podcast feed.")


def _rss(ch, feed_url: str) -> dict:
    show = {
        "title": _text(ch, "title") or feed_url,
        "author": _text(ch, "itunes:author") or _text(ch, "managingEditor") or _text(ch, "googleplay:author"),
        "description": (_text(ch, "itunes:summary") or _text(ch, "description"))[:MAX_TEXT],
        "image": _clean_url(_image(ch)),
        "link": _clean_url(_text(ch, "link")),
        "new_url": _clean_url(_text(ch, "itunes:new-feed-url")),
        "episodes": [],
    }
    seen = set()
    for item in ch.iter("item"):
        if len(show["episodes"]) >= MAX_EPISODES:
            break
        try:
            ep = _rss_item(item, feed_url, show["image"])
        except Exception:  # one odd item never loses the rest of the feed
            continue
        if ep and ep["id"] not in seen:
            seen.add(ep["id"])
            show["episodes"].append(ep)
    show["episodes"].sort(key=lambda e: e["published"], reverse=True)
    return show


def _rss_item(item, feed_url: str, show_image: str) -> dict | None:
    enc = item.find("enclosure")
    url, mime, size = "", "", 0
    if enc is not None:
        url, mime = _clean_url(enc.get("url", "")), (enc.get("type") or "").lower()
        try:
            size = int(enc.get("length") or 0)
        except ValueError:
            size = 0
    if not url:
        url = _clean_url(_attr(item, "media:content", "url"))
        mime = mime or _attr(item, "media:content", "type").lower()
    if not url or not _is_audio(url, mime):
        return None
    guid = _text(item, "guid") or url
    notes = _text(item, "content:encoded") or _text(item, "description") or _text(item, "itunes:summary")
    chapters = []
    psc = item.find("psc:chapters", NS)
    if psc is not None:
        for c in psc.findall("psc:chapter", NS):
            start = parse_clock(c.get("start", ""))
            if start is not None:
                chapters.append({"start": start, "title": (c.get("title") or "").strip()})
    def num(path):
        try:
            return int(_text(item, path) or 0)
        except ValueError:
            return 0
    return {
        "id": episode_id(feed_url, guid),
        "guid": guid,
        "title": _text(item, "title") or _text(item, "itunes:title") or "Untitled episode",
        "published": parse_date(_text(item, "pubDate") or _text(item, "{http://purl.org/dc/elements/1.1/}date")),
        "duration": parse_duration(_text(item, "itunes:duration")),
        "url": url,
        "mime": mime,
        "size": max(0, size),
        "description": notes[:MAX_TEXT],
        "image": _clean_url(_attr(item, "itunes:image", "href")) or show_image,
        "link": _clean_url(_text(item, "link")),
        "chapters_url": _clean_url(_attr(item, "podcast:chapters", "url")),
        "chapters": chapters,
        "season": num("itunes:season"),
        "number": num("itunes:episode"),
        "kind": (_text(item, "itunes:episodeType") or "full").lower(),
    }


def _atom(feed, feed_url: str) -> dict:
    a = NS["atom"]
    def t(el, name):
        f = el.find(f"{{{a}}}{name}")
        return "".join(f.itertext()).strip() if f is not None else ""
    def link(el, rel):
        for l in el.findall(f"{{{a}}}link"):
            if (l.get("rel") or "alternate") == rel:
                return l
        return None
    alt = link(feed, "alternate")
    author = feed.find(f"{{{a}}}author")
    show = {"title": t(feed, "title") or feed_url, "author": t(author, "name") if author is not None else "",
            "description": t(feed, "subtitle")[:MAX_TEXT], "image": _clean_url(t(feed, "logo") or t(feed, "icon") or _image(feed)),
            "link": _clean_url(alt.get("href", "") if alt is not None else ""), "new_url": "", "episodes": []}
    for entry in feed.findall(f"{{{a}}}entry")[:MAX_EPISODES]:
        enc = link(entry, "enclosure")
        if enc is None:
            continue
        url, mime = _clean_url(enc.get("href", "")), (enc.get("type") or "").lower()
        if not url or not _is_audio(url, mime):
            continue
        guid = t(entry, "id") or url
        try:
            size = int(enc.get("length") or 0)
        except ValueError:
            size = 0
        alt = link(entry, "alternate")
        show["episodes"].append({
            "id": episode_id(feed_url, guid), "guid": guid, "title": t(entry, "title") or "Untitled episode",
            "published": parse_date(t(entry, "published") or t(entry, "updated")),
            "duration": parse_duration(_text(entry, "itunes:duration")), "url": url, "mime": mime, "size": size,
            "description": (t(entry, "content") or t(entry, "summary"))[:MAX_TEXT], "image": show["image"],
            "link": _clean_url(alt.get("href", "") if alt is not None else ""), "chapters_url": "", "chapters": [],
            "season": 0, "number": 0, "kind": "full"})
    show["episodes"].sort(key=lambda e: e["published"], reverse=True)
    return show


def parse_chapters_json(data: bytes) -> list[dict]:
    """Podcasting 2.0 JSON chapters: {"chapters": [{"startTime": 0, "title": "…"}]}; hidden ones are skipped."""
    try:
        doc = json.loads(data.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return []
    out = []
    for c in (doc.get("chapters") or []) if isinstance(doc, dict) else []:
        if not isinstance(c, dict) or c.get("toc") is False:
            continue
        try:
            start = float(c.get("startTime", 0))
        except (TypeError, ValueError):
            continue
        out.append({"start": max(0.0, start), "title": str(c.get("title") or "").strip()[:300]})
    out.sort(key=lambda c: c["start"])
    return out


FEED_LINK = re.compile(rb'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', re.I)
HREF = re.compile(rb'href=["\']([^"\']+)["\']', re.I)


def discover_feed_link(html: bytes, base: str) -> str:
    """A web page's advertised feed (<link rel="alternate" type="application/rss+xml">), or ""."""
    from urllib.parse import urljoin
    for tag in FEED_LINK.findall(html[:500_000]):
        m = HREF.search(tag)
        if m:
            return _clean_url(urljoin(base, m.group(1).decode("utf-8", "replace")))
    return ""

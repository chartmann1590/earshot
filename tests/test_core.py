# SPDX-License-Identifier: GPL-3.0-or-later
"""Feeds, the library, OPML and show notes: no GTK, no network."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from earshot import feeds, notes, opml  # noqa: E402
from earshot.store import Store  # noqa: E402

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:podcast="https://podcastindex.org/namespace/1.0"
     xmlns:psc="http://podlove.org/simple-chapters">
<channel>
  <title>Night Shift</title><link>https://example.com</link>
  <itunes:author>Ada &amp; Co</itunes:author><description>Stories after dark.</description>
  <itunes:image href="https://example.com/cover.jpg"/>
  <item>
    <title>Two</title><guid>ep-2</guid><pubDate>Tue, 10 Nov 2026 08:00:00 GMT</pubDate>
    <enclosure url="https://cdn.example.com/2.mp3" type="audio/mpeg" length="1234"/>
    <itunes:duration>1:02:03</itunes:duration>
    <content:encoded><![CDATA[<p>Intro at <b>0:30</b>, see <a href="https://example.com/x">this</a>.</p><script>bad()</script>]]></content:encoded>
    <podcast:chapters url="https://example.com/2.json" type="application/json+chapters"/>
    <psc:chapters version="1.2"><psc:chapter start="00:00:00" title="Hello"/><psc:chapter start="00:10:00.5" title="Main"/></psc:chapters>
  </item>
  <item>
    <title>One</title><guid>ep-1</guid><pubDate>Mon, 02 Nov 2026 08:00:00 +0000</pubDate>
    <enclosure url="https://cdn.example.com/1.m4a" type="audio/x-m4a"/>
    <itunes:duration>3600</itunes:duration><description>Plain notes</description>
  </item>
  <item><title>Not audio</title><guid>blog</guid><enclosure url="https://example.com/a.pdf" type="application/pdf"/></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Show</title>
<author><name>Bea</name></author>
<entry><id>a1</id><title>First</title><published>2026-11-01T10:00:00Z</published>
<link rel="enclosure" href="https://example.com/a1.ogg" type="audio/ogg" length="9"/></entry></feed>"""


def test_parse_rss():
    show = feeds.parse(RSS, "https://example.com/feed")
    assert show["title"] == "Night Shift" and show["author"] == "Ada & Co"
    assert show["image"] == "https://example.com/cover.jpg"
    assert [e["title"] for e in show["episodes"]] == ["Two", "One"]   # newest first, the PDF skipped
    two = show["episodes"][0]
    assert two["duration"] == 3723 and two["size"] == 1234 and two["mime"] == "audio/mpeg"
    assert two["chapters_url"] == "https://example.com/2.json"
    assert two["chapters"] == [{"start": 0.0, "title": "Hello"}, {"start": 600.5, "title": "Main"}]
    assert two["image"] == show["image"]
    assert two["id"] == feeds.episode_id("https://example.com/feed", "ep-2")


def test_parse_atom_and_errors():
    show = feeds.parse(ATOM, "https://e.com/atom")
    assert show["title"] == "Atom Show" and show["author"] == "Bea" and len(show["episodes"]) == 1
    for bad, word in ((b"", "empty"), (b"<!DOCTYPE html><html></html>", "web page"), (b"<rss><chan", "valid XML")):
        try:
            feeds.parse(bad, "u")
        except feeds.FeedError as ex:
            assert word in str(ex), ex
        else:
            raise AssertionError("expected FeedError")
    html = b'<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head></html>'
    assert feeds.discover_feed_link(html, "https://site.com/show/") == "https://site.com/feed.xml"


def test_durations_and_chapters():
    assert feeds.parse_duration("62:03") == 3723 and feeds.parse_duration("90.5") == 90.5
    assert feeds.parse_duration("abc") == 0 and feeds.parse_duration(None) == 0
    assert feeds.parse_date("2026-11-01T10:00:00Z") > 0 and feeds.parse_date("nonsense") == 0
    ch = feeds.parse_chapters_json(b'{"chapters":[{"startTime":90,"title":"B"},{"startTime":0,"title":"A"},'
                                   b'{"startTime":5,"title":"hidden","toc":false}]}')
    assert ch == [{"start": 0.0, "title": "A"}, {"start": 90.0, "title": "B"}]
    assert feeds.parse_chapters_json(b"not json") == []


def test_store_flow():
    s = Store()
    url = "https://example.com/feed"
    show = feeds.parse(RSS, url)
    assert s.subscribe(url, show) == 1
    assert [e["title"] for e in s.fresh()] == ["Two"]
    assert s.counts()["shows"] == 1 and s.podcasts()[0]["fresh"] == 1
    two, one = show["episodes"]
    # position, then near the end counts as played
    assert s.set_position(two["id"], 100) is False and s.in_progress()[0]["id"] == two["id"]
    assert s.fresh() == []                            # started: no longer new
    assert s.set_position(two["id"], 3723 - 10) is True
    s.mark_played(two["id"])
    assert s.episode(two["id"])["played"] == 1 and s.in_progress() == []
    # Up next
    s.enqueue(one["id"])
    s.enqueue(two["id"], front=True)
    assert [e["title"] for e in s.queue()] == ["Two", "One"]
    s.move(two["id"], 5)
    assert [e["title"] for e in s.queue()] == ["One", "Two"]
    assert s.next_in_queue(after=one["id"]) is None   # Two is played
    s.mark_played(one["id"])
    assert s.queue() == [e for e in s.queue() if e["id"] == two["id"]]
    # a refresh brings a newer episode in as new; an old back-catalogue one is not new
    newer = dict(two, id="n3", guid="ep-3", title="Three", published=two["published"] + 86400 * 7)
    older = dict(two, id="n0", guid="ep-0", title="Zero", published=two["published"] - 86400 * 400)
    fresh = s.refreshed(url, dict(show, episodes=[newer, older] + show["episodes"]), etag="x")
    assert fresh == ["n3"] and s.podcast(url)["etag"] == "x"
    assert s.refreshed(url, None) == [] and s.podcast(url)["etag"] == "x"
    # settings, speed and moving feeds
    assert s.get("speed") == 1.0
    s.set("speed", 1.5)
    assert s.get("speed") == 1.5
    s.set_file("n3", "/tmp/n3.mp3")
    assert s.move_feed(url, "https://new.example.com/feed") and s.episode("n3")["podcast"] == "https://new.example.com/feed"
    assert s.unsubscribe("https://new.example.com/feed") == ["/tmp/n3.mp3"] and s.counts()["shows"] == 0


def test_opml_round_trip():
    data = opml.write([{"url": "https://a.com/f?x=1&y=2", "title": "A & B", "link": "https://a.com"},
                       {"url": "https://b.com/f", "title": "B"}])
    back = opml.read(data)
    assert back == [{"url": "https://a.com/f?x=1&y=2", "title": "A & B"}, {"url": "https://b.com/f", "title": "B"}]
    nested = b'<opml><body><outline text="Folder"><outline xmlUrl="https://c.com/f" text="C"/></outline></body></opml>'
    assert opml.read(nested) == [{"url": "https://c.com/f", "title": "C"}]


def test_notes_markup():
    m = notes.to_markup('<p>Intro at <b>0:30</b>, see <a href="https://e.com/x?a=1&amp;b=2">this</a>.</p>'
                        '<script>bad()</script><img src=x onerror=alert(1)><a href="javascript:x">no</a>', 3723)
    assert '<a href="seek:30">0:30</a>' in m and "bad()" not in m and "javascript" not in m
    assert '<a href="https://e.com/x?a=1&amp;b=2">this</a>' in m
    assert notes.to_markup("Chapters\n\n1:02:03 End, 2 < 3 & more", 3723) == \
        'Chapters\n\n<a href="seek:3723">1:02:03</a> End, 2 &lt; 3 &amp; more'
    assert "seek" not in notes.to_markup("At 59:00 in a 10 minute show", 600)
    assert notes.to_markup("<ul><li>One</li><li>Two</li></ul>") == "•  One\n•  Two"
    # badly nested HTML still yields balanced markup
    m = notes.to_markup("<b><i>x</b> y</i> <a href='https://a.com'>z")
    assert m.count("<b>") == m.count("</b>") and m.count("<i>") == m.count("</i>") and m.count("<a ") == m.count("</a>")
    assert notes.to_plain("<p>Hello&nbsp;<b>world</b></p>") == "Hello world"

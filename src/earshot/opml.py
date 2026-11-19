# SPDX-License-Identifier: GPL-3.0-or-later
"""OPML, the file every podcast app uses to move subscriptions in and out."""
from __future__ import annotations

import time
from xml.etree import ElementTree as ET
from xml.sax.saxutils import quoteattr


def read(data: bytes) -> list[dict]:
    """Every feed in an OPML file (nested folders included): [{"url", "title"}], without duplicates."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as ex:
        raise ValueError(f"That isn't an OPML file ({ex}).") from None
    out, seen = [], set()
    for node in root.iter("outline"):
        url = (node.get("xmlUrl") or node.get("xmlurl") or node.get("url") or "").strip()
        if url.lower().startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            out.append({"url": url, "title": (node.get("title") or node.get("text") or url).strip()})
    return out


def write(podcasts: list[dict]) -> bytes:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<opml version="2.0">', "  <head>",
             "    <title>Earshot subscriptions</title>",
             f"    <dateCreated>{time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime())}</dateCreated>",
             "  </head>", "  <body>"]
    for p in podcasts:
        title = quoteattr(p.get("title") or p["url"])
        site = f" htmlUrl={quoteattr(p['link'])}" if p.get("link") else ""
        lines.append(f"    <outline type=\"rss\" text={title} title={title} xmlUrl={quoteattr(p['url'])}{site}/>")
    lines += ["  </body>", "</opml>", ""]
    return "\n".join(lines).encode("utf-8")

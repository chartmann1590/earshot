# Development only: follow a few real shows (run from devshot.py steps: exec(open("tests/seed_dev.py").read()))
FEEDS = ["https://feeds.npr.org/510318/podcast.xml", "https://feeds.simplecast.com/Sl5CSM3S",
         "https://feeds.megaphone.fm/darknetdiaries", "https://changelog.com/podcast/feed",
         "https://feeds.fireside.fm/selfhosted/rss", "https://lexfridman.com/feed/podcast/",
         "https://atp.fm/rss", "https://feeds.npr.org/510289/podcast.xml"]
for u in FEEDS:
    if not w.store.subscribed(u):
        w.follow(u, quiet=True)

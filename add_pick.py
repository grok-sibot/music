#!/usr/bin/env python3
"""Add a daily pick to picks.json, resolving Bandcamp ids/artwork and Apple Music link.

Usage:
  python3 add_pick.py --date 2026-10-20 --artist "Artist" --track "Track" \
      --album "Album" --url https://x.bandcamp.com/album/y \
      --blurb "One or two sentences." --similar "A, B, C" [--no-push] [--dry-run]

Bandcamp serves a JS challenge to plain HTTP clients, so pages are fetched with
plain urllib first and, if challenged, with a real (headed) Chrome via Playwright
(needs DISPLAY). Any value that cannot be verified is stored as null.
Idempotent: a pick with the same date+artist is never added twice.
"""
import argparse, datetime, html, json, os, re, subprocess, sys, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
FEED = os.path.join(HERE, "picks.json")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
ADL = datetime.timezone(datetime.timedelta(hours=10, minutes=30))


def _plain(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def _browser(url):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False, channel="chrome",
                              args=["--disable-blink-features=AutomationControlled"])
        pg = b.new_context(user_agent=UA).new_page()
        pg.goto(url, wait_until="domcontentloaded", timeout=60000)
        for _ in range(25):
            if "Client Challenge" not in pg.title():
                break
            pg.wait_for_timeout(1000)
        pg.wait_for_timeout(1000)
        s = pg.content()
        b.close()
        return s


def fetch_page(url):
    try:
        s = _plain(url)
        if "bc-page-properties" in s:
            return s
    except Exception:
        pass
    return _browser(url)


def norm(s):
    return re.sub(r"[^\w]+", "", (s or "").lower())


def bandcamp_info(url, track):
    out = {"bandcamp_album_id": None, "bandcamp_track_id": None, "artwork_url": None,
           "year": None, "album_title": None, "track_title": None}
    try:
        s = fetch_page(url)
    except Exception as e:
        print("warn: fetch failed:", e, file=sys.stderr)
        return out
    m = re.search(r'name="bc-page-properties"\s+content="([^"]+)"', s)
    if m:
        props = json.loads(html.unescape(m.group(1)))
        if props.get("item_type") == "a":
            out["bandcamp_album_id"] = props["item_id"]
    m = re.search(r'data-tralbum="([^"]+)"', s)
    if m:
        t = json.loads(html.unescape(m.group(1)))
        cur = t.get("current") or {}
        out["album_title"] = cur.get("title")
        if t.get("art_id"):
            out["artwork_url"] = f"https://f4.bcbits.com/img/a{t['art_id']:010d}_10.jpg"
        rd = cur.get("release_date") or t.get("album_release_date")
        if rd and re.search(r"\d{4}", rd):
            out["year"] = int(re.search(r"\d{4}", rd).group(0))
        for tr in t.get("trackinfo") or []:
            if track and norm(tr.get("title")) == norm(track):
                out["bandcamp_track_id"] = tr.get("track_id") or tr.get("id")
                out["track_title"] = tr.get("title")
                break
    return out


def apple_url(artist, track, album):
    def q(term, entity):
        u = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
            {"term": term, "country": "au", "entity": entity, "limit": 25})
        try:
            return json.loads(_plain(u)).get("results", [])
        except Exception:
            return []
    if track:
        for r in q(f"{artist} {track}", "song"):
            if norm(r.get("artistName")).startswith(norm(artist)[:6]) and norm(r.get("trackName")) == norm(track):
                return r.get("trackViewUrl", "").split("&uo=")[0] or None
    if album:
        for r in q(f"{artist} {album}", "album"):
            if norm(r.get("artistName")).startswith(norm(artist)[:6]) and norm(r.get("collectionName")).startswith(norm(album)):
                return r.get("collectionViewUrl", "").split("?uo=")[0] or None
    return None


def build_pick(a):
    info = bandcamp_info(a.url, a.track)
    if a.track and not info["bandcamp_track_id"]:
        print(f"warn: track {a.track!r} not found on {a.url}", file=sys.stderr)
    return {
        "date": a.date, "artist": a.artist, "track": a.track, "album": a.album,
        "year": a.year or info["year"], "blurb": a.blurb,
        "similar_to": [x.strip() for x in a.similar.split(",") if x.strip()],
        "bandcamp_url": a.url, "bandcamp_album_id": info["bandcamp_album_id"],
        "bandcamp_track_id": info["bandcamp_track_id"], "artwork_url": info["artwork_url"],
        "apple_music_url": apple_url(a.artist, a.track, a.album),
    }


def load():
    if os.path.exists(FEED):
        with open(FEED) as f:
            return json.load(f)
    return {"updated": None, "picks": []}


def save(feed):
    feed["updated"] = datetime.datetime.now(ADL).replace(microsecond=0).isoformat()
    feed["picks"].sort(key=lambda p: p["date"], reverse=True)  # stable: same-day keeps order
    with open(FEED, "w") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)
        f.write("\n")


def exists(feed, date, artist):
    return any(p["date"] == date and norm(p["artist"]) == norm(artist) for p in feed["picks"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=datetime.datetime.now(ADL).date().isoformat(), help="Adelaide date YYYY-MM-DD")
    ap.add_argument("--artist", required=True)
    ap.add_argument("--track")
    ap.add_argument("--album")
    ap.add_argument("--url", required=True, help="Bandcamp album URL")
    ap.add_argument("--blurb", required=True)
    ap.add_argument("--similar", default="", help="comma-separated loved artists")
    ap.add_argument("--year", type=int)
    ap.add_argument("--dry-run", action="store_true", help="print the pick, do not write")
    ap.add_argument("--no-push", action="store_true", help="write+commit but do not push")
    a = ap.parse_args()
    datetime.date.fromisoformat(a.date)

    feed = load()
    if exists(feed, a.date, a.artist):
        print(f"skip: {a.artist} already picked on {a.date}")
        return 0
    pick = build_pick(a)
    print(json.dumps(pick, ensure_ascii=False, indent=2))
    if a.dry_run:
        return 0
    feed["picks"].insert(0, pick)
    save(feed)
    git = lambda *c: subprocess.run(["git", "-C", HERE, *c], check=True)
    git("add", "picks.json")
    git("commit", "-m", f"Pick {a.date}: {a.artist} - {a.track or a.album}")
    if not a.no_push:
        git("push", "origin", "main")
    return 0


if __name__ == "__main__":
    sys.exit(main())

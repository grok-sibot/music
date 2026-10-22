#!/usr/bin/env python3
"""Add a daily pick to picks.json, resolving Bandcamp ids/artwork and Apple Music link.

Usage:
  python3 add_pick.py --date 2026-10-20 --artist "Artist" --track "Track" \
      --album "Album" --url https://x.bandcamp.com/album/y \
      --blurb "One or two sentences." --similar "A, B, C" [--no-push] [--dry-run]

Bandcamp serves a JS challenge to plain HTTP clients, so pages are fetched with
plain urllib first and, if challenged, with a real (headed) Chrome via Playwright
(needs DISPLAY). Any value that cannot be verified is stored as null.
Artwork is downloaded (large _10 variant), centre-cropped and resized to a 1200x1200
JPEG (quality 87, no metadata) at artwork/<date>-<artist-slug>.jpg, recorded as
artwork_path, and committed together with picks.json.
Idempotent: a pick with the same date+artist is never added twice.
  python3 add_pick.py --backfill-artwork   # fetch local art for picks lacking it
"""
import argparse, datetime, html, io, json, os, re, subprocess, sys, unicodedata, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
FEED = os.path.join(HERE, "picks.json")
ART_DIR = "artwork"
ART_SIZE = 1200
ART_QUALITY = 87
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


def slug(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "artist"


def artwork_rel(date, artist):
    return f"{ART_DIR}/{date}-{slug(artist)}.jpg"


def large_variant(url):
    # Bandcamp size variants: _10 ~1200px, _16 700px, _7 160px. Always fetch the large one.
    return re.sub(r"_\d+\.(jpg|png)$", "_10.jpg", url)


def save_artwork(url, rel):
    """Download url, centre-crop + resize to ART_SIZE square JPEG (q87, no metadata; never upscales) at rel."""
    from PIL import Image
    req = urllib.request.Request(large_variant(url), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    im = Image.open(io.BytesIO(data))
    im = im.convert("RGB")
    w, h = im.size
    side = min(w, h)
    im = im.crop(((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side))
    if side > ART_SIZE:
        im = im.resize((ART_SIZE, ART_SIZE), Image.LANCZOS)
    path = os.path.join(HERE, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    clean = Image.new("RGB", im.size)  # fresh image: no EXIF/ICC/comment carried over
    clean.paste(im)
    clean.save(path, "JPEG", quality=ART_QUALITY, optimize=True, progressive=True)
    return rel


def with_artwork_path(pick, rel):
    """Return pick with artwork_path placed right after artwork_url (other fields untouched)."""
    out = {}
    for k, v in pick.items():
        if k == "artwork_path":
            continue
        out[k] = v
        if k == "artwork_url":
            out["artwork_path"] = rel
    if "artwork_path" not in out:
        out["artwork_path"] = rel
    return out


def art_too_small(path):
    """True if the file is missing or smaller than ART_SIZE (older 300px caches)."""
    if not os.path.exists(path):
        return True
    try:
        from PIL import Image
        with Image.open(path) as im:
            return min(im.size) < ART_SIZE
    except Exception:
        return True


def ensure_artwork(pick, refresh=False):
    """Make sure the pick's local artwork exists (refresh: also re-fetch if smaller than ART_SIZE)."""
    if not pick.get("artwork_url"):
        return with_artwork_path(pick, None), None
    rel = pick.get("artwork_path") or artwork_rel(pick["date"], pick["artist"])
    path = os.path.join(HERE, rel)
    if not os.path.exists(path) or (refresh and art_too_small(path)):
        try:
            save_artwork(pick["artwork_url"], rel)
        except Exception as e:
            print(f"warn: artwork download failed for {pick['artist']}: {e}", file=sys.stderr)
            if os.path.exists(path):  # keep the old (smaller) file rather than dropping it
                return with_artwork_path(pick, rel), rel
            return with_artwork_path(pick, None), None
    return with_artwork_path(pick, rel), rel


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
    ap.add_argument("--artist")
    ap.add_argument("--track")
    ap.add_argument("--album")
    ap.add_argument("--url", help="Bandcamp album URL")
    ap.add_argument("--blurb")
    ap.add_argument("--similar", default="", help="comma-separated loved artists")
    ap.add_argument("--year", type=int)
    ap.add_argument("--dry-run", action="store_true", help="print the pick, do not write")
    ap.add_argument("--no-push", action="store_true", help="write+commit but do not push")
    ap.add_argument("--backfill-artwork", action="store_true",
                    help="download missing (or smaller than ART_SIZE) local artwork for existing picks (no new pick)")
    a = ap.parse_args()
    git = lambda *c: subprocess.run(["git", "-C", HERE, *c], check=True)

    feed = load()
    if a.backfill_artwork:
        if a.dry_run:
            for p in feed["picks"]:
                rel = p.get("artwork_path") or artwork_rel(p["date"], p["artist"])
                if p.get("artwork_url"):
                    print(("need " if art_too_small(os.path.join(HERE, rel)) else "ok   ") + rel)
            return 0
        changed, files = False, []
        for i, p in enumerate(feed["picks"]):
            new, rel = ensure_artwork(p, refresh=True)
            if new != p:
                feed["picks"][i] = new
                changed = True
            if rel:
                files.append(rel)
        if not changed and not files:
            print("skip: nothing to backfill")
            return 0
        if changed:
            save(feed)
        git("add", "picks.json", *files)
        if subprocess.run(["git", "-C", HERE, "diff", "--cached", "--quiet"]).returncode == 0:
            print("skip: artwork already committed")
            return 0
        git("commit", "-m", "Backfill local artwork")
        if not a.no_push:
            git("push", "origin", "main")
        return 0

    if not (a.artist and a.url and a.blurb):
        ap.error("--artist, --url and --blurb are required")
    datetime.date.fromisoformat(a.date)
    if exists(feed, a.date, a.artist):
        print(f"skip: {a.artist} already picked on {a.date}")
        return 0
    pick = build_pick(a)
    if a.dry_run:
        print(json.dumps(with_artwork_path(pick, artwork_rel(a.date, a.artist) if pick["artwork_url"] else None),
                         ensure_ascii=False, indent=2))
        return 0
    pick, rel = ensure_artwork(pick)
    print(json.dumps(pick, ensure_ascii=False, indent=2))
    feed["picks"].insert(0, pick)
    save(feed)
    git("add", "picks.json", *([rel] if rel else []))
    git("commit", "-m", f"Pick {a.date}: {a.artist} - {a.track or a.album}")
    if not a.no_push:
        git("push", "origin", "main")
    return 0


if __name__ == "__main__":
    sys.exit(main())

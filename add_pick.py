#!/usr/bin/env python3
"""Add a daily pick to picks.json, resolving Bandcamp ids/artwork and Apple Music link.

Usage:
  python3 add_pick.py --date 2026-10-20 --artist "Artist" --track "Track" \
      --album "Album" --url https://x.bandcamp.com/album/y \
      --blurb "One or two sentences." --similar "A, B, C" [--no-push] [--dry-run]

Bandcamp serves a JS challenge to plain HTTP clients, so pages are fetched with
plain urllib first and, if challenged, with a real (headed) Chrome via Playwright
(needs DISPLAY). Artwork is taken from data-tralbum, JSON-LD or og:image, then
iTunes and Cover Art Archive if Bandcamp does not yield a usable image.
Artwork is downloaded (large _10 variant when Bandcamp), centre-cropped and
resized to a 1200x1200 JPEG (quality 87, no metadata) at
artwork/<date>-<artist-slug>.jpg, recorded as artwork_path, and committed
together with picks.json. A new pick is refused if no cover can be saved.
Idempotent: a pick with the same date+artist is never added twice.
  python3 add_pick.py --backfill-artwork   # fetch local art for picks lacking it
"""
import argparse, datetime, html, io, json, os, re, subprocess, sys, time, unicodedata, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
FEED = os.path.join(HERE, "picks.json")
ART_DIR = "artwork"
ART_SIZE = 1200
ART_QUALITY = 87
MAX_ART_BYTES = 5 * 1024 * 1024  # refuse huge originals; use a 1200px variant instead
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
MB_UA = "grok-sibot-music/1.0 (https://github.com/grok-sibot/music)"
ADL = datetime.timezone(datetime.timedelta(hours=10, minutes=30))


def _plain(url, ua=UA, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers, r.geturl()


def _plain_text(url, ua=UA, timeout=30):
    data, _, _ = _plain(url, ua=ua, timeout=timeout)
    return data.decode("utf-8", "replace")


def _page_has_album_data(s):
    """True when the HTML has structured album data (ids, tracks, art), not just a meta tag.

    og:image / bc-page-properties can appear mid-navigation, before JSON-LD or data-tralbum.
    Returning early on those alone is how a pick can be saved without bandcamp_album_id / track_id.
    """
    if not s or "Client Challenge" in s:
        return False
    return "data-tralbum" in s or "application/ld+json" in s


def _safe_content(pg):
    try:
        return pg.content()
    except Exception:
        # Playwright raises if the challenge page is still navigating to the album.
        return ""


def _browser(url):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False, channel="chrome",
                              args=["--disable-blink-features=AutomationControlled"])
        try:
            pg = b.new_context(user_agent=UA).new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=60000)
            for _ in range(30):
                title = pg.title() or ""
                if "Client Challenge" not in title and not title.startswith("Loading "):
                    break
                pg.wait_for_timeout(1000)
            else:
                raise RuntimeError(f"Bandcamp client challenge did not clear for {url}")
            try:
                # script tags are hidden; wait for attached, not visible
                pg.wait_for_selector(
                    'script[type="application/ld+json"], [data-tralbum]',
                    state="attached", timeout=15000)
            except Exception:
                pass
            last = ""
            for _ in range(20):
                last = _safe_content(pg)
                if _page_has_album_data(last):
                    return last
                pg.wait_for_timeout(500)
            raise RuntimeError(f"Bandcamp page had no album data for {url}")
        finally:
            b.close()


def fetch_page(url):
    try:
        s = _plain_text(url)
        if _page_has_album_data(s):
            return s
    except Exception as e:
        print(f"warn: plain fetch failed: {e}", file=sys.stderr)
    last = None
    for attempt in range(1, 3):
        try:
            s = _browser(url)
            if _page_has_album_data(s):
                return s
            last = RuntimeError("Bandcamp page had no album data after challenge")
            print(f"warn: browser fetch {attempt} missing album data", file=sys.stderr)
        except Exception as e:
            last = e
            print(f"warn: browser fetch {attempt} failed: {e}", file=sys.stderr)
        time.sleep(2)
    raise last or RuntimeError(f"could not fetch {url}")


def norm(s):
    return re.sub(r"[^\w]+", "", (s or "").lower())


def artist_match(got, want):
    g, w = norm(got), norm(want)
    if not g or not w:
        return False
    if g == w:
        return True
    # short names ("aya") must match exactly; longer ones may be a prefix
    return len(w) >= 6 and (g.startswith(w) or w.startswith(g))


def large_variant(url):
    # Bandcamp size variants: _10 ~1200px, _16 700px, _7/_5 small. Always fetch the large one.
    if not url:
        return url
    url = re.sub(r"_\d+\.(jpg|png)$", r"_10.jpg", url, flags=re.I)
    # iTunes artwork: /100x100bb.jpg -> /1200x1200bb.jpg
    url = re.sub(r"/\d+x\d+\w*\.(jpg|png)$", r"/1200x1200bb.\1", url, flags=re.I)
    return url


def _meta_content(s, prop):
    pat = r'<meta[^>]+(?:property|name)=["\']' + re.escape(prop) + r'["\'][^>]*content=["\']([^"\']+)["\']'
    m = re.search(pat, s, re.I)
    if not m:
        pat = r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']' + re.escape(prop) + r'["\']'
        m = re.search(pat, s, re.I)
    return html.unescape(m.group(1)) if m else None


def _ld_props(obj):
    out = {}
    if isinstance(obj, list):
        for item in obj:
            out.update(_ld_props(item))
        return out
    if not isinstance(obj, dict):
        return out
    for p in obj.get("additionalProperty") or []:
        if isinstance(p, dict) and p.get("name") is not None:
            out[p["name"]] = p.get("value")
    return out


def _ld_image(obj):
    img = obj.get("image") if isinstance(obj, dict) else None
    if isinstance(img, list):
        img = img[0] if img else None
    if isinstance(img, dict):
        img = img.get("url") or img.get("contentUrl")
    return img if isinstance(img, str) and img.startswith("http") else None


def _ld_types(obj):
    t = obj.get("@type") if isinstance(obj, dict) else None
    if t is None:
        return set()
    return set(t) if isinstance(t, list) else {t}


def _year_of(s):
    m = re.search(r"\d{4}", str(s) if s else "")
    return int(m.group(0)) if m else None


def _art_from_id(art_id):
    try:
        return f"https://f4.bcbits.com/img/a{int(art_id):010d}_10.jpg"
    except (TypeError, ValueError):
        return None


def parse_json_ld(s, track):
    out = {}
    for m in re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', s, re.I | re.S):
        try:
            ld = json.loads(m.group(1))
        except Exception:
            continue
        nodes = ld if isinstance(ld, list) else [ld]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            types = _ld_types(node)
            if types and not types.intersection({"MusicAlbum", "MusicRecording", "MusicRelease"}):
                continue
            if node.get("name") and not out.get("album_title"):
                out["album_title"] = node["name"]
            props = _ld_props(node)
            if props.get("art_id") and not out.get("artwork_url"):
                out["artwork_url"] = _art_from_id(props["art_id"])
            img = _ld_image(node)
            if img and not out.get("artwork_url"):
                out["artwork_url"] = large_variant(img)
            y = _year_of(node.get("datePublished") or node.get("dateCreated"))
            if y and not out.get("year"):
                out["year"] = y
            for rel in node.get("albumRelease") or []:
                if not isinstance(rel, dict):
                    continue
                rprops = _ld_props(rel)
                if rprops.get("item_type") == "a" and rprops.get("item_id"):
                    out.setdefault("bandcamp_album_id", rprops["item_id"])
                    if rprops.get("art_id") and not out.get("artwork_url"):
                        out["artwork_url"] = _art_from_id(rprops["art_id"])
                    img = _ld_image(rel)
                    if img and not out.get("artwork_url"):
                        out["artwork_url"] = large_variant(img)
                    break
            tracks = node.get("track")
            items = tracks.get("itemListElement") if isinstance(tracks, dict) else tracks
            if isinstance(items, list) and track and not out.get("bandcamp_track_id"):
                for el in items:
                    item = el.get("item", el) if isinstance(el, dict) else {}
                    if not isinstance(item, dict):
                        continue
                    title = item.get("name")
                    if title and norm(title) == norm(track):
                        out["bandcamp_track_id"] = _ld_props(item).get("track_id")
                        out["track_title"] = title
                        break
    return out


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
    if not m:
        m = re.search(r"data-tralbum='([^']+)'", s)
    if m:
        t = json.loads(html.unescape(m.group(1)))
        cur = t.get("current") or {}
        out["album_title"] = cur.get("title")
        if t.get("art_id"):
            out["artwork_url"] = _art_from_id(t["art_id"])
        out["year"] = _year_of(cur.get("release_date") or t.get("album_release_date"))
        for tr in t.get("trackinfo") or []:
            if track and norm(tr.get("title")) == norm(track):
                out["bandcamp_track_id"] = tr.get("track_id") or tr.get("id")
                out["track_title"] = tr.get("title")
                break
    ld = parse_json_ld(s, track)
    for k, v in ld.items():
        if v and not out.get(k):
            out[k] = v
    if not out["artwork_url"]:
        og = _meta_content(s, "og:image") or _meta_content(s, "twitter:image")
        if og and "bcbits.com/img" in og:
            out["artwork_url"] = large_variant(og)
        elif og and re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", og, re.I):
            out["artwork_url"] = og
    return out


def _itunes_search(term, entity):
    u = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": term, "country": "au", "entity": entity, "limit": 25})
    try:
        return json.loads(_plain_text(u)).get("results", [])
    except Exception as e:
        print(f"warn: iTunes search failed: {e}", file=sys.stderr)
        return []


def apple_url(artist, track, album):
    if track:
        for r in _itunes_search(f"{artist} {track}", "song"):
            if artist_match(r.get("artistName"), artist) and norm(r.get("trackName")) == norm(track):
                return r.get("trackViewUrl", "").split("&uo=")[0] or None
    if album:
        for r in _itunes_search(f"{artist} {album}", "album"):
            if artist_match(r.get("artistName"), artist) and norm(r.get("collectionName")).startswith(norm(album)):
                return r.get("collectionViewUrl", "").split("?uo=")[0] or None
    return None


def itunes_artwork(artist, track, album):
    """Return a 1200px iTunes artwork URL if the artist (and track/album) match."""
    if track:
        for r in _itunes_search(f"{artist} {track}", "song"):
            if artist_match(r.get("artistName"), artist) and norm(r.get("trackName")) == norm(track):
                url = r.get("artworkUrl100") or r.get("artworkUrl60")
                if url:
                    return large_variant(url)
    if album:
        for r in _itunes_search(f"{artist} {album}", "album"):
            if artist_match(r.get("artistName"), artist) and norm(r.get("collectionName")).startswith(norm(album)):
                url = r.get("artworkUrl100") or r.get("artworkUrl60")
                if url:
                    return large_variant(url)
    return None


def caa_artwork(artist, album):
    """Cover Art Archive 1200px front, via MusicBrainz. Never fetches the huge original."""
    if not artist or not album:
        return None
    q = f'artist:"{artist}" AND release:"{album}"'
    u = "https://musicbrainz.org/ws/2/release/?" + urllib.parse.urlencode(
        {"query": q, "fmt": "json", "limit": 8})
    try:
        time.sleep(1.1)
        js = json.loads(_plain_text(u, ua=MB_UA, timeout=30))
    except Exception as e:
        print(f"warn: MusicBrainz search failed: {e}", file=sys.stderr)
        return None
    for rel in js.get("releases") or []:
        if (rel.get("score") or 0) < 80:
            continue
        credits = rel.get("artist-credit") or []
        names = []
        for c in credits:
            if isinstance(c, dict):
                names.append(c.get("name") or "")
                if isinstance(c.get("artist"), dict):
                    names.append(c["artist"].get("name") or "")
            elif isinstance(c, str):
                names.append(c)
        if not any(artist_match(n, artist) for n in names):
            continue
        if album and not (norm(rel.get("title")).startswith(norm(album)) or norm(album).startswith(norm(rel.get("title")))):
            continue
        mbid = rel.get("id")
        if not mbid:
            continue
        try:
            time.sleep(1.1)
            data, _, _ = _plain(f"https://coverartarchive.org/release/{mbid}", ua=MB_UA, timeout=30)
            idx = json.loads(data.decode("utf-8", "replace"))
        except Exception as e:
            print(f"warn: Cover Art Archive index failed for {mbid}: {e}", file=sys.stderr)
            continue
        for img in idx.get("images") or []:
            if not img.get("front"):
                continue
            thumbs = img.get("thumbnails") or {}
            url = thumbs.get("1200") or thumbs.get("large") or thumbs.get("500")
            if url:
                return url
        # last resort: the dedicated 1200 endpoint (not /front, which can be many MB)
        return f"https://coverartarchive.org/release/{mbid}/front-1200"
    return None


def slug(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "artist"


def artwork_rel(date, artist):
    return f"{ART_DIR}/{date}-{slug(artist)}.jpg"


def _download(url):
    last = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(large_variant(url) if "bcbits.com" in url or "mzstatic.com" in url else url,
                                         headers={"User-Agent": UA, "Accept": "image/*,*/*;q=0.8"})
            with urllib.request.urlopen(req, timeout=60) as r:
                ctype = (r.headers.get_content_type() or "").lower()
                length = r.headers.get("Content-Length")
                if length and int(length) > MAX_ART_BYTES:
                    raise RuntimeError(f"image too large ({length} bytes): {url}")
                data = r.read(MAX_ART_BYTES + 1)
                if len(data) > MAX_ART_BYTES:
                    raise RuntimeError(f"image too large ({len(data)} bytes): {url}")
                if ctype and not ctype.startswith("image/") and ctype not in ("application/octet-stream",):
                    raise RuntimeError(f"not an image ({ctype}): {url}")
                return data, ctype or r.headers.get_content_type()
        except Exception as e:
            last = e
            print(f"warn: artwork download attempt {attempt} failed: {e}", file=sys.stderr)
            time.sleep(1.5 * attempt)
    raise last


def save_artwork(url, rel):
    """Download url, centre-crop + resize to ART_SIZE square JPEG (q87, no metadata; never upscales) at rel."""
    from PIL import Image
    data, _ = _download(url)
    im = Image.open(io.BytesIO(data))
    im = im.convert("RGB")
    w, h = im.size
    side = min(w, h)
    if side < 100:
        raise RuntimeError(f"artwork too small ({w}x{h}): {url}")
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
    """True if the file is missing, unreadable, or a leftover small cache (older 300px files).

    Sources slightly under 1200px are kept: we never upscale, so re-fetching them cannot help.
    """
    if not os.path.exists(path):
        return True
    try:
        from PIL import Image
        with Image.open(path) as im:
            return min(im.size) < 600
    except Exception:
        return True


def merge_info(pick, info):
    """Fill empty pick fields from a Bandcamp parse without overwriting set values."""
    mapping = {
        "bandcamp_album_id": "bandcamp_album_id",
        "bandcamp_track_id": "bandcamp_track_id",
        "artwork_url": "artwork_url",
        "year": "year",
    }
    out = dict(pick)
    for src, dest in mapping.items():
        if out.get(dest) in (None, "", []) and info.get(src):
            out[dest] = info[src]
    return out


def _add_url(urls, seen, url, source):
    if not url:
        return
    url = large_variant(url) if ("bcbits.com" in url or "mzstatic.com" in url) else url
    if url not in seen:
        seen.add(url)
        urls.append((url, source))


def known_artwork_urls(pick):
    urls, seen = [], set()
    url = pick.get("artwork_url")
    source = "bandcamp" if (url or "").find("bcbits.com") >= 0 else "known"
    _add_url(urls, seen, url, source)
    return urls, seen


def fallback_artwork_urls(pick, urls=None, seen=None):
    """iTunes then Cover Art Archive. Called only after known/Bandcamp URLs fail."""
    urls = [] if urls is None else urls
    seen = set() if seen is None else seen
    try:
        _add_url(urls, seen, itunes_artwork(pick.get("artist"), pick.get("track"), pick.get("album")), "itunes")
    except Exception as e:
        print(f"warn: iTunes artwork lookup failed: {e}", file=sys.stderr)
    try:
        _add_url(urls, seen, caa_artwork(pick.get("artist"), pick.get("album")), "coverartarchive")
    except Exception as e:
        print(f"warn: Cover Art Archive lookup failed: {e}", file=sys.stderr)
    return urls


def collect_artwork_urls(pick):
    """All candidates, including fallbacks. Used by --dry-run."""
    urls, seen = known_artwork_urls(pick)
    return fallback_artwork_urls(pick, urls, seen)


def enrich_pick(pick):
    """Re-fetch Bandcamp when artwork (or ids) are missing, then attach fallback artwork URLs."""
    out = dict(pick)
    if out.get("bandcamp_url") and (not out.get("artwork_url") or not out.get("bandcamp_album_id")
                                    or (out.get("track") and not out.get("bandcamp_track_id"))):
        info = bandcamp_info(out["bandcamp_url"], out.get("track"))
        out = merge_info(out, info)
    if not out.get("apple_music_url"):
        try:
            out["apple_music_url"] = apple_url(out.get("artist"), out.get("track"), out.get("album"))
        except Exception as e:
            print(f"warn: Apple Music lookup failed: {e}", file=sys.stderr)
    return out


def ensure_artwork(pick, refresh=False, enrich=True):
    """Make sure the pick's local artwork exists (refresh: also re-fetch if smaller than ART_SIZE).

    When the cover cannot be downloaded, artwork_path stays None and the caller must fail loudly.
    """
    rel = pick.get("artwork_path") or artwork_rel(pick["date"], pick["artist"])
    path = os.path.join(HERE, rel)
    have = os.path.exists(path) and not (refresh and art_too_small(path))
    if have and pick.get("artwork_url"):
        return with_artwork_path(pick, rel), rel

    out = enrich_pick(pick) if (enrich and not have) else dict(pick)
    if have:
        return with_artwork_path(out, rel), rel

    def try_urls(candidates, errors):
        for url, source in candidates:
            try:
                save_artwork(url, rel)
                out["artwork_url"] = url
                print(f"artwork: saved {rel} from {source} ({url})", file=sys.stderr)
                return with_artwork_path(out, rel), rel
            except Exception as e:
                errors.append(f"{source}: {e}")
                print(f"warn: artwork download failed for {out.get('artist')} via {source}: {e}", file=sys.stderr)
        return None

    errors = []
    known, seen = known_artwork_urls(out)
    saved = try_urls(known, errors)
    if saved:
        return saved
    saved = try_urls(fallback_artwork_urls(out, [], seen), errors)
    if saved:
        return saved
    if os.path.exists(path) and not art_too_small(path):
        # kept an existing file after a failed refresh
        return with_artwork_path(out, rel), rel
    if errors:
        print(f"error: no artwork saved for {out.get('artist')}: " + " | ".join(errors), file=sys.stderr)
    else:
        print(f"error: no artwork source found for {out.get('artist')}", file=sys.stderr)
    return with_artwork_path(out, None), None


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
                missing = art_too_small(os.path.join(HERE, rel)) or not p.get("artwork_url")
                print(("need " if missing else "ok   ") + f"{rel}  ({p['artist']})")
            return 0
        changed, files, failed = False, [], []
        for i, p in enumerate(feed["picks"]):
            rel = p.get("artwork_path") or artwork_rel(p["date"], p["artist"])
            needs = art_too_small(os.path.join(HERE, rel)) or not p.get("artwork_url")
            if not needs:
                continue
            new, got = ensure_artwork(p, refresh=True, enrich=True)
            if new != p:
                feed["picks"][i] = new
                changed = True
            if got:
                files.append(got)
            else:
                failed.append(p["artist"])
        if not changed and not files:
            print("skip: nothing to backfill")
            return 0
        if changed:
            save(feed)
        git("add", "picks.json", *files)
        if subprocess.run(["git", "-C", HERE, "diff", "--cached", "--quiet"]).returncode != 0:
            git("commit", "-m", "Backfill local artwork")
        else:
            print("skip: artwork already committed")
        if failed:
            print("error: could not backfill artwork for: " + ", ".join(failed), file=sys.stderr)
            return 1
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
    def has_album_id(p):
        return bool(re.fullmatch(r"\d+", str(p.get("bandcamp_album_id") or "")))

    if a.dry_run:
        # resolve the URL we would save, but do not write
        preview = dict(pick)
        if not preview.get("artwork_url"):
            urls = collect_artwork_urls(preview)
            if urls:
                preview["artwork_url"] = urls[0][0]
        print(json.dumps(with_artwork_path(preview, artwork_rel(a.date, a.artist) if preview.get("artwork_url") else None),
                         ensure_ascii=False, indent=2))
        if not preview.get("artwork_url"):
            print("error: no artwork source found", file=sys.stderr)
            return 1
        if not has_album_id(preview):
            print("error: no Bandcamp album id (the website widget hides a pick without one)", file=sys.stderr)
            return 1
        return 0
    if not has_album_id(pick):
        print(json.dumps(pick, ensure_ascii=False, indent=2))
        print(f"error: refusing to add {a.artist} without a Bandcamp album id "
              "(the website widget hides a pick without one)", file=sys.stderr)
        return 1
    pick, rel = ensure_artwork(pick)
    print(json.dumps(pick, ensure_ascii=False, indent=2))
    if not rel:
        print(f"error: refusing to add {a.artist} without artwork", file=sys.stderr)
        return 1
    feed["picks"].insert(0, pick)
    save(feed)
    git("add", "picks.json", rel)
    git("commit", "-m", f"Pick {a.date}: {a.artist} - {a.track or a.album}")
    if not a.no_push:
        git("push", "origin", "main")
    return 0


if __name__ == "__main__":
    sys.exit(main())

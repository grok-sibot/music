# Music

Daily new-artist picks from Music. Live at **https://grok-sibot.github.io/music/**.

- `index.html` — static page (no framework, no build) that renders `picks.json`.
- `picks.json` — the feed.
- `add_pick.py` — adds a pick, resolving Bandcamp ids/artwork and the Apple Music link, then commits and pushes.

## Feed: `picks.json`

URL: `https://grok-sibot.github.io/music/picks.json` — served by GitHub Pages with
`Access-Control-Allow-Origin: *`, so any website can `fetch()` it directly.
The shape is stable; new fields may be added, existing ones will not be renamed or removed.

```json
{
  "updated": "2026-10-19T15:30:00+10:30",
  "picks": [
    {
      "date": "2026-10-19",
      "artist": "Ziúr",
      "track": "Alive, Unless?",
      "album": "Antifate",
      "year": 2021,
      "blurb": "One or two plain sentences.",
      "similar_to": ["Gazelle Twin", "Hüma Utku"],
      "bandcamp_url": "https://ziurinhell.bandcamp.com/album/antifate",
      "bandcamp_album_id": 3331446416,
      "bandcamp_track_id": 1924480233,
      "artwork_url": "https://f4.bcbits.com/img/a0540927704_10.jpg",
      "apple_music_url": "https://music.apple.com/au/album/..."
    }
  ]
}
```

| field | type | notes |
|---|---|---|
| `updated` | string | ISO 8601 timestamp (Adelaide offset) of the last feed change |
| `picks` | array | **newest first** (by `date`; same-day picks keep insertion order, later pick first) |
| `date` | string | `YYYY-MM-DD`, Australia/Adelaide date of the pick |
| `artist` | string | |
| `track` | string \| null | featured track |
| `album` | string \| null | |
| `year` | integer \| null | release year |
| `blurb` | string | 1–2 plain-text sentences (no HTML) |
| `similar_to` | string[] | artists the listener already loves |
| `bandcamp_url` | string | Bandcamp album page |
| `bandcamp_album_id` | integer \| null | numeric id for the embedded player |
| `bandcamp_track_id` | integer \| null | numeric track id within that album |
| `artwork_url` | string \| null | Bandcamp album art, large (`_10.jpg`, ~1200px) |
| `apple_music_url` | string \| null | from the iTunes Search API (`country=au`) |

Any value that could not be verified from the source page is `null`; ids are never guessed.

### Embedding a pick

```js
const { picks } = await (await fetch("https://grok-sibot.github.io/music/picks.json")).json();
const p = picks[0];
const src = `https://bandcamp.com/EmbeddedPlayer/album=${p.bandcamp_album_id}/size=small/` +
            (p.bandcamp_track_id ? `track=${p.bandcamp_track_id}/` : "") + "transparent=true/";
```

## Adding a pick

```sh
python3 add_pick.py --date 2026-10-20 --artist "Artist" --track "Track" --album "Album" \
  --url https://artist.bandcamp.com/album/album --blurb "One or two sentences." \
  --similar "Barker, Lusine"
```

`--dry-run` prints the resolved pick without writing; `--no-push` commits without pushing.
Re-running with the same `date` + `artist` is a no-op. Bandcamp challenges plain HTTP clients,
so the script falls back to a headed Chrome via Playwright (needs `DISPLAY`).

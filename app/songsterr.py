"""Songsterr fetch layer: turn a song URL/ID into song info and raw note data.

Two endpoints do all the work:
  * https://www.songsterr.com/api/meta/{songId}  -> title, artist, revisionId, track list
  * https://{cdn}.cloudfront.net/{songId}/{revisionId}/{image}/{partId}.json
    -> the per-track note data (measures -> voices -> beats -> notes)

Pure standard library (urllib), like gpif_loader. Based on the approach of the
SongsterrToGuitarPro project (github.com/djrobson5/SongsterrToGuitarPro).
"""
from __future__ import annotations

import gzip
import json
import re
import urllib.error
import urllib.request

META = "https://www.songsterr.com/api/meta/{song_id}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# Host buckets reverse-engineered from the player's vendor bundle. The note data
# lives on different CloudFront distributions depending on the image token:
#   * image ending in "-stage"  -> the single STAGE_HOST
#   * a normal image token      -> any host in NORMAL_HOSTS (player round-robins)
#   * no image token (legacy)   -> LEGACY_HOSTS, with a different "/part/" path
STAGE_HOST = "d3d3l6a6rcgkaf"
NORMAL_HOSTS = ["dqsljvtekg760", "d34shlm8p2ums2", "d3cqchs6g3b5ew"]
LEGACY_HOSTS = ["d3rrfvx08uyjp1", "dodkcbujl0ebx", "dj1usja78sinh"]


def part_url(image, song_id, revision_id, part_id, attempt=0):
    if image and image.endswith("-stage"):
        host = STAGE_HOST
    elif image:
        host = NORMAL_HOSTS[attempt % len(NORMAL_HOSTS)]
    else:
        host = LEGACY_HOSTS[attempt % len(LEGACY_HOSTS)]
        return f"https://{host}.cloudfront.net/part/{revision_id}/{part_id}"
    return f"https://{host}.cloudfront.net/{song_id}/{revision_id}/{image}/{part_id}.json"


def extract_song_id(url_or_id: str) -> int:
    """Accept a full Songsterr URL or a bare numeric id.

    URLs look like .../brian-fallon-a-wonderful-life-tab-s2580013 — the id is the
    digits after the trailing '-s'.
    """
    s = str(url_or_id).strip()
    if s.isdigit():
        return int(s)
    m = re.search(r"-s(\d+)", s)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{3,})", s)  # last-ditch: any long run of digits
    if m:
        return int(m.group(1))
    raise ValueError(f"Could not find a song id in: {url_or_id!r}")


def _get(url: str) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    # CloudFront serves some objects with Content-Encoding: gzip regardless of
    # what we ask for, so sniff the gzip magic bytes and inflate if needed.
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def get_meta(song_id: int) -> dict:
    return json.loads(_get(META.format(song_id=song_id)))


def fetch_part(song_id: int, revision_id: int, image: str, part_id: int) -> dict:
    """Fetch one track's note data, rotating CDN hosts on failure (as the player does)."""
    last = None
    for attempt in range(3):
        url = part_url(image, song_id, revision_id, part_id, attempt)
        try:
            return json.loads(_get(url))
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (403, 404, 500, 502, 503):
                raise
    raise last


def fetch_song(url_or_id: str):
    """Return (meta, parts) where parts is a list of per-track note-data dicts.

    A part may be None when its download failed; songsterr_convert keeps the
    corresponding track as an empty one so indices stay aligned.
    """
    song_id = extract_song_id(url_or_id)
    meta = get_meta(song_id)
    revision_id = meta["revisionId"]
    image = meta.get("image")
    tracks = meta.get("tracks", [])
    parts = []
    for i in range(len(tracks)):
        try:
            parts.append(fetch_part(song_id, revision_id, image, i))
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            print(f"  ! track {i} failed: {e}")
            parts.append(None)
    return meta, parts


def is_percussion(track_meta: dict) -> bool:
    if track_meta.get("instrumentId") == 1024:
        return True
    name = (track_meta.get("instrument") or "").lower()
    return "drum" in name or "percussion" in name


def track_kind(track_meta: dict) -> str:
    """Short label for the track list: Drums / Bass / Guitar / ..."""
    if is_percussion(track_meta):
        return "Drums"
    name = (track_meta.get("instrument") or "").lower()
    if "bass" in name:
        return "Bass"
    return (track_meta.get("instrument") or "").strip() or "Guitar"


if __name__ == "__main__":
    import sys

    meta, parts = fetch_song(sys.argv[1])
    print(f"{meta['artist']} - {meta['title']}  (rev {meta['revisionId']}, {len(parts)} tracks)")
    for i, (t, p) in enumerate(zip(meta["tracks"], parts)):
        n = len(p["measures"]) if p else "?"
        print(f"  [{i}] {track_kind(t):<18} tuning={t.get('tuning')}  measures={n}")

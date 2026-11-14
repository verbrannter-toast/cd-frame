"""
CD frame catalog service
-------------------------
Runs as an always-on service directly on the Pi host (NOT inside Docker),
so it has direct filesystem access to Home Assistant's www/albums folder
via the same bind-mount path your docker-compose file already uses for HA.

Home Assistant triggers it over HTTP via a rest_command, so you can
catalog a new album straight from the dashboard/app — no SSH, no manual
script runs, no rsync.

Endpoints (all POST, JSON body, listening on 127.0.0.1:5005):
    /catalog  {artist, album}        auto-pick best MusicBrainz release and catalog it
    /search   {artist, album}        write up to 10 candidates to search_results.json
    /confirm  {artist, album, mbid}  catalog one specific release
    /remove   {name} or {slug}       delete a catalogued album (index entry + its folder)

Requirements:
    pip install -r requirements.txt   (add --break-system-packages on Debian/Raspberry Pi OS)

Test it manually first:
    HA_TOKEN=... WWW_ALBUMS_DIR=... python3 catalog_service.py

Then install it as a systemd service (see catalog-service.service) so it
survives reboots.
"""

import json
import os
import re
import shutil
import time
from io import BytesIO
from pathlib import Path

import requests
from flask import Flask, request, jsonify
from PIL import Image

# ---------------------------------------------------------------------------
# CONFIG — set via environment variables (see catalog-service.env.example),
# so no secrets live in the repo.
# ---------------------------------------------------------------------------
# MusicBrainz requires a descriptive User-Agent with a contact address.
USER_AGENT = os.environ.get("CATALOG_USER_AGENT", "CDFrameCatalog/1.0 (your-email@example.com)")

HA_URL = os.environ.get("HA_URL", "http://localhost:8123")   # HA's host-mapped port
HA_TOKEN = os.environ.get("HA_TOKEN", "")                    # Long-Lived Access Token
HA_ENTITY = os.environ.get("HA_ENTITY", "input_select.loaded_album")
# Optional second dropdown used by the "Remove an Album" dashboard card. Set to an
# empty string to disable. If the helper doesn't exist yet, the failure is only logged.
HA_REMOVE_ENTITY = os.environ.get("HA_REMOVE_ENTITY", "input_select.remove_album")
# input_select can't have an empty option list, so this is shown when the catalog is empty.
EMPTY_PLACEHOLDER = "(no albums)"

# The HOST path that Docker maps into HA's /config/www/albums, i.e. the
# folder <your HA config dir>/www/albums on the Pi. It MUST sit inside the
# folder bind-mounted into the HA container (e.g. ./config:/config).
WWW_ALBUMS_DIR = Path(os.environ.get("WWW_ALBUMS_DIR", "/home/youruser/homeassistant/config/www/albums"))
INDEX_FILE = WWW_ALBUMS_DIR / "index.json"

if not HA_TOKEN:
    print("[startup] WARNING: HA_TOKEN is not set — updating the HA dropdown will fail", flush=True)

EPD_WIDTH = 122  # 2.13" e-paper panel
MB_BASE = "https://musicbrainz.org/ws/2"
CAA_BASE = "https://coverartarchive.org"
HEADERS = {"User-Agent": USER_AGENT}

app = Flask(__name__)
print(f"[startup] WWW_ALBUMS_DIR resolves to: {WWW_ALBUMS_DIR.resolve()}", flush=True)


def request_with_retry(url, params=None, max_retries=3):
    """GET with retries on 503 — MusicBrainz/Cover Art Archive both throw
    these occasionally even at low request volume, and a short backoff
    usually clears it."""
    for attempt in range(max_retries):
        resp = requests.get(url, params=params, headers=HEADERS, allow_redirects=True)
        if resp.status_code != 503:
            return resp
        wait = 2 ** attempt  # 1s, 2s, 4s
        print(f"[catalog] 503 from {url}, retrying in {wait}s ...", flush=True)
        time.sleep(wait)
    return resp  # give back the last (failed) response if all retries used


# ---------------------------------------------------------------------------
# MusicBrainz / Cover Art Archive
# ---------------------------------------------------------------------------
def search_release(artist, album):
    params = {"query": f'release:"{album}" AND artist:"{artist}"', "fmt": "json", "limit": 10}
    resp = request_with_retry(f"{MB_BASE}/release/", params=params)
    resp.raise_for_status()
    time.sleep(1)
    return resp.json().get("releases", [])


def get_tracklist(mbid):
    params = {"inc": "recordings", "fmt": "json"}
    resp = request_with_retry(f"{MB_BASE}/release/{mbid}", params=params)
    resp.raise_for_status()
    time.sleep(1)
    try:
        data = resp.json()
    except ValueError as e:
        print(f"[catalog] MusicBrainz response wasn't valid JSON: {e}", flush=True)
        print(f"[catalog] Raw response (first 300 chars): {resp.text[:300]!r}", flush=True)
        raise
    tracks = []
    for medium in data.get("media", []):
        for track in medium.get("tracks", []):
            tracks.append({"title": track.get("title"), "duration_ms": track.get("length")})
    return tracks


def get_cover_art(mbid):
    resp = request_with_retry(f"{CAA_BASE}/release/{mbid}/front")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return Image.open(BytesIO(resp.content))


def dither_for_epd(img):
    img = img.convert("RGB")
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((EPD_WIDTH, EPD_WIDTH))
    return img.convert("1")


def save_epd_raw(dithered_img, path):
    """Export a 1-bit PIL image as a raw packed bitmap GxEPD2's drawBitmap
    can read directly: MSB-first, 1 byte per 8 horizontal pixels, rows
    padded to a byte boundary, top-to-bottom, no header. Bit set = black.
    This is a DIFFERENT format from cover.bmp (a real Windows BMP with a
    header) — the ESP32 needs this raw one, not the .bmp."""
    w, h = dithered_img.size
    row_bytes = (w + 7) // 8
    buf = bytearray(row_bytes * h)
    px = dithered_img.load()
    for y in range(h):
        for x in range(w):
            if px[x, y] == 0:  # PIL "1" mode: 0 = black
                buf[y * row_bytes + (x // 8)] |= (0x80 >> (x % 8))
    Path(path).write_bytes(bytes(buf))


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower().strip()).strip("-")


# ---------------------------------------------------------------------------
# Local index + HA dropdown
# ---------------------------------------------------------------------------
def load_index():
    if INDEX_FILE.exists():
        raw = INDEX_FILE.read_text()
        try:
            data = json.loads(raw)
        except ValueError as e:
            print(f"[catalog] index.json wasn't valid JSON: {e}", flush=True)
            print(f"[catalog] Raw content (first 300 chars): {raw[:300]!r}", flush=True)
            raise
        # Backward compatible: earlier versions of this script wrote a
        # bare list instead of {"albums": [...]}
        if isinstance(data, list):
            return data
        return data.get("albums", [])
    return []


def save_index(index):
    # Wrapped in {"albums": [...]} rather than a bare list so HA's
    # command_line sensor (json_attributes) can parse it directly.
    WWW_ALBUMS_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps({"albums": index}, indent=2))


def _set_options(entity_id, names):
    resp = requests.post(
        f"{HA_URL}/api/services/input_select/set_options",
        headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
        json={"entity_id": entity_id, "options": names},
    )
    resp.raise_for_status()


def update_ha_dropdown(index):
    names = [entry["name"] for entry in index] or [EMPTY_PLACEHOLDER]
    _set_options(HA_ENTITY, names)
    if HA_REMOVE_ENTITY:
        # Best effort: the main dropdown matters more than the removal picker.
        try:
            _set_options(HA_REMOVE_ENTITY, names)
        except requests.RequestException as e:
            print(f"[catalog] Could not update {HA_REMOVE_ENTITY}: {e}", flush=True)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def catalog_from_release(artist, album, mbid):
    """Second half of the pipeline: given a confirmed mbid, fetch the
    tracklist + cover art and update the HA library. Shared by both the
    auto-pick /catalog endpoint and the manual /confirm endpoint."""
    tracks = get_tracklist(mbid)

    display_name = f"{artist} - {album}"
    slug = slugify(display_name)
    album_dir = WWW_ALBUMS_DIR / slug
    album_dir.mkdir(parents=True, exist_ok=True)

    with open(album_dir / "tracks.json", "w") as f:
        json.dump({"artist": artist, "album": album, "mbid": mbid, "tracks": tracks}, f, indent=2)

    cover_saved = False
    cover = get_cover_art(mbid)
    if cover:
        cover.convert("RGB").resize((300, 300)).save(album_dir / "cover_full.jpg", quality=85)
        dithered = dither_for_epd(cover)
        dithered.save(album_dir / "cover.bmp")       # for your own reference/debugging
        save_epd_raw(dithered, album_dir / "cover.raw")  # what the ESP32 actually fetches
        cover_saved = True

    index = load_index()
    if not any(e["slug"] == slug for e in index):
        index.append({"name": display_name, "slug": slug})
    save_index(index)
    update_ha_dropdown(index)

    return {"success": True, "track_count": len(tracks), "cover_saved": cover_saved}


def remove_album(name=None, slug=None):
    """Delete a catalogued album: drop it from index.json, remove its folder
    (tracks.json, cover files) and refresh the HA dropdowns. Look-up is by the
    display name shown in the dropdown, or by slug."""
    index = load_index()
    entry = next(
        (e for e in index if (slug and e.get("slug") == slug) or (name and e.get("name") == name)),
        None,
    )
    if entry is None:
        return {"success": False, "error": f"Album not found: {name or slug}"}

    # Only ever delete a folder that sits directly inside WWW_ALBUMS_DIR, so a
    # bad or hand-edited slug (e.g. "../..") can never delete anything else.
    album_dir = (WWW_ALBUMS_DIR / entry["slug"]).resolve()
    base = WWW_ALBUMS_DIR.resolve()
    folder_removed = False
    if album_dir.parent == base and album_dir.is_dir():
        shutil.rmtree(album_dir)
        folder_removed = True
    elif album_dir.exists():
        return {"success": False, "error": f"Refusing to delete unexpected path: {album_dir}"}

    index = [e for e in index if e is not entry]
    save_index(index)
    update_ha_dropdown(index)
    return {"success": True, "removed": entry["name"], "folder_removed": folder_removed, "remaining": len(index)}


def catalog_album(artist, album):
    releases = search_release(artist, album)
    if not releases:
        return {"success": False, "error": "No releases found on MusicBrainz"}

    # No TTY here to ask interactively, unlike the original CLI script.
    # Prefer releases MusicBrainz has marked "high" quality — these are
    # more likely (not guaranteed) to have cover art attached — while
    # keeping MusicBrainz's own relevance ranking as the tiebreaker
    # within each quality group.
    releases.sort(key=lambda r: r.get("quality") != "high")
    chosen = releases[0]
    mbid = chosen["id"]
    alternates = [f"{r.get('title')} ({r.get('date', '?')}, quality={r.get('quality')})" for r in releases[1:4]]

    result = catalog_from_release(artist, album, mbid)
    result.update({
        "chosen": chosen.get("title"),
        "date": chosen.get("date"),
        "quality": chosen.get("quality"),
        "other_candidates_considered": alternates,
    })
    return result


@app.route("/catalog", methods=["POST"])
def catalog_endpoint():
    data = request.get_json(force=True)
    artist, album = data.get("artist"), data.get("album")
    if not artist or not album:
        return jsonify({"success": False, "error": "artist and album are required"}), 400
    # flush=True matters here: stdout is buffered when there's no TTY
    # (i.e. running under systemd), so without it prints can lag behind
    # in journalctl by a while.
    print(f"[catalog] Cataloging '{album}' by {artist} ...", flush=True)
    try:
        result = catalog_album(artist, album)
        print(f"[catalog] Done: {result}", flush=True)
        return jsonify(result)
    except Exception as e:
        print(f"[catalog] ERROR: {e}", flush=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/search", methods=["POST"])
def search_endpoint():
    data = request.get_json(force=True)
    artist, album = data.get("artist"), data.get("album")
    if not artist or not album:
        return jsonify({"success": False, "error": "artist and album are required"}), 400
    print(f"[search] Searching for '{album}' by {artist} ...", flush=True)
    try:
        releases = search_release(artist, album)
        candidates = [{
            "index": i + 1,
            "title": r.get("title"),
            "artist": r.get("artist-credit", [{}])[0].get("name", "?"),
            "date": r.get("date", "?"),
            "country": r.get("country", "?"),
            "quality": r.get("quality", "?"),
            "track_count": r.get("track-count", "?"),
            "mbid": r.get("id"),
            "url": f"https://musicbrainz.org/release/{r.get('id')}",
        } for i, r in enumerate(releases[:10])]

        # Same pattern as index.json: write to a file HA reads via a
        # command_line sensor, rather than trying to pass this back
        # through rest_command's response handling.
        WWW_ALBUMS_DIR.mkdir(parents=True, exist_ok=True)
        results_path = WWW_ALBUMS_DIR / "search_results.json"
        results_path.write_text(json.dumps({"candidates": candidates}, indent=2))
        print(f"[search] Wrote {len(candidates)} candidate(s) to {results_path.resolve()}", flush=True)
        return jsonify({"success": True, "candidates": candidates})
    except Exception as e:
        print(f"[search] ERROR: {e}", flush=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/confirm", methods=["POST"])
def confirm_endpoint():
    data = request.get_json(force=True)
    artist, album, mbid = data.get("artist"), data.get("album"), data.get("mbid")
    if not artist or not album or not mbid:
        return jsonify({"success": False, "error": "artist, album, and mbid are required"}), 400
    print(f"[confirm] Cataloging '{album}' by {artist} (mbid={mbid}) ...", flush=True)
    try:
        result = catalog_from_release(artist, album, mbid)
        print(f"[confirm] Done: {result}", flush=True)
        return jsonify(result)
    except Exception as e:
        print(f"[confirm] ERROR: {e}", flush=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/remove", methods=["POST"])
def remove_endpoint():
    data = request.get_json(force=True)
    name, slug = data.get("name"), data.get("slug")
    if not name and not slug:
        return jsonify({"success": False, "error": "name or slug is required"}), 400
    print(f"[remove] Removing '{name or slug}' ...", flush=True)
    try:
        result = remove_album(name=name, slug=slug)
        print(f"[remove] Done: {result}", flush=True)
        return jsonify(result), (200 if result["success"] else 404)
    except Exception as e:
        print(f"[remove] ERROR: {e}", flush=True)
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5005)
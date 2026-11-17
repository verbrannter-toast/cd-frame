#!/usr/bin/env python3
"""
CD Frame -> Discord Rich Presence
---------------------------------
Runs on the PC that has the Discord client open (Windows 11 or Linux). It polls
the Home Assistant entity that the ESP32 firmware publishes
(sensor.cd_frame_now_playing, one update per track start) and mirrors it as a
"Listening to ..." activity with a progress bar and the album cover.

Nothing about the CD player itself is read here: the presence follows exactly
what the e-paper display shows, with the same limits (no pause detection).

Usage:
    python cd_frame_presence.py                    # uses config.toml next to this file
    python cd_frame_presence.py --config PATH
    python cd_frame_presence.py --dry-run -v       # print what would be shown, never touch Discord

Needs Python 3.11+ (tomllib) and the packages in requirements.txt.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path

import requests

try:
    from pypresence import ActivityType, Presence
except ImportError:  # pragma: no cover - friendly message instead of a traceback
    sys.exit("pypresence is not installed. Run: pip install -r requirements.txt")

log = logging.getLogger("cd-frame-presence")

DEFAULT_CONFIG = Path(__file__).with_name("config.toml")
COVER_URL = "https://coverartarchive.org/release/{mbid}/front-500"
COVER_RETRY_SECONDS = 300      # retry a failed cover lookup after this long
DISCORD_RETRY_SECONDS = 15     # wait between attempts to reach Discord
DISCORD_REFRESH_SECONDS = 60   # re-send the unchanged activity so a restarted Discord is noticed
USER_AGENT = "cd-frame-presence/1.0 (+https://github.com/verbrannter-toast/cd-frame)"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    ha_url: str
    ha_token: str
    discord_client_id: str
    entity_id: str = "sensor.cd_frame_now_playing"
    poll_interval: float = 5.0
    end_grace_seconds: float = 30.0
    fallback_image: str = ""


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        sys.exit(f"Config file not found: {path}\nCopy config.example.toml to config.toml and fill it in.")
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"Could not parse {path}: {e}")

    missing = [k for k in ("ha_url", "ha_token", "discord_client_id") if not str(raw.get(k, "")).strip()]
    if missing:
        sys.exit(f"Missing required setting(s) in {path}: {', '.join(missing)}")

    unknown = set(raw) - {f.name for f in fields(Config)}
    if unknown:
        log.warning("Ignoring unknown setting(s) in %s: %s", path, ", ".join(sorted(unknown)))

    return Config(
        ha_url=str(raw["ha_url"]).strip().rstrip("/"),
        ha_token=str(raw["ha_token"]).strip(),
        discord_client_id=str(raw["discord_client_id"]).strip(),
        entity_id=str(raw.get("entity_id", Config.entity_id)).strip(),
        poll_interval=max(1.0, float(raw.get("poll_interval", Config.poll_interval))),
        end_grace_seconds=max(0.0, float(raw.get("end_grace_seconds", Config.end_grace_seconds))),
        fallback_image=str(raw.get("fallback_image", "")).strip(),
    )


# ---------------------------------------------------------------------------
# Home Assistant
# ---------------------------------------------------------------------------
class HAError(Exception):
    """Home Assistant could not be read (down, wrong token, odd data)."""


@dataclass(frozen=True)
class NowPlaying:
    slug: str
    artist: str
    album: str
    title: str
    track_number: int
    track_count: int
    started: float   # epoch seconds: when the ESP32 posted this track = when the track started
    duration: float  # seconds

    @property
    def end(self) -> float:
        return self.started + self.duration

    @property
    def key(self) -> tuple:
        return (self.slug, self.track_number, self.started)


def fetch_now_playing(ha: requests.Session, cfg: Config) -> NowPlaying | None:
    """Return the current track, or None if nothing is playing.

    Raises HAError if Home Assistant can't be read, so a network blip is not
    mistaken for "playback stopped".
    """
    try:
        resp = ha.get(f"{cfg.ha_url}/api/states/{cfg.entity_id}", timeout=5)
    except requests.RequestException as e:
        raise HAError(f"cannot reach Home Assistant: {e}") from e

    if resp.status_code == 404:
        return None  # entity doesn't exist yet: the ESP32 hasn't reported since HA started
    if resp.status_code in (401, 403):
        raise HAError("Home Assistant rejected the token (check ha_token)")
    if not resp.ok:
        raise HAError(f"Home Assistant returned HTTP {resp.status_code}")

    try:
        data = resp.json()
        if data.get("state") != "playing":
            return None
        a = data["attributes"]
        return NowPlaying(
            slug=str(a["slug"]),
            artist=str(a.get("artist") or ""),
            album=str(a.get("album") or ""),
            title=str(a["track_title"]),
            track_number=int(a["track_number"]),
            track_count=int(a["track_count"]),
            started=datetime.fromisoformat(data["last_updated"]).timestamp(),
            duration=int(a["track_duration_ms"]) / 1000,
        )
    except (ValueError, KeyError, TypeError) as e:
        raise HAError(f"unexpected data from {cfg.entity_id}: {e!r}") from e


# ---------------------------------------------------------------------------
# Cover art
# ---------------------------------------------------------------------------
class CoverResolver:
    """Finds a public cover image URL for an album.

    Discord can only show images it can fetch from the internet, so the cover
    comes from the Cover Art Archive (via the MusicBrainz release id stored in
    the album's tracks.json), not from the Pi.
    """

    def __init__(self, cfg: Config, ha: requests.Session):
        self.cfg = cfg
        self.ha = ha
        # A separate session WITHOUT the Home Assistant token: it must never
        # be sent to coverartarchive.org / archive.org.
        self.web = requests.Session()
        self.web.headers["User-Agent"] = USER_AGENT
        self._cache: dict[str, tuple[str | None, float]] = {}  # slug -> (url, retry_after)

    def url_for(self, slug: str) -> str | None:
        cached = self._cache.get(slug)
        if cached and (cached[0] or time.monotonic() < cached[1]):
            return cached[0]
        url = self._lookup(slug)
        self._cache[slug] = (url, time.monotonic() + COVER_RETRY_SECONDS)
        return url

    def _lookup(self, slug: str) -> str | None:
        try:
            r = self.ha.get(f"{self.cfg.ha_url}/local/albums/{slug}/tracks.json", timeout=5)
            r.raise_for_status()
            mbid = r.json().get("mbid")
            if not mbid:
                log.info("No MusicBrainz id stored for %s, no cover art", slug)
                return None
            # The CAA answers with a redirect to the actual image; give Discord
            # the final URL so it doesn't have to follow redirects itself.
            with self.web.get(COVER_URL.format(mbid=mbid), stream=True, timeout=10) as img:
                if not img.ok:
                    log.info("No cover art on the Cover Art Archive for %s (HTTP %s)", slug, img.status_code)
                    return None
                if len(img.url) > 256:  # Discord's limit for image fields
                    log.info("Cover URL for %s is too long for Discord", slug)
                    return None
                return img.url
        except (requests.RequestException, ValueError) as e:
            log.warning("Cover lookup for %s failed: %s", slug, e)
            return None


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
def _clip(text: str, limit: int = 128) -> str:
    """Discord wants 2-128 characters per text field."""
    text = (text or "").strip() or "Unknown"
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text.ljust(2)


def build_activity(np: NowPlaying, cover_url: str | None, cfg: Config) -> dict:
    activity = {
        "activity_type": ActivityType.LISTENING,
        "details": _clip(np.title),
        "state": _clip(np.artist or "Unknown artist"),
        "large_text": _clip(f"{np.album} (track {np.track_number}/{np.track_count})"),
        "start": int(np.started),
        "end": int(np.end),
    }
    image = cover_url or cfg.fallback_image
    if image:
        activity["large_image"] = image
    return activity


class DiscordLink:
    """Keeps one connection to the local Discord client and (re)applies the activity."""

    def __init__(self, client_id: str):
        self.client_id = client_id
        self.rpc = None
        self.shown_key = None
        self.last_sent = 0.0
        self.next_connect = 0.0
        self.waiting_logged = False

    def _connect(self) -> bool:
        if self.rpc is not None:
            return True
        if time.monotonic() < self.next_connect:
            return False
        rpc = Presence(self.client_id)
        try:
            rpc.connect()
        except Exception as e:  # Discord not running / not logged in yet: expected, keep trying
            self.next_connect = time.monotonic() + DISCORD_RETRY_SECONDS
            if not self.waiting_logged:
                log.info("Discord is not reachable (%s: %s); will keep retrying", type(e).__name__, e)
                self.waiting_logged = True
            self._close(rpc)
            return False
        self.rpc = rpc
        self.shown_key = None
        self.waiting_logged = False
        log.info("Connected to Discord")
        return True

    @staticmethod
    def _close(rpc) -> None:
        try:
            if rpc is not None:
                rpc.close()
        except Exception:
            pass

    def _drop(self, why: str) -> None:
        log.warning("Lost the connection to Discord (%s); will reconnect", why)
        self._close(self.rpc)
        self.rpc = None
        self.shown_key = None
        self.next_connect = time.monotonic() + DISCORD_RETRY_SECONDS

    def show(self, key, make_activity) -> None:
        if not self._connect():
            return
        due = key != self.shown_key or time.monotonic() - self.last_sent > DISCORD_REFRESH_SECONDS
        if not due:
            return
        try:
            self.rpc.update(**make_activity())
        except Exception as e:
            self._drop(f"{type(e).__name__}: {e}")
            return
        self.shown_key = key
        self.last_sent = time.monotonic()

    def hide(self) -> None:
        if self.rpc is None or self.shown_key is None:
            return  # nothing shown (Discord also drops our activity itself when the connection closes)
        try:
            self.rpc.clear()
        except Exception as e:
            self._drop(f"{type(e).__name__}: {e}")
            return
        self.shown_key = None

    def close(self) -> None:
        self._close(self.rpc)
        self.rpc = None


class DryRunLink:
    """Stand-in for DiscordLink that only logs, for --dry-run."""

    def __init__(self):
        self.shown_key = None

    def show(self, key, make_activity) -> None:
        if key != self.shown_key:
            log.info("[dry-run] would show: %s", make_activity())
            self.shown_key = key

    def hide(self) -> None:
        if self.shown_key is not None:
            log.info("[dry-run] would clear the presence")
            self.shown_key = None

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run(cfg: Config, dry_run: bool) -> None:
    ha = requests.Session()
    ha.headers["Authorization"] = f"Bearer {cfg.ha_token}"
    covers = CoverResolver(cfg, ha)
    link = DryRunLink() if dry_run else DiscordLink(cfg.discord_client_id)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    current: NowPlaying | None = None
    logged_key = None
    ha_down = False
    log.info("Watching %s on %s", cfg.entity_id, cfg.ha_url)

    try:
        while not stop.is_set():
            now = time.time()
            try:
                latest = fetch_now_playing(ha, cfg)
                if ha_down:
                    log.info("Home Assistant is reachable again")
                    ha_down = False
                # The ESP32 only posts at track starts. If a track is long over and no new
                # one arrived, the ESP32 is off (or the album ended), so stop showing it.
                alive = latest is not None and now <= latest.end + cfg.end_grace_seconds
                current = latest if alive else None
            except HAError as e:
                if not ha_down:
                    log.warning("%s", e)
                    ha_down = True
                # Keep what we show, but let it lapse once its track should have ended.
                if current is not None and now > current.end + cfg.end_grace_seconds:
                    current = None

            key = current.key if current else None
            if key != logged_key:
                if current:
                    log.info("Now playing: %s - %s (track %d/%d)",
                             current.artist, current.title, current.track_number, current.track_count)
                else:
                    log.info("Nothing playing")
                logged_key = key

            if current:
                link.show(current.key, lambda c=current: build_activity(c, covers.url_for(c.slug), cfg))
            else:
                link.hide()

            stop.wait(cfg.poll_interval)
    finally:
        link.close()
        log.info("Stopped")


def setup_logging(verbose: bool, log_file: Path | None) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    elif log_file is None:
        # pythonw.exe (Windows, no console) has no stderr: write to a file instead.
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        log_file = base / "cd-frame-presence" / "presence.log"
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(log_file, maxBytes=200_000, backupCount=1, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


def main() -> None:
    parser = argparse.ArgumentParser(description="Show the CD Frame's current track as a Discord Rich Presence.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="path to config.toml")
    parser.add_argument("--dry-run", action="store_true", help="log what would be shown instead of talking to Discord")
    parser.add_argument("--log-file", type=Path, help="also write the log to this file")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose, args.log_file)
    run(load_config(args.config), args.dry_run)


if __name__ == "__main__":
    main()

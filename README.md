# CD Frame

A wall-mountable "now playing" frame for a real CD player, in the spirit of the CoolGeek M1: a portable Bluetooth CD player stripped out of its case and mounted vertically behind glass so the disc spins in plain view, with an e-paper display next to it showing the album cover, artist, album and the current track.

The CD player has no digital output of any kind, so it can't tell anyone what it is playing. This project solves that with metadata instead: you tell Home Assistant which CD is in the player, and an ESP32 shows the cover and steps through the tracklist on a timer.

## How it works

![How it works: Home Assistant, catalog service, shared album folder, ESP32 and e-paper display](docs/how-it-works.svg)

Arrows 1 to 4 happen when you catalog an album; arrows 5 and 6 repeat while a disc is selected. The CD player is deliberately not connected to anything.

1. **Catalog an album once.** In the Home Assistant dashboard you enter artist and album. The catalog service looks the release up on MusicBrainz, saves the tracklist with durations, downloads the cover from the Cover Art Archive, and produces a 122×122 dithered 1-bit bitmap for the e-paper plus a colour thumbnail for the dashboard.
2. **Pick the disc.** Selecting an album in the `Loaded Disc` dropdown tells the ESP32 what is in the player.
3. **Press play on the CD player.** The ESP32 notices the new selection (it polls every 15 s), loads the tracklist and cover, and from then on advances to the next track whenever the current track's duration has elapsed. The display only refreshes on track changes, which is what e-paper wants.

There is no connection between the CD player and the ESP32 (see [Known limitations](#known-limitations)).

## Repository layout

```
docs/              Architecture diagram
pi-service/        Flask catalog service + systemd unit + env example
home-assistant/    Config snippets: rest_commands, sensors, helpers, scripts, automations, dashboard cards
esp32-firmware/    Arduino sketch for the ESP32-C3 + e-paper display
hardware/          3D printed parts, photos, wiring notes
desktop-presence/  Optional Discord Rich Presence companion (Windows 11 + Linux)
```

## Hardware

| Part | Notes |
|---|---|
| Portable Bluetooth CD player | A Zelurxx unit (Sunplus SPHE8104LW based). It must **transmit** Bluetooth to external speakers; several cheap players are receive-only. Built-in speaker removed. |
| ESP32-C3 Supermini | Runs the display firmware, powered over USB-C. |
| 2.13" e-paper, 122×250, SPI | SSD1680 controller, driven with `GxEPD2_213_B74` (black/white). |
| Power | Power bank → toggle switch → power-only USB-C splitter feeding the CD player and the ESP32. |
| 3D printed bracket/frame | In `hardware/`. |
| Raspberry Pi running Home Assistant (Docker) | Also hosts the catalog service. |

### Display wiring (ESP32-C3 Supermini)

| E-paper | ESP32-C3 GPIO |
|---|---|
| BUSY | 21 |
| RES | 6 |
| D/C | 7 |
| CS | 10 |
| SCL (CLK) | 20 |
| SDA (MOSI) | 5 |
| GND | GND |
| VCC | 3V3 (not 5 V) |

Use the 4-line SPI setting on the display board. Avoid GPIO8/9 (onboard LED / boot button) for anything else.

### CD player notes

- The player has a lid-closed switch. In the frame the lid doesn't exist, so that switch is held pressed permanently.
- The player's own buttons are covered by the glass in normal use. Tapping them with the ESP32 (to follow skips/pauses) was tried and abandoned: the buttons look like a scanned matrix, and wiring into it made unrelated buttons misbehave.
- **Power bank:** many banks switch their output off when the load looks idle or when the output is opened and closed by a downstream switch. If yours shuts off after a while or won't come back after flipping the switch, check the model's low-current / always-on behaviour and use its plain output-only port rather than a combined in/out port.

## Setup

### 1. Catalog service (on the Pi, outside Docker)

The service writes straight into the folder that Home Assistant's container sees as `/config/www/albums`. That folder **must be inside the directory bind-mounted into the HA container** (for example `./config:/config` in `docker-compose.yml`), otherwise Home Assistant and the ESP32 can't see the files.

```bash
cd pi-service
pip3 install -r requirements.txt --break-system-packages   # or use a venv

sudo cp catalog-service.env.example /etc/catalog-service.env
sudo chmod 600 /etc/catalog-service.env
sudo nano /etc/catalog-service.env        # token, contact address, WWW_ALBUMS_DIR

# edit User= and the paths in the unit file first
sudo cp catalog-service.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now catalog-service
journalctl -u catalog-service -f          # follow the logs
```

Create the Home Assistant token under *Profile → Security → Long-Lived Access Tokens*. MusicBrainz requires a real contact address in `CATALOG_USER_AGENT`.

### 2. Home Assistant

1. Create the dropdown helper `input_select.loaded_album` in the UI (*Settings → Devices & Services → Helpers → Dropdown*). Add any placeholder option; the service replaces the list.
2. Merge `home-assistant/configuration.example.yaml` into `configuration.yaml`, `scripts.example.yaml` into `scripts.yaml`, and (optionally) `automations.example.yaml` into `automations.yaml`. Merge into existing keys rather than adding duplicates.
3. Check the configuration, then restart Home Assistant.
4. Add the cards from `dashboard-cards.yaml`.

### 3. ESP32 firmware

1. Arduino IDE with the ESP32 board package, and these libraries: **GxEPD2**, **U8g2_for_Adafruit_GFX**, **ArduinoJson** (the sketch uses the v6-style `DynamicJsonDocument` API).
2. In `esp32-firmware/cd_frame_display/`, copy `config.h.example` to `config.h` and fill in WiFi, the Home Assistant address and the token. `config.h` is git-ignored.
3. Flash the `cd_frame_display` sketch.

### 4. Discord Rich Presence (optional)

Shows what the frame is playing as a "Listening to ..." activity on your Discord profile: track, artist, album cover and a progress bar. It follows the e-paper display, so it appears when an album is loaded and disappears when the ESP32 loses power or the album ends.

How it works: the ESP32 posts the current track to Home Assistant as `sensor.cd_frame_now_playing` at every track start (no Home Assistant configuration needed, the REST API creates the entity). A small script on your PC polls that entity and talks to the local Discord client. Discord only accepts presence from a program on the same machine as the client, which is why this runs on your PC and not on the Pi. The cover comes from the Cover Art Archive (Discord needs a public image URL), looked up through the MusicBrainz id in the album's `tracks.json`.

**Reflash the ESP32 first.** The sketch change that publishes the entity is part of this feature.

**a) Create the Discord application** (once). Go to <https://discord.com/developers/applications>, click *New Application* and name it. **That name is what Discord shows after "Listening to"**, so something like `CD Frame` works well. Copy the *Application ID* from *General Information*. Optionally upload a fallback image under *Rich Presence → Art Assets*.

**b) Install and configure** (both systems need Python 3.11+ and the Discord desktop app running, not the browser version):

```bash
cd desktop-presence
python -m venv .venv
# Linux:    .venv/bin/pip install -r requirements.txt
# Windows:  .venv\Scripts\pip install -r requirements.txt
cp config.example.toml config.toml        # Windows: copy config.example.toml config.toml
# edit config.toml: ha_url, ha_token, discord_client_id
```

Test it by hand before installing it as a service. `--dry-run` only logs what it would show, without Discord:

```bash
.venv/bin/python cd_frame_presence.py --dry-run -v     # Windows: .venv\Scripts\python ...
.venv/bin/python cd_frame_presence.py -v
```

**c) Run it at login.**

- **CachyOS / Linux:** edit the two paths in `cd-frame-presence.service`, then:
  ```bash
  mkdir -p ~/.config/systemd/user && cp cd-frame-presence.service ~/.config/systemd/user/
  systemctl --user daemon-reload && systemctl --user enable --now cd-frame-presence
  journalctl --user -u cd-frame-presence -f
  ```
  The script finds Discord through its local IPC socket in `$XDG_RUNTIME_DIR`. A native Discord package works as is. A Flatpak or Snap Discord keeps that socket inside its sandbox, where the script usually can't reach it (not tested here); if the log keeps saying "Discord is not reachable" while Discord is open, that is the likely cause.
- **Windows 11:** run `powershell -ExecutionPolicy Bypass -File .\install-windows.ps1` in the `desktop-presence` folder. It registers a hidden scheduled task that starts at every logon. The log is `%LOCALAPPDATA%\cd-frame-presence\presence.log`. Remove it with `Unregister-ScheduledTask -TaskName "CD Frame Presence" -Confirm:$false`.

Make sure *Settings → Activity Privacy → Share your detected activities* is on in Discord, otherwise the presence is set but hidden. Both systems can run the script at once (dual boot is fine), the activity is simply set from whichever one is running.

## Usage

- **Add a CD:** type artist and album into *Catalog an Album*. Either tap *Catalog New Album* (auto-picks the best release, preferring MusicBrainz releases marked high quality), or tap *Search for Album*, look at the candidates (each links to its MusicBrainz page), set the number and tap *Confirm Selected Album*.
- **Play a CD:** put it in the player, select it under *Loaded Disc*, and press play on the player. Do both at about the same time, because the ESP32's timer starts when it loads the album.
- **Remove a CD:** in *Remove an Album*, pick the album, switch on *Confirm removal* and tap *Remove selected album*. This deletes its entry from `index.json` and its folder (tracklist and cover files), refreshes both dropdowns, and turns the confirmation switch back off. If the removed album was the loaded one, the dropdown falls back to the first remaining album.
- Cataloguing the same artist/album again overwrites its folder, which is how to fix a wrong edition.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `sensor.album_catalog` never appears | Old `platform: command_line` syntax under `sensor:`; use the top-level `command_line:` block. Also check for a duplicate top-level key in `configuration.yaml`. |
| Sensor exists but is empty / `cat` fails | `www/albums` is outside the folder mounted into the HA container. |
| Catalog request returns 500 with `503 Server Error` | MusicBrainz throttling; the service retries three times, just try again. |
| HA log: invalid authentication for `set_options` | `HA_TOKEN` not set or wrong in `/etc/catalog-service.env`. |
| `ModuleNotFoundError: flask` in the journal | Dependencies not installed for the user the service runs as. |
| Dropdown empty after an HA restart | Use `automations.example.yaml`, or catalog an album to refill it. |
| Cover missing on the e-paper | The album was catalogued before `cover.raw` existed, or the release has no cover art. Re-catalogue it (try a different release). |
| Display stuck on "Connecting to WiFi..." | Often HA is simply unchanged/unreachable, not WiFi. Check the serial monitor at 115200 baud and try the Pi's IP instead of `homeassistant.local`. |

## Known limitations

- **No sync with the player.** The ESP32 starts at track 1 when it loads an album and then counts the MusicBrainz durations. If you pause, skip, or power-cycle the player mid-album, the display drifts from what is playing. Power the ESP32 and the player together and start albums from track 1.
- **Discord presence follows the timer, not the player.** It has the same drift and no pause detection as the display. It clears when the last track's time is up, or `end_grace_seconds` after a track should have ended with no new report (for example when the ESP32 loses power).
- Track durations come from MusicBrainz and can differ slightly from your disc's pressing, so the display can drift a little over a long album.
- Very long track titles overflow the display width.
- `cover.bmp` is only a debugging copy; the ESP32 reads the headerless `cover.raw`.
- The catalog service has no authentication and binds to `127.0.0.1` only. Don't expose it.

## Credits

Metadata from [MusicBrainz](https://musicbrainz.org) and cover art from the [Cover Art Archive](https://coverartarchive.org). Cover images belong to their respective rights holders, which is why generated album data is git-ignored. Display driver: [GxEPD2](https://github.com/ZinggJM/GxEPD2).

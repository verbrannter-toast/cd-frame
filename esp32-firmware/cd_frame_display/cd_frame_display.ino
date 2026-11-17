/*
  CD Frame — ESP32-C3 "Now Playing" e-paper display firmware

  Polls Home Assistant's input_select.loaded_album entity to see which
  CD is loaded, fetches that album's tracklist and cover art from the
  same HA www/ folder the cataloging pipeline writes to, and auto-
  advances the displayed track using an internal per-track timer — NOT
  a live ticking countdown, since e-paper shouldn't be refreshed that
  often. Purely automatic: no manual skip/prev/play controls, since the
  CD player's physical buttons end up covered by the frame's glass front
  in normal use anyway.

  Display init/pins reused as-is from the working prototype sketch.

  Library dependencies beyond the prototype's:
    - ArduinoJson (by Benoit Blanchon) — Library Manager
*/

#include <SPI.h>
#define ENABLE_GxEPD2_display 0
#include <GxEPD2_BW.h>
#include <U8g2_for_Adafruit_GFX.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// ---------------------------------------------------------------------------
// CONFIG — WIFI_SSID, WIFI_PASSWORD, HA_HOST, HA_TOKEN, ENTITY_ID and
// POLL_INTERVAL_MS live in config.h (copy config.h.example, fill it in;
// config.h is git-ignored so no secrets end up in the repo).
// ---------------------------------------------------------------------------
#include "config.h"

// ---------------------------------------------------------------------------
// Display setup — pins/driver unchanged from the working prototype
// ---------------------------------------------------------------------------
#define SCREEN_WIDTH 255
#define SCREEN_HEIGHT 122

static const uint8_t EPD_BUSY = 21;
static const uint8_t EPD_CS = 10;
static const uint8_t EPD_RST = 6;
static const uint8_t EPD_DC = 7;
static const uint8_t EPD_SCK = 20;
static const uint8_t EPD_MISO = -1;
static const uint8_t EPD_MOSI = 5;

GxEPD2_BW<GxEPD2_213_B74, GxEPD2_213_B74::HEIGHT> display(GxEPD2_213_B74(EPD_CS, EPD_DC, EPD_RST, EPD_BUSY));
U8G2_FOR_ADAFRUIT_GFX u8g2Fonts;

// Bumped from the prototype's 10MHz — WiFi needs far more headroom than a
// static display-only test does.
#define CPU_FREQUENCY_MHZ 80

// ---------------------------------------------------------------------------
// Track state
// ---------------------------------------------------------------------------
#define MAX_TRACKS 30
String trackTitles[MAX_TRACKS];
unsigned long trackDurationsMs[MAX_TRACKS];
int trackCount = 0;
int currentTrackIndex = 0;
unsigned long trackStartMillis = 0;

String currentAlbumName = "";  // last-seen input_select state, used to detect changes
String currentAlbumSlug = "";
String currentArtist = "";
String currentAlbumTitle = "";

unsigned long lastPollMillis = 0;

// Home Assistant entity this sketch publishes the current track to. Created
// on the fly by the REST API (no HA configuration needed); read by the
// Discord presence companion in desktop-presence/. Kept here rather than in
// config.h so existing config.h files keep working.
static const char* NOW_PLAYING_ENTITY = "sensor.cd_frame_now_playing";
bool finishedReported = false;  // so "finished" is only posted once per album

// ---------------------------------------------------------------------------
// Cover art — 122x122, matches GxEPD2 raw format written by save_epd_raw()
// in the Python cataloging script: MSB-first packed 1-bit, no header.
// ---------------------------------------------------------------------------
#define COVER_WIDTH 122
#define COVER_HEIGHT 122
#define COVER_ROW_BYTES ((COVER_WIDTH + 7) / 8)
#define COVER_BUFFER_SIZE (COVER_ROW_BYTES * COVER_HEIGHT)

uint8_t coverBuffer[COVER_BUFFER_SIZE];
bool coverLoaded = false;

bool loadCoverArt(String slug) {
  String url = String(HA_HOST) + "/local/albums/" + slug + "/cover.raw";
  HTTPClient http;
  http.begin(url);
  http.addHeader("Authorization", String("Bearer ") + HA_TOKEN);
  int code = http.GET();
  if (code != 200) {
    Serial.printf("Cover fetch failed, code=%d\n", code);
    http.end();
    return false;
  }

  WiFiClient* stream = http.getStreamPtr();
  int bytesRead = 0;
  unsigned long timeout = millis() + 5000;
  while (bytesRead < COVER_BUFFER_SIZE && millis() < timeout) {
    if (stream->available()) {
      bytesRead += stream->readBytes(coverBuffer + bytesRead, COVER_BUFFER_SIZE - bytesRead);
    }
  }
  http.end();

  if (bytesRead != COVER_BUFFER_SIZE) {
    Serial.printf("Only read %d of %d cover bytes\n", bytesRead, COVER_BUFFER_SIZE);
    return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Display helpers — unchanged from the working prototype
// ---------------------------------------------------------------------------
void InitialiseDisplay() {
  display.init(115200, true, 2, false);
  SPI.end();
  SPI.begin(EPD_SCK, EPD_MISO, EPD_MOSI, EPD_CS);
  display.setRotation(1);
  u8g2Fonts.begin(display);
  u8g2Fonts.setFontMode(1);
  u8g2Fonts.setFontDirection(0);
  u8g2Fonts.setForegroundColor(GxEPD_BLACK);
  u8g2Fonts.setBackgroundColor(GxEPD_WHITE);
  u8g2Fonts.setFont(u8g2_font_helvB10_tf);
  display.fillScreen(GxEPD_WHITE);
  display.setFullWindow();
}

void wakeDisplay() {
  // Re-init after hibernate() before drawing again — the standard GxEPD2
  // power-cycling pattern (init -> draw -> display -> hibernate, repeat).
  display.init(115200, true, 2, false);
  SPI.begin(EPD_SCK, EPD_MISO, EPD_MOSI, EPD_CS);
}

void drawString(int x, int y, String text) {
  int16_t x1, y1;
  uint16_t w, h;
  display.setTextWrap(false);
  display.getTextBounds(text, x, y, &x1, &y1, &w, &h);
  u8g2Fonts.setCursor(x, y + h);
  u8g2Fonts.print(text);
}

// ---------------------------------------------------------------------------
// HTTP helper
// ---------------------------------------------------------------------------
String httpGet(String url, bool withAuth) {
  HTTPClient http;
  http.begin(url);
  if (withAuth) {
    http.addHeader("Authorization", String("Bearer ") + HA_TOKEN);
  }
  int code = http.GET();
  String body = "";
  if (code == 200) {
    body = http.getString();
  } else {
    Serial.printf("GET %s failed, code=%d\n", url.c_str(), code);
  }
  http.end();
  return body;
}

// ---------------------------------------------------------------------------
// Publish what the display is showing to Home Assistant. Called exactly once
// per track start (and once at the end of the album), so the entity's
// last_updated timestamp is the moment the track began. Best effort: a
// failure only costs a log line and at most the short timeout below.
// state is "playing" or "finished".
// ---------------------------------------------------------------------------
void reportNowPlaying(const char* state) {
  if (trackCount == 0 || WiFi.status() != WL_CONNECTED) return;

  DynamicJsonDocument doc(2048);
  doc["state"] = state;
  JsonObject attrs = doc.createNestedObject("attributes");
  attrs["friendly_name"] = "CD Frame Now Playing";
  attrs["album_name"] = currentAlbumName;
  attrs["slug"] = currentAlbumSlug;
  attrs["artist"] = currentArtist;
  attrs["album"] = currentAlbumTitle;
  attrs["track_number"] = currentTrackIndex + 1;
  attrs["track_count"] = trackCount;
  attrs["track_title"] = trackTitles[currentTrackIndex];
  attrs["track_duration_ms"] = trackDurationsMs[currentTrackIndex];

  String body;
  serializeJson(doc, body);

  HTTPClient http;
  http.begin(String(HA_HOST) + "/api/states/" + NOW_PLAYING_ENTITY);
  http.setTimeout(3000);
  http.addHeader("Authorization", String("Bearer ") + HA_TOKEN);
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(body);
  if (code != 200 && code != 201) {
    Serial.printf("Now-playing report failed, code=%d\n", code);
  }
  http.end();
}

String formatMinSec(unsigned long ms) {
  unsigned long totalSec = ms / 1000;
  unsigned long m = totalSec / 60;
  unsigned long s = totalSec % 60;
  char buf[8];
  snprintf(buf, sizeof(buf), "%lu:%02lu", m, s);
  return String(buf);
}

// ---------------------------------------------------------------------------
// Album loading
// ---------------------------------------------------------------------------
bool resolveSlugFromIndex(String albumName, String &slugOut) {
  String indexJson = httpGet(String(HA_HOST) + "/local/albums/index.json", true);
  if (indexJson.length() == 0) return false;

  DynamicJsonDocument doc(4096);
  DeserializationError err = deserializeJson(doc, indexJson);
  if (err) {
    Serial.print("index.json parse failed: ");
    Serial.println(err.c_str());
    return false;
  }

  for (JsonObject album : doc["albums"].as<JsonArray>()) {
    const char* name = album["name"];
    if (name && albumName == String(name)) {
      const char* slug = album["slug"];
      if (slug) {
        slugOut = String(slug);
        return true;
      }
    }
  }
  return false;
}

bool loadTracklist(String slug) {
  String url = String(HA_HOST) + "/local/albums/" + slug + "/tracks.json";
  String tracksJson = httpGet(url, true);
  if (tracksJson.length() == 0) return false;

  DynamicJsonDocument doc(8192);
  DeserializationError err = deserializeJson(doc, tracksJson);
  if (err) {
    Serial.print("tracks.json parse failed: ");
    Serial.println(err.c_str());
    return false;
  }

  const char* artist = doc["artist"];
  const char* albumTitle = doc["album"];
  currentArtist = artist ? String(artist) : "";
  currentAlbumTitle = albumTitle ? String(albumTitle) : "";

  trackCount = 0;
  for (JsonObject track : doc["tracks"].as<JsonArray>()) {
    if (trackCount >= MAX_TRACKS) break;
    const char* title = track["title"];
    trackTitles[trackCount] = title ? String(title) : "";
    trackDurationsMs[trackCount] = track["duration_ms"] | 180000; // fallback: 3 min if missing
    trackCount++;
  }
  return trackCount > 0;
}

void loadAlbum(String albumName) {
  Serial.println("Loading album: " + albumName);
  String slug;
  if (!resolveSlugFromIndex(albumName, slug)) {
    Serial.println("Could not resolve album to a slug via index.json");
    return;
  }
  currentAlbumSlug = slug;
  if (!loadTracklist(slug)) {
    Serial.println("Could not load tracklist");
    return;
  }
  coverLoaded = loadCoverArt(slug);
  currentAlbumName = albumName;
  currentTrackIndex = 0;
  trackStartMillis = millis();
  finishedReported = false;
  reportNowPlaying("playing");
  renderCurrentTrack();
}

// ---------------------------------------------------------------------------
// Display rendering
// ---------------------------------------------------------------------------
void renderCurrentTrack() {
  display.setFullWindow();
  display.fillScreen(GxEPD_WHITE);

  if (coverLoaded) {
    display.drawBitmap(0, 0, coverBuffer, COVER_WIDTH, COVER_HEIGHT, GxEPD_BLACK);
  }

  int textX = COVER_WIDTH + 8; // start just right of the cover

  u8g2Fonts.setFont(u8g2_font_helvB10_tf);
  drawString(textX, 8, currentArtist);
  drawString(textX, 28, currentAlbumTitle);

  u8g2Fonts.setFont(u8g2_font_helvB14_tf);
  drawString(textX, 55, trackTitles[currentTrackIndex]); // long titles overflow — same known limitation as the breadboard prototype

  display.drawRect(0, 0, COVER_WIDTH, COVER_HEIGHT, GxEPD_BLACK); // frame around the cover only — its top/left/bottom already sit flush on the screen edge
  display.display(false); // full refresh — track changes are minutes apart, well within the panel's refresh guidance
  display.hibernate();
}

// ---------------------------------------------------------------------------
// Advance to the next track once its duration has elapsed. Only redraws
// at track boundaries, never on a per-second tick.
// ---------------------------------------------------------------------------
void tickTrackTimer() {
  if (trackCount == 0) return;
  unsigned long elapsed = millis() - trackStartMillis;
  if (elapsed >= trackDurationsMs[currentTrackIndex]) {
    currentTrackIndex++;
    if (currentTrackIndex >= trackCount) {
      currentTrackIndex = trackCount - 1; // stop at the last track rather than wrap
      if (!finishedReported) {
        finishedReported = true;
        reportNowPlaying("finished");
      }
      return;
    }
    trackStartMillis = millis();
    reportNowPlaying("playing");
    wakeDisplay();
    renderCurrentTrack();
  }
}

// ---------------------------------------------------------------------------
// Poll HA for a change in which album is loaded
// ---------------------------------------------------------------------------
void checkForAlbumChange() {
  String url = String(HA_HOST) + "/api/states/" + ENTITY_ID;
  String stateJson = httpGet(url, true);
  if (stateJson.length() == 0) {
    wakeDisplay();
    display.setFullWindow();
    display.fillScreen(GxEPD_WHITE);
    drawString(10, 8, "Could not reach Home Assistant");
    drawString(10, 30, "Check HA_HOST / HA_TOKEN / network");
    display.drawRect(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT, GxEPD_BLACK);
    display.display(false);
    display.hibernate();
    return;
  }

  DynamicJsonDocument doc(1024);
  DeserializationError err = deserializeJson(doc, stateJson);
  if (err) {
    Serial.print("state parse failed: ");
    Serial.println(err.c_str());
    return;
  }

  const char* state = doc["state"];
  String newAlbumName = state ? String(state) : "";
  if (newAlbumName.length() == 0 || newAlbumName == "unknown" || newAlbumName == "unavailable") return;

  if (newAlbumName != currentAlbumName) {
    wakeDisplay();
    loadAlbum(newAlbumName);
  }
}

// ---------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  setCpuFrequencyMhz(CPU_FREQUENCY_MHZ);

  InitialiseDisplay();
  drawString(10, 10, "Connecting to WiFi...");
  display.display(false);

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi connected: " + WiFi.localIP().toString());

  drawString(10, 30, "WiFi OK: " + WiFi.localIP().toString());
  drawString(10, 50, "Checking Home Assistant...");
  display.display(false);

  checkForAlbumChange(); // initial load on boot
  lastPollMillis = millis();
}

void loop() {
  if (millis() - lastPollMillis >= POLL_INTERVAL_MS) {
    checkForAlbumChange();
    lastPollMillis = millis();
  }
  tickTrackTimer();
  delay(1000); // once-a-second granularity is plenty for a minutes-long track timer
}

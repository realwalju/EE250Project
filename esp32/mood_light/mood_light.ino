// Mood light: ESP32-S3 (YD-ESP32-S3) using its built-in RGB LED.
//
// Subscribes to desk/leds/effect and plays effects locally, so animations
// stay smooth even if Wi-Fi lags. The Pi only sends short commands like
// {"effect":"victory"}.
//
// Base modes (stay on):      lobby, match, study, off
// One-shot effects (then return to base mode): elim, knocked, eliminated, victory, tilt
//
// AI-assisted: initial version generated with Claude (Anthropic), then
// reviewed and tested by the team.
//
// Requires: Arduino-ESP32 core 3.x (for rgbLedWrite), PubSubClient.

#include <WiFi.h>
#include <PubSubClient.h>
#include <math.h>
#include "secrets.h"  // copy secrets_example.h -> secrets.h

// YD-ESP32-S3 built-in WS2812 RGB LED. On some board revisions you must
// bridge the "RGB" solder pad for it to work.
const int   PIN_RGB    = 48;
const float BRIGHTNESS = 0.6f;  // 0..1, the built-in LED is very bright up close

const char* CLIENT_ID = "desk-mood-light";
const char* T_EFFECT  = "desk/leds/effect";

WiFiClient net;
PubSubClient mqtt(net);

enum Base { BASE_LOBBY, BASE_MATCH, BASE_STUDY, BASE_OFF };
enum OneShot { FX_NONE, FX_ELIM, FX_KNOCKED, FX_ELIMINATED, FX_VICTORY, FX_TILT };

Base     baseMode = BASE_LOBBY;
OneShot  effect   = FX_NONE;
uint32_t effectStart = 0;

uint32_t lastRenderMs = 0;
uint32_t lastMqttTryMs = 0;

// ---------- Helpers ----------
// Pull a string value out of a tiny JSON payload, e.g. {"effect":"victory"}.
String jsonString(const String& json, const char* key) {
  String k = String("\"") + key + "\"";
  int i = json.indexOf(k);
  if (i < 0) return "";
  i = json.indexOf(':', i + k.length());
  if (i < 0) return "";
  int s = json.indexOf('"', i + 1);
  int e = (s < 0) ? -1 : json.indexOf('"', s + 1);
  if (s < 0 || e < 0) return "";
  return json.substring(s + 1, e);
}

void setRGB(float r, float g, float b) {  // each 0..1
  rgbLedWrite(PIN_RGB,
              (uint8_t)(constrain(r, 0.0f, 1.0f) * 255 * BRIGHTNESS),
              (uint8_t)(constrain(g, 0.0f, 1.0f) * 255 * BRIGHTNESS),
              (uint8_t)(constrain(b, 0.0f, 1.0f) * 255 * BRIGHTNESS));
}

// Smooth 0..1..0 wave with the given period.
float breathe(uint32_t now, uint32_t periodMs) {
  float phase = (now % periodMs) / (float)periodMs;
  return (1.0f - cosf(phase * 2.0f * PI)) / 2.0f;
}

// ---------- Commands ----------
void applyCommand(const String& name) {
  uint32_t now = millis();
  if      (name == "lobby")      baseMode = BASE_LOBBY;
  else if (name == "match")      baseMode = BASE_MATCH;
  else if (name == "study")      baseMode = BASE_STUDY;
  else if (name == "off")        baseMode = BASE_OFF;
  else if (name == "elim")       { effect = FX_ELIM;       effectStart = now; }
  else if (name == "knocked")    { effect = FX_KNOCKED;    effectStart = now; }
  else if (name == "eliminated") { effect = FX_ELIMINATED; effectStart = now; }
  else if (name == "victory")    { effect = FX_VICTORY;    effectStart = now; }
  else if (name == "tilt")       { effect = FX_TILT;       effectStart = now; }
  else Serial.printf("unknown effect: %s\n", name.c_str());
}

void onMqttMessage(char* topic, byte* payload, unsigned int len) {
  String body;
  body.reserve(len);
  for (unsigned int i = 0; i < len; i++) body += (char)payload[i];
  String name = jsonString(body, "effect");
  Serial.printf("effect: %s\n", name.c_str());
  if (name.length()) applyCommand(name);
}

void maintainConnections() {
  if (WiFi.status() != WL_CONNECTED) return;
  if (mqtt.connected()) return;
  if (millis() - lastMqttTryMs < 2000) return;
  lastMqttTryMs = millis();
  Serial.print("MQTT connecting... ");
  if (mqtt.connect(CLIENT_ID)) {
    Serial.println("ok");
    mqtt.subscribe(T_EFFECT);
  } else {
    Serial.printf("failed (state %d)\n", mqtt.state());
  }
}

// ---------- Rendering ----------
// Returns true while a one-shot effect is still playing.
bool renderOneShot(uint32_t now) {
  uint32_t t = now - effectStart;
  switch (effect) {
    case FX_ELIM: {  // quick white pulse
      if (t > 400) return false;
      float b = sinf(PI * t / 400.0f);
      setRGB(b, b, b);
      return true;
    }
    case FX_KNOCKED: {  // three red flashes
      if (t > 900) return false;
      setRGB(((t / 150) % 2 == 0) ? 1 : 0, 0, 0);
      return true;
    }
    case FX_ELIMINATED: {  // red fading out
      if (t > 1500) return false;
      setRGB(1.0f - t / 1500.0f, 0, 0);
      return true;
    }
    case FX_VICTORY: {  // gold glow pulsing for 5 s
      if (t > 5000) return false;
      float b = 0.4f + 0.6f * breathe(now, 1000);
      setRGB(1.0f * b, 0.65f * b, 0.0f);
      return true;
    }
    case FX_TILT: {  // orange flicker for 3 s
      if (t > 3000) return false;
      float b = random(30, 100) / 100.0f;
      setRGB(1.0f * b, 0.35f * b, 0.0f);
      return true;
    }
    default:
      return false;
  }
}

void renderBase(uint32_t now) {
  switch (baseMode) {
    case BASE_LOBBY: {  // slow purple breathing
      float b = 0.1f + 0.5f * breathe(now, 4000);
      setRGB(0.6f * b, 0.0f, 1.0f * b);
      break;
    }
    case BASE_MATCH:  setRGB(0.0f, 0.15f, 0.12f); break;  // dim teal while playing
    case BASE_STUDY:  setRGB(0.0f, 0.2f, 0.6f);   break;  // calm blue
    case BASE_OFF:    setRGB(0, 0, 0);            break;
  }
}

void render() {
  uint32_t now = millis();
  if (!mqtt.connected()) {  // dim white blink = not connected
    float b = ((now / 500) % 2) ? 0.15f : 0.0f;
    setRGB(b, b, b);
    return;
  }
  if (effect != FX_NONE && !renderOneShot(now)) effect = FX_NONE;
  if (effect == FX_NONE) renderBase(now);
}

// ---------- Setup / loop ----------
void setup() {
  Serial.begin(115200);
  setRGB(0, 0, 0);

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMqttMessage);
}

void loop() {
  maintainConnections();
  mqtt.loop();

  if (millis() - lastRenderMs >= 20) {  // ~50 fps
    lastRenderMs = millis();
    render();
  }
}

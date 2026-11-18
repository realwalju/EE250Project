// Desk sensor board: ESP32-S3 (YD-ESP32-S3) on our custom PCB.
//
// Reads the button, light sensor, thermistor and vibration switch, publishes
// them over MQTT, and drives the status LED.
//
// AI-assisted: initial version generated with Claude (Anthropic), then
// reviewed and tested by the team.
//
// Requires: Arduino-ESP32 core 3.x, PubSubClient, OneButton.

#include <WiFi.h>
#include <PubSubClient.h>
#include <OneButton.h>
#include <math.h>
#include "secrets.h"  // copy secrets_example.h -> secrets.h

// ---------- Pins (match our KiCad PCB) ----------
const int PIN_LIGHT = 4;  // 3V3 - LDR - GPIO4 - 10k - GND  (ADC1)
const int PIN_TEMP  = 5;  // 3V3 - NTC - GPIO5 - 10k - GND  (ADC1)
const int PIN_VIB   = 6;  // vibration switch to GND, 10k pull-up -> LOW when shaken
const int PIN_BTN   = 7;  // push button to GND, 10k pull-up -> LOW when pressed
const int PIN_LED   = 8;  // LED through 1k to GND -> HIGH = on

// ---------- Thermistor (check your part's datasheet) ----------
const float VCC_MV   = 3300.0f;
const float R_FIXED  = 10000.0f;  // the 10k resistor to GND
const float NTC_R0   = 10000.0f;  // NTC resistance at 25 C
const float NTC_BETA = 3950.0f;   // NTC Beta value
const float T0_K     = 298.15f;   // 25 C in Kelvin

// ---------- Timing ----------
const uint32_t SENSOR_PERIOD_MS = 1000;  // publish sensors once a second
const uint32_t VIB_DEBOUNCE_US  = 5000;  // ignore switch chatter < 5 ms
const float    EMA_ALPHA        = 0.2f;  // smoothing for analog readings

// ---------- MQTT ----------
const char* CLIENT_ID = "desk-sensor-board";
const char* T_SENSORS = "desk/sensors";
const char* T_BUTTON  = "desk/input/button";
const char* T_NOTIFY  = "desk/notify";

WiFiClient net;
PubSubClient mqtt(net);
OneButton button(PIN_BTN, true, false);  // active LOW; PCB already has a pull-up

// Vibration pulses are counted in an interrupt and read once per period.
volatile uint32_t vibPulses = 0;
volatile uint32_t lastVibUs = 0;
portMUX_TYPE vibMux = portMUX_INITIALIZER_UNLOCKED;

// LED state
uint32_t flashUntil = 0;      // short flash after a button press
bool     notifyPending = false;
uint32_t notifyStart = 0;

// Smoothed readings
float lightMvAvg = -1;
float tempMvAvg  = -1;

uint32_t lastSensorMs = 0;
uint32_t lastMqttTryMs = 0;

// ---------- Vibration interrupt ----------
void IRAM_ATTR onVibration() {
  uint32_t now = micros();
  portENTER_CRITICAL_ISR(&vibMux);
  if (now - lastVibUs > VIB_DEBOUNCE_US) {
    vibPulses++;
    lastVibUs = now;
  }
  portEXIT_CRITICAL_ISR(&vibMux);
}

uint32_t takeVibPulses() {
  portENTER_CRITICAL(&vibMux);
  uint32_t n = vibPulses;
  vibPulses = 0;
  portEXIT_CRITICAL(&vibMux);
  return n;
}

// ---------- Helpers ----------
float smooth(float avg, float sample) {
  return (avg < 0) ? sample : avg + EMA_ALPHA * (sample - avg);
}

// Convert the divider voltage to temperature with the Beta equation.
// Returns NAN if the reading is out of range (sensor missing or shorted).
float thermistorC(float mv) {
  if (mv < 10 || mv > VCC_MV - 10) return NAN;
  float rNtc = R_FIXED * (VCC_MV / mv - 1.0f);
  float invT = 1.0f / T0_K + logf(rNtc / NTC_R0) / NTC_BETA;
  return 1.0f / invT - 273.15f;
}

void publishButton(const char* id, const char* press) {
  flashUntil = millis() + 120;  // local feedback even if MQTT is down
  if (!mqtt.connected()) return;
  char msg[64];
  snprintf(msg, sizeof(msg), "{\"id\":\"%s\",\"press\":\"%s\"}", id, press);
  mqtt.publish(T_BUTTON, msg);
  Serial.printf("button: %s\n", msg);
}

// ---------- Button actions ----------
void onClick()       { publishButton("clip", "short"); }
void onDoubleClick() { notifyPending = false; publishButton("interact", "double"); }
void onLongPress()   { publishButton("mode", "long"); }

// ---------- MQTT ----------
void onMqttMessage(char* topic, byte* payload, unsigned int len) {
  String body;
  body.reserve(len);
  for (unsigned int i = 0; i < len; i++) body += (char)payload[i];
  if (String(topic) == T_NOTIFY) {
    // {"msg":1} -> new companion message waiting, {"msg":0} -> cleared
    notifyPending = body.indexOf("\"msg\":1") >= 0;
    notifyStart = millis();
  }
}

void maintainConnections() {
  if (WiFi.status() != WL_CONNECTED) return;  // WiFi auto-reconnects
  if (mqtt.connected()) return;
  if (millis() - lastMqttTryMs < 2000) return;  // don't block; retry every 2 s
  lastMqttTryMs = millis();
  Serial.print("MQTT connecting... ");
  if (mqtt.connect(CLIENT_ID)) {
    Serial.println("ok");
    mqtt.subscribe(T_NOTIFY);
  } else {
    Serial.printf("failed (state %d)\n", mqtt.state());
  }
}

// ---------- Status LED ----------
// flash > notification double-blink > disconnected fast blink > breathing
void updateLed() {
  uint32_t now = millis();
  int duty;
  if (now < flashUntil) {
    duty = 255;
  } else if (notifyPending) {
    uint32_t t = (now - notifyStart) % 2000;
    duty = (t < 120 || (t >= 240 && t < 360)) ? 255 : 0;
  } else if (!mqtt.connected()) {
    duty = ((now / 150) % 2) ? 255 : 0;
  } else {
    float phase = (now % 3000) / 3000.0f;
    float b = (1.0f - cosf(phase * 2.0f * PI)) / 2.0f;  // 0..1
    duty = 5 + (int)(b * b * 200);  // squared so it looks smoother to the eye
  }
  ledcWrite(PIN_LED, duty);
}

// ---------- Sensors ----------
void publishSensors() {
  float lightMv = analogReadMilliVolts(PIN_LIGHT);
  float tempMv  = analogReadMilliVolts(PIN_TEMP);
  lightMvAvg = smooth(lightMvAvg, lightMv);
  tempMvAvg  = smooth(tempMvAvg, tempMv);

  int   lightPct = constrain((int)(lightMvAvg / VCC_MV * 100.0f), 0, 100);  // brighter = higher
  float tempC    = thermistorC(tempMvAvg);
  uint32_t vib   = takeVibPulses();

  if (!mqtt.connected()) return;

  char tempStr[12];
  if (isnan(tempC)) strcpy(tempStr, "null");
  else snprintf(tempStr, sizeof(tempStr), "%.1f", tempC);

  char msg[128];
  snprintf(msg, sizeof(msg),
           "{\"light_mv\":%d,\"light_pct\":%d,\"temp_c\":%s,\"vib\":%lu}",
           (int)lightMvAvg, lightPct, tempStr, (unsigned long)vib);
  mqtt.publish(T_SENSORS, msg);
  Serial.println(msg);
}

// ---------- Setup / loop ----------
void setup() {
  Serial.begin(115200);

  pinMode(PIN_VIB, INPUT);  // PCB has a 10k pull-up
  attachInterrupt(digitalPinToInterrupt(PIN_VIB), onVibration, FALLING);

  ledcAttach(PIN_LED, 5000, 8);  // 5 kHz PWM, 8-bit (core 3.x API)
  analogReadResolution(12);

  button.attachClick(onClick);
  button.attachDoubleClick(onDoubleClick);
  button.attachLongPressStart(onLongPress);

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMqttMessage);
}

void loop() {
  button.tick();
  maintainConnections();
  mqtt.loop();
  updateLed();

  if (millis() - lastSensorMs >= SENSOR_PERIOD_MS) {
    lastSensorMs = millis();
    publishSensors();
  }
}

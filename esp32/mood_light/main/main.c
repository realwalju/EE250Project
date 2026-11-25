// Mood light: ESP32-S3 (YD-ESP32-S3) using its built-in RGB LED, ESP-IDF v5/v6.
//
// Subscribes to desk/leds/effect and plays effects locally, so animations
// stay smooth even if Wi-Fi lags. The Pi only sends short commands like
// {"effect":"victory"}.
//
//   Base modes (stay on):      lobby, match, study, off
//   One-shot effects (then back to the base mode): elim, knocked, eliminated, victory, tilt,
//                                                  clip, ping
//
// AI-assisted: initial version generated with Claude (Anthropic), then
// reviewed and tested by the team.

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "desk_net.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "led_strip.h"
#include "secrets.h"  // copy secrets_example.h -> secrets.h

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// YD-ESP32-S3 built-in WS2812 RGB LED. On some board revisions you must
// bridge the "RGB" solder pad for it to work.
#define PIN_RGB    48
#define BRIGHTNESS 0.6f  // 0..1, the built-in LED is very bright up close

#define CLIENT_ID "desk-mood-light"
#define T_EFFECT  "desk/leds/effect"

static const char *TAG = "mood_light";

typedef enum { BASE_LOBBY, BASE_MATCH, BASE_STUDY, BASE_OFF } base_mode_t;
typedef enum { FX_NONE, FX_ELIM, FX_KNOCKED, FX_ELIMINATED, FX_VICTORY, FX_TILT, FX_CLIP, FX_PING } effect_t;

// Written by the MQTT task, read by the render task
static volatile base_mode_t s_base = BASE_LOBBY;
static volatile effect_t s_effect = FX_NONE;
static volatile int64_t s_effect_start_ms;

static led_strip_handle_t s_strip;

// ---------- Helpers ----------
static float clamp01(float v)
{
    return v < 0 ? 0 : (v > 1 ? 1 : v);
}

static void set_rgb(float r, float g, float b)  // each 0..1
{
    led_strip_set_pixel(s_strip, 0,
                        (uint32_t)(clamp01(r) * 255 * BRIGHTNESS),
                        (uint32_t)(clamp01(g) * 255 * BRIGHTNESS),
                        (uint32_t)(clamp01(b) * 255 * BRIGHTNESS));
    led_strip_refresh(s_strip);
}

// Smooth 0..1..0 wave with the given period
static float breathe(int64_t now_ms, int period_ms)
{
    float phase = (now_ms % period_ms) / (float)period_ms;
    return (1.0f - cosf(phase * 2.0f * (float)M_PI)) / 2.0f;
}

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

// ---------- Commands ----------
static void apply_command(const char *name)
{
    int64_t t = now_ms();
    if      (!strcmp(name, "lobby"))      s_base = BASE_LOBBY;
    else if (!strcmp(name, "match"))      s_base = BASE_MATCH;
    else if (!strcmp(name, "study"))      s_base = BASE_STUDY;
    else if (!strcmp(name, "off"))        s_base = BASE_OFF;
    else if (!strcmp(name, "elim"))       { s_effect_start_ms = t; s_effect = FX_ELIM; }
    else if (!strcmp(name, "knocked"))    { s_effect_start_ms = t; s_effect = FX_KNOCKED; }
    else if (!strcmp(name, "eliminated")) { s_effect_start_ms = t; s_effect = FX_ELIMINATED; }
    else if (!strcmp(name, "victory"))    { s_effect_start_ms = t; s_effect = FX_VICTORY; }
    else if (!strcmp(name, "tilt"))       { s_effect_start_ms = t; s_effect = FX_TILT; }
    else if (!strcmp(name, "clip"))       { s_effect_start_ms = t; s_effect = FX_CLIP; }
    else if (!strcmp(name, "ping"))       { s_effect_start_ms = t; s_effect = FX_PING; }
    else ESP_LOGW(TAG, "unknown effect: %s", name);
}

// Pulls the value out of {"effect":"victory"}
static void on_message(const char *topic, int topic_len, const char *data, int data_len)
{
    if (!desk_net_topic_is(topic, topic_len, T_EFFECT)) {
        return;
    }
    char buf[96];
    int n = data_len < (int)sizeof(buf) - 1 ? data_len : (int)sizeof(buf) - 1;
    memcpy(buf, data, n);
    buf[n] = '\0';

    const char *key = strstr(buf, "\"effect\"");
    const char *colon = key ? strchr(key, ':') : NULL;
    const char *start = colon ? strchr(colon, '"') : NULL;
    const char *end = start ? strchr(start + 1, '"') : NULL;
    if (!end) {
        ESP_LOGW(TAG, "bad payload: %s", buf);
        return;
    }
    char name[24];
    int len = end - start - 1;
    if (len >= (int)sizeof(name)) {
        len = sizeof(name) - 1;
    }
    memcpy(name, start + 1, len);
    name[len] = '\0';
    ESP_LOGI(TAG, "effect: %s", name);
    apply_command(name);
}

// ---------- Rendering ----------
// Returns true while a one-shot effect is still playing
static bool render_one_shot(int64_t now)
{
    int64_t t = now - s_effect_start_ms;
    switch (s_effect) {
    case FX_ELIM: {  // quick white pulse
        if (t > 400) return false;
        float b = sinf((float)M_PI * t / 400.0f);
        set_rgb(b, b, b);
        return true;
    }
    case FX_KNOCKED:  // three red flashes
        if (t > 900) return false;
        set_rgb(((t / 150) % 2 == 0) ? 1 : 0, 0, 0);
        return true;
    case FX_ELIMINATED:  // red fading out
        if (t > 1500) return false;
        set_rgb(1.0f - t / 1500.0f, 0, 0);
        return true;
    case FX_VICTORY: {  // gold glow pulsing for 5 s
        if (t > 5000) return false;
        float b = 0.4f + 0.6f * breathe(now, 1000);
        set_rgb(1.0f * b, 0.65f * b, 0.0f);
        return true;
    }
    case FX_TILT: {  // orange flicker for 3 s
        if (t > 3000) return false;
        float b = (30 + esp_random() % 70) / 100.0f;
        set_rgb(1.0f * b, 0.35f * b, 0.0f);
        return true;
    }
    case FX_CLIP:  // camera flash: two white flashes, then a cyan glow fading out
        if (t > 1400) return false;
        if (t < 120 || (t >= 240 && t < 360)) {
            set_rgb(1, 1, 1);
        } else if (t < 360) {
            set_rgb(0, 0, 0);
        } else {
            float b = 1.0f - (t - 360) / 1040.0f;
            set_rgb(0.0f, 0.8f * b, 1.0f * b);
        }
        return true;
    case FX_PING: {  // short cyan blip (interact button)
        if (t > 300) return false;
        float b = sinf((float)M_PI * t / 300.0f);
        set_rgb(0.0f, 0.8f * b, 1.0f * b);
        return true;
    }
    default:
        return false;
    }
}

static void render_base(int64_t now)
{
    switch (s_base) {
    case BASE_LOBBY: {  // slow purple breathing
        float b = 0.1f + 0.5f * breathe(now, 4000);
        set_rgb(0.6f * b, 0.0f, 1.0f * b);
        break;
    }
    case BASE_MATCH: set_rgb(0.0f, 0.15f, 0.12f); break;  // dim teal while playing
    case BASE_STUDY: set_rgb(0.0f, 0.2f, 0.6f); break;    // calm blue
    case BASE_OFF:   set_rgb(0, 0, 0); break;
    }
}

static void render_task(void *arg)
{
    while (true) {
        int64_t now = now_ms();
        if (!desk_net_connected()) {  // dim white blink = not connected
            float b = ((now / 500) % 2) ? 0.15f : 0.0f;
            set_rgb(b, b, b);
        } else {
            if (s_effect != FX_NONE && !render_one_shot(now)) {
                s_effect = FX_NONE;
            }
            if (s_effect == FX_NONE) {
                render_base(now);
            }
        }
        vTaskDelay(pdMS_TO_TICKS(20));  // ~50 fps
    }
}

// ---------- Setup ----------
void app_main(void)
{
    led_strip_config_t strip_cfg = {
        .strip_gpio_num = PIN_RGB,
        .max_leds = 1,
        .led_model = LED_MODEL_WS2812,
        .color_component_format = LED_STRIP_COLOR_COMPONENT_FMT_GRB,
        .flags.invert_out = false,
    };
    led_strip_rmt_config_t rmt_cfg = {
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = 10 * 1000 * 1000,  // 10 MHz
        .flags.with_dma = false,
    };
    ESP_ERROR_CHECK(led_strip_new_rmt_device(&strip_cfg, &rmt_cfg, &s_strip));
    led_strip_clear(s_strip);

    xTaskCreate(render_task, "render", 3072, NULL, 5, NULL);

    static const char *const topics[] = {T_EFFECT};
    desk_net_start(WIFI_SSID, WIFI_PASS, MQTT_HOST, MQTT_PORT, CLIENT_ID, topics, 1, on_message);
}

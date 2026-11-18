// Desk sensor board: ESP32-S3 (YD-ESP32-S3) on our custom PCB, ESP-IDF v5.x.
//
// Reads the button, light sensor, thermistor and vibration switch, publishes
// them over MQTT, and drives the status LED.
//
//   Button  (GPIO 7): short press = clip, double press = interact, long press = mode
//   LED     (GPIO 8): breathing = connected, fast blink = disconnected,
//                     double blink = new companion message, flash = button seen
//   Sensors: published to desk/sensors once a second
//
// AI-assisted: initial version generated with Claude (Anthropic), then
// reviewed and tested by the team.

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "desk_net.h"
#include "driver/gpio.h"
#include "driver/ledc.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "secrets.h"  // copy secrets_example.h -> secrets.h

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// ---------- Pins (match our KiCad PCB) ----------
#define PIN_LIGHT GPIO_NUM_4  // 3V3 - LDR - GPIO4 - 10k - GND  (ADC1)
#define PIN_TEMP  GPIO_NUM_5  // 3V3 - NTC - GPIO5 - 10k - GND  (ADC1)
#define PIN_VIB   GPIO_NUM_6  // vibration switch to GND, 10k pull-up -> LOW when shaken
#define PIN_BTN   GPIO_NUM_7  // push button to GND, 10k pull-up -> LOW when pressed
#define PIN_LED   GPIO_NUM_8  // LED through 1k to GND -> HIGH = on

// ---------- Thermistor (check your part's datasheet) ----------
#define VCC_MV   3300.0f
#define R_FIXED  10000.0f  // the 10k resistor to GND
#define NTC_R0   10000.0f  // NTC resistance at 25 C
#define NTC_BETA 3950.0f   // NTC Beta value
#define T0_K     298.15f   // 25 C in Kelvin

// ---------- Timing ----------
#define SENSOR_PERIOD_MS  1000
#define VIB_DEBOUNCE_US   5000    // ignore switch chatter < 5 ms
#define EMA_ALPHA         0.2f    // smoothing for analog readings
#define BTN_DEBOUNCE_US   30000   // a press must be stable for 30 ms
#define BTN_DOUBLE_US     300000  // second press within 300 ms = double press
#define BTN_LONG_US       800000  // held 800 ms = long press

// ---------- MQTT ----------
#define CLIENT_ID "desk-sensor-board"
#define T_SENSORS "desk/sensors"
#define T_BUTTON  "desk/input/button"
#define T_NOTIFY  "desk/notify"

static const char *TAG = "sensor_board";

// LED state, shared between tasks
static volatile int64_t s_flash_until_us;
static volatile bool s_notify_pending;
static volatile int64_t s_notify_start_us;

// Vibration pulses are counted in an interrupt and read once per period
static volatile uint32_t s_vib_pulses;
static volatile int64_t s_last_vib_us;
static portMUX_TYPE s_vib_mux = portMUX_INITIALIZER_UNLOCKED;

static adc_oneshot_unit_handle_t s_adc;
static adc_cali_handle_t s_cali;
static adc_channel_t s_ch_light, s_ch_temp;

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 1, 0)
#define ADC_ATTEN ADC_ATTEN_DB_12
#else
#define ADC_ATTEN ADC_ATTEN_DB_11
#endif

// ---------- Vibration interrupt ----------
static void IRAM_ATTR vib_isr(void *arg)
{
    int64_t now = esp_timer_get_time();
    portENTER_CRITICAL_ISR(&s_vib_mux);
    if (now - s_last_vib_us > VIB_DEBOUNCE_US) {
        s_vib_pulses++;
        s_last_vib_us = now;
    }
    portEXIT_CRITICAL_ISR(&s_vib_mux);
}

static uint32_t take_vib_pulses(void)
{
    portENTER_CRITICAL(&s_vib_mux);
    uint32_t n = s_vib_pulses;
    s_vib_pulses = 0;
    portEXIT_CRITICAL(&s_vib_mux);
    return n;
}

// ---------- MQTT messages in ----------
static void on_message(const char *topic, int topic_len, const char *data, int data_len)
{
    if (!desk_net_topic_is(topic, topic_len, T_NOTIFY)) {
        return;
    }
    // {"msg":1} -> new companion message waiting, {"msg":0} -> cleared
    char buf[64];
    int n = data_len < (int)sizeof(buf) - 1 ? data_len : (int)sizeof(buf) - 1;
    memcpy(buf, data, n);
    buf[n] = '\0';
    s_notify_pending = strstr(buf, "\"msg\":1") != NULL;
    s_notify_start_us = esp_timer_get_time();
}

// ---------- Button ----------
static void publish_button(const char *id, const char *press)
{
    s_flash_until_us = esp_timer_get_time() + 120000;  // local feedback even if offline
    char msg[64];
    snprintf(msg, sizeof(msg), "{\"id\":\"%s\",\"press\":\"%s\"}", id, press);
    desk_net_publish(T_BUTTON, msg);
    ESP_LOGI(TAG, "button: %s", msg);
}

// Polls the button every 10 ms: debounces it, then tells short, double and
// long presses apart.
static void button_task(void *arg)
{
    bool stable_pressed = false, raw_last = false, long_fired = false;
    int64_t raw_changed_us = 0, press_start_us = 0, last_release_us = 0;
    int clicks = 0;

    while (true) {
        int64_t now = esp_timer_get_time();
        bool raw = gpio_get_level(PIN_BTN) == 0;  // active LOW
        if (raw != raw_last) {
            raw_last = raw;
            raw_changed_us = now;
        }

        if (raw != stable_pressed && now - raw_changed_us >= BTN_DEBOUNCE_US) {
            stable_pressed = raw;
            if (stable_pressed) {          // pressed
                press_start_us = now;
                long_fired = false;
            } else if (!long_fired) {      // released after a short press
                clicks++;
                last_release_us = now;
            }
        }

        if (stable_pressed && !long_fired && now - press_start_us >= BTN_LONG_US) {
            long_fired = true;
            clicks = 0;
            publish_button("mode", "long");
        }

        if (!stable_pressed && clicks > 0 && now - last_release_us >= BTN_DOUBLE_US) {
            if (clicks == 1) {
                publish_button("clip", "short");
            } else {
                s_notify_pending = false;
                publish_button("interact", "double");
            }
            clicks = 0;
        }

        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

// ---------- Status LED ----------
// flash > notification double-blink > disconnected fast blink > breathing
static void led_task(void *arg)
{
    while (true) {
        int64_t now = esp_timer_get_time();
        int64_t ms = now / 1000;
        int duty;
        if (now < s_flash_until_us) {
            duty = 255;
        } else if (s_notify_pending) {
            int64_t t = ((now - s_notify_start_us) / 1000) % 2000;
            duty = (t < 120 || (t >= 240 && t < 360)) ? 255 : 0;
        } else if (!desk_net_connected()) {
            duty = ((ms / 150) % 2) ? 255 : 0;
        } else {
            float phase = (ms % 3000) / 3000.0f;
            float b = (1.0f - cosf(phase * 2.0f * (float)M_PI)) / 2.0f;  // 0..1
            duty = 5 + (int)(b * b * 200);  // squared so it looks smoother to the eye
        }
        ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, duty);
        ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

// ---------- Sensors ----------
static int read_mv(adc_channel_t ch)
{
    int raw = 0, mv = 0;
    if (adc_oneshot_read(s_adc, ch, &raw) != ESP_OK) {
        return -1;
    }
    if (s_cali && adc_cali_raw_to_voltage(s_cali, raw, &mv) == ESP_OK) {
        return mv;
    }
    return raw * 3300 / 4095;  // uncalibrated estimate
}

static float smooth(float avg, float sample)
{
    return (avg < 0) ? sample : avg + EMA_ALPHA * (sample - avg);
}

// Divider voltage -> temperature with the Beta equation. NAN if out of range.
static float thermistor_c(float mv)
{
    if (mv < 10 || mv > VCC_MV - 10) {
        return NAN;
    }
    float r_ntc = R_FIXED * (VCC_MV / mv - 1.0f);
    float inv_t = 1.0f / T0_K + logf(r_ntc / NTC_R0) / NTC_BETA;
    return 1.0f / inv_t - 273.15f;
}

// ---------- Setup ----------
static void hw_init(void)
{
    // Button: plain input (the PCB has a 10k pull-up; the internal one is a harmless backup)
    gpio_config_t btn = {
        .pin_bit_mask = 1ULL << PIN_BTN,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&btn));

    // Vibration switch: interrupt on each falling edge
    gpio_config_t vib = {
        .pin_bit_mask = 1ULL << PIN_VIB,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .intr_type = GPIO_INTR_NEGEDGE,
    };
    ESP_ERROR_CHECK(gpio_config(&vib));
    ESP_ERROR_CHECK(gpio_install_isr_service(0));
    ESP_ERROR_CHECK(gpio_isr_handler_add(PIN_VIB, vib_isr, NULL));

    // Status LED: 5 kHz, 8-bit PWM
    ledc_timer_config_t timer = {
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .duty_resolution = LEDC_TIMER_8_BIT,
        .timer_num = LEDC_TIMER_0,
        .freq_hz = 5000,
        .clk_cfg = LEDC_AUTO_CLK,
    };
    ESP_ERROR_CHECK(ledc_timer_config(&timer));
    ledc_channel_config_t channel = {
        .gpio_num = PIN_LED,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel = LEDC_CHANNEL_0,
        .timer_sel = LEDC_TIMER_0,
        .duty = 0,
        .hpoint = 0,
    };
    ESP_ERROR_CHECK(ledc_channel_config(&channel));

    // ADC1 for the light sensor and thermistor, with factory calibration
    adc_oneshot_unit_init_cfg_t unit_cfg = {.unit_id = ADC_UNIT_1};
    ESP_ERROR_CHECK(adc_oneshot_new_unit(&unit_cfg, &s_adc));
    adc_unit_t unit;
    ESP_ERROR_CHECK(adc_oneshot_io_to_channel(PIN_LIGHT, &unit, &s_ch_light));
    ESP_ERROR_CHECK(adc_oneshot_io_to_channel(PIN_TEMP, &unit, &s_ch_temp));
    adc_oneshot_chan_cfg_t chan_cfg = {.atten = ADC_ATTEN, .bitwidth = ADC_BITWIDTH_DEFAULT};
    ESP_ERROR_CHECK(adc_oneshot_config_channel(s_adc, s_ch_light, &chan_cfg));
    ESP_ERROR_CHECK(adc_oneshot_config_channel(s_adc, s_ch_temp, &chan_cfg));
    adc_cali_curve_fitting_config_t cali_cfg = {
        .unit_id = ADC_UNIT_1,
        .atten = ADC_ATTEN,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    if (adc_cali_create_scheme_curve_fitting(&cali_cfg, &s_cali) != ESP_OK) {
        s_cali = NULL;
        ESP_LOGW(TAG, "ADC calibration unavailable, using raw estimate");
    }
}

void app_main(void)
{
    hw_init();
    xTaskCreate(led_task, "led", 3072, NULL, 5, NULL);
    xTaskCreate(button_task, "button", 4096, NULL, 6, NULL);

    static const char *const topics[] = {T_NOTIFY};
    desk_net_start(WIFI_SSID, WIFI_PASS, MQTT_HOST, MQTT_PORT, CLIENT_ID, topics, 1, on_message);

    float light_avg = -1, temp_avg = -1;
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(SENSOR_PERIOD_MS));

        light_avg = smooth(light_avg, (float)read_mv(s_ch_light));
        temp_avg = smooth(temp_avg, (float)read_mv(s_ch_temp));
        int light_pct = (int)(light_avg / VCC_MV * 100.0f);  // brighter = higher
        light_pct = light_pct < 0 ? 0 : (light_pct > 100 ? 100 : light_pct);
        float temp_c = thermistor_c(temp_avg);
        uint32_t vib = take_vib_pulses();

        char temp_str[12];
        if (isnan(temp_c)) {
            strcpy(temp_str, "null");
        } else {
            snprintf(temp_str, sizeof(temp_str), "%.1f", temp_c);
        }
        char msg[128];
        snprintf(msg, sizeof(msg), "{\"light_mv\":%d,\"light_pct\":%d,\"temp_c\":%s,\"vib\":%lu}",
                 (int)light_avg, light_pct, temp_str, (unsigned long)vib);
        if (desk_net_publish(T_SENSORS, msg)) {
            ESP_LOGI(TAG, "%s", msg);
        }
    }
}

// Shared Wi-Fi + MQTT connection for both desk ESP32-S3 boards.
//
// AI-assisted: initial version generated with Claude (Anthropic), then
// reviewed and tested by the team.

#include "desk_net.h"

#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "mqtt_client.h"
#include "nvs_flash.h"

static const char *TAG = "desk_net";

static esp_mqtt_client_handle_t s_mqtt;
static volatile bool s_connected;
static const char *const *s_topics;
static int s_n_topics;
static desk_msg_cb s_on_message;

static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW(TAG, "Wi-Fi disconnected, retrying");
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "Wi-Fi connected, IP " IPSTR, IP2STR(&e->ip_info.ip));
    }
}

static void mqtt_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    esp_mqtt_event_handle_t e = (esp_mqtt_event_handle_t)data;
    switch ((esp_mqtt_event_id_t)id) {
    case MQTT_EVENT_CONNECTED:
        s_connected = true;
        ESP_LOGI(TAG, "MQTT connected");
        for (int i = 0; i < s_n_topics; i++) {
            esp_mqtt_client_subscribe(s_mqtt, s_topics[i], 0);
        }
        break;
    case MQTT_EVENT_DISCONNECTED:
        s_connected = false;
        ESP_LOGW(TAG, "MQTT disconnected (the client reconnects automatically)");
        break;
    case MQTT_EVENT_DATA:
        // Messages here are tiny, so they always arrive in one piece.
        if (s_on_message && e->topic_len > 0) {
            s_on_message(e->topic, e->topic_len, e->data, e->data_len);
        }
        break;
    default:
        break;
    }
}

void desk_net_start(const char *ssid, const char *pass, const char *host, int port,
                    const char *client_id, const char *const *topics, int n_topics,
                    desk_msg_cb on_message)
{
    s_topics = topics;
    s_n_topics = n_topics;
    s_on_message = on_message;

    // NVS is required by the Wi-Fi driver
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    // Wi-Fi station
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t init_cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event, NULL));

    wifi_config_t wifi_cfg = {0};
    strlcpy((char *)wifi_cfg.sta.ssid, ssid, sizeof(wifi_cfg.sta.ssid));
    strlcpy((char *)wifi_cfg.sta.password, pass, sizeof(wifi_cfg.sta.password));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    // MQTT client: keeps retrying until Wi-Fi and the broker are reachable
    static char uri[96];
    snprintf(uri, sizeof(uri), "mqtt://%s:%d", host, port);
    esp_mqtt_client_config_t mqtt_cfg = {
        .broker.address.uri = uri,
        .credentials.client_id = client_id,
    };
    s_mqtt = esp_mqtt_client_init(&mqtt_cfg);
    ESP_ERROR_CHECK(esp_mqtt_client_register_event(s_mqtt, ESP_EVENT_ANY_ID, mqtt_event, NULL));
    ESP_ERROR_CHECK(esp_mqtt_client_start(s_mqtt));
    ESP_LOGI(TAG, "connecting to Wi-Fi \"%s\" and broker %s", ssid, uri);
}

bool desk_net_connected(void)
{
    return s_connected;
}

bool desk_net_publish(const char *topic, const char *payload)
{
    if (!s_connected) {
        return false;
    }
    return esp_mqtt_client_publish(s_mqtt, topic, payload, 0, 0, 0) >= 0;
}

bool desk_net_topic_is(const char *topic, int topic_len, const char *expected)
{
    return (int)strlen(expected) == topic_len && strncmp(topic, expected, topic_len) == 0;
}

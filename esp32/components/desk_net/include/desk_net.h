// Shared Wi-Fi + MQTT connection for both desk ESP32-S3 boards.
//
// desk_net_start() connects to Wi-Fi and the Pi's MQTT broker in the
// background and keeps reconnecting on its own, so the caller's tasks (LEDs,
// buttons) never block waiting for the network.
//
// AI-assisted: initial version generated with Claude (Anthropic), then
// reviewed and tested by the team.
#pragma once

#include <stdbool.h>

// Called from the MQTT task for every message on a subscribed topic.
// topic/data are NOT null-terminated; use the lengths.
typedef void (*desk_msg_cb)(const char *topic, int topic_len, const char *data, int data_len);

// topics: array of topic strings to subscribe to (kept by pointer, so pass
// string literals or static storage). n_topics may be 0.
void desk_net_start(const char *ssid, const char *pass, const char *host, int port,
                    const char *client_id, const char *const *topics, int n_topics,
                    desk_msg_cb on_message);

bool desk_net_connected(void);  // true while connected to the MQTT broker

// QoS 0 publish; silently skipped while disconnected. Returns true if sent.
bool desk_net_publish(const char *topic, const char *payload);

// True if a received topic equals the given null-terminated string.
bool desk_net_topic_is(const char *topic, int topic_len, const char *expected);

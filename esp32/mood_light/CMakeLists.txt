# Mood light (ESP32-S3 built-in RGB LED) - ESP-IDF v5.x project
cmake_minimum_required(VERSION 3.16)
# Shared Wi-Fi + MQTT code lives in esp32/components/desk_net
set(EXTRA_COMPONENT_DIRS ../components)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(mood_light)

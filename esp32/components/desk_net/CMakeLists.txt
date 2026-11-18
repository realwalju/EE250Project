set(reqs esp_wifi esp_netif esp_event nvs_flash)
if(IDF_VERSION_MAJOR LESS 6)
    # v5: MQTT is built into ESP-IDF. v6: it comes from idf_component.yml,
    # which the component manager adds as a requirement automatically.
    list(APPEND reqs mqtt)
endif()

idf_component_register(SRCS "desk_net.c"
                       INCLUDE_DIRS "include"
                       PRIV_REQUIRES ${reqs})

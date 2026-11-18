# GPIO and LEDC drivers: separate components since v5.3 (the old "driver"
# component is deprecated in v6)
if(IDF_VERSION_MAJOR GREATER_EQUAL 6 OR (IDF_VERSION_MAJOR EQUAL 5 AND IDF_VERSION_MINOR GREATER_EQUAL 3))
    set(drivers esp_driver_gpio esp_driver_ledc)
else()
    set(drivers driver)
endif()

# ".." so main.c finds secrets.h in the project folder (gitignored)
idf_component_register(SRCS "main.c"
                       PRIV_INCLUDE_DIRS ".."
                       PRIV_REQUIRES desk_net ${drivers} esp_adc esp_timer)

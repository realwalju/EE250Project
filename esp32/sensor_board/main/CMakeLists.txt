# ".." so main.c finds secrets.h in the project folder (gitignored)
idf_component_register(SRCS "main.c"
                       PRIV_INCLUDE_DIRS ".."
                       PRIV_REQUIRES desk_net driver esp_adc esp_timer)

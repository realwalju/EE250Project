# ".." so main.c finds secrets.h in the project folder (gitignored).
# led_strip is downloaded automatically from idf_component.yml.
idf_component_register(SRCS "main.c"
                       PRIV_INCLUDE_DIRS ".."
                       PRIV_REQUIRES desk_net esp_timer)

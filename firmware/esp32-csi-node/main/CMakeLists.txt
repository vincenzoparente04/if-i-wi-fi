set(SRCS
    "main.c"
    "csi_collector.c"
    "stream_sender.c"
    "nvs_config.c"
)

set(REQUIRES
    esp_wifi
    esp_netif
    esp_event
    nvs_flash
    esp_app_format
    esp_timer
    lwip
)

idf_component_register(
    SRCS ${SRCS}
    INCLUDE_DIRS "."
    REQUIRES ${REQUIRES}
)

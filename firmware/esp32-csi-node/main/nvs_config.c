/**
 * @file nvs_config.c
 * @brief NVS-backed config loader for If-I-Wi-Fi firmware.
 *
 * Reads namespace "csi_cfg" for: ssid, password, target_ip, target_port,
 * node_id, channel, hop_count, hop_list, dwell_ms, filter_mac.
 * Missing keys fall back to Kconfig defaults.
 */

#include "nvs_config.h"

#include <string.h>
#include "esp_log.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "sdkconfig.h"

static const char *TAG = "nvs_config";

void nvs_config_load(nvs_config_t *cfg)
{
    if (!cfg) {
        ESP_LOGE(TAG, "cfg is NULL");
        return;
    }

    /* Kconfig defaults */
    strncpy(cfg->wifi_ssid, CONFIG_CSI_WIFI_SSID, NVS_CFG_SSID_MAX - 1);
    cfg->wifi_ssid[NVS_CFG_SSID_MAX - 1] = '\0';

#ifdef CONFIG_CSI_WIFI_PASSWORD
    strncpy(cfg->wifi_password, CONFIG_CSI_WIFI_PASSWORD, NVS_CFG_PASS_MAX - 1);
    cfg->wifi_password[NVS_CFG_PASS_MAX - 1] = '\0';
#else
    cfg->wifi_password[0] = '\0';
#endif

    strncpy(cfg->target_ip, CONFIG_CSI_TARGET_IP, NVS_CFG_IP_MAX - 1);
    cfg->target_ip[NVS_CFG_IP_MAX - 1] = '\0';

    cfg->target_port = (uint16_t)CONFIG_CSI_TARGET_PORT;
    cfg->node_id     = (uint8_t)CONFIG_CSI_NODE_ID;

    cfg->channel_hop_count = 1;
    cfg->channel_list[0]   = (uint8_t)CONFIG_CSI_WIFI_CHANNEL;
    for (uint8_t i = 1; i < NVS_CFG_HOP_MAX; i++) cfg->channel_list[i] = 0;
    cfg->dwell_ms = 50;

    memset(cfg->filter_mac, 0, sizeof(cfg->filter_mac));
    cfg->filter_mac_set = 0;

    /* NVS overrides */
    nvs_handle_t h;
    esp_err_t ret = nvs_open("csi_cfg", NVS_READONLY, &h);
    if (ret != ESP_OK) {
        ESP_LOGI(TAG, "NVS namespace 'csi_cfg' not found; using Kconfig defaults.");
        return;
    }

    size_t sz;

    sz = sizeof(cfg->wifi_ssid);
    nvs_get_str(h, "ssid", cfg->wifi_ssid, &sz);

    sz = sizeof(cfg->wifi_password);
    nvs_get_str(h, "password", cfg->wifi_password, &sz);

    sz = sizeof(cfg->target_ip);
    nvs_get_str(h, "target_ip", cfg->target_ip, &sz);

    uint16_t tport = 0;
    if (nvs_get_u16(h, "target_port", &tport) == ESP_OK) cfg->target_port = tport;

    uint8_t nid = 0;
    if (nvs_get_u8(h, "node_id", &nid) == ESP_OK) cfg->node_id = nid;

    uint8_t hop_count = 0;
    if (nvs_get_u8(h, "hop_count", &hop_count) == ESP_OK && hop_count >= 1 && hop_count <= NVS_CFG_HOP_MAX) {
        cfg->channel_hop_count = hop_count;
        sz = sizeof(cfg->channel_list);
        nvs_get_blob(h, "hop_list", cfg->channel_list, &sz);
    }

    uint32_t dwell = 0;
    if (nvs_get_u32(h, "dwell_ms", &dwell) == ESP_OK && dwell >= 10) cfg->dwell_ms = dwell;

    sz = sizeof(cfg->filter_mac);
    if (nvs_get_blob(h, "filter_mac", cfg->filter_mac, &sz) == ESP_OK && sz == 6) {
        cfg->filter_mac_set = 1;
    }

    nvs_close(h);

    ESP_LOGI(TAG, "Config: ssid='%s' target=%s:%u node=%u hop=%u",
             cfg->wifi_ssid, cfg->target_ip, cfg->target_port,
             cfg->node_id, cfg->channel_hop_count);
}

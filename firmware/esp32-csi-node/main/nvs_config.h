/**
 * @file nvs_config.h
 * @brief Slim runtime config loaded from NVS or Kconfig defaults.
 *
 * If-I-Wi-Fi build — only the fields needed for CSI capture + UDP stream
 * + optional channel hopping. No edge tier, no WASM keys, no swarm, no
 * mmWave, no display.
 */

#ifndef NVS_CONFIG_H
#define NVS_CONFIG_H

#include <stdint.h>

#define NVS_CFG_SSID_MAX 33
#define NVS_CFG_PASS_MAX 65
#define NVS_CFG_IP_MAX   16
#define NVS_CFG_HOP_MAX  6

typedef struct {
    char     wifi_ssid[NVS_CFG_SSID_MAX];
    char     wifi_password[NVS_CFG_PASS_MAX];
    char     target_ip[NVS_CFG_IP_MAX];
    uint16_t target_port;
    uint8_t  node_id;

    /* Optional channel hopping. hop_count=1 → stay on channel_list[0]. */
    uint8_t  channel_hop_count;
    uint8_t  channel_list[NVS_CFG_HOP_MAX];
    uint32_t dwell_ms;

    /* Optional source-MAC filter for CSI callbacks. */
    uint8_t  filter_mac[6];
    uint8_t  filter_mac_set;
} nvs_config_t;

void nvs_config_load(nvs_config_t *cfg);

#endif /* NVS_CONFIG_H */

/**
 * @file csi_collector.c
 * @brief CSI capture + ADR-018 binary serialization (If-I-Wi-Fi build).
 *
 * Trimmed down from the upstream wifi-densepose firmware. We kept the
 * defensive bits that exist because of real shipped-device bugs:
 *
 *   - 50 Hz rate-gate to avoid the wDev_ProcessFiq SPI-cache race.
 *   - ENOMEM backoff in the UDP sender.
 *   - Module-local copy of node_id / MAC filter (g_nvs_config can be
 *     corrupted by wifi_init_sta on some boards — confirmed on
 *     80:b5:4e:c1:be:b8).
 *
 * Removed:
 *   - ADR-110 PPDU tagging (HE/HT/STBC byte 18/19) — bytes are still
 *     written but always zeroed; downstream parsers in this repo ignore them.
 *   - ESP-NOW / 802.15.4 cross-node sync (sync packet emission stripped).
 *   - Edge processing handoff (edge_enqueue_csi removed).
 */

#include "csi_collector.h"
#include "nvs_config.h"
#include "stream_sender.h"

#include <string.h>
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_timer.h"
#include "sdkconfig.h"

extern nvs_config_t g_nvs_config;

static const char *TAG = "csi_collector";

#ifndef CONFIG_ESP_WIFI_CSI_ENABLED
#error "CONFIG_ESP_WIFI_CSI_ENABLED must be set in sdkconfig. " \
       "Run: idf.py menuconfig -> Component config -> Wi-Fi -> Enable WiFi CSI, " \
       "or copy sdkconfig.defaults.template to sdkconfig.defaults before building."
#endif

/* ---- Defensive module-local copies ---- */
static uint8_t s_node_id = 1;
static uint8_t s_filter_mac[6] = {0};
static bool    s_filter_mac_set = false;

/* ---- Counters ---- */
static uint32_t s_sequence    = 0;
static uint32_t s_cb_count    = 0;
static uint32_t s_send_ok     = 0;
static uint32_t s_send_fail   = 0;
static uint32_t s_rate_skip   = 0;
static uint32_t s_early_drop  = 0;

/* ---- Rate gating ---- */
#define CSI_MIN_SEND_INTERVAL_US     (20 * 1000)  /* 50 Hz max UDP send */
#define CSI_MIN_PROCESS_INTERVAL_US  (20 * 1000)  /* 50 Hz max processing */
static int64_t s_last_send_us    = 0;
static int64_t s_last_process_us = 0;

/* ---- Channel hopping ---- */
static uint8_t  s_hop_channels[CSI_HOP_CHANNELS_MAX] = {1, 6, 11, 36, 40, 44};
static uint8_t  s_hop_count = 1;
static uint32_t s_dwell_ms  = 50;
static uint8_t  s_hop_index = 0;
static esp_timer_handle_t s_hop_timer = NULL;

size_t csi_serialize_frame(const wifi_csi_info_t *info, uint8_t *buf, size_t buf_len)
{
    if (info == NULL || buf == NULL || info->buf == NULL) return 0;

    uint8_t  n_antennas    = 1;
    uint16_t iq_len        = (uint16_t)info->len;
    uint16_t n_subcarriers = iq_len / (2 * n_antennas);

    size_t frame_size = CSI_HEADER_SIZE + iq_len;
    if (frame_size > buf_len) {
        ESP_LOGW(TAG, "Buffer too small: need %u, have %u",
                 (unsigned)frame_size, (unsigned)buf_len);
        return 0;
    }

    uint8_t channel = info->rx_ctrl.channel;
    uint32_t freq_mhz;
    if (channel >= 1 && channel <= 13)         freq_mhz = 2412 + (channel - 1) * 5;
    else if (channel == 14)                    freq_mhz = 2484;
    else if (channel >= 36 && channel <= 177)  freq_mhz = 5000 + channel * 5;
    else                                       freq_mhz = 0;

    uint32_t magic = CSI_MAGIC;
    memcpy(&buf[0], &magic, 4);
    buf[4] = s_node_id;
    buf[5] = n_antennas;
    memcpy(&buf[6],  &n_subcarriers, 2);
    memcpy(&buf[8],  &freq_mhz, 4);
    uint32_t seq = s_sequence++;
    memcpy(&buf[12], &seq, 4);
    buf[16] = (uint8_t)(int8_t)info->rx_ctrl.rssi;
    buf[17] = (uint8_t)(int8_t)info->rx_ctrl.noise_floor;
    buf[18] = 0;   /* ppdu_type — unused in If-I-Wi-Fi */
    buf[19] = 0;   /* flags     — unused in If-I-Wi-Fi */

    memcpy(&buf[CSI_HEADER_SIZE], info->buf, iq_len);
    return frame_size;
}

static void wifi_csi_callback(void *ctx, wifi_csi_info_t *info)
{
    (void)ctx;

    int64_t now_us = esp_timer_get_time();
    if ((now_us - s_last_process_us) < CSI_MIN_PROCESS_INTERVAL_US) {
        s_early_drop++;
        return;
    }
    s_last_process_us = now_us;

    if (s_filter_mac_set) {
        if (memcmp(info->mac, s_filter_mac, 6) != 0) return;
    }

    s_cb_count++;
    if (s_cb_count <= 3 || (s_cb_count % 100) == 0) {
        ESP_LOGI(TAG, "CSI cb #%lu: len=%d rssi=%d ch=%d",
                 (unsigned long)s_cb_count, info->len,
                 info->rx_ctrl.rssi, info->rx_ctrl.channel);
    }

    uint8_t frame_buf[CSI_MAX_FRAME_SIZE];
    size_t frame_len = csi_serialize_frame(info, frame_buf, sizeof(frame_buf));
    if (frame_len == 0) return;

    int64_t now = esp_timer_get_time();
    if ((now - s_last_send_us) >= CSI_MIN_SEND_INTERVAL_US) {
        int sent = stream_sender_send(frame_buf, frame_len);
        if (sent > 0) {
            s_send_ok++;
            s_last_send_us = now;
        } else {
            s_send_fail++;
            if (s_send_fail <= 5) {
                ESP_LOGW(TAG, "sendto failed (fail #%lu)", (unsigned long)s_send_fail);
            }
        }
    } else {
        s_rate_skip++;
    }
}

static void wifi_promiscuous_cb(void *buf, wifi_promiscuous_pkt_type_t type)
{
    (void)buf;
    (void)type;
}

void csi_collector_init(void)
{
    /* Copy MAC filter from NVS into our module-local copy now, before
     * the WiFi driver can corrupt g_nvs_config. */
    if (g_nvs_config.filter_mac_set) {
        memcpy(s_filter_mac, g_nvs_config.filter_mac, 6);
        s_filter_mac_set = true;
        ESP_LOGI(TAG, "MAC filter: %02x:%02x:%02x:%02x:%02x:%02x",
                 s_filter_mac[0], s_filter_mac[1], s_filter_mac[2],
                 s_filter_mac[3], s_filter_mac[4], s_filter_mac[5]);
    }

    /* Promiscuous mode is required for CSI to fire on every received frame. */
    wifi_promiscuous_filter_t filter = {
        .filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT | WIFI_PROMIS_FILTER_MASK_DATA,
    };
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_filter(&filter));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_rx_cb(&wifi_promiscuous_cb));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));

    wifi_csi_config_t cfg = {
        .lltf_en           = true,
        .htltf_en          = true,
        .stbc_htltf2_en    = true,
        .ltf_merge_en      = true,
        .channel_filter_en = true,
        .manu_scale        = false,
        .shift             = 0,
    };
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(&wifi_csi_callback, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));

    ESP_LOGI(TAG, "CSI callback registered; node_id=%u", s_node_id);
}

void csi_collector_set_node_id(uint8_t node_id)
{
    s_node_id = node_id;
}

uint8_t csi_collector_get_node_id(void) { return s_node_id; }

void csi_collector_set_hop_table(const uint8_t *channels, uint8_t hop_count, uint32_t dwell_ms)
{
    if (!channels || hop_count == 0) return;
    if (hop_count > CSI_HOP_CHANNELS_MAX) hop_count = CSI_HOP_CHANNELS_MAX;
    memcpy(s_hop_channels, channels, hop_count);
    s_hop_count = hop_count;
    s_dwell_ms  = dwell_ms < 10 ? 10 : dwell_ms;
    s_hop_index = 0;
    ESP_LOGI(TAG, "Hop table set: %u channels, dwell=%lu ms",
             (unsigned)hop_count, (unsigned long)s_dwell_ms);
}

void csi_hop_next_channel(void)
{
    if (s_hop_count <= 1) return;
    s_hop_index = (s_hop_index + 1) % s_hop_count;
    uint8_t ch = s_hop_channels[s_hop_index];
    esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
}

static void hop_timer_cb(void *arg) { (void)arg; csi_hop_next_channel(); }

void csi_collector_start_hop_timer(void)
{
    if (s_hop_count <= 1 || s_hop_timer != NULL) return;
    esp_timer_create_args_t args = {
        .callback = hop_timer_cb,
        .arg = NULL,
        .name = "csi_hop",
    };
    ESP_ERROR_CHECK(esp_timer_create(&args, &s_hop_timer));
    ESP_ERROR_CHECK(esp_timer_start_periodic(s_hop_timer, (uint64_t)s_dwell_ms * 1000));
    ESP_LOGI(TAG, "Hop timer started: %lu ms", (unsigned long)s_dwell_ms);
}

uint16_t csi_collector_get_pkt_yield_per_sec(void)
{
    /* Crude estimate: callbacks observed in the last ~1 s. We don't keep a
     * sliding window here; return 0 if you need precision and add one. */
    return 0;
}

uint16_t csi_collector_get_send_fail_count(void)
{
    return (uint16_t)s_send_fail;
}

/**
 * @file csi_collector.h
 * @brief CSI capture + ADR-018 binary serialization (If-I-Wi-Fi build).
 */

#ifndef CSI_COLLECTOR_H
#define CSI_COLLECTOR_H

#include <stdint.h>
#include <stddef.h>
#include "esp_err.h"
#include "esp_wifi_types.h"

/** Magic for ADR-018 CSI frames (little-endian on the wire). */
#define CSI_MAGIC 0xC5110001u

/** Header size in bytes. */
#define CSI_HEADER_SIZE 20

/** Worst-case single-frame size (header + 4 antennas × 256 subcarriers × 2 bytes). */
#define CSI_MAX_FRAME_SIZE (CSI_HEADER_SIZE + 4 * 256 * 2)

/** Maximum channels in the hop table. */
#define CSI_HOP_CHANNELS_MAX 6

void   csi_collector_init(void);
void   csi_collector_set_node_id(uint8_t node_id);
uint8_t csi_collector_get_node_id(void);

size_t csi_serialize_frame(const wifi_csi_info_t *info, uint8_t *buf, size_t buf_len);

void csi_collector_set_hop_table(const uint8_t *channels, uint8_t hop_count, uint32_t dwell_ms);
void csi_collector_start_hop_timer(void);
void csi_hop_next_channel(void);

uint16_t csi_collector_get_pkt_yield_per_sec(void);
uint16_t csi_collector_get_send_fail_count(void);

#endif /* CSI_COLLECTOR_H */

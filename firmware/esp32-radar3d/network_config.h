/*
 * network_config.h: per-board configuration for esp32-radar3d
 * ===========================================================
 * The 3 ESP32 must have different NODE_IDs (0, 1, 2).
 * Find each board's MAC once (it is printed on the serial monitor at boot)
 * and fill in NODE_MACS below, in node order.
 * UDP_TARGET_IP is the IP of the host running csi.quadrants.ws_server.
 */

#ifndef NETWORK_CONFIG_H
#define NETWORK_CONFIG_H

// ============================================================
// NODE ID: unique per board (0, 1, 2)
// ============================================================
#define NODE_ID 0  // <-- CHANGE for every ESP32

// ============================================================
// MAC addresses of the 3 radar nodes, in order [0, 1, 2]
// ============================================================
static const uint8_t NODE_MACS[3][6] = {
  { 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 },  // NODE 0  <-- replace with your board's MAC
  { 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 },  // NODE 1
  { 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 },  // NODE 2
};

// ============================================================
// Wi-Fi + UDP target
// ============================================================
#include "secrets.h"  // WIFI_SSID, WIFI_PASS (copy secrets.h.example)

#define UDP_TARGET_IP   "192.168.1.100"  // IP of the host (change for your network)
#define UDP_TARGET_PORT 5005             // must match ws_server --udp-port

#endif  // NETWORK_CONFIG_H

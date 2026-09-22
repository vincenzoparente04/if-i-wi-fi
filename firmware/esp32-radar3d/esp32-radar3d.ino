/*
 * ESP32 Radar 3D: cross-ping CSI firmware
 * ======================================
 *
 * Firmware for 3x ESP32: nodes ping each other over 802.11, capture CSI and
 * stream it over UDP to a central host.
 * Inspired by:
 *   - ESP32-CSI-Tool (StevenMHernandez)  -> CSI callback and configuration
 *   - Wifi-3D-Fusion (MaliosDark)        -> multi-source UDP streaming
 *   - RuView (ruvnet)                    -> ADR-018 frame format, precise timestamps
 *
 * ARCHITECTURE:
 *   Every node broadcasts an 802.11 frame at 100 Hz on a fixed channel.
 *   The CSI callback fires on every received frame; the source MAC tells us
 *   which node transmitted it.
 *
 *   With 3 nodes the (tx, rx) matrix has 3x3 = 9 pairs, but only the 6
 *   cross-bistatic ones (i, j) with i != j are physical paths through the
 *   room. The 3 diagonals (i, i) are self-loops: on a half-duplex ESP32 the
 *   TX path bypasses RX, so they carry no useful CSI. The firmware drops them
 *   explicitly (counter csi_dropped_self_loop).
 *
 *   Every frame is packed with a timestamp and sent over UDP to the host.
 *
 * UDP OUTPUT, magic 0xC5110003 (LE u32):
 *   [0..3]   Magic: 0xC5110003
 *   [4]      TX node ID (0, 1, 2)
 *   [5]      RX node ID (0, 1, 2)
 *   [6..7]   Number of subcarriers (LE u16)
 *   [8..11]  Sequence number (LE u32)
 *   [12]     RSSI (i8)
 *   [13]     Noise floor (i8)
 *   [14..15] Reserved
 *   [16..23] Timestamp in microseconds (LE i64)
 *   [24..]   I/Q pairs (int8_t, subcarrier x 2 bytes)
 *
 * SETUP (edit network_config.h, copy secrets.h.example to secrets.h):
 *   NODE_ID = 0, 1 or 2  (different on every board)
 *   MAC addresses of the 3 nodes in NODE_MACS
 *   UDP_TARGET_IP = IP of the host running csi.quadrants.ws_server
 *   WIFI_SSID / WIFI_PASS in secrets.h
 *
 * DEPENDENCIES:
 *   ESP32 Arduino core (Tools -> Board -> ESP32 Dev Module)
 *   Uses esp_wifi internally for CSI and raw 802.11 TX.
 */

#include <WiFi.h>
#include <WiFiUdp.h>
#include "esp_wifi.h"
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"
#include "network_config.h"
#include "secrets.h"

// ============================================================
// Constants
// ============================================================
#define SERIAL_BAUD 115200
#define TX_INTERVAL_MS 10  // 100 Hz
#define STATS_INTERVAL_MS 5000
#define CHANNEL 6
#define MAX_SUBCARRIER 128

#define MAGIC_RADAR3D 0xC5110003
#define HEADER_SIZE 24  // header fisso 24 byte

// ============================================================
// Struttura pacchetto UDP
// ============================================================
#pragma pack(push, 1)
typedef struct {
  uint32_t magic;         // 0xC5110003
  uint8_t tx_node;        // chi ha trasmesso
  uint8_t rx_node;        // chi ha ricevuto (NODE_ID locale)
  uint16_t n_subcarrier;  // numero subcarrier
  uint32_t seq;           // sequenza progressiva
  int8_t rssi;            // RSSI del frame
  int8_t noise_floor;     // noise floor
  uint16_t reserved;      // padding
  int64_t timestamp_us;   // microsecondi ESP32
} radar3d_header_t;
#pragma pack(pop)

// ============================================================
// Globali
// ============================================================
WiFiUDP udp;
IPAddress remote_ip;

uint32_t seq_counter = 0;
uint32_t frames_tx = 0;
uint32_t csi_callbacks = 0;
uint32_t csi_dropped_per_tx[3] = {0, 0, 0};  // drop dal rate gate, per tx_node
uint32_t csi_dropped_unknown = 0;            // frame da MAC fuori NODE_MACS
uint32_t csi_dropped_self_loop = 0;          // frame con tx_node == NODE_ID (loopback)
uint32_t udp_send_fail = 0;                  // sendto returns 0 or fail
int8_t iq_buffer[MAX_SUBCARRIER * 2];

// Rate gate PER-TX: ogni tx_node (0,1,2) ha la sua finestra di 10 ms.
// Razionale: con 3 nodi che broadcastano a 100 Hz ognuno, ogni RX vede ~300
// callback/s. Un gate globale a 100 Hz farebbe passare solo 1 frame su 3,
// scegliendo NON-DETERMINISTICAMENTE quale TX vince ciascuna finestra; nel
// lungo termine ogni path (tx,rx) finirebbe a ~33 Hz con bias verso il
// loopback locale (latenza ~0 vs OTA). Gate per-TX garantisce 100 Hz a
// ciascuno dei 3 path verso questo RX.
//
// Limiti che il cap previene (validi anche per-TX):
//   (a) saturazione pbuf pool lwIP,
//   (b) race WiFi ISR ↔ SPI flash cache (panic Core 0, vedi
//       firmware/esp32-csi-node/main/csi_collector.c).
#define CSI_MIN_PROCESS_INTERVAL_US 10000  // 10 ms = 100 Hz max per TX
static int64_t s_last_process_us[3] = {0, 0, 0};

// UDP ENOMEM backoff: se l'invio fallisce, ferma i send per 100 ms per
// dare a lwIP il tempo di riallocare i pbuf. Senza questo, il fallimento
// si propaga in cascata su ogni callback successiva.
#define UDP_BACKOFF_MS 100
static int64_t s_udp_backoff_until_us = 0;

// ============================================================
// Callback CSI
// ============================================================
static void wifi_csi_cb(void *ctx, wifi_csi_info_t *data) {
  wifi_csi_info_t *info = data;
  int64_t now_us = esp_timer_get_time();

  // ---- Identifica TX node dal MAC source (PRIMA del rate gate) ----
  // Il gate è per-TX, quindi deve sapere CHI è il TX prima di decidere.
  uint8_t *mac = info->mac;
  int tx_node = -1;
  for (int i = 0; i < 3; i++) {
    if (memcmp(mac, NODE_MACS[i], 6) == 0) {
      tx_node = i;
      break;
    }
  }
  if (tx_node < 0) {
    // MAC fuori NODE_MACS: telefono, AP, vicino di casa.
    // SCARTATO (vs. la vecchia assegnazione a NODE_ID che gonfiava il path
    // loopback con frame estranei). Contato per visibilità nei diag.
    csi_dropped_unknown++;
    return;
  }

  // ---- Filtro self-loop (tx_node == NODE_ID) ----
  // ESP32 in half-duplex normalmente NON genera CSI sui propri TX (TX path
  // e RX path sono mutuamente esclusivi nel chip). Il path (i, i) quindi
  // non è informativo per il sensing bistatico. Se per qualche ragione
  // (loopback hardware, eco coexistence WiFi/BT, configurazione monitor
  // particolare) un frame da NODE_ID stesso arrivasse al callback, lo
  // scartiamo a monte: invariante esplicita "nessun path diagonale entra
  // mai nella pipeline".
  if (tx_node == NODE_ID) {
    csi_dropped_self_loop++;
    return;
  }

  // ---- Rate gate PER-TX (10 ms = 100 Hz per ciascun tx_node) ----
  if ((now_us - s_last_process_us[tx_node]) < CSI_MIN_PROCESS_INTERVAL_US) {
    csi_dropped_per_tx[tx_node]++;
    return;
  }
  s_last_process_us[tx_node] = now_us;

  // ---- UDP backoff window check (globale, lwIP-wide) ----
  if (s_udp_backoff_until_us > 0 && now_us < s_udp_backoff_until_us) {
    // Sotto backoff: il consumo del gate sopra è già avvenuto (s_last_process_us
    // è aggiornato) — accettabile: in cooldown vogliamo skippare la serializzazione
    // e ripartire pulito quando lwIP si riprende.
    return;
  }

  // Legge I/Q raw dal buffer CSI.
  // ESP-IDF: info->buf è int8_t* con layout (Im0, Re0, Im1, Re1, ...);
  // info->len è in BYTE, 2 byte per subcarrier.
  // Il parser host (parse_csi_radar3d) legge ogni coppia come (real, imag),
  // quindi qui invertiamo l'ordine dei due byte per subcarrier.
  int8_t *iq = (int8_t *)info->buf;
  uint16_t n_sub = info->len / 2;
  if (n_sub > MAX_SUBCARRIER) n_sub = MAX_SUBCARRIER;

  for (uint16_t i = 0; i < n_sub; i++) {
    iq_buffer[i * 2 + 0] = iq[i * 2 + 1];  // Re
    iq_buffer[i * 2 + 1] = iq[i * 2 + 0];  // Im
  }

  // Costruisce header
  radar3d_header_t hdr;
  hdr.magic = MAGIC_RADAR3D;
  hdr.tx_node = (uint8_t)tx_node;
  hdr.rx_node = NODE_ID;
  hdr.n_subcarrier = n_sub;
  hdr.seq = seq_counter++;
  hdr.rssi = (int8_t)info->rx_ctrl.rssi;  hdr.noise_floor = -90;
  hdr.reserved = 0;
  hdr.timestamp_us = esp_timer_get_time();

  // Spedisce via UDP. endPacket() ritorna 1 su ok, 0 su errore (es. ENOMEM
  // del pbuf pool lwIP). Su fallimento entriamo in backoff: stop send per
  // UDP_BACKOFF_MS millisecondi per permettere a lwIP di riallocare i buffer.
  udp.beginPacket(remote_ip, UDP_TARGET_PORT);
  udp.write((uint8_t *)&hdr, sizeof(radar3d_header_t));
  udp.write((uint8_t *)iq_buffer, n_sub * 2);
  if (udp.endPacket() == 0) {
    udp_send_fail++;
    s_udp_backoff_until_us = now_us + ((int64_t)UDP_BACKOFF_MS * 1000);
  }

  csi_callbacks++;
}

// ============================================================
// Inietta frame 802.11 broadcast (ping)
// ============================================================
static void inject_ping() {
  uint8_t pkt[32];
  memset(pkt, 0, sizeof(pkt));

  // Data frame + From DS
  pkt[0] = 0x08;
  pkt[1] = 0x02;

  // Address 1: Broadcast
  memset(&pkt[4], 0xFF, 6);

  // Address 2: MAC sorgente = NODE_MACS[NODE_ID]
  memcpy(&pkt[10], NODE_MACS[NODE_ID], 6);

  // Address 3: BSSID = stesso MAC
  memcpy(&pkt[16], NODE_MACS[NODE_ID], 6);

  // Sequence control
  static uint16_t seq = 0;
  pkt[22] = seq & 0xFF;
  pkt[23] = (seq >> 8) & 0xFF;
  seq += 16;

  esp_wifi_80211_tx(WIFI_IF_STA, pkt, sizeof(pkt), false);
  frames_tx++;
}

// ============================================================
// Setup
// ============================================================
void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(500);

  Serial.printf("\n=== ESP32 Radar 3D — NODE %d ===\n", NODE_ID);

  // Brownout detector: LASCIATO ATTIVO (era disabilitato nella versione team).
  // Il brownout è un sintomo di alimentazione USB inadeguata (cavi lunghi,
  // hub passivi, porte USB-2 a 500 mA). Disabilitarlo nasconde il problema
  // e può portare a comportamento erratico più difficile da diagnosticare.
  // Se vedi reboot continui, usa un alimentatore decente o un cavo corto
  // a un USB-3 a 900 mA, non commentare la riga sotto.
  // WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0);  // DON'T DO THIS

  // CPU alla massima frequenza per CSI stabile
  setCpuFrequencyMhz(240);

  // WiFi STA
  WiFi.mode(WIFI_STA);
  Serial.printf("ESP32 MAC Address: %s\n", WiFi.macAddress().c_str());  // read by tools/discover_macs.py
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  int att = 0;
  while (WiFi.status() != WL_CONNECTED && att < 40) {
    delay(500);
    Serial.print(".");
    att++;
  }
  if (WiFi.status() != WL_CONNECTED) {

    Serial.println("\nWiFi FAIL — reboot");
    delay(3000);
    ESP.restart();
  }

  Serial.print("\nESP32 MAC Address: ");
  Serial.println(WiFi.macAddress());
  Serial.print("ESP32 IP Address: ");
  Serial.println(WiFi.localIP());

  remote_ip.fromString(UDP_TARGET_IP);

  udp.begin(WiFi.localIP(), UDP_TARGET_PORT);

  // === Config CSI ===
  esp_wifi_set_promiscuous(true);
  uint8_t ch = WiFi.channel();
  Serial.printf("Canale WiFi: %d\n", ch);
  esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);

  wifi_csi_config_t csi_conf = { 0 };
#if CONFIG_SOC_WIFI_HE_SUPPORT
  /* HE-capable chip (C6/C5): bitfield struct wifi_csi_acquire_config_t.
   *  Fields differ between MAC version 3 and non-3. */
  csi_conf.enable = 1;
  csi_conf.acquire_csi_legacy = 1;
  csi_conf.acquire_csi_ht20 = 1;
  csi_conf.acquire_csi_ht40 = 1;
#if CONFIG_SOC_WIFI_MAC_VERSION_NUM == 3
  csi_conf.acquire_csi_force_lltf = 1;
  csi_conf.acquire_csi_vht = 1;
#endif
  csi_conf.acquire_csi_su = 1;
  csi_conf.acquire_csi_mu = 1;
  csi_conf.acquire_csi_dcm = 1;
#else
  /* Legacy ESP32: bool / uint8_t struct */
  csi_conf.lltf_en = 1;
  csi_conf.htltf_en = 1;
  csi_conf.stbc_htltf2_en = 0;
  csi_conf.ltf_merge_en = 1;
  csi_conf.channel_filter_en = 0;
  csi_conf.manu_scale = 0;
  csi_conf.shift = 0;
#endif
  ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_conf));
  ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(&wifi_csi_cb, NULL));
  ESP_ERROR_CHECK(esp_wifi_set_csi(true));

  Serial.println("CSI attivo. Cross-ping 100 Hz...\n");
  Serial.println("tx_node rx_node n_sub seq rssi ts");
  Serial.println(WiFi.status());
  Serial.println(WiFi.SSID());
}

// ============================================================
// Loop
// ============================================================
static unsigned long last_tx = 0;
static unsigned long last_stats = 0;

void loop() {
  unsigned long now = millis();

  if (now - last_tx >= TX_INTERVAL_MS) {
    inject_ping();
    last_tx = now;
  }

  if (now - last_stats >= STATS_INTERVAL_MS) {
    Serial.printf("[%d] TX:%u CSI:%u drop[tx0=%u tx1=%u tx2=%u self=%u unk=%u] udp_fail:%u\n",
                  NODE_ID, frames_tx, csi_callbacks,
                  csi_dropped_per_tx[0], csi_dropped_per_tx[1], csi_dropped_per_tx[2],
                  csi_dropped_self_loop, csi_dropped_unknown, udp_send_fail);
    last_stats = now;
  }
}

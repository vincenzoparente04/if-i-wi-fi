# ESP32-S3 CSI node (ESP-IDF)

ESP-IDF v5.2+ component. Builds a single-image firmware that:

1. Joins a 2.4 GHz WiFi network using credentials from NVS or Kconfig.
2. Registers a CSI callback in promiscuous mode (MGMT + DATA filter).
3. Serializes each CSI callback into an [ADR-018 binary
   frame](../../docs/csi-frame-format.md) (20-byte header + raw I/Q).
4. Sends frames over UDP to the host aggregator.

## Build

```bash
. $IDF_PATH/export.sh           # or source the ESP-IDF env however you do it
idf.py set-target esp32s3
idf.py menuconfig               # set "CSI Node Config" → SSID/password/target
idf.py build
idf.py -p /dev/ttyUSB0 flash monitor
```

The default `sdkconfig.defaults` is for an 8 MB ESP32-S3 (regular or
SuperMini). For 4 MB boards, edit `partitions.csv` to shrink the factory
partition to ~1.5 MB and the storage partition accordingly, then
`idf.py fullclean && idf.py build`.

## Provisioning a device per-unit

If you want a single firmware binary across multiple boards (recommended
for fleets), write the per-device config to NVS using ESP-IDF's standard
`nvs_partition_gen.py`:

```csv
key,type,encoding,value
csi_cfg,namespace,,
ssid,data,string,YourWiFi
password,data,string,secret
target_ip,data,string,192.168.1.20
target_port,data,u16,5555
node_id,data,u8,3
```

Save as `node-3.csv`, then:

```bash
python $IDF_PATH/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py \
    generate node-3.csv node-3.bin 0x6000
esptool.py -p /dev/ttyUSB0 write_flash 0x9000 node-3.bin
```

## What was removed compared to upstream

- OTA update server, WASM runtime, mmWave UART sensor, swarm bridge,
  Cognitum integration, 802.15.4 timesync, ESP-NOW sync, AMOLED display
  driver, adaptive controller, mock CSI generator, ADR-110 PPDU tagging.
- The original `main/CMakeLists.txt` listed 18 source files; this build
  has 4. The resulting binary is about 250 KB instead of 1.1 MB.

If you need any of those, the upstream lives at
`ruvnet/wifi-densepose` and the source files we removed are still under
`RuView/firmware/esp32-csi-node/main/` if you have the snapshot.

## Known hardware quirks (carried over from upstream)

- The CSI callback is rate-gated to 50 Hz. Above that, the WiFi ISR
  (`wDev_ProcessFiq`) races with SPI flash cache and can panic Core 0.
  Don't raise the gate without rerunning the cache-race stress test.
- On some boards the WiFi driver initialization can corrupt `g_nvs_config`
  (observed device MAC: `80:b5:4e:c1:be:b8`). The firmware captures
  `node_id` into a module-local static before `wifi_init_sta()` runs.
- UDP `sendto` can fail with ENOMEM under callback storms. We back off
  for 100 ms when that happens and resume.

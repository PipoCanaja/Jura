# Debug tools (fork only)

These tools live on the `debug/tools` branch of PipoCanaja/Jura and are not meant for upstream.

## Frame log

Every BLE frame is logged at debug level by `custom_components.jura.core.debug`:

```
W 1529 P_MODE        plain=2a 7f 80 raw=77 ... 
R 1527 PROGRESS      plain=2a 3e 03 ... raw=...
W 1529 P_MODE        plain=2a 47 ff raw=... ERROR BleakError: ...
```

`plain` = before encryption (writes) or after decryption (reads), `raw` = bytes on the air.
While connected, status (1524) is read every 6 s by the debug tools; progress (1527) is read
by the integration itself (every 5 s, every 1 s during a product), and traced too.

## Diagnostics

Settings > Devices & services > JURA > device > ⋮ > Download diagnostics: advertisement data,
key, model, alerts, statistics, last frame of each characteristic and the last 500 frames.

## Recording a cycle

1. `jura.record_start` (optional `duration`, default 600 s, max 3600): keeps the connection open,
   reads progress and status every second.
2. Make a product or start a maintenance process.
3. `jura.record_stop`: writes `/config/jura_<mac>_<date>.jsonl` (header line + one frame per line).
   The service response gives the file path.

Turning the connection switch off also stops the polling (the recording then stays empty until stop).

## Raw read / write

Developer tools > Actions:

```yaml
action: jura.raw_read
data:
  uuid: "27"          # 27, 1527, 5a401527 or full UUID
```

```yaml
action: jura.raw_write
data:
  uuid: "29"          # P_MODE
  data: "00 47 FF"    # before encryption, byte 0 replaced by the key
  encrypt: true
```

`raw_write` can send any command to the machine: check the protocol document first.

## Raw progress sensor

`sensor.<name>_progress_raw` (diagnostic, disabled by default): last decrypted progress frame,
with the last status frame in its attributes.

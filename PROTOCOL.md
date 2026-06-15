# Ergostol BLE protocol (reverse-engineered)

App: `com.pairlink.ergostol` (Ergostol, SingApp), RuStore. Native Java, lib type
`LIB_DIRECT_DING`. The desk's USB BLE adapter (PairLink module) is the peripheral.

## BLE transport

| Role   | Characteristic UUID                      |
|--------|------------------------------------------|
| Write  | `0000ff01-0000-1000-8000-00805f9b34fb`   |
| Notify | `0000ff02-0000-1000-8000-00805f9b34fb`   |

- Enable notifications on `ff02` (standard CCCD `00002902`, NOTIFY).
- Commands are written to `ff01`. No password / pairing handshake is sent on connect.
- No app-level auth; the desk accepts commands immediately after connect.

## Command frame (what goes on the wire = 6 bytes)

```
[ op, p1, dHi, dLo, crcLo, crcHi ]
```

- `op`  — opcode (see table)
- `p1`  — sub-parameter (default `0x01`)
- `dHi`,`dLo` — 16-bit data (big-endian), usually `0x00 0x00` for movement
- `crcLo,crcHi` — CRC-16, little-endian on the wire

### CRC-16

CRC is computed over an **8-byte buffer with a constant 4-byte header that is NOT
transmitted** (the firmware prepends it itself):

```
crc_input = [0x04, 0xFC, 0x42, 0x06, op, p1, dHi, dLo]
```

Nibble-table CRC-16 (init `0xFFFF`, reflected, poly 0x8408 family):

```python
TABLE = [0,52225,55297,5120,61441,15360,10240,58369,
         40961,27648,30720,46081,20480,39937,34817,17408]
def crc16(buf):
    crc = 0xFFFF
    for b in buf:
        crc = ((crc >> 4) ^ TABLE[(crc & 0xF) ^ (b & 0xF & 0x7F)]) & 0xFFFF
        crc = ((crc >> 4) ^ TABLE[(crc & 0xF) ^ ((b >> 4) & 0xF & 0x7F)]) & 0xFFFF
    return crc
```

Validated: `crc16([04 FC 42 06 50 01 00 00]) = 0x15BA`, matching the hard-coded
command `{80,1,0,0,-70,21}` in `MainActivity` (op `0x50`).

## Opcodes (op)

| op  | meaning                              | notes |
|-----|--------------------------------------|-------|
| 1   | **move DOWN** (hold)                 | `BUTTON_DOWN`; sent once on press, STOP on release |
| 2   | **move UP** (hold)                   | `BUTTON_UP` |
| 3   | recall **STAND** preset (auto-move)  | desk drives to stored standing pos & stops itself |
| 4   | recall **MIDDLE** preset (auto-move) | |
| 5   | recall **SIT** preset (auto-move)    | |
| 6   | config / save (`p1` selects)         | p1=1/2/3 save stand/middle/sit (= current height); p1=4 cm/inch; p1=5/6/7 base/min/max calibration. dHi,dLo = target hall from field `l` |
| 7   | init / query sequence (`p1`=index)   | desk replies with calibration values; app walks p1=1..8 |
| 8   | **query current height**             | desk replies (and streams while moving) |
| 9   | **STOP**                             | |
| 11  | handset (physical remote) handshake  | |
| 12  | reminder / hot state                 | |

Movement model: send `UP`/`DOWN` once → desk moves continuously and streams height
notifications → send `STOP` to halt. Presets (3/4/5) auto-stop at the stored target.

## Notification frame (from `ff02`)

```
[ opEcho, p1, hallHi, hallLo, ... ]
```

- `hall = (hallHi << 8) | hallLo`  (unsigned, big-endian)
- During movement / on query the desk streams `op=8` packets; `op=9` confirms stop.
- The streamed hall is **relative to base**. Displayed absolute height adds the base:
  `abs_cm = (run_hall + base_hall) / 29.333334`

## Height ↔ hall conversion (EXACT, from the app)

op-8 returns the motor **run hall** (`0` at the lowest position). The app
(`utils/c.java` `b(int)` + `setDeskHeight`) computes the displayed centimetres as:

```
real_cm  = (run_hall + base_hall) / g.u
run_hall = round(real_cm * g.u - base_hall)
```

- `base_hall` = op-7 walk param **5** (here 2816).
- `g.u` is selected by the **desk model index = op-7 walk param 9** (= MCU version,
  byte 3 of that reply). Model→g.u table:
  `1,2→29.333334 · 3→11.0 · 4→44.0 · 5→26.0 · 6→58.666668 · 7→29.8 · 8→26.0 ·
  9→27.5 · 10→44.0 · 11→22.0`.
- This desk: model **4** → `g.u = 44.0`. Verified to 0.1 cm against the handset
  (e.g. hall 1515 → (1515+2816)/44 = 98.4 cm).

op-7 walk replies (value echoed in `hallHi,hallLo`):

| p1 | value                          |
|----|--------------------------------|
| 5  | base_hall                      |
| 6  | min_hall (absolute = base)     |
| 7  | max_hall (absolute)            |
| 8  | current run_hall               |
| 9  | **model index / MCU version**  |

Travel range in run hall: `0 .. (max_hall - base_hall)`. NB: op-8 run_hall is
**relative to base**; `min_hall`/`max_hall` from the walk are absolute (add base).

## Setting an arbitrary height (no native "go to X" command)

The firmware exposes only 3 presets + manual up/down. To reach an arbitrary height,
run a closed loop:

1. `QUERY` (op 8) → current relative hall.
2. `target_run_hall = round(cm * 29.333334) - base_hall`, clamped to [min,max].
3. If current < target → `UP` (op 2); if current > target → `DOWN` (op 1).
4. Watch notifications; when `|current - target| <= tolerance` → `STOP` (op 9).
5. Always STOP on exit / timeout / overshoot.

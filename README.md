# Ergostol Desk — Home Assistant integration

Control an **Ergostol** height-adjustable desk (PairLink BLE adapter, app
`com.pairlink.ergostol`) from Home Assistant over Bluetooth.

The desk firmware only exposes presets + manual up/down — there is no native
"go to X cm" command — so this integration drives the desk with a closed loop:
it polls the height sensor while moving and brakes early to land on the target.

## Entities

| Entity | Type | Purpose |
|--------|------|---------|
| Height | `number` (cm) | Set an arbitrary target height; also shows current |
| Current height | `sensor` (cm) | Read-only, graphable |
| Moving | `binary_sensor` | On while the desk is in motion |
| Stop | `button` | Halt motion |
| Sit / Middle / Stand preset | `button` | Recall a stored preset |

Height in cm matches the desk's own handset display. The conversion is read
straight from the desk during setup (`real_cm = (run_hall + base) / g.u`, with
`base` and the model-specific `g.u` taken from the init handshake), so it
auto-calibrates for any Ergostol model.

## Install

1. Copy `custom_components/ergostol/` into your HA `config/custom_components/`.
2. Make sure a Bluetooth adapter reachable by HA is in range of the desk.
3. Restart Home Assistant.
4. **Settings → Devices & Services** → the desk is auto-discovered (service
   `0xff12`); or **Add Integration → Ergostol Desk** and pick it from the list.

## Layout

- `custom_components/ergostol/` — the integration.
- `PROTOCOL.md` — reverse-engineered BLE protocol (validated against a capture).
- `ergostol.py` — standalone CLI controller (bleak) for testing from a laptop.

See `PROTOCOL.md` for the wire protocol and `ergostol.py` for a reference
implementation of the height closed loop.

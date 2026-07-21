"""Ergostol BLE protocol (reverse-engineered from com.pairlink.ergostol).

See PROTOCOL.md at the repo root for the full description. Frames on the wire are
6 bytes: [op, p1, dHi, dLo, crcLo, crcHi]. CRC-16 is computed over a fixed header
plus the 4 payload bytes; the header differs for the TX and RX directions.
"""

from __future__ import annotations

from typing import NamedTuple

# GATT
SERVICE_UUID = "0000ff12-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"

# Opcodes
OP_DOWN = 1
OP_UP = 2
OP_STAND = 3
OP_MIDDLE = 4
OP_SIT = 5
OP_INIT = 7
OP_QUERY = 8
OP_STOP = 9
OP_HANDSHAKE = 11  # "handset is running" staged exchange, desk-initiated
OP_HEARTBEAT = 12  # desk status ping (d=1 response, d=2 unsolicited report)

# Status frames pushed by the desk: the p1 byte doubles as a flag field.
# p1=0x80 (any op) carries an error code in the data bytes — the code shown on
# the handset display (4 -> "E04"); 0 means the fault cleared. p1=0x20 is the
# "hot state" push during init (the vendor app answers it with op-12).
P1_ERROR = 0x80
P1_HOT = 0x20

_ERROR_KEYS = {
    0: "none",
    1: "e01",  # motor stop
    2: "e02",  # synchronisation >15 mm
    3: "e03",  # cable
    4: "e04",  # communication fault (handset<->controller bus)
    5: "e05",  # overload
    32: "hot",  # thermal protection
}


def error_key(code: int) -> str:
    """Stable key for a desk error code (matches the handset display)."""
    return _ERROR_KEYS.get(code, "unknown")


def is_calib_step(p1: int) -> bool:
    """True if an op-7 frame with this p1 is an init-walk calibration reply."""
    return 1 <= p1 <= 11


# op-11 handshake: the desk opens a staged exchange (stage in d_lo) and RETRIES
# until the client answers. The vendor app replies op-11 carrying the NEXT
# stage (its g.C state machine); stages 2/7/11 close an exchange (no reply).
_HANDSHAKE_ACKS = {
    0: 1,  # handset running: 0 -> ack 1 -> desk sends 2 ("start to get height")
    5: 6,  # sync base/min/max: 5 -> ack 6 -> desk sends 7
    9: 10,  # hot-over: 9 -> ack 10 -> desk sends 11
}


def handshake_ack(stage: int) -> int | None:
    """d_lo to send back for an op-11 stage, or None if no reply is expected."""
    return _HANDSHAKE_ACKS.get(stage)


# Desk model index (init op-7 param 9 / MCU version) -> hall-per-cm factor (g.u).
MODEL_GU = {
    1: 29.333334,
    2: 29.333334,
    3: 11.0,
    4: 44.0,
    5: 26.0,
    6: 58.666668,
    7: 29.8,
    8: 26.0,
    9: 27.5,
    10: 44.0,
    11: 22.0,
}
DEFAULT_GU = 44.0
DEFAULT_BASE = 2816
DEFAULT_MAX_RUN = 2875  # max_abs - base for the reference desk


class Calibration(NamedTuple):
    """Static per-desk values learned from the init walk."""

    base: int  # hall offset of the lowest position (op-7 p5)
    gu: float  # hall units per cm, from the model index (op-7 p9)
    max_run: int  # travel range in hall units (op-7 p7 minus base)


def derive_calibration(calib: dict[int, int]) -> Calibration:
    """Derive desk calibration from collected init-walk replies (p1 -> value)."""
    base = calib.get(5, DEFAULT_BASE)
    gu = MODEL_GU.get(calib.get(9), DEFAULT_GU)
    max_abs = calib.get(7)
    max_run = (max_abs - base) if max_abs else DEFAULT_MAX_RUN
    return Calibration(base, gu, max_run)


_CRC_TABLE = [
    0,
    52225,
    55297,
    5120,
    61441,
    15360,
    10240,
    58369,
    40961,
    27648,
    30720,
    46081,
    20480,
    39937,
    34817,
    17408,
]
_TX_HEADER = bytes([0x04, 0xFC, 0x42, 0x06])
_RX_HEADER = bytes([0x01, 0xFC, 0x41, 0x06])


def crc16(buf: bytes) -> int:
    crc = 0xFFFF
    for b in buf:
        crc = ((crc >> 4) ^ _CRC_TABLE[(crc & 0xF) ^ (b & 0xF & 0x7F)]) & 0xFFFF
        crc = ((crc >> 4) ^ _CRC_TABLE[(crc & 0xF) ^ ((b >> 4) & 0xF & 0x7F)]) & 0xFFFF
    return crc


def build(op: int, p1: int = 1, d_hi: int = 0, d_lo: int = 0) -> bytes:
    """Build a 6-byte command frame for the write characteristic."""
    body = bytes([op & 0xFF, p1 & 0xFF, d_hi & 0xFF, d_lo & 0xFF])
    crc = crc16(_TX_HEADER + body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def parse(data: bytes) -> tuple[int, int, int] | None:
    """Parse a notification frame -> (op, p1, hall) or None if not a valid frame.

    hall is the big-endian value in bytes 2-3. CRC is verified with the RX header.
    """
    if len(data) < 6:
        return None
    op, p1, d_hi, d_lo, c_lo, c_hi = (
        data[0],
        data[1],
        data[2],
        data[3],
        data[4],
        data[5],
    )
    crc = crc16(_RX_HEADER + bytes([op, p1, d_hi, d_lo]))
    if (crc & 0xFF) != c_lo or ((crc >> 8) & 0xFF) != c_hi:
        return None
    return op, p1, (d_hi << 8) | d_lo

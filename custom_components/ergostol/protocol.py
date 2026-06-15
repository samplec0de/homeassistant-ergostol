"""Ergostol BLE protocol (reverse-engineered from com.pairlink.ergostol).

See PROTOCOL.md at the repo root for the full description. Frames on the wire are
6 bytes: [op, p1, dHi, dLo, crcLo, crcHi]. CRC-16 is computed over a fixed header
plus the 4 payload bytes; the header differs for the TX and RX directions.
"""
from __future__ import annotations

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

# Desk model index (init op-7 param 9 / MCU version) -> hall-per-cm factor (g.u).
MODEL_GU = {
    1: 29.333334, 2: 29.333334, 3: 11.0, 4: 44.0, 5: 26.0,
    6: 58.666668, 7: 29.8, 8: 26.0, 9: 27.5, 10: 44.0, 11: 22.0,
}
DEFAULT_GU = 44.0
DEFAULT_BASE = 2816
DEFAULT_MAX_RUN = 2875  # max_abs - base for the reference desk

_CRC_TABLE = [0, 52225, 55297, 5120, 61441, 15360, 10240, 58369,
              40961, 27648, 30720, 46081, 20480, 39937, 34817, 17408]
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
    op, p1, d_hi, d_lo, c_lo, c_hi = data[0], data[1], data[2], data[3], data[4], data[5]
    crc = crc16(_RX_HEADER + bytes([op, p1, d_hi, d_lo]))
    if (crc & 0xFF) != c_lo or ((crc >> 8) & 0xFF) != c_hi:
        return None
    return op, p1, (d_hi << 8) | d_lo

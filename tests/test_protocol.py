"""Tests for the pure BLE protocol layer (no Home Assistant imports)."""

from protocol import (
    DEFAULT_BASE,
    DEFAULT_GU,
    DEFAULT_MAX_RUN,
    OP_INIT,
    OP_QUERY,
    P1_ERROR,
    P1_HOT,
    build,
    crc16,
    derive_calibration,
    error_key,
    is_calib_step,
    parse,
)

_RX_HEADER = bytes([0x01, 0xFC, 0x41, 0x06])


def rx_frame(op: int, p1: int, value: int) -> bytes:
    """Build a desk->client notification frame (RX CRC header)."""
    body = bytes([op & 0xFF, p1 & 0xFF, (value >> 8) & 0xFF, value & 0xFF])
    crc = crc16(_RX_HEADER + body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


# ---- parse ----


def test_parse_height_reply() -> None:
    assert parse(rx_frame(OP_QUERY, 1, 1395)) == (OP_QUERY, 1, 1395)


def test_parse_error_frame_keeps_p1_and_code() -> None:
    # E04 push: any-op frame with p1=0x80, error code in the data bytes.
    assert parse(rx_frame(OP_INIT, P1_ERROR, 4)) == (OP_INIT, P1_ERROR, 4)


def test_parse_rejects_bad_crc() -> None:
    frame = bytearray(rx_frame(OP_QUERY, 1, 1395))
    frame[4] ^= 0xFF
    assert parse(bytes(frame)) is None


def test_parse_rejects_short_frame() -> None:
    assert parse(b"\x08\x01\x00") is None


def test_parse_rejects_tx_frame() -> None:
    # Frames we send use the TX CRC header — must not parse as notifications.
    assert parse(build(OP_QUERY)) is None


# ---- status / error frames ----


def test_error_key_known_codes() -> None:
    assert error_key(0) == "none"
    assert error_key(1) == "e01"
    assert error_key(2) == "e02"
    assert error_key(3) == "e03"
    assert error_key(4) == "e04"  # the E04 communication fault
    assert error_key(5) == "e05"
    assert error_key(32) == "hot"


def test_error_key_unknown_code() -> None:
    assert error_key(99) == "unknown"


def test_is_calib_step_accepts_walk_range() -> None:
    assert all(is_calib_step(p1) for p1 in range(1, 12))


def test_is_calib_step_rejects_status_frames() -> None:
    assert not is_calib_step(0)
    assert not is_calib_step(12)
    assert not is_calib_step(P1_HOT)  # 0x20 "hot state"
    assert not is_calib_step(P1_ERROR)  # 0x80 error push


# ---- calibration ----


def test_derive_calibration_reference_desk() -> None:
    # Real init-walk values from the reference desk (model index 4 -> g.u 44.0).
    calib = derive_calibration({5: 2816, 7: 5691, 9: 4})
    assert calib.base == 2816
    assert calib.gu == 44.0
    assert calib.max_run == 2875


def test_derive_calibration_other_model() -> None:
    calib = derive_calibration({5: 3000, 7: 6600, 9: 3})
    assert calib.base == 3000
    assert calib.gu == 11.0
    assert calib.max_run == 3600


def test_derive_calibration_defaults_when_missing() -> None:
    calib = derive_calibration({})
    assert calib.base == DEFAULT_BASE
    assert calib.gu == DEFAULT_GU
    assert calib.max_run == DEFAULT_MAX_RUN

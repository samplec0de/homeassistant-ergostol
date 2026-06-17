"""BLE connection + height control for an Ergostol desk."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import time as dt_time, timedelta

from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_QUIET_END,
    CONF_QUIET_START,
    DOMAIN,
    IDLE_POLL_INTERVAL,
    MOVE_TIMEOUT,
    STOP_LEAD_HALL,
    TOLERANCE_CM,
)
from .protocol import (
    DEFAULT_BASE,
    DEFAULT_GU,
    DEFAULT_MAX_RUN,
    MODEL_GU,
    NOTIFY_UUID,
    OP_DOWN,
    OP_INIT,
    OP_MIDDLE,
    OP_QUERY,
    OP_SIT,
    OP_STAND,
    OP_STOP,
    OP_UP,
    WRITE_UUID,
    build,
    parse,
)

_LOGGER = logging.getLogger(__name__)

PRESET_OPS = {"sit": OP_SIT, "middle": OP_MIDDLE, "stand": OP_STAND}


@dataclass
class ErgostolData:
    """State published to entities."""

    height_cm: float | None
    moving: bool
    available: bool


class ErgostolCoordinator(DataUpdateCoordinator[ErgostolData]):
    """Maintains a BLE connection and drives the desk."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, address: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {address}",
            update_interval=timedelta(seconds=IDLE_POLL_INTERVAL),
        )
        self.address = address
        self.entry = entry
        self._client: BleakClientWithServiceCache | None = None
        self._lock = asyncio.Lock()        # serialises high-level operations
        self._write_lock = asyncio.Lock()  # serialises raw GATT writes
        self._abort = asyncio.Event()      # set by Stop to interrupt a move
        self._last_hall: int | None = None
        self._height_event = asyncio.Event()
        self._calib: dict[int, int] = {}
        self._gu = DEFAULT_GU
        self._base = DEFAULT_BASE
        self._max_run = DEFAULT_MAX_RUN
        self._moving = False
        self._height_cm: float | None = None

    # ---- conversions ----
    def hall_to_cm(self, hall: int) -> float:
        return (hall + self._base) / self._gu

    def cm_to_hall(self, cm: float) -> int:
        return round(cm * self._gu - self._base)

    @property
    def min_cm(self) -> float:
        return round(self.hall_to_cm(0), 1)

    @property
    def max_cm(self) -> float:
        return round(self.hall_to_cm(self._max_run), 1)

    # ---- connection ----
    def _on_disconnect(self, _client) -> None:
        _LOGGER.debug("Ergostol %s disconnected", self.address)
        self._client = None

    async def _ensure_connected(self) -> None:
        if self._client is not None and self._client.is_connected:
            return
        ble_device: BLEDevice | None = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise UpdateFailed(f"Ergostol {self.address} not in range / no adapter")
        self._client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            self.address,
            self._on_disconnect,
        )
        await self._client.start_notify(NOTIFY_UUID, self._handle_notify)
        await self._init_walk()

    def _handle_notify(self, _char, data: bytearray) -> None:
        parsed = parse(bytes(data))
        if parsed is None:
            return
        op, p1, hall = parsed
        if op in (OP_QUERY, OP_STOP):
            self._last_hall = hall
            self._height_event.set()
            # Reflect pushed / handset-driven height changes immediately. During
            # our own moves the move loop already publishes, so skip then.
            if not self._moving and self.data is not None:
                self._publish(hall, moving=False)
        elif op == OP_INIT:
            self._calib[p1] = hall

    async def _write(self, payload: bytes) -> None:
        # Serialised so Stop can write op-9 while a move loop is also writing.
        async with self._write_lock:
            if self._client is None:
                return
            await self._client.write_gatt_char(WRITE_UUID, payload, response=False)

    async def _init_walk(self) -> None:
        """Read the desk's calibration (base hall, model -> g.u, travel range)."""
        self._calib = {}
        for p1 in range(1, 12):
            await self._write(build(OP_INIT, p1))
            await asyncio.sleep(0.15)
        await asyncio.sleep(0.3)
        self._base = self._calib.get(5, DEFAULT_BASE)
        self._gu = MODEL_GU.get(self._calib.get(9), DEFAULT_GU)
        max_abs = self._calib.get(7)
        self._max_run = (max_abs - self._base) if max_abs else DEFAULT_MAX_RUN
        _LOGGER.debug(
            "Ergostol %s calibrated: base=%s g.u=%s range=%s..%s cm",
            self.address, self._base, self._gu, self.min_cm, self.max_cm,
        )

    async def _read_height_hall(self, tries: int = 4) -> int | None:
        for _ in range(tries):
            self._height_event.clear()
            await self._write(build(OP_QUERY))
            try:
                await asyncio.wait_for(self._height_event.wait(), 0.5)
                return self._last_hall
            except asyncio.TimeoutError:
                continue
        return None

    # ---- quiet hours ----
    def _in_quiet_hours(self) -> bool:
        qs = self.entry.options.get(CONF_QUIET_START)
        qe = self.entry.options.get(CONF_QUIET_END)
        if not qs or not qe:
            return False
        try:
            sh, sm = (int(x) for x in qs.split(":")[:2])
            eh, em = (int(x) for x in qe.split(":")[:2])
        except ValueError:
            return False
        start, end = dt_time(sh, sm), dt_time(eh, em)
        if start == end:
            return False
        now = dt_util.now().time()
        if start < end:
            return start <= now < end
        return now >= start or now < end  # window wraps past midnight

    # ---- coordinator poll ----
    async def _async_update_data(self) -> ErgostolData:
        # During quiet hours skip the height query so the desk's LED panel stays
        # dark. Explicit moves (set height / presets / stop) still work.
        if self._in_quiet_hours():
            return ErgostolData(
                height_cm=self._height_cm, moving=self._moving, available=True
            )
        async with self._lock:
            await self._ensure_connected()
            hall = await self._read_height_hall()
            if hall is not None:
                self._height_cm = round(self.hall_to_cm(hall), 1)
        return ErgostolData(
            height_cm=self._height_cm, moving=self._moving, available=True
        )

    def _publish(self, hall: int | None, moving: bool) -> None:
        if hall is not None:
            self._height_cm = round(self.hall_to_cm(hall), 1)
        self.async_set_updated_data(
            ErgostolData(height_cm=self._height_cm, moving=moving, available=True)
        )

    # ---- actions ----
    async def async_set_height(self, cm: float) -> None:
        async with self._lock:
            await self._ensure_connected()
            await self._move_to(cm)

    async def _move_to(self, cm: float) -> None:
        target = max(0, min(self._max_run, self.cm_to_hall(cm)))
        tol = max(2, round(TOLERANCE_CM * self._gu))
        cur = await self._read_height_hall()
        if cur is None:
            raise HomeAssistantError("No height feedback from the desk")
        if abs(cur - target) <= tol:
            return
        direction = OP_UP if cur < target else OP_DOWN
        brake = target - STOP_LEAD_HALL if direction == OP_UP else target + STOP_LEAD_HALL
        self._abort.clear()
        self._moving = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + MOVE_TIMEOUT
        last, last_prog = cur, loop.time()
        try:
            await self._write(build(direction))
            while not self._abort.is_set():
                await self._write(build(direction))
                cur = await self._read_height_hall(tries=2)
                now = loop.time()
                if cur is None:
                    if now > deadline:
                        break
                    continue
                self._publish(cur, moving=True)
                if (direction == OP_UP and cur >= brake) or (
                    direction == OP_DOWN and cur <= brake
                ):
                    break
                if abs(cur - last) > 1:
                    last, last_prog = cur, now
                elif now - last_prog > 3.0:
                    break  # reached a hard limit / stalled
                if now > deadline:
                    break
            await self._brake()
            # Fine approach: short pulses toward the target. Stop once within
            # tolerance, or as soon as a pulse crosses the target (direction
            # flips) — that is the closest a single pulse can land without
            # hunting back and forth.
            prev_dir = None
            for _ in range(10):
                if self._abort.is_set():
                    break
                cur = await self._read_height_hall()
                if cur is None or abs(cur - target) <= tol:
                    break
                d = OP_UP if cur < target else OP_DOWN
                if prev_dir is not None and d != prev_dir:
                    break
                prev_dir = d
                await self._write(build(d))
                await asyncio.sleep(0.13)
                await self._brake()
        finally:
            self._moving = False
            cur = await self._read_height_hall()
            self._publish(cur, moving=False)

    async def _brake(self) -> None:
        await self._write(build(OP_STOP))
        await asyncio.sleep(0.35)
        await self._write(build(OP_STOP))

    async def async_stop(self) -> None:
        # Must NOT take self._lock: a move in progress holds it for its whole
        # duration. Signal the move loop to abort and send op-9 right away
        # (the write lock keeps it from colliding with the move's writes).
        self._abort.set()
        if self._client is not None and self._client.is_connected:
            for _ in range(2):
                try:
                    await self._write(build(OP_STOP))
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(0.2)
        if not self._moving:
            # No move loop running (idle / handset) — refresh state ourselves.
            try:
                hall = await self._read_height_hall()
                self._publish(hall, moving=False)
            except Exception:  # noqa: BLE001
                pass

    async def async_preset(self, which: str) -> None:
        """Recall a stored preset (the desk drives to it and stops itself)."""
        op = PRESET_OPS[which]
        async with self._lock:
            await self._ensure_connected()
            self._abort.clear()
            self._moving = True
            try:
                await self._write(build(op))
                loop = asyncio.get_running_loop()
                deadline = loop.time() + MOVE_TIMEOUT
                last = await self._read_height_hall()
                last_prog = loop.time()
                while loop.time() < deadline and not self._abort.is_set():
                    await asyncio.sleep(0.4)
                    cur = await self._read_height_hall(tries=2)
                    if cur is None:
                        continue
                    self._publish(cur, moving=True)
                    if abs(cur - (last or cur)) <= 1:
                        if loop.time() - last_prog > 1.5:
                            break
                    else:
                        last, last_prog = cur, loop.time()
            finally:
                self._moving = False
                cur = await self._read_height_hall()
                self._publish(cur, moving=False)

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

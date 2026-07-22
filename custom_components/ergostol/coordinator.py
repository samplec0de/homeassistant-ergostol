"""BLE connection + height control for an Ergostol desk."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import time as dt_time, timedelta
import logging
import time

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CALIBRATION,
    CONF_QUIET_END,
    CONF_QUIET_START,
    CONF_SIT_HEIGHT,
    CONF_STAND_HEIGHT,
    CONNECT_MAX_ATTEMPTS,
    CONNECT_SETTLE_DELAY,
    DEFAULT_SIT_HEIGHT,
    DEFAULT_STAND_HEIGHT,
    DOMAIN,
    IDLE_POLL_INTERVAL,
    INIT_STEP_RETRIES,
    INIT_STEP_TIMEOUT,
    MOVE_TIMEOUT,
    RECONNECT_COOLDOWN,
    STOP_LEAD_HALL,
    TOLERANCE_CM,
)
from .protocol import (
    DEFAULT_BASE,
    DEFAULT_GU,
    DEFAULT_MAX_RUN,
    NOTIFY_UUID,
    OP_DOWN,
    OP_HANDSHAKE,
    OP_HEARTBEAT,
    OP_INIT,
    OP_MIDDLE,
    OP_QUERY,
    OP_SIT,
    OP_STAND,
    OP_STOP,
    OP_UP,
    P1_ERROR,
    P1_HOT,
    WRITE_UUID,
    Calibration,
    build,
    derive_calibration,
    error_key,
    handshake_ack,
    is_calib_step,
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
    error_code: int = 0  # desk fault as pushed in p1=0x80 frames (4 = E04)


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
        self._lock = asyncio.Lock()  # serialises high-level operations
        self._write_lock = asyncio.Lock()  # serialises raw GATT writes
        self._abort = asyncio.Event()  # set by Stop to interrupt a move
        self._last_hall: int | None = None
        self._height_event = asyncio.Event()
        self._calib: dict[int, int] = {}
        self._calib_event = asyncio.Event()
        cached = entry.data.get(CONF_CALIBRATION) or {}
        self._gu = cached.get("gu", DEFAULT_GU)
        self._base = cached.get("base", DEFAULT_BASE)
        self._max_run = cached.get("max_run", DEFAULT_MAX_RUN)
        self._calibrated = bool(cached)
        self._error_code = 0
        self._last_hs: tuple[int, float] | None = None  # (stage, ts) ack dedup
        self._last_drop: float | None = None  # spontaneous-drop ts (cooldown)
        self._expected_disconnect = False  # our own disconnect in progress
        self._requery_pending = False  # handset-move op-8 ping-pong armed
        self._moving = False
        self._height_cm: float | None = None
        self._target_hall: int | None = None  # last commanded target (for snap)
        self._silent_since: float | None = None  # op-8 unanswered since (monotonic)

    # ---- conversions ----
    def hall_to_cm(self, hall: int) -> float:
        return (hall + self._base) / self._gu

    def cm_to_hall(self, cm: float) -> int:
        return round(cm * self._gu - self._base)

    def _display_cm(self, hall: int) -> float:
        # The desk positions to ~0.1 cm (motor start/stop granularity). When it
        # settles within ~0.15 cm of the height we commanded, report exactly the
        # commanded value so "set 72 -> shows 72.0". Handset moves (no target
        # nearby) show the real height.
        if self._target_hall is not None and abs(hall - self._target_hall) <= round(
            0.15 * self._gu
        ):
            return round(self.hall_to_cm(self._target_hall), 1)
        return round(self.hall_to_cm(hall), 1)

    @property
    def min_cm(self) -> float:
        return round(self.hall_to_cm(0), 1)

    @property
    def max_cm(self) -> float:
        return round(self.hall_to_cm(self._max_run), 1)

    @property
    def sit_height(self) -> float:
        return float(self.entry.options.get(CONF_SIT_HEIGHT, DEFAULT_SIT_HEIGHT))

    @property
    def stand_height(self) -> float:
        return float(self.entry.options.get(CONF_STAND_HEIGHT, DEFAULT_STAND_HEIGHT))

    # ---- connection ----
    def _on_disconnect(self, _client) -> None:
        if self._expected_disconnect:
            _LOGGER.debug("Ergostol %s: BLE link closed (expected)", self.address)
        else:
            # Spontaneous drop: start the reconnect cooldown. A connect/drop
            # storm (bleak-retry hammering a flapping link) preceded E04 in
            # the field logs — never reconnect instantly.
            self._last_drop = time.monotonic()
            # INFO on purpose: spontaneous drops are a key E04 diagnostic.
            _LOGGER.info("Ergostol %s: BLE link closed", self.address)
        self._client = None

    async def _ensure_connected(self, force: bool = False) -> None:
        if self._client is not None and self._client.is_connected:
            return
        if not force and self._last_drop is not None:
            since = time.monotonic() - self._last_drop
            if since < RECONNECT_COOLDOWN:
                raise UpdateFailed(
                    f"Ergostol {self.address}: link dropped {since:.0f}s ago — "
                    "cooling down before reconnecting"
                )
        ble_device: BLEDevice | None = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise UpdateFailed(f"Ergostol {self.address} not in range / no adapter")
        try:
            self._client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                self.address,
                self._on_disconnect,
                max_attempts=CONNECT_MAX_ATTEMPTS,
            )
            _LOGGER.info("Ergostol %s: BLE connected", self.address)
            await self._client.start_notify(NOTIFY_UUID, self._handle_notify)
            # Give the module a moment to finish waking its side of the
            # handset<->controller bus before we write anything — a write
            # burst right at wake-up is the prime E04 suspect.
            await asyncio.sleep(CONNECT_SETTLE_DELAY)
            if not self._calibrated:
                await self._init_walk()
        except (BleakError, TimeoutError) as err:
            # Transient: the desk stopped advertising, the adapter is out of
            # connection slots, or GATT setup failed. Wrap as UpdateFailed so the
            # coordinator logs one clean line and retries, instead of dumping a
            # full traceback under "Unexpected error" on every poll. Drop any
            # half-open client so the next poll reconnects from a clean state.
            await self._disconnect()
            raise UpdateFailed(
                f"Ergostol {self.address}: BLE connect failed: {err}"
            ) from err

    def _handle_notify(self, _char, data: bytearray) -> None:
        parsed = parse(bytes(data))
        if parsed is None:
            return
        op, p1, hall = parsed
        if p1 == P1_ERROR:
            # Fault push (any op): the code the handset displays, 0 = cleared.
            # Must be checked before the op dispatch — an op-8 error frame
            # would otherwise be read as a height.
            self._set_error(hall)
            return
        if op == OP_INIT:
            if is_calib_step(p1):
                self._calib[p1] = hall
                self._calib_event.set()
            elif p1 == P1_HOT:
                # "hot state" push — the vendor app answers with a heartbeat.
                _LOGGER.debug(
                    "Ergostol %s: hot-state push d=%s — answering op-12",
                    self.address,
                    hall,
                )
                self.hass.async_create_task(self._write(build(OP_HEARTBEAT)))
            else:
                _LOGGER.debug(
                    "Ergostol %s: status frame op=7 p1=0x%02X d=%s",
                    self.address,
                    p1,
                    hall,
                )
            return
        if op == OP_HANDSHAKE:
            self._handle_handshake(hall)
            return
        if op == OP_HEARTBEAT:
            # p1=1 response to our op-12, p1=2 unsolicited report — the vendor
            # app sends nothing back for either (Android uses its own timers).
            _LOGGER.debug(
                "Ergostol %s: heartbeat frame p1=%s d=%s", self.address, p1, hall
            )
            return
        if op in (OP_QUERY, OP_STOP):
            prev = self._last_hall
            self._last_hall = hall
            self._height_event.set()
            # Reflect pushed / handset-driven height changes immediately. During
            # our own moves the move loop already publishes, so skip then.
            if not self._moving and self.data is not None:
                self._publish(hall, moving=False)
                # Handset-driven motion: keep the op-8 ping-pong going while
                # the height keeps changing (the vendor app streams the same
                # way while "running"), so slow idle polling loses nothing.
                if prev is not None and abs(hall - prev) > 1:
                    self.hass.async_create_task(self._requery_soon())
            return
        _LOGGER.debug(
            "Ergostol %s: unhandled frame op=%s p1=%s d=%s",
            self.address,
            op,
            p1,
            hall,
        )

    def _handle_handshake(self, stage: int) -> None:
        # The desk RETRIES an op-11 stage in rapid bursts until answered (seen
        # live: 8 identical d=0 frames during one handset move). Answer each
        # stage once per burst — more writes on the fragile handset bus is
        # exactly what we are trying to avoid.
        now = time.monotonic()
        if self._last_hs and self._last_hs[0] == stage and now - self._last_hs[1] < 1.0:
            return
        self._last_hs = (stage, now)
        ack = handshake_ack(stage)
        if ack is not None:
            _LOGGER.debug(
                "Ergostol %s: handshake stage %s — acking with %s",
                self.address,
                stage,
                ack,
            )
            self.hass.async_create_task(self._write(build(OP_HANDSHAKE, 1, 0, ack)))
        elif stage == 2:
            # "Finished, start to get height" — mirror the app with one op-8 so
            # the handset-driven height lands immediately.
            _LOGGER.debug(
                "Ergostol %s: handshake stage 2 — requesting height", self.address
            )
            self.hass.async_create_task(self._write(build(OP_QUERY)))
        else:
            _LOGGER.debug(
                "Ergostol %s: handshake stage %s (no reply expected)",
                self.address,
                stage,
            )

    def _set_error(self, code: int) -> None:
        if code == self._error_code:
            return
        self._error_code = code
        if code:
            _LOGGER.warning(
                "Ergostol %s: desk reports fault %s (code %s)",
                self.address,
                error_key(code).upper(),
                code,
            )
        else:
            _LOGGER.info("Ergostol %s: desk fault cleared", self.address)
        if self.data is not None:
            self._publish(None, moving=self._moving)

    async def _write(self, payload: bytes) -> None:
        # Serialised so Stop can write op-9 while a move loop is also writing.
        async with self._write_lock:
            if self._client is None:
                return
            await self._client.write_gatt_char(WRITE_UUID, payload, response=False)

    async def _init_walk(self) -> None:
        """Read the desk's calibration (base hall, model -> g.u, travel range).

        Reply-paced like the vendor app: send one op-7 step and wait for its
        reply before the next (the app's BLE lib repeats a frame until any
        notify arrives — same effect). Our old blind 0.15s-cadence burst right
        after wake-up collided with the desk's own traffic on the shared
        handset<->controller bus (the E04 "communication fault").
        """
        self._calib = {}
        for p1 in range(1, 12):
            for _ in range(INIT_STEP_RETRIES):
                self._calib_event.clear()
                await self._write(build(OP_INIT, p1))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._calib_event.wait(), INIT_STEP_TIMEOUT)
                if p1 in self._calib:
                    break
        missing = [p1 for p1 in (5, 7, 9) if p1 not in self._calib]
        if missing:
            # No usable calibration — treat as a failed connect attempt (the
            # caller wraps this into UpdateFailed) rather than caching guesses.
            raise TimeoutError(f"init walk unanswered for steps {missing}")
        cal = derive_calibration(self._calib)
        self._base, self._gu, self._max_run = cal.base, cal.gu, cal.max_run
        self._calibrated = True
        self._store_calibration(cal)
        _LOGGER.info(
            "Ergostol %s calibrated: base=%s g.u=%s range=%s..%s cm",
            self.address,
            self._base,
            self._gu,
            self.min_cm,
            self.max_cm,
        )

    def _store_calibration(self, cal: Calibration) -> None:
        # Persist so reconnects and restarts skip the walk entirely. Written
        # only when the values change: the entry update listener reloads the
        # integration, so an unconditional write would reload on every walk.
        as_dict = {"base": cal.base, "gu": cal.gu, "max_run": cal.max_run}
        if self.entry.data.get(CONF_CALIBRATION) != as_dict:
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_CALIBRATION: as_dict}
            )

    async def _read_height_hall(self, tries: int = 4) -> int | None:
        for _ in range(tries):
            self._height_event.clear()
            await self._write(build(OP_QUERY))
            try:
                await asyncio.wait_for(self._height_event.wait(), 0.5)
            except TimeoutError:
                continue
            if self._silent_since is not None:
                _LOGGER.warning(
                    "Ergostol %s: op-8 replies resumed after %.0f s of silence",
                    self.address,
                    time.monotonic() - self._silent_since,
                )
                self._silent_since = None
            return self._last_hall
        # GATT connected but the controller bus does not answer op-8 — the
        # E04 signature. Log once per silence episode, not per poll.
        if self._silent_since is None and self._client is not None:
            self._silent_since = time.monotonic()
            _LOGGER.warning(
                "Ergostol %s: connected but no reply to op-8 after %d tries "
                "(controller bus silent — E04?)",
                self.address,
                tries,
            )
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
        # During quiet hours drop the BLE link entirely instead of holding it
        # open in silence: after hours without traffic the desk's BLE module
        # wedges, jamming the controller bus (E04 "communication fault", the
        # handset stops responding, no advertising until a power-cycle).
        # Disconnected overnight is the desk's normal state; explicit moves
        # (set height / presets / stop) still reconnect on demand.
        if self._in_quiet_hours():
            async with self._lock:
                if self._client is not None:
                    _LOGGER.info(
                        "Ergostol %s: quiet hours — dropping BLE for the night",
                        self.address,
                    )
                await self._disconnect()
            return ErgostolData(
                height_cm=self._height_cm,
                moving=self._moving,
                available=True,
                error_code=self._error_code,
            )
        async with self._lock:
            await self._ensure_connected()
            hall = await self._read_height_hall()
            if hall is not None:
                self._height_cm = self._display_cm(hall)
        return ErgostolData(
            height_cm=self._height_cm,
            moving=self._moving,
            available=True,
            error_code=self._error_code,
        )

    def _publish(self, hall: int | None, moving: bool) -> None:
        if hall is not None:
            self._height_cm = self._display_cm(hall)
        self.async_set_updated_data(
            ErgostolData(
                height_cm=self._height_cm,
                moving=moving,
                available=True,
                error_code=self._error_code,
            )
        )

    # ---- actions ----
    async def async_set_height(self, cm: float) -> None:
        async with self._lock:
            # Explicit user action: one deliberate connect is not a storm, so
            # it may bypass the post-drop cooldown.
            await self._ensure_connected(force=True)
            await self._move_to(cm)

    async def _move_to(self, cm: float) -> None:
        target = max(0, min(self._max_run, self.cm_to_hall(cm)))
        self._target_hall = target  # snap the displayed height to this value
        tol = max(2, round(TOLERANCE_CM * self._gu))
        cur = await self._read_height_hall()
        if cur is None:
            raise HomeAssistantError("No height feedback from the desk")
        if abs(cur - target) <= tol:
            return
        direction = OP_UP if cur < target else OP_DOWN
        brake = (
            target - STOP_LEAD_HALL if direction == OP_UP else target + STOP_LEAD_HALL
        )
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
                with contextlib.suppress(Exception):
                    await self._write(build(OP_STOP))
                await asyncio.sleep(0.2)
        if not self._moving:
            # No move loop running (idle / handset) — refresh state ourselves.
            try:
                hall = await self._read_height_hall()
                self._publish(hall, moving=False)
            except Exception:
                pass

    async def async_preset(self, which: str) -> None:
        """Recall a stored preset (the desk drives to it and stops itself)."""
        op = PRESET_OPS[which]
        async with self._lock:
            await self._ensure_connected(force=True)
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

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            self._expected_disconnect = True
            try:
                with contextlib.suppress(Exception):
                    await client.disconnect()
            finally:
                self._expected_disconnect = False

    async def _requery_soon(self) -> None:
        if self._requery_pending:
            return
        self._requery_pending = True
        try:
            await asyncio.sleep(0.4)
            with contextlib.suppress(Exception):
                await self._write(build(OP_QUERY))
        finally:
            self._requery_pending = False

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self._disconnect()

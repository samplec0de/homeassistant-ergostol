#!/usr/bin/env python3
"""Управление столом Ergostol по BLE с macOS.

Реконструировано из приложения com.pairlink.ergostol (см. PROTOCOL.md).

Использование:
    ergostol.py scan                       # список ближайших BLE-устройств
    ergostol.py info   [--address UUID]    # подключиться, вывести калибровку + высоту
    ergostol.py monitor [--address UUID]   # стрим живой высоты
    ergostol.py up|down [--secs N]         # подвинуть на N секунд (по умолч. 1.0)
    ergostol.py stop
    ergostol.py set CM  [--address UUID]   # доехать до высоты (см) по контуру
    ergostol.py preset stand|middle|sit    # вызвать сохранённый пресет
"""

import argparse
import asyncio
import contextlib
import sys

from bleak import BleakClient, BleakScanner

WRITE_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"

# Точная модель высоты, воспроизведённая из приложения (utils/c.java b(int) +
# setDeskHeight): real_cm = (run_hall + base_hall) / g.u
# op-8 возвращает run_hall (0 в самом низу). base_hall = параметр 5 init op-7.
# g.u выбирается по индексу модели стола = параметр 9 init op-7 (= версия MCU, bArr[3]).
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

# Значения по умолчанию для известного стола (модель 4); перезаписываются
# на каждом подключении в init_walk.
GU = 44.0
BASE = 2816


def hall_to_cm(h):
    return (h + BASE) / GU


def cm_to_hall(cm):
    return round(cm * GU - BASE)


# коды операций
OP_DOWN, OP_UP = 1, 2
OP_STAND, OP_MIDDLE, OP_SIT = 3, 4, 5
OP_QUERY, OP_STOP, OP_INIT = 8, 9, 7

_TABLE = [
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


def crc16(buf):
    crc = 0xFFFF
    for b in buf:
        crc = ((crc >> 4) ^ _TABLE[(crc & 0xF) ^ (b & 0xF & 0x7F)]) & 0xFFFF
        crc = ((crc >> 4) ^ _TABLE[(crc & 0xF) ^ ((b >> 4) & 0xF & 0x7F)]) & 0xFFFF
    return crc


def build(op, p1=1, d_hi=0, d_lo=0):
    body = [op & 0xFF, p1 & 0xFF, d_hi & 0xFF, d_lo & 0xFF]
    c = crc16(bytes([4, 252, 66, 6, *body]))
    return bytes([*body, c & 255, c >> 8 & 255])


def hall_of(data):
    return ((data[2] & 0xFF) << 8) | (data[3] & 0xFF)


# ---------- поиск устройства ----------


async def find_device(address=None, timeout=8.0):
    if address:
        dev = await BleakScanner.find_device_by_address(address, timeout=timeout)
        if dev:
            return dev
    # авто: выбираем устройство с сервисом ff01/ff02 или с похожим на стол именем
    print(f"Сканирование {timeout:.0f}с в поиске адаптера стола...", file=sys.stderr)
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    candidates = []
    for dev, adv in found.values():
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        name = (adv.local_name or dev.name or "").lower()
        score = 0
        if any(u.startswith("0000ff0") for u in uuids):
            score += 10
        if any(k in name for k in ("ergo", "stol", "desk", "ding", "pairlink", "bt")):
            score += 5
        if score:
            candidates.append((score, dev, adv))
    if not candidates:
        return None
    candidates.sort(key=lambda x: -x[0])
    return candidates[0][1]


# ---------- помощник подключения ----------


class Desk:
    def __init__(self, client):
        self.client = client
        self.last_hall = None
        self.calib = {}  # p1 -> hall из init walk
        self._listeners = []
        self.write_char = None
        self.notify_char = None

    def _resolve_chars(self):
        """Найти характеристики записи и уведомлений: предпочитаем ff01/ff02,
        иначе берём любую пару write/notify (GATT может использовать другие UUID)."""
        write = notify = None
        for s in self.client.services:
            for ch in s.characteristics:
                u = ch.uuid.lower()
                props = ch.properties
                if u == WRITE_UUID:
                    write = ch
                elif u == NOTIFY_UUID:
                    notify = ch
                if write is None and (
                    "write" in props or "write-without-response" in props
                ):
                    write = write or ch
                if notify is None and ("notify" in props or "indicate" in props):
                    notify = notify or ch
        # точное совпадение по UUID имеет приоритет, если оно есть
        for s in self.client.services:
            for ch in s.characteristics:
                if ch.uuid.lower() == WRITE_UUID:
                    write = ch
                if ch.uuid.lower() == NOTIFY_UUID:
                    notify = ch
        self.write_char, self.notify_char = write, notify
        return write, notify

    async def start_notify(self):
        write, notify = self._resolve_chars()
        if notify is None or write is None:
            raise RuntimeError(
                "Не найдены характеристики write/notify. Таблица GATT:\n"
                + "\n".join(
                    f"  {s.uuid}: "
                    + ", ".join(
                        f"{c.uuid}({'/'.join(c.properties)})" for c in s.characteristics
                    )
                    for s in self.client.services
                )
            )
        print(
            f"write  char: {write.uuid} ({'/'.join(write.properties)})", file=sys.stderr
        )
        print(
            f"notify char: {notify.uuid} ({'/'.join(notify.properties)})",
            file=sys.stderr,
        )
        await self.client.start_notify(notify, self._on_notify)

    def _on_notify(self, _char, data):
        b = bytes(data)
        if len(b) >= 4:
            op = b[0]
            # Высота приходит только из ответов query/run (8) и stop (9). Остальные
            # пакеты (эхо команд, напоминания) несут 0 в байтах 2-3 и иначе
            # испортили бы last_hall.
            if op in (OP_QUERY, OP_STOP):
                self.last_hall = hall_of(b)
            if op == OP_INIT:
                self.calib[b[1]] = hall_of(b)
        for cb in self._listeners:
            cb(b)

    async def send(self, payload):
        ch = self.write_char or WRITE_UUID
        no_resp = (
            getattr(ch, "properties", [])
            and "write" not in ch.properties
            and "write-without-response" in ch.properties
        )
        try:
            await self.client.write_gatt_char(ch, payload, response=not no_resp)
        except Exception:
            await self.client.write_gatt_char(ch, payload, response=no_resp)

    async def cmd(self, op, p1=1, d_hi=0, d_lo=0):
        await self.send(build(op, p1, d_hi, d_lo))

    async def init_walk(self, timeout=4.0):
        """Обойти op-7 p1=1..8 для чтения калибровки; вернуть dict p1->hall."""
        done = asyncio.Event()

        def watch(b):
            if b and b[0] == OP_INIT and b[1] >= 8:
                done.set()

        self._listeners.append(watch)
        try:
            for p1 in list(range(1, 12)):
                await self.cmd(OP_INIT, p1)
                await asyncio.sleep(0.15)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(done.wait(), timeout=timeout)
        finally:
            self._listeners.remove(watch)
        # Устанавливаем точный пересчёт в см из собственной калибровки стола.
        global GU, BASE  # noqa: PLW0603
        if self.calib.get(5) is not None:
            BASE = self.calib[5]
        model = self.calib.get(9)
        if model in MODEL_GU:
            GU = MODEL_GU[model]
        return dict(self.calib)


async def connected_desk(address):
    dev = await find_device(address)
    if not dev:
        print(
            "Адаптер стола не найден. Он включён и виден в эфире? "
            "Запустите `ergostol.py scan` для списка устройств.",
            file=sys.stderr,
        )
        sys.exit(2)
    print(f"Подключение к {dev.name or '?'} [{dev.address}]...", file=sys.stderr)
    client = BleakClient(dev)
    await client.connect()
    try:
        desk = Desk(client)
        await desk.start_notify()
    except Exception:
        await client.disconnect()
        raise
    return client, desk


# ---------- команды ----------


async def cmd_scan(args):
    found = await BleakScanner.discover(timeout=args.secs, return_adv=True)
    rows = []
    for dev, adv in found.values():
        uuids = ",".join(adv.service_uuids or [])
        rows.append(
            (adv.rssi or -999, dev.address, adv.local_name or dev.name or "", uuids)
        )
    rows.sort(key=lambda r: -r[0])
    print(f"{'RSSI':>5}  {'ADDRESS':36}  ИМЯ / сервисы")
    for rssi, addr, name, uuids in rows:
        mark = (
            "  <-- кандидат"
            if (
                "ff0" in uuids.lower()
                or any(k in name.lower() for k in ("ergo", "stol", "ding", "desk"))
            )
            else ""
        )
        suffix = f"  [{uuids}]" if uuids else ""
        print(f"{rssi:>5}  {addr:36}  {name}{suffix}{mark}")


async def cmd_info(args):
    client, desk = await connected_desk(args.address)
    try:
        calib = await desk.init_walk()
        base = calib.get(5, 0)
        mn, mx = calib.get(6), calib.get(7)
        print("Калибровка (hall):", calib)
        if mn is not None:
            print(f"мин. высота: {hall_to_cm(mn - base):6.1f} см")
        if mx is not None:
            print(f"макс. высота: {hall_to_cm(mx - base):6.1f} см")
        cur = await read_height(desk)
        if cur is not None:
            print(f"текущая    : {hall_to_cm(cur):6.1f} см (hall {cur})")
    finally:
        await client.disconnect()


async def cmd_monitor(args):
    client, desk = await connected_desk(args.address)

    def show(b):
        print(
            "notify:",
            " ".join(f"{x:02x}" for x in b),
            f"  hall={hall_of(b) if len(b) >= 4 else '?'}",
        )

    desk._listeners.append(show)
    try:
        await desk.cmd(OP_QUERY)
        print("Мониторинг (Ctrl-C для остановки)...", file=sys.stderr)
        await asyncio.sleep(args.secs)
    finally:
        await client.disconnect()


async def cmd_nudge(args, op):
    client, desk = await connected_desk(args.address)
    try:
        await desk.init_walk()  # выставить GU/BASE для верного показа в см
        before = await read_height(desk)
        if before is not None:
            print(f"до: {hall_to_cm(before):.1f} см")
        await desk.cmd(op)
        t = 0.0
        while t < args.secs:
            await asyncio.sleep(0.12)
            t += 0.12
            await desk.cmd(op)
            await desk.cmd(OP_QUERY)
            await asyncio.sleep(0.03)
            if desk.last_hall is not None:
                print(f"  {hall_to_cm(desk.last_hall):6.1f} см", file=sys.stderr)
    finally:
        await desk.cmd(OP_STOP)
        await asyncio.sleep(0.3)
        await desk.cmd(OP_STOP)
        after = await read_height(desk)
        await client.disconnect()
    if after is not None:
        print(f"после: {hall_to_cm(after):.1f} см")


async def cmd_stop(args):
    client, desk = await connected_desk(args.address)
    try:
        await desk.cmd(OP_STOP)
        await asyncio.sleep(0.2)
    finally:
        await client.disconnect()


async def cmd_preset(args):
    op = {"stand": OP_STAND, "middle": OP_MIDDLE, "sit": OP_SIT}[args.which]
    client, desk = await connected_desk(args.address)
    try:
        await desk.cmd(op)
        await asyncio.sleep(args.secs)
    finally:
        await client.disconnect()
    print(f"пресет {args.which} отправлен; last hall={desk.last_hall}")


async def read_height(desk, tries=8):
    """Опрашивать op-8, пока не получим свежее показание run-hall."""
    for _ in range(tries):
        desk.last_hall = None
        await desk.cmd(OP_QUERY)
        await asyncio.sleep(0.15)
        if desk.last_hall is not None:
            return desk.last_hall
    return None


async def cmd_set(args):
    # op-8 возвращает run hall (0..max_run). cm = (hall + base) / g.u.
    client, desk = await connected_desk(args.address)
    try:
        calib = await desk.init_walk()
        base = calib.get(5, BASE)
        max_run = (calib.get(7) - base) if calib.get(7) else round(65 * GU)
        target = cm_to_hall(args.cm)
        target = max(0, min(max_run, target))
        clamped = hall_to_cm(target)
        if abs(clamped - args.cm) > 0.1:
            print(
                f"ограничено диапазоном [{hall_to_cm(0):.1f}, "
                f"{hall_to_cm(max_run):.1f}] см -> {clamped:.1f} см",
                file=sys.stderr,
            )
        tol = round(0.4 * GU)  # ~0.4 см

        cur = await read_height(desk)
        if cur is None:
            print("Нет обратной связи по высоте; прерываю.", file=sys.stderr)
            return
        print(
            f"текущая {hall_to_cm(cur):.1f} см (hall {cur}) -> "
            f"цель {clamped:.1f} см (hall {target})"
        )
        if abs(cur - target) <= tol:
            print("уже на цели.")
            return

        # Каретка проезжает ~LEAD hall по инерции после STOP — тормозим заранее.
        LEAD = 55
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 60.0

        async def glide(tgt):
            """Ехать к tgt на полной скорости, тормозя за LEAD hall до цели."""
            cur = await read_height(desk, tries=3)
            if cur is None or abs(cur - tgt) <= tol:
                return cur
            direction = OP_UP if cur < tgt else OP_DOWN
            brake = (tgt - LEAD) if direction == OP_UP else (tgt + LEAD)
            print("еду", "ВВЕРХ" if direction == OP_UP else "ВНИЗ", file=sys.stderr)
            last, last_prog = cur, loop.time()
            await desk.cmd(direction)
            while True:
                await desk.cmd(direction)
                cur = await read_height(desk, tries=2)
                now = loop.time()
                if cur is None:
                    if now > deadline:
                        break
                    continue
                print(f"  {hall_to_cm(cur):6.1f} см (hall {cur})", file=sys.stderr)
                if (direction == OP_UP and cur >= brake) or (
                    direction == OP_DOWN and cur <= brake
                ):
                    break
                if abs(cur - last) > 1:
                    last, last_prog = cur, now
                elif now - last_prog > 3.0:
                    print("нет движения (достигнут предел?); стоп.", file=sys.stderr)
                    break
                if now > deadline:
                    print("таймаут; стоп.", file=sys.stderr)
                    break
            await desk.cmd(OP_STOP)
            await asyncio.sleep(0.35)
            await desk.cmd(OP_STOP)
            return await read_height(desk)

        async def nudge_to(tgt):
            """Точное доведение короткими импульсами (минимум инерции с места)."""
            for _ in range(6):
                cur = await read_height(desk)
                if cur is None or abs(cur - tgt) <= tol:
                    return cur
                d = OP_UP if cur < tgt else OP_DOWN
                await desk.cmd(d)
                await asyncio.sleep(0.18)
                await desk.cmd(OP_STOP)
                await asyncio.sleep(0.35)
                await desk.cmd(OP_STOP)
            return await read_height(desk)

        cur = await glide(target)
        if cur is not None and abs(cur - target) > tol:
            print(f"  доводка с {hall_to_cm(cur):.1f} см...", file=sys.stderr)
            cur = await nudge_to(target)
        final = cur if cur is not None else await read_height(desk)
        print(f"остановился на {hall_to_cm(final):.1f} см (hall {final})")
    finally:
        with contextlib.suppress(Exception):
            await desk.cmd(OP_STOP)
        await client.disconnect()


def main():
    p = argparse.ArgumentParser(description="BLE-контроллер стола Ergostol")
    p.add_argument("--address", help="BLE-адрес/UUID адаптера стола")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan")
    s.add_argument("--secs", type=float, default=8.0)
    sub.add_parser("info")
    m = sub.add_parser("monitor")
    m.add_argument("--secs", type=float, default=30.0)
    u = sub.add_parser("up")
    u.add_argument("--secs", type=float, default=1.0)
    d = sub.add_parser("down")
    d.add_argument("--secs", type=float, default=1.0)
    sub.add_parser("stop")
    st = sub.add_parser("set")
    st.add_argument("cm", type=float)
    pr = sub.add_parser("preset")
    pr.add_argument("which", choices=["stand", "middle", "sit"])
    pr.add_argument("--secs", type=float, default=20.0)

    args = p.parse_args()
    dispatch = {
        "scan": cmd_scan,
        "info": cmd_info,
        "monitor": cmd_monitor,
        "stop": cmd_stop,
        "set": cmd_set,
        "preset": cmd_preset,
        "up": lambda a: cmd_nudge(a, OP_UP),
        "down": lambda a: cmd_nudge(a, OP_DOWN),
    }
    asyncio.run(dispatch[args.cmd](args))


if __name__ == "__main__":
    main()

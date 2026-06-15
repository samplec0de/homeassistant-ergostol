#!/usr/bin/env python3
"""Control an Ergostol standing desk over BLE from macOS.

Reverse-engineered from com.pairlink.ergostol (see PROTOCOL.md).

Usage:
    ergostol.py scan                       # list nearby BLE devices
    ergostol.py info   [--address UUID]    # connect, print calibration + height
    ergostol.py monitor [--address UUID]   # stream live height
    ergostol.py up|down [--secs N]         # nudge for N seconds (default 1.0)
    ergostol.py stop
    ergostol.py set CM  [--address UUID]   # closed-loop move to absolute height (cm)
    ergostol.py preset stand|middle|sit    # recall a stored preset
"""
import argparse
import asyncio
import sys

from bleak import BleakClient, BleakScanner

WRITE_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"

# Exact height model, reproduced from the app (utils/c.java b(int) + setDeskHeight):
#   real_cm = (run_hall + base_hall) / g.u
# op-8 returns run_hall (0 at lowest). base_hall = init op-7 param 5. g.u is chosen
# by the desk model index = init op-7 param 9 (= MCU version, bArr[3]).
MODEL_GU = {1: 29.333334, 2: 29.333334, 3: 11.0, 4: 44.0, 5: 26.0,
            6: 58.666668, 7: 29.8, 8: 26.0, 9: 27.5, 10: 44.0, 11: 22.0}

# Defaults for the known desk (model 4); overwritten per-connection by init_walk.
GU = 44.0
BASE = 2816


def hall_to_cm(h):
    return (h + BASE) / GU


def cm_to_hall(cm):
    return round(cm * GU - BASE)

# opcodes
OP_DOWN, OP_UP = 1, 2
OP_STAND, OP_MIDDLE, OP_SIT = 3, 4, 5
OP_QUERY, OP_STOP, OP_INIT = 8, 9, 7

_TABLE = [0, 52225, 55297, 5120, 61441, 15360, 10240, 58369,
          40961, 27648, 30720, 46081, 20480, 39937, 34817, 17408]


def crc16(buf):
    crc = 0xFFFF
    for b in buf:
        crc = ((crc >> 4) ^ _TABLE[(crc & 0xF) ^ (b & 0xF & 0x7F)]) & 0xFFFF
        crc = ((crc >> 4) ^ _TABLE[(crc & 0xF) ^ ((b >> 4) & 0xF & 0x7F)]) & 0xFFFF
    return crc


def build(op, p1=1, d_hi=0, d_lo=0):
    body = [op & 0xFF, p1 & 0xFF, d_hi & 0xFF, d_lo & 0xFF]
    c = crc16(bytes([0x04, 0xFC, 0x42, 0x06] + body))
    return bytes(body + [c & 0xFF, (c >> 8) & 0xFF])


def hall_of(data):
    return ((data[2] & 0xFF) << 8) | (data[3] & 0xFF)


# ---------- device discovery ----------

async def find_device(address=None, timeout=8.0):
    if address:
        dev = await BleakScanner.find_device_by_address(address, timeout=timeout)
        if dev:
            return dev
    # auto: pick a device that advertises the ff01/ff02 service or a desk-like name
    print(f"Scanning {timeout:.0f}s for the desk adapter...", file=sys.stderr)
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


# ---------- connection helper ----------

class Desk:
    def __init__(self, client):
        self.client = client
        self.last_hall = None
        self.calib = {}          # p1 -> hall from init walk
        self._listeners = []
        self.write_char = None
        self.notify_char = None

    def _resolve_chars(self):
        """Find write + notify characteristics, preferring ff01/ff02 but
        falling back to any write/notify pair (GATT may use other UUIDs)."""
        write = notify = None
        for s in self.client.services:
            for ch in s.characteristics:
                u = ch.uuid.lower()
                props = ch.properties
                if u == WRITE_UUID:
                    write = ch
                elif u == NOTIFY_UUID:
                    notify = ch
                if write is None and ("write" in props or
                                      "write-without-response" in props):
                    write = write or ch
                if notify is None and ("notify" in props or "indicate" in props):
                    notify = notify or ch
        # exact UUID match wins if present
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
                "Could not find write/notify characteristics. GATT table:\n" +
                "\n".join(f"  {s.uuid}: " + ", ".join(
                    f"{c.uuid}({'/'.join(c.properties)})" for c in s.characteristics)
                    for s in self.client.services))
        print(f"write  char: {write.uuid} ({'/'.join(write.properties)})",
              file=sys.stderr)
        print(f"notify char: {notify.uuid} ({'/'.join(notify.properties)})",
              file=sys.stderr)
        await self.client.start_notify(notify, self._on_notify)

    def _on_notify(self, _char, data):
        b = bytes(data)
        if len(b) >= 4:
            op = b[0]
            # Height only comes from query/run (8) and stop (9) replies. Other
            # packets (command echoes, reminders) carry 0 in bytes 2-3 and would
            # otherwise corrupt last_hall.
            if op in (OP_QUERY, OP_STOP):
                self.last_hall = hall_of(b)
            if op == OP_INIT:
                self.calib[b[1]] = hall_of(b)
        for cb in self._listeners:
            cb(b)

    async def send(self, payload):
        ch = self.write_char or WRITE_UUID
        no_resp = getattr(ch, "properties", []) and \
            "write" not in ch.properties and \
            "write-without-response" in ch.properties
        try:
            await self.client.write_gatt_char(ch, payload, response=not no_resp)
        except Exception:
            await self.client.write_gatt_char(ch, payload, response=no_resp)

    async def cmd(self, op, p1=1, d_hi=0, d_lo=0):
        await self.send(build(op, p1, d_hi, d_lo))

    async def init_walk(self, timeout=4.0):
        """Walk op-7 p1=1..8 to read calibration; returns dict p1->hall."""
        done = asyncio.Event()

        def watch(b):
            if b and b[0] == OP_INIT and b[1] >= 8:
                done.set()

        self._listeners.append(watch)
        try:
            for p1 in list(range(1, 12)):
                await self.cmd(OP_INIT, p1)
                await asyncio.sleep(0.15)
            try:
                await asyncio.wait_for(done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        finally:
            self._listeners.remove(watch)
        # Set the exact cm conversion from the desk's own calibration.
        global GU, BASE
        if self.calib.get(5) is not None:
            BASE = self.calib[5]
        model = self.calib.get(9)
        if model in MODEL_GU:
            GU = MODEL_GU[model]
        return dict(self.calib)


async def connected_desk(address):
    dev = await find_device(address)
    if not dev:
        print("Desk adapter not found. Is it powered and advertising? "
              "Run `ergostol.py scan` to list devices.", file=sys.stderr)
        sys.exit(2)
    print(f"Connecting to {dev.name or '?'} [{dev.address}]...", file=sys.stderr)
    client = BleakClient(dev)
    await client.connect()
    try:
        desk = Desk(client)
        await desk.start_notify()
    except Exception:
        await client.disconnect()
        raise
    return client, desk


# ---------- commands ----------

async def cmd_scan(args):
    found = await BleakScanner.discover(timeout=args.secs, return_adv=True)
    rows = []
    for dev, adv in found.values():
        uuids = ",".join(adv.service_uuids or [])
        rows.append((adv.rssi or -999, dev.address, adv.local_name or dev.name or "", uuids))
    rows.sort(key=lambda r: -r[0])
    print(f"{'RSSI':>5}  {'ADDRESS':36}  NAME / services")
    for rssi, addr, name, uuids in rows:
        mark = "  <-- candidate" if ("ff0" in uuids.lower() or
               any(k in name.lower() for k in ("ergo", "stol", "ding", "desk"))) else ""
        print(f"{rssi:>5}  {addr:36}  {name}{('  ['+uuids+']') if uuids else ''}{mark}")


async def cmd_info(args):
    client, desk = await connected_desk(args.address)
    try:
        calib = await desk.init_walk()
        base = calib.get(5, 0)
        mn, mx = calib.get(6), calib.get(7)
        print("Calibration (hall):", calib)
        if mn is not None:
            print(f"min height: {hall_to_cm(mn - base):6.1f} cm")
        if mx is not None:
            print(f"max height: {hall_to_cm(mx - base):6.1f} cm")
        cur = await read_height(desk)
        if cur is not None:
            print(f"current   : {hall_to_cm(cur):6.1f} cm (hall {cur})")
    finally:
        await client.disconnect()


async def cmd_monitor(args):
    client, desk = await connected_desk(args.address)

    def show(b):
        print("notify:", " ".join(f"{x:02x}" for x in b),
              f"  hall={hall_of(b) if len(b) >= 4 else '?'}")
    desk._listeners.append(show)
    try:
        await desk.cmd(OP_QUERY)
        print("Monitoring (Ctrl-C to stop)...", file=sys.stderr)
        await asyncio.sleep(args.secs)
    finally:
        await client.disconnect()


async def cmd_nudge(args, op):
    client, desk = await connected_desk(args.address)
    try:
        await desk.init_walk()   # set GU/BASE for correct cm display
        before = await read_height(desk)
        if before is not None:
            print(f"before: {hall_to_cm(before):.1f} cm")
        await desk.cmd(op)
        t = 0.0
        while t < args.secs:
            await asyncio.sleep(0.12)
            t += 0.12
            await desk.cmd(op)
            await desk.cmd(OP_QUERY)
            await asyncio.sleep(0.03)
            if desk.last_hall is not None:
                print(f"  {hall_to_cm(desk.last_hall):6.1f} cm", file=sys.stderr)
    finally:
        await desk.cmd(OP_STOP)
        await asyncio.sleep(0.3)
        await desk.cmd(OP_STOP)
        after = await read_height(desk)
        await client.disconnect()
    if after is not None:
        print(f"after: {hall_to_cm(after):.1f} cm")


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
    print(f"preset {args.which} sent; last hall={desk.last_hall}")


async def read_height(desk, tries=8):
    """Poll op-8 until we get a fresh run-hall reading."""
    for _ in range(tries):
        desk.last_hall = None
        await desk.cmd(OP_QUERY)
        await asyncio.sleep(0.15)
        if desk.last_hall is not None:
            return desk.last_hall
    return None


async def cmd_set(args):
    # op-8 returns the run hall (0..max_run). cm = (hall + base) / g.u.
    client, desk = await connected_desk(args.address)
    try:
        calib = await desk.init_walk()
        base = calib.get(5, BASE)
        max_run = (calib.get(7) - base) if calib.get(7) else round(65 * GU)
        target = cm_to_hall(args.cm)
        target = max(0, min(max_run, target))
        clamped = hall_to_cm(target)
        if abs(clamped - args.cm) > 0.1:
            print(f"clamped to range [{hall_to_cm(0):.1f}, "
                  f"{hall_to_cm(max_run):.1f}] cm -> {clamped:.1f} cm",
                  file=sys.stderr)
        tol = round(0.4 * GU)   # ~0.4 cm

        cur = await read_height(desk)
        if cur is None:
            print("No height feedback; aborting.", file=sys.stderr)
            return
        print(f"current {hall_to_cm(cur):.1f} cm (hall {cur}) -> "
              f"target {clamped:.1f} cm (hall {target})")
        if abs(cur - target) <= tol:
            print("already at target.")
            return

        # The carriage coasts ~LEAD hall after STOP, so brake early.
        LEAD = 55
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 60.0

        async def glide(tgt):
            """Drive toward tgt at full speed, braking LEAD hall early."""
            cur = await read_height(desk, tries=3)
            if cur is None or abs(cur - tgt) <= tol:
                return cur
            direction = OP_UP if cur < tgt else OP_DOWN
            brake = (tgt - LEAD) if direction == OP_UP else (tgt + LEAD)
            print("moving", "UP" if direction == OP_UP else "DOWN",
                  file=sys.stderr)
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
                print(f"  {hall_to_cm(cur):6.1f} cm (hall {cur})", file=sys.stderr)
                if (direction == OP_UP and cur >= brake) or \
                   (direction == OP_DOWN and cur <= brake):
                    break
                if abs(cur - last) > 1:
                    last, last_prog = cur, now
                elif now - last_prog > 3.0:
                    print("no progress (limit reached?); stopping.", file=sys.stderr)
                    break
                if now > deadline:
                    print("timeout; stopping.", file=sys.stderr)
                    break
            await desk.cmd(OP_STOP)
            await asyncio.sleep(0.35)
            await desk.cmd(OP_STOP)
            return await read_height(desk)

        async def nudge_to(tgt):
            """Fine approach with short pulses (minimal coast from standstill)."""
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
            print(f"  fine-tuning from {hall_to_cm(cur):.1f} cm...", file=sys.stderr)
            cur = await nudge_to(target)
        final = cur if cur is not None else await read_height(desk)
        print(f"stopped at {hall_to_cm(final):.1f} cm (hall {final})")
    finally:
        try:
            await desk.cmd(OP_STOP)
        except Exception:
            pass
        await client.disconnect()


def main():
    p = argparse.ArgumentParser(description="Ergostol desk BLE controller")
    p.add_argument("--address", help="BLE address/UUID of the desk adapter")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan"); s.add_argument("--secs", type=float, default=8.0)
    sub.add_parser("info")
    m = sub.add_parser("monitor"); m.add_argument("--secs", type=float, default=30.0)
    u = sub.add_parser("up"); u.add_argument("--secs", type=float, default=1.0)
    d = sub.add_parser("down"); d.add_argument("--secs", type=float, default=1.0)
    sub.add_parser("stop")
    st = sub.add_parser("set"); st.add_argument("cm", type=float)
    pr = sub.add_parser("preset")
    pr.add_argument("which", choices=["stand", "middle", "sit"])
    pr.add_argument("--secs", type=float, default=20.0)

    args = p.parse_args()
    dispatch = {
        "scan": cmd_scan, "info": cmd_info, "monitor": cmd_monitor,
        "stop": cmd_stop, "set": cmd_set, "preset": cmd_preset,
        "up": lambda a: cmd_nudge(a, OP_UP),
        "down": lambda a: cmd_nudge(a, OP_DOWN),
    }
    asyncio.run(dispatch[args.cmd](args))


if __name__ == "__main__":
    main()

"""Step-by-step I2C diagnosis, with every line of the wire shown.

    python -m ver.tools.i2cdebug

When the code looks correct and the hardware disagrees, reading the code
harder does not help. This walks the bus one operation at a time and prints
exactly what was sent and exactly what came back, so the wire can settle the
argument instead of a theory.

Answers, in order:
  1. Is the firmware there, and is it v2 (I2C-capable)?
  2. Does the bus come up, and who's on it?
  3. Do READS work?      (WHO_AM_I is a known-value register)
  4. Do WRITES work?     (write a config register, read it back)
  5. Does DEVICE_RESET work?
  6. Is the chip awake, and does data actually change?
"""

import sys
import time

from ver.backends.esp32.backend import ESP32Backend
from ver.backends.esp32.transport import find_ports
from ver.hal.errors import VERError

WHO_AM_I = 0x75
PWR_MGMT_1 = 0x6B
ACCEL_CONFIG = 0x1C
SMPLRT_DIV = 0x19
ACCEL_XOUT_H = 0x3B

ADDRESS = 0x68


def rule(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 56 - len(title)))


def verdict(ok: bool, message: str) -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {message}")
    return ok


def main() -> None:
    ports = find_ports()
    if not ports:
        print("no board found. run:  python -m ver.tools.ports")
        return

    print(f"board: {ports[0][0]}  ({ports[0][1]})")
    print("every line the host sends and receives is shown below.\n")

    backend = ESP32Backend()
    bus = backend.i2c()
    bus._gpio._t.echo = True          # show the wire

    results = {}

    try:
        rule("1. firmware handshake")
        bus.open()
        print(f"  firmware: {backend.gpio().info().transport}")

        rule("2. bus scan")
        found = bus.scan()
        print(f"  found: {[hex(a) for a in found]}")
        results["scan"] = verdict(ADDRESS in found, f"{ADDRESS:#04x} present")
        if ADDRESS not in found:
            print("\nnothing else can work without this. check wiring.")
            return

        rule("3. reads")
        who = bus.read_u8(ADDRESS, WHO_AM_I)
        # WHO_AM_I is read-only with a known value. If this is right, then
        # register addressing, repeated-start, and byte framing all work.
        results["read"] = verdict(
            who in (0x68, 0x70, 0x71, 0x73, 0x74),
            f"WHO_AM_I = {who:#04x} (a real MPU-family id)",
        )

        rule("4. power state")
        # Check this BEFORE testing writes. A sleeping chip drops config
        # writes while still ACKing them, so a write test run against a
        # sleeping chip measures nothing and blames the wrong thing.
        pwr = bus.read_u8(ADDRESS, PWR_MGMT_1)
        print(f"  PWR_MGMT_1: {pwr:#04x}  (SLEEP = {bool(pwr & 0x40)})")
        if pwr & 0x40:
            print("  chip is asleep -- config writes would be silently dropped.")
            print("  waking it before testing writes.")
        bus.write_u8(ADDRESS, PWR_MGMT_1, 0x00)
        time.sleep(0.05)
        results["wake"] = verdict(
            not bus.read_u8(ADDRESS, PWR_MGMT_1) & 0x40, "chip is awake"
        )

        rule("5. writes (awake)")
        before = bus.read_u8(ADDRESS, ACCEL_CONFIG)
        print(f"  ACCEL_CONFIG before: {before:#04x}")
        bus.write_u8(ADDRESS, ACCEL_CONFIG, 0x08)
        after = bus.read_u8(ADDRESS, ACCEL_CONFIG)
        print(f"  ACCEL_CONFIG after writing 0x08: {after:#04x}")
        results["write"] = verdict(after == 0x08, "config write took effect")
        bus.write_u8(ADDRESS, ACCEL_CONFIG, before)

        rule("6. a second, unrelated register")
        # If ACCEL_CONFIG is special-cased by a clone, SMPLRT_DIV won't be.
        # Two registers disagreeing tells a very different story from both
        # failing.
        bus.write_u8(ADDRESS, SMPLRT_DIV, 0x07)
        smplrt = bus.read_u8(ADDRESS, SMPLRT_DIV)
        print(f"  SMPLRT_DIV after writing 0x07: {smplrt:#04x}")
        results["write2"] = verdict(smplrt == 0x07, "second write took effect")

        rule("7. sleep gating")
        bus.write_u8(ADDRESS, PWR_MGMT_1, 0x40)      # request sleep
        slept = bus.read_u8(ADDRESS, PWR_MGMT_1)
        print(f"  after writing 0x40: {slept:#04x}  (SLEEP bit = {bool(slept & 0x40)})")
        results["sleep"] = verdict(bool(slept & 0x40), "SLEEP bit set")

        bus.write_u8(ADDRESS, ACCEL_CONFIG, 0x08)    # should be ignored now
        gated = bus.read_u8(ADDRESS, ACCEL_CONFIG)
        print(f"  config write while asleep -> {gated:#04x} (expect 0x00)")
        results["gating"] = verdict(gated == 0x00,
                                    "sleeping chip ignores config writes")

        bus.write_u8(ADDRESS, PWR_MGMT_1, 0x00)      # wake
        woke = bus.read_u8(ADDRESS, PWR_MGMT_1)
        print(f"  after writing 0x00: {woke:#04x}  (SLEEP bit = {bool(woke & 0x40)})")
        results["woke"] = verdict(not woke & 0x40, "SLEEP bit cleared")

        rule("8. device reset")
        bus.write_u8(ADDRESS, PWR_MGMT_1, 0x80)      # DEVICE_RESET
        time.sleep(0.15)
        post = bus.read_u8(ADDRESS, PWR_MGMT_1)
        print(f"  PWR_MGMT_1 after reset: {post:#04x}")
        print(f"  (0x40 = MPU-6050 style, 0x01 = MPU-6500 style)")
        results["reset"] = verdict(not post & 0x80, "reset bit self-cleared")

        rule("9. live data")
        bus.write_u8(ADDRESS, PWR_MGMT_1, 0x00)
        time.sleep(0.1)
        first = bus.read(ADDRESS, ACCEL_XOUT_H, 14)
        time.sleep(0.15)
        second = bus.read(ADDRESS, ACCEL_XOUT_H, 14)
        print(f"  sample 1: {first.hex()}")
        print(f"  sample 2: {second.hex()}")
        results["data"] = verdict(
            first != bytes(14) and first != second,
            "data is non-zero and changing",
        )

    except VERError as exc:
        print(f"\nFAILED: {exc}")
        return
    finally:
        backend.close()

    rule("summary")
    for name, ok in results.items():
        print(f"  {name:8s} {'ok' if ok else 'FAILED'}")

    if results.get("read") and not results.get("write"):
        print("\nReads work, writes don't -- even with the chip awake.")
        print("Check the '->' lines: I2CWRITE must have four fields")
        print("(command, address, register, hex payload). Three fields means")
        print("the payload was dropped host-side and only the register")
        print("pointer reached the chip, which ACKs and changes nothing.")
    elif all(results.values()):
        print("\nEverything works. If a test still fails, the test is wrong.")


if __name__ == "__main__":
    main()

"""Simulated I2C devices, for the fake firmware.

A fake bus with nothing on it would let us test that commands are *encoded*
correctly and nothing else. These simulate the registers of real chips, so
the MPU6050 driver -- the actual code that ships -- gets exercised end to
end on a machine with no hardware: it writes the wake-up register, reads
WHO_AM_I, parses big-endian two's-complement burst reads, and if it gets any
of that wrong, the test fails.

The bar: if the simulation and the real chip ever disagree, that's a bug in
one of them, and the hardware conformance run is how we find out which.
"""

from __future__ import annotations

import math
import time


class FakeI2CDevice:
    """A register file that answers on one address."""

    address: int = 0x00

    def read(self, register: int, length: int) -> bytes:
        raise NotImplementedError

    def write(self, register: int, data: bytes) -> None:
        raise NotImplementedError


class FakeMPU6050(FakeI2CDevice):
    """InvenSense MPU6050, enough of it to drive a real driver.

    Models the parts that matter and, importantly, the part that bites:
    the chip boots in sleep mode and returns zeros until you clear the
    SLEEP bit in PWR_MGMT_1. A driver that forgets that reads a
    perfectly-formatted stream of nothing -- so the fake refuses to
    produce data until it's woken, exactly like the real one.
    """

    address = 0x68

    WHO_AM_I = 0x75
    PWR_MGMT_1 = 0x6B
    PWR_MGMT_2 = 0x6C
    ACCEL_XOUT_H = 0x3B
    ACCEL_CONFIG = 0x1C
    GYRO_CONFIG = 0x1B
    SMPLRT_DIV = 0x19
    CONFIG = 0x1A

    # While SLEEP is set, the chip ignores writes to configuration
    # registers -- but honours writes to the power-management path, since
    # otherwise nothing could ever wake it.
    #
    # This is observed behaviour, verified on a real MPU-6500 with
    # ver.tools.i2cdebug: writes to 0x1C and 0x19 silently did nothing while
    # 0x6B took effect immediately. The fake used to accept every write in
    # any state, which made it *easier* than the hardware -- so code that
    # configured before waking passed against the simulation and quietly did
    # nothing on a real board. A fake more permissive than reality is worse
    # than no fake at all.
    ALWAYS_WRITABLE = (PWR_MGMT_1, PWR_MGMT_2)

    def __init__(self):
        self.registers = {
            self.WHO_AM_I: 0x68,
            self.PWR_MGMT_1: 0x40,   # boots asleep. this is not a mistake.
            self.ACCEL_CONFIG: 0x00,
            self.GYRO_CONFIG: 0x00,
        }
        self._t0 = time.time()

    @property
    def asleep(self) -> bool:
        return bool(self.registers[self.PWR_MGMT_1] & 0x40)

    def _sensor_block(self) -> bytes:
        """14 bytes from ACCEL_XOUT_H: accel(6), temp(2), gyro(6).

        Big-endian signed 16-bit, which is the format a real driver has to
        get right. Sitting flat: ~1g on Z, gentle rocking on the gyro.
        """
        if self.asleep:
            return bytes(14)

        t = time.time() - self._t0
        # 16384 LSB/g at the default +/-2g range
        ax, ay, az = 0, int(0.02 * 16384 * math.sin(t)), 16384
        temp = 8420  # ~36 C via the datasheet's formula
        # 131 LSB/(deg/s) at the default +/-250 dps
        gx = int(5.0 * 131 * math.sin(t))
        gy = int(5.0 * 131 * math.cos(t))
        gz = 0

        out = bytearray()
        for value in (ax, ay, az, temp, gx, gy, gz):
            out += int(value).to_bytes(2, "big", signed=True)
        return bytes(out)

    def read(self, register: int, length: int) -> bytes:
        if register == self.ACCEL_XOUT_H:
            return self._sensor_block()[:length]
        out = bytearray()
        for offset in range(length):
            out.append(self.registers.get(register + offset, 0x00))
        return bytes(out)

    def _defaults(self) -> dict:
        return {
            self.WHO_AM_I: 0x68,
            self.PWR_MGMT_1: 0x40,
            self.ACCEL_CONFIG: 0x00,
            self.GYRO_CONFIG: 0x00,
        }

    def write(self, register: int, data: bytes) -> None:
        for offset, value in enumerate(data):
            target = register + offset

            if target == self.PWR_MGMT_1 and value & 0x80:
                # DEVICE_RESET: restore defaults and self-clear the bit,
                # which is what the real part does.
                who = self.registers[self.WHO_AM_I]
                self.registers = self._defaults()
                self.registers[self.WHO_AM_I] = who
                continue

            if self.asleep and target not in self.ALWAYS_WRITABLE:
                continue   # silently dropped, exactly as the real chip does

            self.registers[target] = value


class FakeMPU6500(FakeMPU6050):
    """The chip that actually ships on most boards labelled "MPU6050".

    Same registers, different WHO_AM_I, different temperature formula. It
    exists here so the driver's family handling is exercised rather than
    merely intended -- the real board on the author's desk is one of these.
    """

    def __init__(self):
        super().__init__()
        self.registers = self._defaults()

    def _defaults(self) -> dict:
        # The 6500 resets to 0x01 -- CLKSEL=1, SLEEP *clear*. It boots AWAKE,
        # unlike the 6050 which resets to 0x40 and boots asleep. Confirmed on
        # real silicon: DEVICE_RESET left PWR_MGMT_1 at 0x01.
        defaults = super()._defaults()
        defaults[self.WHO_AM_I] = 0x70
        defaults[self.PWR_MGMT_1] = 0x01
        return defaults


class FakeSSD1306(FakeI2CDevice):
    """A 128x64 OLED. Swallows everything; remembers the last write.

    Enough to prove the bus carries display traffic and that the driver's
    control-byte framing is right. Rendering is not the runtime's problem.
    """

    address = 0x3C

    def __init__(self):
        self.commands: list[int] = []
        self.data: bytearray = bytearray()

    def read(self, register: int, length: int) -> bytes:
        return bytes(length)  # write-only in practice

    def write(self, register: int, data: bytes) -> None:
        if register == 0x00:
            self.commands.extend(data)
        elif register == 0x40:
            self.data.extend(data)


DEFAULT_DEVICES = (FakeMPU6050, FakeSSD1306)
MPU6500_DEVICES = (FakeMPU6500, FakeSSD1306)

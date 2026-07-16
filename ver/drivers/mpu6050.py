"""InvenSense MPU-6050 (and its register-compatible relatives).

Note where this lives: `ver/drivers/`, not `ver/backends/`. That distinction
is the whole portability argument made concrete.

A backend knows about a platform. A driver knows about a *chip*. This file
is written against VirtualI2C and nothing else -- it has no idea whether the
bus is an ESP32 on USB, a Raspberry Pi's native /dev/i2c-1, a Jetson, or a
simulation. Ship a Pi backend tomorrow and this file works on it, unchanged,
with no review and no port.

That's the thing a blinking LED can't demonstrate and this can: you write a
sensor driver once, for the ecosystem, not once per board.

A note on identity: boards sold as "MPU6050" frequently carry an MPU-6500
(WHO_AM_I 0x70), MPU-9250 (0x71), or similar. They're register-compatible
for accel and gyro, so this driver accepts the whole family and only
special-cases the temperature formula, which is the one thing that actually
differs. Pass strict=True to demand a genuine 0x68.

Datasheets:
  MPU-6050: https://invensense.tdk.com/products/motion-tracking/6-axis/mpu-6050/
  MPU-6500: https://invensense.tdk.com/wp-content/uploads/2015/02/MPU-6500-Register-Map2.pdf
"""

from __future__ import annotations

import time

from ..hal.base import VirtualI2C, VirtualIMU
from ..hal.errors import DeviceNotFound, VERError
from ..hal.types import DeviceInfo, ImuReading, Vector3

# Registers
WHO_AM_I = 0x75
PWR_MGMT_1 = 0x6B
SMPLRT_DIV = 0x19
CONFIG = 0x1A
GYRO_CONFIG = 0x1B
ACCEL_CONFIG = 0x1C
ACCEL_XOUT_H = 0x3B

DEFAULT_ADDRESS = 0x68      # 0x69 when AD0 is pulled high
EXPECTED_WHO_AM_I = 0x68

# Boards sold as "MPU6050" very often carry a different InvenSense part.
# They are register-compatible for accel and gyro, so refusing to talk to
# them helps nobody -- Linux's own inv_mpu6050 driver treats the mismatch as
# a warning, not an error, for exactly this reason.
#
# The one thing that genuinely differs is the temperature formula, so that
# lives in the table rather than as a constant.
#
#   who_am_i -> (name, temp_divisor, temp_offset)
KNOWN_CHIPS = {
    0x68: ("MPU-6050", 340.0, 36.53),
    0x70: ("MPU-6500", 333.87, 21.0),
    0x71: ("MPU-9250", 333.87, 21.0),
    0x73: ("MPU-9255", 333.87, 21.0),
    0x74: ("MPU-9515", 333.87, 21.0),
}

# Scale factors from the datasheet, for the default ranges.
ACCEL_LSB_PER_G = 16384.0   # +/- 2g
GYRO_LSB_PER_DPS = 131.0    # +/- 250 deg/s
G = 9.80665
DEG_TO_RAD = 3.141592653589793 / 180.0


class MPU6050(VirtualIMU):
    """A 6-axis IMU on any VirtualI2C bus."""

    def __init__(self, bus: VirtualI2C, address: int = DEFAULT_ADDRESS,
                 strict: bool = False):
        self._bus = bus
        self.address = address
        self.strict = strict
        self.chip = "unknown"
        self.who_am_i = None
        self._temp_divisor = 340.0
        self._temp_offset = 36.53
        self._open = False

    def open(self) -> None:
        if self._open:
            return
        if not self._bus.is_open:
            self._bus.open()

        try:
            who = self._bus.read_u8(self.address, WHO_AM_I)
        except VERError as exc:
            raise DeviceNotFound(
                f"no I2C response from {self.address:#04x}.\n"
                "  - check SDA/SCL wiring and that the sensor has 3.3V\n"
                "  - some breakouts sit at 0x69 (AD0 pulled high):\n"
                "      rt.imu(address=0x69)\n"
                "  - list what's actually on the bus:\n"
                "      python -m ver.tools.i2c"
            ) from exc

        self.who_am_i = who

        if who not in KNOWN_CHIPS:
            # Reject rather than "try anyway". An unknown ID at 0x68 is more
            # likely a different chip entirely than an unlisted MPU clone --
            # point this driver at an OLED and it would happily return 14
            # bytes of nonsense parsed as gravity. Numbers that look
            # plausible but are wrong are the worst failure mode there is.
            raise DeviceNotFound(
                f"device at {self.address:#04x} reports WHO_AM_I="
                f"{who:#04x}, which isn't an MPU-family part.\n"
                "  known: " + ", ".join(
                    f"{k:#04x}={v[0]}" for k, v in sorted(KNOWN_CHIPS.items())
                ) + "\n"
                "  if your board really is an MPU and reports something else,\n"
                "  that's a bug worth reporting -- the list is easy to extend."
            )

        if self.strict and who != EXPECTED_WHO_AM_I:
            raise DeviceNotFound(
                f"strict=True, but this is a {KNOWN_CHIPS[who][0]} "
                f"(WHO_AM_I={who:#04x}), not a genuine MPU-6050.\n"
                "  drop strict=True -- it's register-compatible."
            )

        self.chip, self._temp_divisor, self._temp_offset = KNOWN_CHIPS[who]

        # Wake first. This ordering is load-bearing, not stylistic.
        #
        # A sleeping MPU ACKs writes to configuration registers on the bus
        # and then silently drops them -- no error, no NACK, no clue. Only
        # the power-management path answers while asleep, which makes sense:
        # otherwise nothing could ever wake it. Configure before waking and
        # every setting below evaporates while the code looks perfect.
        #
        # An MPU-6050 resets to 0x40 and boots asleep; an MPU-6500 resets to
        # 0x01 and boots awake. Writing 0x00 is right either way, and not
        # depending on which is which is the point.
        self._bus.write_u8(self.address, PWR_MGMT_1, 0x00)
        time.sleep(0.05)

        self._bus.write_u8(self.address, ACCEL_CONFIG, 0x00)   # +/- 2g
        self._bus.write_u8(self.address, GYRO_CONFIG, 0x00)    # +/- 250 dps
        self._bus.write_u8(self.address, CONFIG, 0x03)         # 44Hz DLPF
        self._bus.write_u8(self.address, SMPLRT_DIV, 0x04)     # 200Hz
        self._open = True

    def close(self) -> None:
        """Put the chip to sleep on the way out.

        Worth knowing, because it surprised its own author: this leaves the
        chip asleep for whatever runs next. Since a sleeping chip drops
        config writes, anything that pokes registers without calling open()
        first will find its writes vanishing. open() handles it; raw bus
        users must wake it themselves.
        """
        if self._open:
            try:
                self._bus.write_u8(self.address, PWR_MGMT_1, 0x40)
            except Exception:
                pass
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    @staticmethod
    def _s16(high: int, low: int) -> int:
        value = (high << 8) | low
        return value - 65536 if value & 0x8000 else value

    def read(self) -> ImuReading:
        if not self._open:
            raise VERError("IMU used before open()")

        # One 14-byte burst, not seven 2-byte reads. Over a 40ms-round-trip
        # bridge that's the difference between 40ms and 280ms per sample --
        # and, more importantly, a burst is atomic: seven separate reads
        # can straddle a sensor update and hand you half of one sample
        # stitched to half of the next.
        raw = self._bus.read(self.address, ACCEL_XOUT_H, 14)

        ax = self._s16(raw[0], raw[1]) / ACCEL_LSB_PER_G * G
        ay = self._s16(raw[2], raw[3]) / ACCEL_LSB_PER_G * G
        az = self._s16(raw[4], raw[5]) / ACCEL_LSB_PER_G * G
        # raw[6:8] is temperature; the HAL has nowhere to put it yet.
        gx = self._s16(raw[8], raw[9]) / GYRO_LSB_PER_DPS * DEG_TO_RAD
        gy = self._s16(raw[10], raw[11]) / GYRO_LSB_PER_DPS * DEG_TO_RAD
        gz = self._s16(raw[12], raw[13]) / GYRO_LSB_PER_DPS * DEG_TO_RAD

        return ImuReading(accel=Vector3(ax, ay, az), gyro=Vector3(gx, gy, gz))

    def temperature(self) -> float:
        """Degrees C. The datasheet's magic numbers, not mine -- and they
        differ per chip, which is the one place these parts aren't
        interchangeable."""
        raw = self._bus.read(self.address, 0x41, 2)
        return self._s16(raw[0], raw[1]) / self._temp_divisor + self._temp_offset

    def info(self) -> DeviceInfo:
        bus = self._bus.info()
        return DeviceInfo(
            backend=bus.backend,
            platform=bus.platform,
            transport=bus.transport,
            details={"chip": self.chip, "address": f"{self.address:#04x}",
                     "who_am_i": f"{self.who_am_i:#04x}" if self.who_am_i else None,
                     "accel_range": "+/-2g", "gyro_range": "+/-250dps"},
        )

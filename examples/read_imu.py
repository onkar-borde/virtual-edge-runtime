"""Stream IMU samples and print a live tilt estimate.

    python examples/read_imu.py                  # real MPU6050 if one's wired
    VER_BACKEND=mock python examples/read_imu.py # simulated

Tilt the sensor; roll and pitch follow. Sitting flat, both read ~0.

This file has not changed since it was written against a simulated IMU,
before any I2C code existed. The tilt maths below has no idea whether the
gravity vector came from `random.gauss()` or from a real accelerometer
over a real bus. That was the promise; this is it being kept.
"""

import math

from ver import Runtime, VERError


def tilt_degrees(reading) -> tuple[float, float]:
    """Crude accelerometer-only tilt. Good enough to see it working."""
    a = reading.accel
    roll = math.degrees(math.atan2(a.y, math.sqrt(a.x**2 + a.z**2)))
    pitch = math.degrees(math.atan2(-a.x, math.sqrt(a.y**2 + a.z**2)))
    return roll, pitch


def main() -> None:
    with Runtime() as rt:
        print(f"running on: {rt}")

        try:
            imu = rt.imu()
            imu.open()
        except VERError as exc:
            print(f"\nno IMU:\n  {exc}")
            return

        print(f"imu: {imu.info().details}\n")

        with imu:
            for _, reading in zip(range(60), imu.stream(hz=20)):
                roll, pitch = tilt_degrees(reading)
                print(f"  roll={roll:+7.2f}deg  pitch={pitch:+7.2f}deg  "
                      f"gyro_x={reading.gyro.x:+.3f}")


if __name__ == "__main__":
    main()

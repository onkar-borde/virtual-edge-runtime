"""Stream IMU samples and print a live tilt estimate.

    python examples/read_imu.py

Runs against the mock IMU today, against a real one over the ESP32
bridge tomorrow, with no edit to this file.
"""

import math

from ver import Runtime


def tilt_degrees(reading) -> tuple[float, float]:
    """Crude accelerometer-only tilt. Good enough to see it working."""
    a = reading.accel
    roll = math.degrees(math.atan2(a.y, math.sqrt(a.x**2 + a.z**2)))
    pitch = math.degrees(math.atan2(-a.x, math.sqrt(a.y**2 + a.z**2)))
    return roll, pitch


def main() -> None:
    with Runtime() as rt:
        print(f"running on: {rt}")

        with rt.imu() as imu:
            for _, reading in zip(range(20), imu.stream(hz=20)):
                roll, pitch = tilt_degrees(reading)
                print(f"  roll={roll:+7.2f}deg  pitch={pitch:+7.2f}deg  "
                      f"gyro_x={reading.gyro.x:+.3f}")


if __name__ == "__main__":
    main()

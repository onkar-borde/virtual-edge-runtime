"""Scan the I2C bus.

    python -m ver.tools.i2c

The first thing to run when a sensor "doesn't work". Wiring and address
conflicts account for most of it, and this tells you which in two seconds.
"""

from ver import Runtime, VERError

KNOWN = {
    0x0D: "QMC5883L magnetometer",
    0x1E: "HMC5883L magnetometer",
    0x27: "PCF8574 / LCD backpack",
    0x29: "VL53L0X ToF rangefinder",
    0x3C: "SSD1306 OLED",
    0x3D: "SSD1306 OLED (alt)",
    0x40: "INA219 / HTU21D",
    0x48: "ADS1115 ADC",
    0x53: "ADXL345 accelerometer",
    0x40 + 0x28: "PCA9685 servo driver",
    0x68: "MPU6050 IMU / DS3231 RTC",
    0x69: "MPU6050 IMU (AD0 high)",
    0x76: "BMP280 / BME280",
    0x77: "BMP280 / BME280 (alt)",
}


def main() -> None:
    try:
        rt = Runtime()
    except VERError as exc:
        print(f"no runtime: {exc}")
        return

    print(f"runtime: {rt}")
    try:
        bus = rt.i2c()
        bus.open()
    except VERError as exc:
        print(f"\nno I2C bus:\n  {exc}")
        return

    print(f"bus: {bus.info().details}\n")

    found = bus.scan()
    if not found:
        print("nothing on the bus.")
        print()
        print("  - SDA -> GPIO 21, SCL -> GPIO 22 by default")
        print("  - sensor needs 3.3V and a shared GND with the board")
        print("  - most breakouts have pull-up resistors already; a bare")
        print("    chip needs 4.7k from SDA and SCL to 3.3V")
        return

    print(f"{len(found)} device(s):\n")
    for address in found:
        guess = KNOWN.get(address, "unknown")
        print(f"  {address:#04x}  {guess}")

    if 0x68 in found or 0x69 in found:
        # Address alone doesn't say which chip it is; ask.
        try:
            from ver.drivers.mpu6050 import KNOWN_CHIPS

            address = 0x68 if 0x68 in found else 0x69
            who = bus.read_u8(address, 0x75)
            name = KNOWN_CHIPS.get(who, ("unrecognised",))[0]
            print(f"\n{address:#04x} WHO_AM_I = {who:#04x} -> {name}")
        except VERError:
            pass
        print("\ntry:  python examples/read_imu.py")


if __name__ == "__main__":
    main()

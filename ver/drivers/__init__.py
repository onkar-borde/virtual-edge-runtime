"""Chip drivers, written against the HAL rather than against a platform.

A backend knows a platform; a driver knows a chip. Anything here works on
any backend that provides the bus it needs -- laptop+ESP32 today, Raspberry
Pi or Jetson the day those backends land, with no changes.
"""

from .mpu6050 import MPU6050

__all__ = ["MPU6050"]

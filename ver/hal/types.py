"""Platform-neutral data types used across every backend.

Nothing here knows what an ESP32, a Pi, or a laptop is. That's the point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import time


class PinMode(Enum):
    INPUT = "input"
    INPUT_PULLUP = "input_pullup"
    OUTPUT = "output"
    PWM = "pwm"
    ANALOG = "analog"


class PinState(Enum):
    LOW = 0
    HIGH = 1

    @classmethod
    def from_value(cls, value) -> "PinState":
        if isinstance(value, PinState):
            return value
        return cls.HIGH if value else cls.LOW

    def __bool__(self) -> bool:
        return self is PinState.HIGH


@dataclass(frozen=True)
class Vector3:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


@dataclass(frozen=True)
class ImuReading:
    """One IMU sample. Units are SI: m/s^2 and rad/s."""

    accel: Vector3
    gyro: Vector3
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class Frame:
    """One camera frame.

    `data` is a numpy array (H, W, 3) in BGR, matching OpenCV convention.
    Typed loosely so the HAL doesn't hard-depend on numpy.
    """

    data: object
    width: int
    height: int
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class DeviceInfo:
    """What a backend reports about itself."""

    backend: str
    platform: str
    transport: Optional[str] = None
    details: dict = field(default_factory=dict)

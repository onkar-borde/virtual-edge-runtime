"""Hardware Abstraction Layer: interfaces, types, errors."""

from .base import (
    Backend,
    VirtualCamera,
    VirtualDevice,
    VirtualGPIO,
    VirtualI2C,
    VirtualIMU,
    VirtualMotor,
)
from .errors import (
    BackendNotAvailable,
    DeviceNotFound,
    PinError,
    TransportError,
    UnsupportedCapability,
    VERError,
)
from .types import DeviceInfo, Frame, ImuReading, PinMode, PinState, Vector3

__all__ = [
    "Backend",
    "VirtualDevice",
    "VirtualGPIO",
    "VirtualI2C",
    "VirtualIMU",
    "VirtualCamera",
    "VirtualMotor",
    "PinMode",
    "PinState",
    "Vector3",
    "ImuReading",
    "Frame",
    "DeviceInfo",
    "VERError",
    "BackendNotAvailable",
    "DeviceNotFound",
    "UnsupportedCapability",
    "TransportError",
    "PinError",
]

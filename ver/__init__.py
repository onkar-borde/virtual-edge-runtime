"""Virtual Edge Runtime — write once, run on any edge target."""

from .hal import (
    BackendNotAvailable,
    DeviceInfo,
    DeviceNotFound,
    Frame,
    ImuReading,
    PinError,
    PinMode,
    PinState,
    TransportError,
    UnsupportedCapability,
    Vector3,
    VERError,
    VirtualCamera,
    VirtualGPIO,
    VirtualI2C,
    VirtualIMU,
    VirtualMotor,
)
from .runtime import Runtime

__version__ = "0.1.0"

__all__ = [
    "Runtime",
    "PinMode",
    "PinState",
    "Vector3",
    "ImuReading",
    "Frame",
    "DeviceInfo",
    "VirtualGPIO",
    "VirtualI2C",
    "VirtualIMU",
    "VirtualCamera",
    "VirtualMotor",
    "VERError",
    "BackendNotAvailable",
    "DeviceNotFound",
    "UnsupportedCapability",
    "TransportError",
    "PinError",
    "__version__",
]

"""The Hardware Abstraction Layer contract.

Every backend implements these. Application code only ever touches these.
If you find yourself importing `serial` or `cv2` in an app, the abstraction
has leaked and something is wrong.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator, Optional

from .types import DeviceInfo, Frame, ImuReading, PinMode, PinState


class VirtualDevice(ABC):
    """Common lifecycle for anything the runtime hands you.

    Every device is a context manager, so `with runtime.gpio() as gpio:`
    always releases the hardware even if the app crashes.
    """

    @abstractmethod
    def open(self) -> None:
        """Acquire the hardware. Idempotent."""

    @abstractmethod
    def close(self) -> None:
        """Release the hardware. Idempotent. Must be safe to call twice."""

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    @abstractmethod
    def info(self) -> DeviceInfo: ...

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False


class VirtualGPIO(VirtualDevice):
    """Digital and PWM pin access.

    On a Pi this maps to the 40-pin header. On a laptop it maps to a
    microcontroller over USB. On mock it maps to a dict.
    """

    @abstractmethod
    def setup(self, pin: int, mode: PinMode) -> None:
        """Configure a pin before use. Required before read/write."""

    @abstractmethod
    def write(self, pin: int, state: PinState | bool | int) -> None:
        """Drive an OUTPUT pin high or low."""

    @abstractmethod
    def read(self, pin: int) -> PinState:
        """Read an INPUT pin."""

    @abstractmethod
    def pwm(self, pin: int, duty: float, frequency: int = 1000) -> None:
        """Set PWM on a pin. `duty` is 0.0-1.0."""

    @abstractmethod
    def analog_read(self, pin: int) -> float:
        """Read an ADC pin, normalised 0.0-1.0."""


class VirtualIMU(VirtualDevice):
    """Accelerometer + gyroscope."""

    @abstractmethod
    def read(self) -> ImuReading:
        """Grab the latest sample."""

    def stream(self, hz: float = 50.0) -> Iterator[ImuReading]:
        """Yield samples at roughly `hz`. Backends may override for
        hardware-timed streaming; this default is good enough for most."""
        import time

        period = 1.0 / hz
        while self.is_open:
            started = time.perf_counter()
            yield self.read()
            drift = period - (time.perf_counter() - started)
            if drift > 0:
                time.sleep(drift)


class VirtualCamera(VirtualDevice):
    """A single video source."""

    @abstractmethod
    def read(self) -> Frame:
        """Grab one frame. Blocks until available."""

    def stream(self) -> Iterator[Frame]:
        while self.is_open:
            yield self.read()


class VirtualMotor(VirtualDevice):
    """A single motor channel, direction + speed.

    Deliberately not a 'drivetrain' — kinematics belong above the HAL.
    """

    @abstractmethod
    def set_speed(self, speed: float) -> None:
        """`speed` is -1.0 (full reverse) to 1.0 (full forward). 0 = stop."""

    @abstractmethod
    def stop(self) -> None:
        """Immediate stop. Must be safe to call from anywhere, any time."""

    @property
    @abstractmethod
    def speed(self) -> float:
        """Last commanded speed. Not measured — that's an encoder's job."""


class Backend(ABC):
    """A platform adapter. Produces devices; owns nothing else."""

    name: str = "unnamed"
    platform: str = "unknown"

    @classmethod
    @abstractmethod
    def available(cls) -> bool:
        """Can this backend actually run here, right now? Never raises."""

    @abstractmethod
    def gpio(self, **kwargs) -> VirtualGPIO: ...

    def imu(self, **kwargs) -> VirtualIMU:
        from .errors import UnsupportedCapability

        raise UnsupportedCapability(f"{self.name} backend has no IMU")

    def camera(self, index: int = 0, **kwargs) -> VirtualCamera:
        from .errors import UnsupportedCapability

        raise UnsupportedCapability(f"{self.name} backend has no camera")

    def motor(self, **kwargs) -> VirtualMotor:
        from .errors import UnsupportedCapability

        raise UnsupportedCapability(f"{self.name} backend has no motor")

    def info(self) -> DeviceInfo:
        return DeviceInfo(backend=self.name, platform=self.platform)

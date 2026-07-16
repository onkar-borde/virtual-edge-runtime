"""Mock backend — a complete, honest fake.

Runs anywhere, needs nothing. Two jobs:
  1. Let you write and test application code with no hardware plugged in.
  2. Be the reference implementation: if a real backend behaves differently
     from mock for the same calls, one of them has a bug.

It records every operation, so tests can assert on what the app *did*.
"""

from __future__ import annotations

import math
import random
import time
from typing import Optional

from ...hal.base import Backend, VirtualCamera, VirtualGPIO, VirtualIMU, VirtualMotor
from ...hal.errors import PinError
from ...hal.types import DeviceInfo, Frame, ImuReading, PinMode, PinState, Vector3

MAX_PIN = 39


class _MockDevice:
    """Shared open/close bookkeeping."""

    def __init__(self):
        self._open = False

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def _require_open(self):
        if not self._open:
            raise PinError(f"{type(self).__name__} used before open()")


class MockGPIO(_MockDevice, VirtualGPIO):
    """Pins in a dict. Enforces the same rules real hardware would."""

    def __init__(self):
        super().__init__()
        self.modes: dict[int, PinMode] = {}
        self.states: dict[int, PinState] = {}
        self.pwm_values: dict[int, tuple[float, int]] = {}
        self.log: list[tuple] = []

    def _check_pin(self, pin: int):
        if not isinstance(pin, int) or not 0 <= pin <= MAX_PIN:
            raise PinError(f"pin {pin!r} out of range 0-{MAX_PIN}")

    def setup(self, pin: int, mode: PinMode) -> None:
        self._require_open()
        self._check_pin(pin)
        self.modes[pin] = mode
        if mode is PinMode.OUTPUT:
            self.states[pin] = PinState.LOW
        elif mode is PinMode.INPUT_PULLUP:
            self.states[pin] = PinState.HIGH
        elif mode is PinMode.INPUT:
            self.states[pin] = PinState.LOW
        self.log.append(("setup", pin, mode))

    def write(self, pin: int, state) -> None:
        self._require_open()
        self._check_pin(pin)
        if self.modes.get(pin) is not PinMode.OUTPUT:
            raise PinError(f"pin {pin} not configured as OUTPUT")
        value = PinState.from_value(state)
        self.states[pin] = value
        self.log.append(("write", pin, value))

    def read(self, pin: int) -> PinState:
        self._require_open()
        self._check_pin(pin)
        mode = self.modes.get(pin)
        if mode not in (PinMode.INPUT, PinMode.INPUT_PULLUP, PinMode.OUTPUT):
            raise PinError(f"pin {pin} not configured for reading")
        value = self.states.get(pin, PinState.LOW)
        self.log.append(("read", pin, value))
        return value

    def pwm(self, pin: int, duty: float, frequency: int = 1000) -> None:
        self._require_open()
        self._check_pin(pin)
        if self.modes.get(pin) is not PinMode.PWM:
            raise PinError(f"pin {pin} not configured as PWM")
        if not 0.0 <= duty <= 1.0:
            raise PinError(f"duty {duty} outside 0.0-1.0")
        self.pwm_values[pin] = (duty, frequency)
        self.log.append(("pwm", pin, duty, frequency))

    def analog_read(self, pin: int) -> float:
        self._require_open()
        self._check_pin(pin)
        if self.modes.get(pin) is not PinMode.ANALOG:
            raise PinError(f"pin {pin} not configured as ANALOG")
        value = random.uniform(0.0, 1.0)
        self.log.append(("analog_read", pin, value))
        return value

    def inject(self, pin: int, state) -> None:
        """Test helper: pretend the outside world drove an input pin."""
        self.states[pin] = PinState.from_value(state)

    def info(self) -> DeviceInfo:
        return DeviceInfo(backend="mock", platform="any", transport="memory",
                          details={"pins": MAX_PIN + 1})


class MockIMU(_MockDevice, VirtualIMU):
    """Synthesises a gently rocking IMU so filters have something to chew on."""

    def __init__(self, noise: float = 0.02):
        super().__init__()
        self.noise = noise
        self._t0 = time.time()

    def read(self) -> ImuReading:
        self._require_open()
        t = time.time() - self._t0
        n = lambda: random.gauss(0.0, self.noise)
        return ImuReading(
            accel=Vector3(n(), n(), 9.81 + n()),
            gyro=Vector3(0.1 * math.sin(t) + n(), 0.1 * math.cos(t) + n(), n()),
        )

    def info(self) -> DeviceInfo:
        return DeviceInfo(backend="mock", platform="any", transport="memory",
                          details={"simulated": True})


class MockMotor(_MockDevice, VirtualMotor):
    def __init__(self, label: str = "motor0"):
        super().__init__()
        self.label = label
        self._speed = 0.0
        self.log: list[tuple] = []

    def set_speed(self, speed: float) -> None:
        self._require_open()
        if not -1.0 <= speed <= 1.0:
            raise ValueError(f"speed {speed} outside -1.0..1.0")
        self._speed = float(speed)
        self.log.append(("set_speed", speed))

    def stop(self) -> None:
        self._speed = 0.0
        self.log.append(("stop",))

    @property
    def speed(self) -> float:
        return self._speed

    def close(self) -> None:
        # Safety habit worth baking in everywhere: never leave a motor spinning.
        if self._open:
            self.stop()
        super().close()

    def info(self) -> DeviceInfo:
        return DeviceInfo(backend="mock", platform="any", transport="memory",
                          details={"label": self.label})


class MockCamera(_MockDevice, VirtualCamera):
    """Emits a moving gradient so vision pipelines see *something* change."""

    def __init__(self, width: int = 640, height: int = 480):
        super().__init__()
        self.width = width
        self.height = height
        self._n = 0

    def read(self) -> Frame:
        self._require_open()
        try:
            import numpy as np
        except ImportError:
            data = None
        else:
            xs = np.linspace(0, 255, self.width, dtype=np.uint8)
            row = np.roll(xs, self._n * 4)
            data = np.dstack([
                np.tile(row, (self.height, 1)),
                np.tile(row[::-1], (self.height, 1)),
                np.full((self.height, self.width), (self._n * 2) % 255, np.uint8),
            ])
        self._n += 1
        return Frame(data=data, width=self.width, height=self.height)

    def info(self) -> DeviceInfo:
        return DeviceInfo(backend="mock", platform="any", transport="memory",
                          details={"resolution": f"{self.width}x{self.height}"})


class MockBackend(Backend):
    name = "mock"
    platform = "any"

    @classmethod
    def available(cls) -> bool:
        return True  # always. that's the whole point.

    def gpio(self, **kwargs) -> VirtualGPIO:
        return MockGPIO()

    def imu(self, **kwargs) -> VirtualIMU:
        return MockIMU(**kwargs)

    def camera(self, index: int = 0, **kwargs) -> VirtualCamera:
        return MockCamera(**kwargs)

    def motor(self, label: str = "motor0", **kwargs) -> VirtualMotor:
        return MockMotor(label=label)

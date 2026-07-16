"""The Runtime: the one object an application talks to.

    from ver import Runtime

    rt = Runtime()            # autodetect
    rt = Runtime("mock")      # force a backend
    VER_BACKEND=mock python app.py   # or force it from the environment

Application code never imports a backend directly. If it does, portability
is gone and this whole project is pointless.
"""

from __future__ import annotations

from typing import Optional, Type

from .backends import registry
from .hal.base import (
    Backend,
    VirtualCamera,
    VirtualGPIO,
    VirtualI2C,
    VirtualIMU,
    VirtualMotor,
)
from .hal.types import DeviceInfo


class Runtime:
    def __init__(self, backend: Optional[str | Type[Backend] | Backend] = None):
        if backend is None:
            self._backend = registry.autodetect()()
        elif isinstance(backend, str):
            self._backend = registry.get(backend)()
        elif isinstance(backend, Backend):
            self._backend = backend
        else:
            self._backend = backend()
        self._devices: list = []

    @property
    def backend(self) -> Backend:
        return self._backend

    @property
    def backend_name(self) -> str:
        return self._backend.name

    def info(self) -> DeviceInfo:
        return self._backend.info()

    def _track(self, device):
        self._devices.append(device)
        return device

    def gpio(self, **kwargs) -> VirtualGPIO:
        return self._track(self._backend.gpio(**kwargs))

    def imu(self, **kwargs) -> VirtualIMU:
        return self._track(self._backend.imu(**kwargs))

    def camera(self, index: int = 0, **kwargs) -> VirtualCamera:
        return self._track(self._backend.camera(index=index, **kwargs))

    def motor(self, **kwargs) -> VirtualMotor:
        return self._track(self._backend.motor(**kwargs))

    def i2c(self, **kwargs) -> VirtualI2C:
        return self._track(self._backend.i2c(**kwargs))

    def shutdown(self) -> None:
        """Close every device this runtime handed out. Motors stop first."""
        for device in reversed(self._devices):
            stop = getattr(device, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    pass
        for device in reversed(self._devices):
            try:
                device.close()
            except Exception:
                pass
        self._devices.clear()
        # Devices don't own the transport underneath them -- the backend
        # does. Without this, an ESP32's serial port survives shutdown() and
        # the next process to want the board is told "Access is denied".
        try:
            self._backend.close()
        except Exception:
            pass

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.shutdown()
        return False

    def __repr__(self) -> str:
        return f"<Runtime backend={self._backend.name!r} platform={self._backend.platform!r}>"

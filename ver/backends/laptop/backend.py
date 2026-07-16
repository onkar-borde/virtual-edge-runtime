"""Laptop backend — a real desktop/laptop running Windows, Linux, or macOS.

What a laptop genuinely has:
  - a camera (webcam)          -> VirtualCamera, via OpenCV
  - lots of compute            -> not the HAL's problem
  - storage                    -> not the HAL's problem

What a laptop genuinely does NOT have:
  - GPIO pins
  - an IMU (usually)
  - motor drivers

We do not pretend otherwise. Calls for hardware that isn't there raise
UnsupportedCapability with an explanation, rather than silently faking it.
Faking it is what mock is for, and mock is honest about being a fake.

Physical I/O arrives in the next backend (esp32), which bridges over USB.
"""

from __future__ import annotations

import platform
import time
from typing import Optional

from ...hal.base import Backend, VirtualCamera
from ...hal.errors import DeviceNotFound, TransportError, UnsupportedCapability
from ...hal.types import DeviceInfo, Frame


def _has_opencv() -> bool:
    try:
        import cv2  # noqa: F401
    except Exception:
        return False
    return True


class LaptopCamera(VirtualCamera):
    """A webcam, via OpenCV.

    Deliberately thin. Resolution requests are advisory — cameras lie about
    what they support, so we ask, then report back whatever we actually got
    rather than what we wanted.
    """

    def __init__(self, index: int = 0, width: Optional[int] = None,
                 height: Optional[int] = None, warmup: float = 0.3):
        self.index = index
        self._requested = (width, height)
        self._warmup = warmup
        self._cap = None
        self._width = 0
        self._height = 0

    def open(self) -> None:
        if self._cap is not None:
            return
        try:
            import cv2
        except ImportError as exc:
            raise UnsupportedCapability(
                "camera needs OpenCV. install it with:  pip install -e \".[laptop]\""
            ) from exc

        cap = cv2.VideoCapture(self.index)
        if not cap.isOpened():
            cap.release()
            raise DeviceNotFound(
                f"no camera at index {self.index}. "
                "is another app using the webcam, or is the privacy shutter closed?"
            )

        want_w, want_h = self._requested
        if want_w:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, want_w)
        if want_h:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, want_h)

        # Webcams hand back black or garbage frames for the first fraction of
        # a second while exposure settles. Burn that time here so the app's
        # first read() is a real frame.
        deadline = time.time() + self._warmup
        while time.time() < deadline:
            cap.read()

        self._cap = cap
        self._width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self._height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    @property
    def is_open(self) -> bool:
        return self._cap is not None

    def read(self) -> Frame:
        if self._cap is None:
            raise TransportError("camera used before open()")
        ok, data = self._cap.read()
        if not ok or data is None:
            raise TransportError(
                f"camera {self.index} stopped delivering frames (unplugged?)"
            )
        h, w = data.shape[:2]
        return Frame(data=data, width=w, height=h)

    def info(self) -> DeviceInfo:
        return DeviceInfo(
            backend="laptop",
            platform=platform.system().lower(),
            transport="opencv",
            details={
                "index": self.index,
                "resolution": f"{self._width}x{self._height}" if self.is_open else "unopened",
            },
        )


class LaptopBackend(Backend):
    """A laptop, plus whatever is bolted onto its USB ports.

    This is the "Laptop Backend" box from the architecture doc:

        Camera      -> webcam, natively
        USB GPIO    -> an ESP32 over serial
        Filesystem  -> not the HAL's problem
        CUDA        -> not the HAL's problem

    The ESP32 is not a rival platform to the laptop; it's the laptop's pin
    header, reached over a wire. So it lives here rather than competing for
    autodetect. A laptop with a board plugged in is still a laptop -- it
    just grew hands.
    """

    name = "laptop"
    platform = platform.system().lower()

    def __init__(self, port: Optional[str] = None):
        self.port = port
        self._esp32 = None

    @classmethod
    def available(cls) -> bool:
        # A laptop backend that can't even open a camera has nothing to offer
        # over mock, so it declines rather than winning autodetect and then
        # failing on every call.
        return platform.system() in ("Windows", "Linux", "Darwin") and _has_opencv()

    def camera(self, index: int = 0, **kwargs) -> VirtualCamera:
        return LaptopCamera(index=index, **kwargs)

    def _bridge(self):
        """The ESP32 hanging off USB, if there is one."""
        if self._esp32 is None:
            from ..esp32.backend import ESP32Backend

            self._esp32 = ESP32Backend(port=self.port)
        return self._esp32

    def gpio(self, **kwargs):
        from ..esp32.transport import find_ports

        if not find_ports() and self.port is None:
            raise UnsupportedCapability(
                "a laptop has no GPIO pins of its own, and no ESP32 is "
                "connected.\n"
                "  - plug in an ESP32 flashed with ver_bridge, or\n"
                "  - develop without hardware:  VER_BACKEND=mock\n"
                "  - check what's connected:    python -m ver.tools.ports"
            )
        return self._bridge().gpio(**kwargs)

    def motor(self, **kwargs):
        from ..esp32.transport import find_ports

        if not find_ports() and self.port is None:
            raise UnsupportedCapability(
                "motors need an ESP32 (or similar) on USB. none is connected.\n"
                "  - for development without hardware:  VER_BACKEND=mock"
            )
        return self._bridge().motor(**kwargs)

    def imu(self, **kwargs):
        raise UnsupportedCapability(
            "no IMU on this laptop, and ver_bridge has no I2C support yet. "
            "use VER_BACKEND=mock for now."
        )

    def info(self) -> DeviceInfo:
        from ..esp32.transport import find_ports

        return DeviceInfo(
            backend=self.name,
            platform=self.platform,
            transport="native",
            details={
                "machine": platform.machine(),
                "python": platform.python_version(),
                "opencv": _has_opencv(),
                "gpio_bridge": [p for p, _ in find_ports()] or None,
            },
        )

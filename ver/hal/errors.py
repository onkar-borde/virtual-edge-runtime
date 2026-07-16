"""Errors raised by the runtime.

Applications catch these instead of catching serial.SerialException or
cv2.error, so swapping backends never changes error handling.
"""


class VERError(Exception):
    """Base class for every Virtual Edge Runtime error."""


class BackendNotAvailable(VERError):
    """A backend was requested but cannot run on this machine."""


class DeviceNotFound(VERError):
    """The backend is fine, but the physical device isn't there."""


class UnsupportedCapability(VERError):
    """This backend genuinely cannot do that (e.g. no camera on a Pico)."""


class TransportError(VERError):
    """The link to the device broke: serial dropped, timeout, bad framing."""


class PinError(VERError):
    """Invalid pin number, or pin used in a mode it wasn't configured for."""

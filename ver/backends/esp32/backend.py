"""ESP32 backend — real pins, reached over USB.

Application code that ran against MockGPIO runs against this unchanged.
That is the entire point of the last few hours of work.
"""

from __future__ import annotations

from typing import Optional

from ...hal.base import Backend, VirtualGPIO, VirtualMotor
from ...hal.errors import DeviceNotFound, PinError, TransportError, UnsupportedCapability
from ...hal.types import DeviceInfo, PinMode, PinState
from . import protocol
from .protocol import BAUD, PROTOCOL_VERSION
from .transport import FakeTransport, SerialTransport, Transport, find_ports


class ESP32GPIO(VirtualGPIO):
    """Pins on an ESP32, driven over serial."""

    def __init__(self, transport: Transport):
        self._t = transport
        self._modes: dict[int, PinMode] = {}

    def open(self) -> None:
        self._t.open()
        # Confirm we're talking to ver_bridge and not, say, a board still
        # running whatever sketch was on it before.
        reply = self._t.command("INFO")
        if not reply or protocol.FIRMWARE_NAME not in reply:
            raise TransportError(
                f"device replied {reply!r}; expected {protocol.FIRMWARE_NAME}.\n"
                "  flash firmware/ver_bridge/ver_bridge.ino to the board first."
            )
        version = reply.split()[1] if len(reply.split()) > 1 else "?"
        if version != str(PROTOCOL_VERSION):
            raise TransportError(
                f"firmware speaks protocol v{version}, host speaks "
                f"v{PROTOCOL_VERSION}. reflash the board."
            )

    def close(self) -> None:
        self._t.close()
        self._modes.clear()

    @property
    def is_open(self) -> bool:
        return self._t.is_open

    def _command(self, line: str) -> Optional[str]:
        try:
            return self._t.command(line)
        except TransportError as exc:
            # The firmware refuses bad pin usage, and that's a PinError to
            # the app -- same class MockGPIO raises. The app must not have
            # to know a wire was involved.
            text = str(exc).lower()
            if "not configured" in text or "out of range" in text \
                    or "reserved" in text or "input-only" in text:
                raise PinError(str(exc)) from exc
            raise

    def setup(self, pin: int, mode: PinMode) -> None:
        self._command(protocol.encode("MODE", pin, protocol.mode_to_wire(mode)))
        self._modes[pin] = mode

    def write(self, pin: int, state) -> None:
        value = PinState.from_value(state)
        self._command(protocol.encode("WRITE", pin, value.value))

    def read(self, pin: int) -> PinState:
        reply = self._command(protocol.encode("READ", pin))
        return PinState.from_value(int(reply))

    def pwm(self, pin: int, duty: float, frequency: int = 1000) -> None:
        if not 0.0 <= duty <= 1.0:
            raise PinError(f"duty {duty} outside 0.0-1.0")
        self._command(
            protocol.encode("PWM", pin, protocol.duty_to_wire(duty), frequency)
        )

    def analog_read(self, pin: int) -> float:
        reply = self._command(protocol.encode("ADC", pin))
        return protocol.adc_from_wire(int(reply))

    def stop_all(self) -> None:
        """Everything off, right now. Safe to call any time."""
        self._command("STOP")

    def info(self) -> DeviceInfo:
        return DeviceInfo(
            backend="esp32",
            platform="esp32",
            transport=self._t.describe(),
            details={"configured_pins": len(self._modes)},
        )


class ESP32Motor(VirtualMotor):
    """One motor on an H-bridge: two direction pins and one PWM enable.

    Wiring assumed (e.g. L298N, TB6612, DRV8833):
        forward_pin, reverse_pin -> direction inputs
        enable_pin               -> PWM speed input
    """

    def __init__(self, gpio: ESP32GPIO, forward_pin: int, reverse_pin: int,
                 enable_pin: int, frequency: int = 5000, label: str = "motor"):
        self._gpio = gpio
        self.forward_pin = forward_pin
        self.reverse_pin = reverse_pin
        self.enable_pin = enable_pin
        self.frequency = frequency
        self.label = label
        self._speed = 0.0
        self._open = False

    def open(self) -> None:
        if self._open:
            return
        if not self._gpio.is_open:
            self._gpio.open()
        self._gpio.setup(self.forward_pin, PinMode.OUTPUT)
        self._gpio.setup(self.reverse_pin, PinMode.OUTPUT)
        self._gpio.setup(self.enable_pin, PinMode.PWM)
        self._open = True
        self.stop()

    def close(self) -> None:
        if self._open:
            self.stop()
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def set_speed(self, speed: float) -> None:
        if not -1.0 <= speed <= 1.0:
            raise ValueError(f"speed {speed} outside -1.0..1.0")
        if not self._open:
            raise TransportError("motor used before open()")

        forward = speed > 0
        reverse = speed < 0
        # Both direction pins low before changing direction: on an H-bridge,
        # driving both high is shoot-through and can cook the driver.
        self._gpio.write(self.forward_pin, PinState.LOW)
        self._gpio.write(self.reverse_pin, PinState.LOW)
        self._gpio.pwm(self.enable_pin, abs(speed), self.frequency)
        if forward:
            self._gpio.write(self.forward_pin, PinState.HIGH)
        elif reverse:
            self._gpio.write(self.reverse_pin, PinState.HIGH)
        self._speed = float(speed)

    def stop(self) -> None:
        try:
            self._gpio.pwm(self.enable_pin, 0.0, self.frequency)
            self._gpio.write(self.forward_pin, PinState.LOW)
            self._gpio.write(self.reverse_pin, PinState.LOW)
        except Exception:
            pass  # stop() must never raise; it's the panic path
        self._speed = 0.0

    @property
    def speed(self) -> float:
        return self._speed

    def info(self) -> DeviceInfo:
        return DeviceInfo(
            backend="esp32", platform="esp32", transport=self._gpio._t.describe(),
            details={"label": self.label, "pins": [self.forward_pin,
                                                   self.reverse_pin,
                                                   self.enable_pin]},
        )


class ESP32Backend(Backend):
    """A host talking to an ESP32 over USB.

    `port=None` autodetects. `port="fake"` runs the in-process firmware
    simulation, which is how the tests exercise this whole file.
    """

    name = "esp32"
    platform = "esp32"

    def __init__(self, port: Optional[str] = None, baud: int = BAUD):
        self.port = port
        self.baud = baud
        self._gpio: Optional[ESP32GPIO] = None

    @classmethod
    def available(cls) -> bool:
        return bool(find_ports())

    def _transport(self) -> Transport:
        if self.port == "fake":
            return FakeTransport()
        port = self.port
        if port is None:
            ports = find_ports()
            if not ports:
                raise DeviceNotFound(
                    "no ESP32 found on any serial port.\n"
                    "  - is it plugged in with a DATA cable? many USB cables "
                    "are charge-only and look identical.\n"
                    "  - run:  python -m ver.tools.ports"
                )
            port = ports[0][0]
        return SerialTransport(port, self.baud)

    def gpio(self, **kwargs) -> VirtualGPIO:
        # One transport per board: two ESP32GPIO objects on one port would
        # interleave their replies and read each other's mail.
        if self._gpio is None:
            self._gpio = ESP32GPIO(self._transport())
        return self._gpio

    def motor(self, forward_pin: int, reverse_pin: int, enable_pin: int,
              **kwargs) -> VirtualMotor:
        gpio = self.gpio()
        return ESP32Motor(gpio, forward_pin, reverse_pin, enable_pin, **kwargs)

    def camera(self, index: int = 0, **kwargs):
        raise UnsupportedCapability(
            "a bare ESP32 has no camera. use the laptop backend for the "
            "webcam, or VER_BACKEND=mock."
        )

    def imu(self, **kwargs):
        raise UnsupportedCapability(
            "no IMU yet. an I2C IMU (MPU6050) needs firmware support -- "
            "that's the next milestone."
        )

    def info(self) -> DeviceInfo:
        ports = find_ports()
        return DeviceInfo(
            backend=self.name, platform=self.platform,
            transport=self.port or (ports[0][0] if ports else "none"),
            details={"detected": ports, "protocol": PROTOCOL_VERSION},
        )

"""Moving lines of text to and from the board.

Two transports:

  SerialTransport  - a real USB serial link to a real ESP32.
  FakeTransport    - the firmware's logic, reimplemented in Python.

FakeTransport is not a stub that returns "OK" to everything. It enforces the
same rules the C++ firmware enforces: unconfigured pins error, out-of-range
pins error, duty cycles are clamped. That means the entire protocol layer and
the entire backend get tested on a machine with no ESP32 attached, and a bug
in either shows up in CI instead of on your desk at 2am with a multimeter.

The rule this buys us: if FakeTransport and the real firmware ever disagree,
that's a bug in one of them, and the tests are how we find out which.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import Optional

from ...hal.errors import DeviceNotFound, TransportError
from . import protocol
from .protocol import ADC_MAX, BAUD, DEFAULT_TIMEOUT, FIRMWARE_NAME, PROTOCOL_VERSION, PWM_MAX

MAX_PIN = 39

# Pins the ESP32 uses for its own survival. Driving these is not a mistake
# the runtime should let you make quietly.
RESERVED_PINS = {
    6: "flash", 7: "flash", 8: "flash", 9: "flash", 10: "flash", 11: "flash",
}
INPUT_ONLY_PINS = {34, 35, 36, 37, 38, 39}


class Transport(ABC):
    """A request/response line channel. One command in, one line out."""

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    @abstractmethod
    def command(self, line: str) -> Optional[str]:
        """Send one command, wait for one response, return its payload."""

    @abstractmethod
    def describe(self) -> str: ...


class SerialTransport(Transport):
    """A real ESP32 over USB.

    Serialised with a lock: a Twist callback and a sensor poll running on
    different threads must not interleave half-written lines on the wire.
    """

    def __init__(self, port: str, baud: int = BAUD, timeout: float = DEFAULT_TIMEOUT,
                 reset_delay: float = 3.0):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.reset_delay = reset_delay
        self._serial = None
        self._lock = threading.Lock()

    def open(self) -> None:
        if self._serial is not None:
            return
        try:
            import serial
        except ImportError as exc:
            raise TransportError(
                "the esp32 backend needs pyserial:  pip install -e \".[laptop]\""
            ) from exc

        try:
            # Build the port unopened so DTR and RTS can be set *before* the
            # first electrical contact.
            #
            # This matters more than it looks. On an ESP32 devkit these two
            # lines are not serial signalling -- an auto-reset circuit wires
            # RTS -> EN (reset) and DTR -> IO0 (bootloader select). pyserial
            # asserts both on open by default, which drags EN low and holds
            # the chip in reset for as long as the port is open. The board
            # then answers nothing, and every error points at the firmware,
            # which is fine and innocent.
            link = serial.Serial()
            link.port = self.port
            link.baudrate = self.baud
            link.timeout = self.timeout
            link.dtr = False
            link.rts = False
            link.open()
        except Exception as exc:
            raise DeviceNotFound(
                f"could not open {self.port}: {exc}\n"
                "  - is the board plugged in?\n"
                "  - is the Arduino IDE serial monitor still open? close it."
            ) from exc

        self._serial = link
        self._reset_and_wait()

    def _reset_and_wait(self) -> None:
        """Pulse the board into a known state, then wait for it to boot.

        Without this we inherit whatever state the last program left the
        board in. With it, every session starts from a fresh boot and a
        clean pin configuration.
        """
        link = self._serial
        try:
            link.dtr = False   # IO0 high -> boot the sketch, not the ROM loader
            link.rts = True    # EN low   -> hold in reset
            time.sleep(0.1)
            link.reset_input_buffer()
            link.rts = False   # EN high  -> released; the board boots
        except Exception:
            # Boards without an auto-reset circuit ignore all this. Fine --
            # they were never in reset to begin with.
            pass

        # The firmware says RDY once it's up. Wait for it rather than
        # sleeping a fixed guess: slow boards get the time they need, fast
        # ones don't cost us anything.
        deadline = time.time() + self.reset_delay
        while time.time() < deadline:
            try:
                raw = link.readline()
            except Exception:
                break
            if raw and raw.startswith(b"RDY"):
                return

        # No RDY. Could be a board without auto-reset that was already
        # running, which is recoverable. Clear the ROM bootloader's boot
        # chatter and let the INFO handshake be the real test.
        try:
            link.reset_input_buffer()
            link.reset_output_buffer()
        except Exception:
            pass

    def close(self) -> None:
        if self._serial is None:
            return
        try:
            # Best effort: leave the board with everything off, even if the
            # app crashed. If this fails we're closing anyway.
            self._serial.write(b"STOP\n")
            self._serial.flush()
        except Exception:
            pass
        try:
            self._serial.close()
        finally:
            self._serial = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None

    def command(self, line: str) -> Optional[str]:
        if self._serial is None:
            raise TransportError("transport used before open()")
        with self._lock:
            try:
                self._serial.write(f"{line}\n".encode("ascii"))
                self._serial.flush()
                raw = self._serial.readline()
            except Exception as exc:
                raise TransportError(f"serial link failed on {self.port}: {exc}") from exc

        if not raw:
            raise TransportError(
                f"no reply to {line!r} within {self.timeout}s.\n"
                "  the board is connected but silent. usual causes:\n"
                "  - ver_bridge not flashed. open the Arduino serial monitor\n"
                "    at 115200, press EN, and look for 'RDY ver_bridge 1'.\n"
                "  - baud mismatch: firmware and host must both be "
                f"{self.baud}.\n"
                "  - the board is held in reset by DTR/RTS. if the serial\n"
                "    monitor works but this doesn't, that's the cause."
            )
        try:
            return protocol.parse_response(raw.decode("ascii", errors="replace"))
        except protocol.ProtocolError as exc:
            raise TransportError(str(exc)) from exc

    def describe(self) -> str:
        return f"serial {self.port} @ {self.baud}"


class FakeTransport(Transport):
    """The firmware, in Python. Same rules, no hardware.

    Keep this in lockstep with firmware/ver_bridge/ver_bridge.ino. If you
    change a rule in one, change it in both, and the tests will tell you
    if you forgot.
    """

    def __init__(self):
        self._open = False
        self.modes: dict[int, str] = {}
        self.states: dict[int, int] = {}
        self.pwm: dict[int, tuple[int, int]] = {}
        self.log: list[str] = []
        self.stopped = False

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        # Idempotent, per the VirtualDevice contract -- and matching
        # SerialTransport, which guards the same way. Closing an already
        # closed link is not an error; it's what a crash path does.
        if not self._open:
            return
        self.command("STOP")
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def describe(self) -> str:
        return "fake (in-process firmware simulation)"

    def inject(self, pin: int, value: int) -> None:
        """Test hook: pretend the physical world drove an input pin."""
        self.states[pin] = value

    def command(self, line: str) -> Optional[str]:
        if not self._open:
            raise TransportError("transport used before open()")
        self.log.append(line)
        try:
            return protocol.parse_response(self._handle(line))
        except protocol.ProtocolError as exc:
            raise TransportError(str(exc)) from exc

    # --- everything below mirrors the .ino ---

    def _handle(self, line: str) -> str:
        parts = line.strip().split()
        if not parts:
            return "ERR empty"
        cmd, args = parts[0], parts[1:]
        handler = getattr(self, f"_cmd_{cmd.lower()}", None)
        if handler is None:
            return f"ERR unknown command {cmd}"

        # Pin validity is checked here, in dispatch, before the handler runs
        # -- exactly where the .ino checks it. It used to be checked inside
        # the handlers, which reported "bad arguments" where the real board
        # reports "pin out of range". Two different errors for one mistake,
        # so `except PinError` worked on the fake and missed on real
        # hardware. The conformance suite caught it; keep the structures
        # aligned and it stays caught.
        if cmd not in ("PING", "INFO", "STOP"):
            if not args:
                return f"ERR bad arguments for {cmd}"
            try:
                pin = int(args[0])
            except ValueError:
                return f"ERR bad arguments for {cmd}"
            if not 0 <= pin <= MAX_PIN:
                return f"ERR pin {pin} out of range"

        try:
            return handler(args)
        except (ValueError, IndexError):
            return f"ERR bad arguments for {cmd}"

    def _pin(self, raw: str) -> int:
        pin = int(raw)
        if not 0 <= pin <= MAX_PIN:
            raise ValueError("pin out of range")
        return pin

    def _cmd_ping(self, args) -> str:
        return "OK"

    def _cmd_info(self, args) -> str:
        return f"OK {FIRMWARE_NAME} {PROTOCOL_VERSION} pins={MAX_PIN + 1}"

    def _cmd_mode(self, args) -> str:
        pin, mode = self._pin(args[0]), args[1]
        if pin in RESERVED_PINS:
            return f"ERR pin {pin} is reserved for {RESERVED_PINS[pin]}"
        if mode in ("output", "pwm") and pin in INPUT_ONLY_PINS:
            return f"ERR pin {pin} is input-only on the ESP32"
        if mode not in ("input", "pullup", "output", "pwm", "analog"):
            return f"ERR unknown mode {mode}"
        self.modes[pin] = mode
        self.states[pin] = 1 if mode == "pullup" else 0
        return "OK"

    def _cmd_write(self, args) -> str:
        pin, value = self._pin(args[0]), int(args[1])
        if self.modes.get(pin) != "output":
            return f"ERR pin {pin} not configured as output"
        self.states[pin] = 1 if value else 0
        self.stopped = False
        return "OK"

    def _cmd_read(self, args) -> str:
        pin = self._pin(args[0])
        if self.modes.get(pin) not in ("input", "pullup", "output"):
            return f"ERR pin {pin} not configured for reading"
        return f"OK {self.states.get(pin, 0)}"

    def _cmd_pwm(self, args) -> str:
        pin, duty, freq = self._pin(args[0]), int(args[1]), int(args[2])
        if self.modes.get(pin) != "pwm":
            return f"ERR pin {pin} not configured as pwm"
        self.pwm[pin] = (max(0, min(PWM_MAX, duty)), freq)
        self.stopped = False
        return "OK"

    def _cmd_adc(self, args) -> str:
        pin = self._pin(args[0])
        if self.modes.get(pin) != "analog":
            return f"ERR pin {pin} not configured as analog"
        return f"OK {ADC_MAX // 2}"

    def _cmd_stop(self, args) -> str:
        for pin in self.pwm:
            self.pwm[pin] = (0, self.pwm[pin][1])
        for pin, mode in self.modes.items():
            if mode == "output":
                self.states[pin] = 0
        self.stopped = True
        return "OK"


def find_ports() -> list[tuple[str, str]]:
    """Find likely ESP32/Pico boards. Returns [(port, description)].

    Matches on the USB-serial chips these boards ship with. Never raises --
    a missing pyserial just means no ports found.
    """
    try:
        from serial.tools import list_ports
    except ImportError:
        return []

    # CP210x (most ESP32 devkits), CH340/CH9102, FTDI, and RP2040 CDC.
    known_vids = {0x10C4, 0x1A86, 0x0403, 0x303A, 0x2E8A}
    found = []
    try:
        for port in list_ports.comports():
            if port.vid in known_vids:
                found.append((port.device, port.description or "unknown"))
    except Exception:
        return []
    return found

"""The wire protocol between the host and the microcontroller.

Design choice: line-based ASCII, not binary.

Binary would be a few hundred microseconds faster per round trip. ASCII buys
something worth more: you can open a serial monitor, type `WRITE 13 1`, and
watch the LED turn on. When the link misbehaves at 2am, being able to read
the wire with your eyes is worth more than the microseconds.

If profiling ever shows framing is the bottleneck (it won't -- USB latency
dominates by an order of magnitude), this module is the only thing that
changes. Nothing above it knows the encoding.

    host -> device      device -> host
    -------------       --------------
    PING                OK
    INFO                OK ver_bridge 1 pins=40
    MODE 13 output      OK
    WRITE 13 1          OK
    READ 13             OK 1
    PWM 18 512 5000     OK
    ADC 34              OK 2048
    STOP                OK
    I2CINIT 21 22 400   OK
    I2CSCAN             OK 68 3C
    I2CREAD 68 3B 6     OK 00FF01AB2C03
    I2CWRITE 68 6B 00   OK
    (bad command)       ERR unknown command

I2C addresses, registers, and data are hex; pins and frequency are decimal.
Hex for the bus because that's how every datasheet in the world writes them
-- 0x68, 0x3C -- and a protocol you can debug by eye should use the same
notation as the reference you're debugging against.

On boot the device sends `RDY ver_bridge 1` unprompted. The host uses that
to know the board rebooted and any pin configuration is now gone.
"""

from __future__ import annotations

from ...hal.types import PinMode

PROTOCOL_VERSION = 2
FIRMWARE_NAME = "ver_bridge"

# ESP32 ADC and PWM are 12-bit and 10-bit respectively in the Arduino core.
# The HAL speaks 0.0-1.0; these are the only place those raw numbers exist.
PWM_MAX = 1023
ADC_MAX = 4095

BAUD = 115200

# I2C defaults for a standard ESP32 devkit.
I2C_SDA = 21
I2C_SCL = 22
I2C_KHZ = 400

# Anything above this and we assume the board is wedged or unplugged.
DEFAULT_TIMEOUT = 1.0

_MODE_WIRE = {
    PinMode.INPUT: "input",
    PinMode.INPUT_PULLUP: "pullup",
    PinMode.OUTPUT: "output",
    PinMode.PWM: "pwm",
    PinMode.ANALOG: "analog",
}


def mode_to_wire(mode: PinMode) -> str:
    try:
        return _MODE_WIRE[mode]
    except KeyError:
        raise ValueError(f"unsupported pin mode: {mode}") from None


def duty_to_wire(duty: float) -> int:
    """0.0-1.0 -> 0-1023. Clamped, because a rounding error should not
    silently wrap a motor to full speed."""
    return max(0, min(PWM_MAX, round(duty * PWM_MAX)))


def adc_from_wire(raw: int) -> float:
    """0-4095 -> 0.0-1.0."""
    return max(0.0, min(1.0, raw / ADC_MAX))


def encode(command: str, *args) -> str:
    """Build one command line. No trailing newline -- the transport adds it."""
    parts = [command]
    parts.extend(str(a) for a in args)
    return " ".join(parts)


def bytes_to_wire(data: bytes) -> str:
    """b'\x00\xff' -> '00FF'"""
    return data.hex().upper()


def bytes_from_wire(text: str) -> bytes:
    """'00FF' -> b'\x00\xff'. Empty string is a legal empty read."""
    text = (text or "").strip()
    if not text:
        return b""
    try:
        return bytes.fromhex(text)
    except ValueError:
        raise ProtocolError(f"malformed hex payload: {text!r}") from None


class ProtocolError(Exception):
    """The device said something we don't understand."""


def parse_response(line: str) -> str | None:
    """Turn a device reply into a value, or raise.

    Returns the payload after OK (or None if there wasn't one).
    """
    line = line.strip()
    if not line:
        raise ProtocolError("empty response (device silent or timed out)")

    head, _, rest = line.partition(" ")

    if head == "OK":
        return rest or None
    if head == "ERR":
        raise ProtocolError(rest or "device reported an unspecified error")
    if head == "RDY":
        raise ProtocolError(
            "device rebooted mid-session; pin configuration was lost"
        )
    raise ProtocolError(f"unrecognised response: {line!r}")

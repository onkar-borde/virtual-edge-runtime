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
    (bad command)       ERR unknown command

On boot the device sends `RDY ver_bridge 1` unprompted. The host uses that
to know the board rebooted and any pin configuration is now gone.
"""

from __future__ import annotations

from ...hal.types import PinMode

PROTOCOL_VERSION = 1
FIRMWARE_NAME = "ver_bridge"

# ESP32 ADC and PWM are 12-bit and 10-bit respectively in the Arduino core.
# The HAL speaks 0.0-1.0; these are the only place those raw numbers exist.
PWM_MAX = 1023
ADC_MAX = 4095

BAUD = 115200

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

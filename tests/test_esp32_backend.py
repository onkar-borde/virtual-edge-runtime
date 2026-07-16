"""ESP32 backend tests, run against the in-process firmware simulation.

No hardware required. These test the real protocol encoder, the real
ESP32GPIO, and the real ESP32Motor -- only the wire is fake.
"""

import pytest

from ver import PinError, PinMode, PinState
from ver.backends.esp32.backend import ESP32Backend, ESP32GPIO, ESP32Motor
from ver.backends.esp32.protocol import (
    ADC_MAX, PWM_MAX, ProtocolError, adc_from_wire, duty_to_wire, encode,
    parse_response,
)
from ver.backends.esp32.transport import FakeTransport


@pytest.fixture
def gpio():
    g = ESP32GPIO(FakeTransport())
    g.open()
    yield g
    g.close()


# ----------------------------------------------------------- protocol units

def test_encode_joins_args():
    assert encode("MODE", 13, "output") == "MODE 13 output"
    assert encode("PING") == "PING"


def test_parse_ok_with_and_without_payload():
    assert parse_response("OK") is None
    assert parse_response("OK 1") == "1"


def test_parse_err_raises():
    with pytest.raises(ProtocolError, match="pin 13 not configured"):
        parse_response("ERR pin 13 not configured as output")


def test_parse_reboot_is_an_error_not_a_value():
    # RDY mid-session means the board reset and forgot its pin config.
    # Silently continuing would drive pins that are no longer outputs.
    with pytest.raises(ProtocolError, match="rebooted"):
        parse_response("RDY ver_bridge 1")


def test_parse_silence_raises():
    with pytest.raises(ProtocolError):
        parse_response("")


def test_duty_conversion_clamps():
    assert duty_to_wire(0.0) == 0
    assert duty_to_wire(1.0) == PWM_MAX
    assert duty_to_wire(0.5) == round(0.5 * PWM_MAX)
    # A float rounding error must never wrap a motor to full throttle.
    assert duty_to_wire(1.0001) == PWM_MAX
    assert duty_to_wire(-0.5) == 0


def test_adc_conversion_normalises():
    assert adc_from_wire(0) == 0.0
    assert adc_from_wire(ADC_MAX) == 1.0


# ----------------------------------------------------- gpio, same contract

def test_handshake_rejects_wrong_firmware():
    from ver.hal.errors import TransportError

    class WrongFirmware(FakeTransport):
        def _cmd_info(self, args):
            return "OK blink_sketch 1"

    g = ESP32GPIO(WrongFirmware())
    with pytest.raises(TransportError, match="ver_bridge"):
        g.open()


def test_handshake_rejects_protocol_mismatch():
    from ver.hal.errors import TransportError

    class OldFirmware(FakeTransport):
        def _cmd_info(self, args):
            return "OK ver_bridge 99 pins=40"

    g = ESP32GPIO(OldFirmware())
    with pytest.raises(TransportError, match="reflash"):
        g.open()


def test_write_read_roundtrip(gpio):
    gpio.setup(13, PinMode.OUTPUT)
    gpio.write(13, PinState.HIGH)
    assert gpio.read(13) is PinState.HIGH
    gpio.write(13, False)
    assert gpio.read(13) is PinState.LOW


def test_unconfigured_pin_raises_pin_error_not_transport_error(gpio):
    """The firmware refuses over a wire; the app still sees a PinError --
    identical to mock. If this leaks TransportError, the abstraction failed."""
    with pytest.raises(PinError):
        gpio.write(13, PinState.HIGH)


def test_reserved_flash_pin_refused(gpio):
    # GPIO 6-11 are the SPI flash. Driving them bricks the running sketch.
    with pytest.raises(PinError, match="reserved"):
        gpio.setup(6, PinMode.OUTPUT)


def test_input_only_pin_refused_as_output(gpio):
    with pytest.raises(PinError, match="input-only"):
        gpio.setup(34, PinMode.OUTPUT)


def test_input_only_pin_allowed_as_analog(gpio):
    gpio.setup(34, PinMode.ANALOG)
    assert 0.0 <= gpio.analog_read(34) <= 1.0


def test_pullup_reads_high(gpio):
    gpio.setup(4, PinMode.INPUT_PULLUP)
    assert gpio.read(4) is PinState.HIGH


def test_pwm_duty_out_of_range_rejected_before_the_wire(gpio):
    gpio.setup(18, PinMode.PWM)
    with pytest.raises(PinError):
        gpio.pwm(18, 1.5)
    # Rejected host-side: nothing bad should have reached the board.
    assert not any(line.startswith("PWM") for line in gpio._t.log)


def test_pwm_sets_duty(gpio):
    gpio.setup(18, PinMode.PWM)
    gpio.pwm(18, 0.5, 5000)
    duty, freq = gpio._t.pwm[18]
    assert duty == duty_to_wire(0.5)
    assert freq == 5000


def test_stop_all_zeroes_outputs(gpio):
    gpio.setup(13, PinMode.OUTPUT)
    gpio.setup(18, PinMode.PWM)
    gpio.write(13, PinState.HIGH)
    gpio.pwm(18, 1.0)
    gpio.stop_all()
    assert gpio._t.states[13] == 0
    assert gpio._t.pwm[18][0] == 0


def test_close_sends_stop():
    t = FakeTransport()
    g = ESP32GPIO(t)
    g.open()
    g.setup(13, PinMode.OUTPUT)
    g.write(13, PinState.HIGH)
    g.close()
    assert t.stopped


# ------------------------------------------------------------------ motors

@pytest.fixture
def motor(gpio):
    m = ESP32Motor(gpio, forward_pin=25, reverse_pin=26, enable_pin=27)
    m.open()
    yield m
    m.close()


def test_motor_forward_sets_one_direction_pin(motor):
    motor.set_speed(0.5)
    t = motor._gpio._t
    assert t.states[25] == 1
    assert t.states[26] == 0
    assert motor.speed == 0.5


def test_motor_reverse_sets_other_direction_pin(motor):
    motor.set_speed(-0.5)
    t = motor._gpio._t
    assert t.states[25] == 0
    assert t.states[26] == 1


def test_motor_never_drives_both_direction_pins_high(motor):
    """Both high on an H-bridge is shoot-through: a dead short across the
    supply, and a dead driver IC. Direction pins must go low before any
    change, every time."""
    motor.set_speed(1.0)
    motor.set_speed(-1.0)  # straight from full forward to full reverse
    t = motor._gpio._t

    high = set()
    for line in t.log:
        parts = line.split()
        if parts[0] == "WRITE":
            pin, value = int(parts[1]), int(parts[2])
            high.add(pin) if value else high.discard(pin)
            assert not (25 in high and 26 in high), "shoot-through!"


def test_motor_stops_on_close(gpio):
    m = ESP32Motor(gpio, 25, 26, 27)
    m.open()
    m.set_speed(1.0)
    m.close()
    assert m.speed == 0.0
    assert gpio._t.pwm[27][0] == 0


def test_motor_speed_bounds(motor):
    with pytest.raises(ValueError):
        motor.set_speed(1.5)


def test_motor_stop_never_raises(gpio):
    """stop() is the panic path. It runs when things are already broken,
    so it must not add a second exception on top of the first."""
    m = ESP32Motor(gpio, 25, 26, 27)
    m.open()
    gpio.close()  # yank the link out from under it
    m.stop()      # must not raise
    assert m.speed == 0.0


# ----------------------------------------------------------------- backend

def test_backend_fake_port_end_to_end():
    backend = ESP32Backend(port="fake")
    g = backend.gpio()
    g.open()
    g.setup(2, PinMode.OUTPUT)
    g.write(2, PinState.HIGH)
    assert g.read(2) is PinState.HIGH
    g.close()


def test_backend_shares_one_transport_per_board():
    # Two transports on one COM port would read each other's replies.
    backend = ESP32Backend(port="fake")
    assert backend.gpio() is backend.gpio()


def test_backend_has_no_camera():
    from ver import UnsupportedCapability

    with pytest.raises(UnsupportedCapability, match="laptop|mock"):
        ESP32Backend(port="fake").camera()


def test_missing_board_gives_actionable_error():
    from ver.hal.errors import DeviceNotFound

    backend = ESP32Backend(port=None)
    if backend.available():
        pytest.skip("a real board is plugged in")
    with pytest.raises(DeviceNotFound, match="cable"):
        backend.gpio().open()

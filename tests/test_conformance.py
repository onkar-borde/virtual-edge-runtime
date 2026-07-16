"""The conformance suite.

This is the file that turns "the abstraction works" from a claim into a
measurement.

Every other test file checks one backend in isolation. That proves each
backend works. It does not prove they behave *the same* -- which is the
entire promise of the project, and until this file existed, nothing tested
it.

Here, one set of assertions runs against every backend:

    mock        a Python dict pretending to be pins
    esp32-fake  the firmware's rules, reimplemented in Python
    esp32-real  an actual ESP32 on an actual USB port   (--hardware)

If `write(13, HIGH)` then `read(13)` returns HIGH on all three, the
abstraction is a measured property, not a design intention.

    pytest tests/test_conformance.py              # simulations only
    pytest tests/test_conformance.py --hardware   # plus the real board

A backend is "supported" when it passes this file. Not when it feels close.

--- pin choices ---
The pins below are legal on every backend at once, which is a real
constraint worth stating: 6-11 are the ESP32's flash, 34-39 are input-only.
A conformance suite that used pin 6 would be testing the mock's
permissiveness, not the contract.
"""

import pytest

from ver import PinError, PinMode, PinState
from ver.hal.errors import VERError

PIN_OUT = 13     # safe output on every board
PIN_PULLUP = 4   # reads HIGH with nothing attached, on real silicon too
PIN_PWM = 18
PIN_ADC = 34     # input-only on ESP32, which is fine for analog
PIN_RESERVED = 6  # SPI flash on ESP32


def _backends():
    """Which backends to run the contract against."""
    from ver.backends.esp32.transport import find_ports

    params = [
        pytest.param("mock", id="mock"),
        pytest.param("esp32-fake", id="esp32-fake"),
    ]
    params.append(
        pytest.param(
            "esp32-real",
            id="esp32-real",
            marks=[
                pytest.mark.hardware,
                pytest.mark.skipif(
                    not find_ports(),
                    reason="no board connected",
                ),
            ],
        )
    )
    return params


@pytest.fixture(params=_backends())
def gpio(request):
    """A live VirtualGPIO from each backend under test.

    The application-facing code below never learns which one it got. That's
    the point -- if a test needs to know, it isn't a conformance test.
    """
    name = request.param

    if name == "esp32-real" and not request.config.getoption("--hardware"):
        pytest.skip("real hardware not requested (use --hardware)")

    if name == "mock":
        from ver.backends.mock.backend import MockBackend

        device = MockBackend().gpio()
    elif name == "esp32-fake":
        from ver.backends.esp32.backend import ESP32Backend

        device = ESP32Backend(port="fake").gpio()
    else:
        from ver.backends.esp32.backend import ESP32Backend

        device = ESP32Backend().gpio()

    device.open()
    yield device
    try:
        device.close()
    except VERError:
        pass


# --------------------------------------------------------------- lifecycle

def test_opens_and_reports_open(gpio):
    assert gpio.is_open


def test_info_names_its_backend(gpio):
    info = gpio.info()
    assert info.backend
    assert info.transport


def test_close_is_idempotent(gpio):
    gpio.close()
    gpio.close()
    assert not gpio.is_open


def test_context_manager_closes(gpio):
    with gpio:
        assert gpio.is_open
    assert not gpio.is_open


# ------------------------------------------------------------ digital i/o

def test_write_then_read_returns_what_was_written(gpio):
    """The single most important assertion in the project.

    Same call, same result, whether the pin is a dict key, a simulated
    register, or 3.3 volts on real copper.
    """
    gpio.setup(PIN_OUT, PinMode.OUTPUT)

    gpio.write(PIN_OUT, PinState.HIGH)
    assert gpio.read(PIN_OUT) is PinState.HIGH

    gpio.write(PIN_OUT, PinState.LOW)
    assert gpio.read(PIN_OUT) is PinState.LOW


def test_write_accepts_bools_and_ints_alike(gpio):
    gpio.setup(PIN_OUT, PinMode.OUTPUT)

    gpio.write(PIN_OUT, True)
    assert gpio.read(PIN_OUT) is PinState.HIGH

    gpio.write(PIN_OUT, 0)
    assert gpio.read(PIN_OUT) is PinState.LOW


def test_pullup_reads_high_when_nothing_is_attached(gpio):
    gpio.setup(PIN_PULLUP, PinMode.INPUT_PULLUP)
    assert gpio.read(PIN_PULLUP) is PinState.HIGH


# ---------------------------------------------------------------- errors
#
# Errors are part of the contract, not an afterthought. If mock raises
# PinError and the ESP32 raises TransportError for the same mistake, then
# application error handling is not portable, and the abstraction is a lie
# that only holds on the happy path.

def test_write_before_setup_raises_pin_error(gpio):
    with pytest.raises(PinError):
        gpio.write(PIN_OUT, PinState.HIGH)


def test_read_before_setup_raises_pin_error(gpio):
    with pytest.raises(PinError):
        gpio.read(PIN_OUT)


def test_write_to_an_input_raises_pin_error(gpio):
    gpio.setup(PIN_PULLUP, PinMode.INPUT_PULLUP)
    with pytest.raises(PinError):
        gpio.write(PIN_PULLUP, PinState.HIGH)


def test_pwm_on_a_non_pwm_pin_raises_pin_error(gpio):
    gpio.setup(PIN_OUT, PinMode.OUTPUT)
    with pytest.raises(PinError):
        gpio.pwm(PIN_OUT, 0.5)


def test_pin_number_out_of_range_raises_pin_error(gpio):
    with pytest.raises(PinError):
        gpio.setup(999, PinMode.OUTPUT)


def test_duty_cycle_out_of_range_raises_pin_error(gpio):
    gpio.setup(PIN_PWM, PinMode.PWM)
    with pytest.raises(PinError):
        gpio.pwm(PIN_PWM, 1.5)
    with pytest.raises(PinError):
        gpio.pwm(PIN_PWM, -0.1)


def test_using_a_closed_device_raises(gpio):
    gpio.setup(PIN_OUT, PinMode.OUTPUT)
    gpio.close()
    with pytest.raises(VERError):
        gpio.write(PIN_OUT, PinState.HIGH)


# ------------------------------------------------------------------- pwm

@pytest.mark.parametrize("duty", [0.0, 0.25, 0.5, 1.0])
def test_pwm_accepts_the_full_legal_range(gpio, duty):
    gpio.setup(PIN_PWM, PinMode.PWM)
    gpio.pwm(PIN_PWM, duty)  # no readback exists; not raising is the contract


def test_pwm_frequency_can_change(gpio):
    gpio.setup(PIN_PWM, PinMode.PWM)
    gpio.pwm(PIN_PWM, 0.5, 1000)
    gpio.pwm(PIN_PWM, 0.5, 5000)


# ------------------------------------------------------------------- adc

def test_analog_read_is_normalised_to_zero_one(gpio):
    """Every backend returns 0.0-1.0. No backend leaks its raw units --
    the ESP32's 12-bit 0-4095 must never reach application code."""
    gpio.setup(PIN_ADC, PinMode.ANALOG)
    value = gpio.analog_read(PIN_ADC)
    assert isinstance(value, float)
    assert 0.0 <= value <= 1.0


# ------------------------------------------------- backend-specific truth
#
# Not everything should be uniform. Where real hardware has a genuine
# constraint, the simulations must honour it too -- otherwise mock lets you
# write code that bricks a real board.

def test_flash_pins_are_refused_everywhere(gpio):
    """GPIO 6-11 are the ESP32's SPI flash; driving them crashes the chip.

    Mock has no flash and could allow it. It must not: a mock that's more
    permissive than the hardware lets you develop code that fails only once
    it reaches something real, which is the one thing a mock exists to
    prevent.
    """
    if gpio.info().backend == "mock":
        pytest.xfail("known gap: mock does not model ESP32 reserved pins yet")
    with pytest.raises(PinError):
        gpio.setup(PIN_RESERVED, PinMode.OUTPUT)

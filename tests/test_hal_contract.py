"""Tests that pin down the HAL contract.

These run with zero hardware. When the ESP32 backend lands, the same
assertions should hold against it — that's how we know the abstraction
is real and not decorative.
"""

import pytest

from ver import PinError, PinMode, PinState, Runtime, UnsupportedCapability


@pytest.fixture
def rt():
    with Runtime("mock") as runtime:
        yield runtime


def test_runtime_selects_mock(rt):
    assert rt.backend_name == "mock"
    assert rt.info().backend == "mock"


def test_env_override(monkeypatch):
    monkeypatch.setenv("VER_BACKEND", "mock")
    assert Runtime().backend_name == "mock"


def test_gpio_write_read_roundtrip(rt):
    with rt.gpio() as gpio:
        gpio.setup(13, PinMode.OUTPUT)
        gpio.write(13, PinState.HIGH)
        assert gpio.read(13) is PinState.HIGH
        gpio.write(13, False)
        assert gpio.read(13) is PinState.LOW


def test_gpio_rejects_unconfigured_pin(rt):
    with rt.gpio() as gpio:
        with pytest.raises(PinError):
            gpio.write(5, PinState.HIGH)


def test_gpio_rejects_write_to_input(rt):
    with rt.gpio() as gpio:
        gpio.setup(5, PinMode.INPUT)
        with pytest.raises(PinError):
            gpio.write(5, PinState.HIGH)


def test_gpio_rejects_bad_pin_number(rt):
    with rt.gpio() as gpio:
        with pytest.raises(PinError):
            gpio.setup(999, PinMode.OUTPUT)


def test_gpio_use_before_open_fails(rt):
    gpio = rt.gpio()
    with pytest.raises(PinError):
        gpio.setup(2, PinMode.OUTPUT)


def test_pullup_reads_high_by_default(rt):
    with rt.gpio() as gpio:
        gpio.setup(4, PinMode.INPUT_PULLUP)
        assert gpio.read(4) is PinState.HIGH


def test_pwm_duty_bounds(rt):
    with rt.gpio() as gpio:
        gpio.setup(18, PinMode.PWM)
        gpio.pwm(18, 0.5, 5000)
        assert gpio.pwm_values[18] == (0.5, 5000)
        with pytest.raises(PinError):
            gpio.pwm(18, 1.5)


def test_imu_reads_gravity(rt):
    with rt.imu() as imu:
        reading = imu.read()
        assert 9.0 < reading.accel.z < 10.5
        assert reading.timestamp > 0


def test_imu_stream_yields(rt):
    with rt.imu() as imu:
        samples = [s for _, s in zip(range(5), imu.stream(hz=500))]
        assert len(samples) == 5


def test_motor_speed_bounds(rt):
    with rt.motor() as motor:
        motor.set_speed(0.75)
        assert motor.speed == 0.75
        with pytest.raises(ValueError):
            motor.set_speed(2.0)


def test_motor_stops_on_close(rt):
    motor = rt.motor()
    motor.open()
    motor.set_speed(1.0)
    motor.close()
    assert motor.speed == 0.0


def test_shutdown_stops_motors():
    runtime = Runtime("mock")
    motor = runtime.motor()
    motor.open()
    motor.set_speed(1.0)
    runtime.shutdown()
    assert motor.speed == 0.0
    assert not motor.is_open


def test_camera_frame_shape(rt):
    with rt.camera() as cam:
        frame = cam.read()
        assert (frame.width, frame.height) == (640, 480)


def test_unknown_backend_raises():
    from ver import BackendNotAvailable

    with pytest.raises(BackendNotAvailable):
        Runtime("jetson-orin-that-doesnt-exist")


def test_pinstate_truthiness():
    assert bool(PinState.HIGH) is True
    assert bool(PinState.LOW) is False

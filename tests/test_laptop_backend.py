"""Laptop backend tests.

These must pass whether or not an ESP32 is plugged in. The previous version
of this file assumed no board -- which is why it passed in CI and failed the
moment a real board appeared. Tests that only pass on the author's desk are
worse than no tests.
"""

import pytest

from ver import Runtime, UnsupportedCapability
from ver.backends import registry
from ver.backends.esp32.transport import find_ports
from ver.backends.laptop.backend import LaptopBackend, _has_opencv

needs_opencv = pytest.mark.skipif(not _has_opencv(), reason="OpenCV not installed")
has_board = bool(find_ports())
needs_board = pytest.mark.skipif(not has_board, reason="no ESP32 connected")
needs_no_board = pytest.mark.skipif(has_board, reason="an ESP32 is connected")


# ------------------------------------------------------------- autodetect

def test_all_three_backends_are_registered():
    assert set(registry.all_backends()) == {"mock", "laptop", "esp32"}


def test_mock_is_always_available():
    assert "mock" in registry.available_backends()


@needs_opencv
def test_laptop_wins_autodetect():
    """The laptop wins whether or not a board is attached.

    An ESP32 on USB is a peripheral, not a platform. If plugging one in
    changed which backend you land on, camera_view.py would break the moment
    you connected a microcontroller -- which is exactly the bug this test
    now guards.
    """
    assert Runtime().backend_name == "laptop"


def test_env_var_still_overrides_autodetect(monkeypatch):
    monkeypatch.setenv("VER_BACKEND", "mock")
    assert Runtime().backend_name == "mock"


def test_laptop_reports_honest_platform_info():
    info = LaptopBackend().info()
    assert info.backend == "laptop"
    assert info.platform in ("windows", "linux", "darwin")
    assert "python" in info.details


# ------------------------------------------------------ capability routing

def test_laptop_never_claims_an_imu():
    with pytest.raises(UnsupportedCapability, match="mock"):
        LaptopBackend().imu()


@needs_no_board
@pytest.mark.parametrize("capability", ["gpio", "motor"])
def test_pins_refused_when_nothing_is_plugged_in(capability):
    backend = LaptopBackend()
    with pytest.raises(UnsupportedCapability) as exc:
        getattr(backend, capability)()
    # An error that doesn't say what to do next is just a complaint.
    text = str(exc.value).lower()
    assert "esp32" in text and "mock" in text


@needs_board
def test_gpio_routes_through_the_board_when_present():
    from ver.backends.esp32.backend import ESP32GPIO

    assert isinstance(LaptopBackend().gpio(), ESP32GPIO)


def test_gpio_delegates_to_esp32_backend():
    """port='fake' runs the firmware simulation, so this exercises the whole
    laptop -> esp32 delegation path with no hardware at all."""
    from ver import PinMode, PinState

    gpio = LaptopBackend(port="fake").gpio()
    gpio.open()
    gpio.setup(13, PinMode.OUTPUT)
    gpio.write(13, PinState.HIGH)
    assert gpio.read(13) is PinState.HIGH
    gpio.close()


def test_one_bridge_shared_across_calls():
    backend = LaptopBackend(port="fake")
    assert backend.gpio() is backend.gpio()


def test_motor_delegates_to_esp32_backend():
    motor = LaptopBackend(port="fake").motor(
        forward_pin=25, reverse_pin=26, enable_pin=27
    )
    motor.open()
    motor.set_speed(0.5)
    assert motor.speed == 0.5
    motor.close()
    assert motor.speed == 0.0


# ----------------------------------------------------------------- camera

@needs_opencv
def test_camera_read_before_open_raises():
    from ver.hal.errors import TransportError

    cam = LaptopBackend().camera()
    with pytest.raises(TransportError):
        cam.read()


@needs_opencv
def test_camera_close_is_idempotent():
    cam = LaptopBackend().camera()
    cam.close()
    cam.close()
    assert not cam.is_open


@needs_opencv
def test_missing_camera_index_raises_device_not_found():
    from ver.hal.errors import DeviceNotFound

    cam = LaptopBackend().camera(index=99)
    with pytest.raises(DeviceNotFound):
        cam.open()

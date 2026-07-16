"""I2C conformance: one contract, every backend.

Same shape as test_conformance.py. The simulated MPU6050 lives in one place
(ver/backends/esp32/fake_devices.py) and is shared by mock and esp32-fake on
purpose -- two simulations of one chip would drift, and then "it works on
mock" would stop meaning anything.

    pytest tests/test_i2c_conformance.py
    pytest tests/test_i2c_conformance.py --hardware   # real MPU6050 required
"""

import pytest

from ver.drivers.mpu6050 import (
    ACCEL_CONFIG,
    DEFAULT_ADDRESS,
    KNOWN_CHIPS,
    MPU6050,
    PWR_MGMT_1,
    WHO_AM_I,
)
from ver.hal.errors import VERError

OLED_ADDRESS = 0x3C


def _backends():
    from ver.backends.esp32.transport import find_ports

    return [
        pytest.param("mock", id="mock"),
        pytest.param("esp32-fake", id="esp32-fake"),
        pytest.param(
            "esp32-real", id="esp32-real",
            marks=[
                pytest.mark.hardware,
                pytest.mark.skipif(not find_ports(), reason="no board connected"),
            ],
        ),
    ]


@pytest.fixture(scope="session")
def real_backend():
    """One board, one serial port, one connection for the whole session.

    Opening the port resets the ESP32 and costs ~3s waiting for it to boot.
    Doing that per-test turned a 2-second suite into a minute of watching a
    board reboot. There is only one board; there should be one connection.
    """
    from ver.backends.esp32.backend import ESP32Backend

    backend = ESP32Backend()
    yield backend
    backend.close()


@pytest.fixture(params=_backends())
def bus(request):
    name = request.param
    if name == "esp32-real" and not request.config.getoption("--hardware"):
        pytest.skip("real hardware not requested (use --hardware)")

    if name == "mock":
        from ver.backends.mock.backend import MockBackend

        backend = MockBackend()
    elif name == "esp32-fake":
        from ver.backends.esp32.backend import ESP32Backend

        backend = ESP32Backend(port="fake")
    else:
        backend = request.getfixturevalue("real_backend")

    device = backend.i2c()
    device.open()
    yield device
    try:
        device.close()
        # The backend owns the transport, not the device. Simulations don't
        # care; a real COM port very much does -- leak it and the next test
        # gets "Access is denied".
        if name != "esp32-real":
            backend.close()
    except VERError:
        pass


# ------------------------------------------------------------------- bus

def test_bus_opens(bus):
    assert bus.is_open


def test_scan_finds_the_imu(bus):
    """On esp32-real this requires an MPU6050 actually wired to SDA/SCL."""
    assert DEFAULT_ADDRESS in bus.scan()


def test_scan_addresses_are_in_range(bus):
    assert all(0x08 <= a <= 0x77 for a in bus.scan())


def test_who_am_i(bus):
    """Any of the MPU family is a pass.

    This asserted == 0x68 until a real board turned up reporting 0x70 -- an
    MPU-6500, which is what most boards sold as "MPU6050" actually carry.
    The driver already knew that; this test didn't. A conformance suite that
    hardcodes one part number isn't testing the contract, it's testing the
    author's shopping history.
    """
    who = bus.read_u8(DEFAULT_ADDRESS, WHO_AM_I)
    assert who in KNOWN_CHIPS, f"unknown WHO_AM_I {who:#04x}"


def test_read_returns_exactly_the_requested_length(bus):
    for length in (1, 2, 6, 14):
        assert len(bus.read(DEFAULT_ADDRESS, 0x3B, length)) == length


def test_read_length_bounds_rejected_before_the_wire(bus):
    with pytest.raises(ValueError):
        bus.read(DEFAULT_ADDRESS, 0x3B, 0)
    with pytest.raises(ValueError):
        bus.read(DEFAULT_ADDRESS, 0x3B, 65)


def test_nack_on_an_empty_address(bus):
    """0x7F has nothing on it. Every backend must say so the same way."""
    with pytest.raises(VERError):
        bus.read(0x7F, 0x00, 1)


def test_write_then_read_back_a_register(bus):
    """Config writes stick -- once the chip is awake.

    The wake-up on the first line is not ceremony. A sleeping MPU silently
    drops writes to configuration registers while still ACKing them on the
    bus, and still honouring writes to PWR_MGMT_1 (otherwise nothing could
    ever wake it). This test spent an evening failing on real hardware for
    exactly that reason: the previous run's MPU6050.close() had put the chip
    to sleep, and every "write" here was a no-op that returned OK.

    The simulation used to accept writes in any state, so this passed
    against the fake and failed on silicon -- the precise failure mode fakes
    exist to prevent. It models the gating now, and this test would fail
    without the line below on every backend.
    """
    bus.write_u8(DEFAULT_ADDRESS, PWR_MGMT_1, 0x00)   # wake; config is gated on it

    bus.write_u8(DEFAULT_ADDRESS, ACCEL_CONFIG, 0x08)      # +/-4g
    assert bus.read_u8(DEFAULT_ADDRESS, ACCEL_CONFIG) == 0x08
    bus.write_u8(DEFAULT_ADDRESS, ACCEL_CONFIG, 0x00)      # put it back


def test_config_writes_are_ignored_while_asleep(bus):
    """The behaviour that cost an evening, pinned down so it can't surprise
    anyone again.

    A sleeping chip ACKs a config write on the bus and drops it. No error,
    no NACK, no clue -- the register simply doesn't change. Any driver that
    configures before waking is silently writing to nothing.
    """
    bus.write_u8(DEFAULT_ADDRESS, PWR_MGMT_1, 0x00)
    bus.write_u8(DEFAULT_ADDRESS, ACCEL_CONFIG, 0x00)
    bus.write_u8(DEFAULT_ADDRESS, PWR_MGMT_1, 0x40)        # sleep

    bus.write_u8(DEFAULT_ADDRESS, ACCEL_CONFIG, 0x08)      # ignored
    assert bus.read_u8(DEFAULT_ADDRESS, ACCEL_CONFIG) == 0x00, \
        "a sleeping chip must not accept config writes"

    # ...but the power path still answers, or it could never be woken.
    bus.write_u8(DEFAULT_ADDRESS, PWR_MGMT_1, 0x00)
    assert not bus.read_u8(DEFAULT_ADDRESS, PWR_MGMT_1) & 0x40

    bus.write_u8(DEFAULT_ADDRESS, ACCEL_CONFIG, 0x08)      # now it lands
    assert bus.read_u8(DEFAULT_ADDRESS, ACCEL_CONFIG) == 0x08
    bus.write_u8(DEFAULT_ADDRESS, ACCEL_CONFIG, 0x00)


def test_driver_wakes_before_it_configures(bus):
    """The driver's open() order is load-bearing, not stylistic.

    PWR_MGMT_1 = 0x00 must come before ACCEL_CONFIG / GYRO_CONFIG /
    SMPLRT_DIV, or those writes land on a sleeping chip and evaporate. This
    asserts the ordering survives future edits to open().
    """
    bus.write_u8(DEFAULT_ADDRESS, PWR_MGMT_1, 0x40)        # start asleep
    bus.write_u8(DEFAULT_ADDRESS, PWR_MGMT_1, 0x00)
    bus.write_u8(DEFAULT_ADDRESS, ACCEL_CONFIG, 0x18)      # a wrong value
    bus.write_u8(DEFAULT_ADDRESS, PWR_MGMT_1, 0x40)        # asleep again

    device = MPU6050(bus)
    device.open()
    # If open() had configured before waking, ACCEL_CONFIG would still be
    # 0x18 -- the driver's write would have been dropped.
    assert bus.read_u8(DEFAULT_ADDRESS, ACCEL_CONFIG) == 0x00, \
        "open() configured the chip before waking it"
    device.close()


def test_use_before_open_raises():
    from ver.backends.mock.backend import MockBackend

    device = MockBackend().i2c()
    with pytest.raises(VERError):
        device.scan()


# --------------------------------------------------- the driver on the bus

@pytest.fixture
def imu(bus):
    device = MPU6050(bus)
    device.open()
    yield device
    device.close()


def test_driver_wakes_the_chip(bus):
    """The chip returns zeros while asleep; open() must clear SLEEP.

    Every "my IMU reads 0,0,0" bug is this register, so the simulation
    models it and the driver can't forget.

    This test has now been wrong twice, in two different ways, and both are
    worth remembering.

    First it assumed the chip booted asleep -- true for a simulation built
    fresh each time, false for silicon that stayed awake from the previous
    test. State a fake resets for free is state hardware remembers.

    Then it asserted "a sleeping chip returns zeros". Also false: that's
    only true after a power-on reset, when the data registers happen to be
    zeroed. Set SLEEP on a chip that's been running and it keeps the last
    sample sitting in those registers. The simulation zeroes them because I
    wrote it that way, not because the datasheet says so.

    So assert on the bit, which is specified, rather than on the data, which
    is a side effect I guessed at.
    """
    bus.write_u8(DEFAULT_ADDRESS, PWR_MGMT_1, 0x40)      # request sleep
    assert bus.read_u8(DEFAULT_ADDRESS, PWR_MGMT_1) & 0x40, \
        "the SLEEP write did not take effect"

    device = MPU6050(bus)
    device.open()                                        # must clear SLEEP
    assert not bus.read_u8(DEFAULT_ADDRESS, PWR_MGMT_1) & 0x40, \
        "open() did not clear the SLEEP bit"
    assert device.read().accel.z > 5.0, "no gravity after wake-up"
    device.close()


def test_driver_reads_gravity(imu):
    """Sitting flat, Z should read about 1g. On real hardware this is the
    assertion that catches a wrong scale factor, a byte-order mistake, or a
    sign error -- all of which still produce plausible-looking numbers."""
    reading = imu.read()
    assert 8.0 < reading.accel.z < 11.5, f"expected ~9.81, got {reading.accel.z}"


def test_driver_gyro_is_near_zero_at_rest(imu):
    reading = imu.read()
    # rad/s. A chip on a desk isn't spinning; anything large means the
    # scale factor or the parse is wrong.
    assert abs(reading.gyro.z) < 1.0


def test_driver_reports_si_units(imu):
    reading = imu.read()
    magnitude = (reading.accel.x**2 + reading.accel.y**2 + reading.accel.z**2) ** 0.5
    # If someone returns raw LSBs or g instead of m/s^2, this catches it.
    assert 8.0 < magnitude < 11.5


def test_driver_temperature_is_plausible(imu):
    assert 5.0 < imu.temperature() < 85.0


def test_driver_rejects_the_wrong_chip(bus):
    """Pointing the IMU driver at an OLED must fail loudly.

    The tempting fix for "unknown WHO_AM_I" is to shrug and read anyway --
    but then this returns 14 bytes of display memory parsed as gravity.
    Plausible-looking wrong numbers are worse than an exception.
    """
    with pytest.raises(VERError):
        MPU6050(bus, address=OLED_ADDRESS).open()


def test_driver_accepts_an_mpu6500(bus):
    """Boards sold as "MPU6050" very often carry an MPU-6500 (WHO_AM_I
    0x70). They're register-compatible; rejecting them helps nobody, and
    Linux's own inv_mpu6050 driver treats the mismatch as a warning too."""
    from ver.backends.esp32.fake_devices import FakeMPU6500
    from ver.backends.mock.backend import MockI2C

    fake_bus = MockI2C(devices=(FakeMPU6500,))
    fake_bus.open()
    imu = MPU6050(fake_bus)
    imu.open()
    assert imu.chip == "MPU-6500"
    assert imu.who_am_i == 0x70
    assert imu.read().accel.z > 5.0
    imu.close()


def test_strict_mode_demands_a_genuine_6050():
    from ver.backends.esp32.fake_devices import FakeMPU6500
    from ver.backends.mock.backend import MockI2C

    fake_bus = MockI2C(devices=(FakeMPU6500,))
    fake_bus.open()
    with pytest.raises(VERError, match="MPU-6500"):
        MPU6050(fake_bus, strict=True).open()


def test_temperature_formula_follows_the_chip():
    """The one place these parts genuinely differ. A 6500 read with the
    6050's constants is off by ~15 degrees -- plausible enough to ship."""
    from ver.backends.esp32.fake_devices import FakeMPU6050, FakeMPU6500
    from ver.backends.mock.backend import MockI2C

    for devices, expected_divisor in ((FakeMPU6050, 340.0), (FakeMPU6500, 333.87)):
        bus = MockI2C(devices=(devices,))
        bus.open()
        imu = MPU6050(bus)
        imu.open()
        assert imu._temp_divisor == expected_divisor
        assert 5.0 < imu.temperature() < 85.0
        imu.close()


def test_driver_reports_which_chip_it_found(bus):
    imu = MPU6050(bus)
    imu.open()
    assert imu.info().details["who_am_i"] is not None
    assert imu.info().details["chip"] != "unknown"
    imu.close()   # that's an OLED


def test_driver_read_before_open(bus):
    with pytest.raises(VERError):
        MPU6050(bus).read()


def test_driver_is_a_virtual_imu(imu):
    from ver.hal.base import VirtualIMU

    assert isinstance(imu, VirtualIMU)


def test_driver_stream_yields_samples(imu):
    samples = [s for _, s in zip(range(3), imu.stream(hz=50))]
    assert len(samples) == 3

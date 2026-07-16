"""SerialTransport tests with a fake pyserial.

The bug these guard against: pyserial asserts DTR and RTS when it opens a
port. On an ESP32 devkit those lines run to the auto-reset circuit --
RTS -> EN, DTR -> IO0 -- so opening the port with them asserted holds the
chip in reset. The board goes silent, and every error message points at the
firmware, which is innocent.

It cost real debugging time, and it is invisible without hardware. So these
tests fake the serial layer and assert on the control lines directly.
"""

import sys
import types

import pytest

from ver.backends.esp32.transport import SerialTransport
from ver.hal.errors import DeviceNotFound


class FakeSerialPort:
    """Records what was done to it, in order."""

    def __init__(self):
        self.port = None
        self.baudrate = None
        self.timeout = None
        self._dtr = None
        self._rts = None
        self.is_open = False
        self.events: list[tuple] = []
        self.to_read: list[bytes] = []
        self.written: list[bytes] = []

    # dtr/rts as properties so we can log every change with its timing
    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, value):
        self._dtr = value
        self.events.append(("dtr", value, self.is_open))

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, value):
        self._rts = value
        self.events.append(("rts", value, self.is_open))

    def open(self):
        self.is_open = True
        self.events.append(("open", None, True))

    def close(self):
        self.is_open = False
        self.events.append(("close", None, False))

    def write(self, data):
        self.written.append(data)

    def flush(self):
        pass

    def readline(self):
        return self.to_read.pop(0) if self.to_read else b""

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass


@pytest.fixture
def fake_serial(monkeypatch):
    port = FakeSerialPort()
    module = types.ModuleType("serial")
    module.Serial = lambda *a, **kw: port
    monkeypatch.setitem(sys.modules, "serial", module)
    return port


def test_control_lines_are_deasserted_before_the_port_opens(fake_serial):
    """The whole bug in one assertion.

    DTR and RTS must be set False *before* open(), not after. Setting them
    after means the port has already opened with them asserted, EN has
    already gone low, and the board has already been yanked into reset.
    """
    fake_serial.to_read = [b"RDY ver_bridge 1\n"]
    SerialTransport("COM9", reset_delay=0.5).open()

    kinds = [name for name, _, _ in fake_serial.events]
    open_at = kinds.index("open")

    dtr_before = [v for n, v, _ in fake_serial.events[:open_at] if n == "dtr"]
    rts_before = [v for n, v, _ in fake_serial.events[:open_at] if n == "rts"]

    assert dtr_before == [False], "DTR must be deasserted before open()"
    assert rts_before == [False], "RTS must be deasserted before open()"


def test_reset_pulse_sequence_is_correct(fake_serial):
    """RTS high then low = pulse EN low then release = a clean boot.

    DTR must stay low throughout: DTR high pulls IO0 down, which boots the
    ROM bootloader instead of our sketch. Board would come up in flashing
    mode and answer nothing.
    """
    fake_serial.to_read = [b"RDY ver_bridge 1\n"]
    SerialTransport("COM9", reset_delay=0.5).open()

    after_open = fake_serial.events[fake_serial.events.index(("open", None, True)) + 1:]
    rts = [v for n, v, _ in after_open if n == "rts"]
    dtr = [v for n, v, _ in after_open if n == "dtr"]

    assert rts == [True, False], "RTS must pulse high (reset) then low (release)"
    assert all(v is False for v in dtr), "DTR must stay low or the ROM loader boots"


def test_open_waits_for_the_firmware_banner(fake_serial):
    # Boot chatter from the ROM loader, then our firmware announcing itself.
    fake_serial.to_read = [
        b"ets Jul 29 2019 12:21:46\n",
        b"rst:0x1 (POWERON_RESET),boot:0x13\n",
        b"RDY ver_bridge 1\n",
    ]
    transport = SerialTransport("COM9", reset_delay=2.0)
    transport.open()
    assert transport.is_open
    # All three lines consumed: we read until RDY rather than sleeping blind.
    assert fake_serial.to_read == []


def test_silent_board_still_opens_and_lets_handshake_decide(fake_serial):
    """A board with no auto-reset circuit never sends RDY. That's survivable,
    so don't fail here -- let the INFO handshake be the real verdict."""
    fake_serial.to_read = []
    transport = SerialTransport("COM9", reset_delay=0.2)
    transport.open()
    assert transport.is_open


def test_close_sends_stop_before_hanging_up(fake_serial):
    fake_serial.to_read = [b"RDY ver_bridge 1\n"]
    transport = SerialTransport("COM9", reset_delay=0.2)
    transport.open()
    transport.close()
    assert b"STOP\n" in fake_serial.written
    assert not transport.is_open


def test_close_is_idempotent(fake_serial):
    fake_serial.to_read = [b"RDY ver_bridge 1\n"]
    transport = SerialTransport("COM9", reset_delay=0.2)
    transport.open()
    transport.close()
    transport.close()


def test_port_in_use_gives_an_actionable_error(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("Access is denied.")

    module = types.ModuleType("serial")
    module.Serial = boom
    monkeypatch.setitem(sys.modules, "serial", module)

    with pytest.raises(DeviceNotFound, match="serial monitor"):
        SerialTransport("COM9").open()


def test_timeout_message_mentions_the_reset_trap(fake_serial):
    from ver.hal.errors import TransportError

    fake_serial.to_read = [b"RDY ver_bridge 1\n"]
    transport = SerialTransport("COM9", reset_delay=0.2)
    transport.open()

    fake_serial.to_read = []  # board goes silent
    with pytest.raises(TransportError, match="held in reset"):
        transport.command("INFO")

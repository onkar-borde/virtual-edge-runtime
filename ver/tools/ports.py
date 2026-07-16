"""List serial ports and flag likely ESP32 boards.

    python -m ver.tools.ports

The first thing to run when the board "isn't working". Usually it's a
charge-only USB cable, and this tells you in two seconds.
"""

from ver.backends.esp32.transport import find_ports


def main() -> None:
    try:
        from serial.tools import list_ports
    except ImportError:
        print("pyserial not installed.  pip install -e \".[laptop]\"")
        return

    all_ports = list(list_ports.comports())
    likely = {port for port, _ in find_ports()}

    if not all_ports:
        print("no serial ports found at all.")
        print()
        print("  - is the board plugged in?")
        print("  - try a different USB cable. many are charge-only and")
        print("    look identical to data cables. this is the #1 cause.")
        return

    print(f"{len(all_ports)} serial port(s):\n")
    for port in all_ports:
        mark = "  <-- likely your board" if port.device in likely else ""
        print(f"  {port.device:8s}  {port.description}{mark}")
        if port.vid is not None:
            print(f"            usb {port.vid:04X}:{port.pid:04X}")

    print()
    if likely:
        print(f"use it with:  VER_BACKEND=esp32 python examples/blink.py")
    else:
        print("no recognised board. if yours is listed above anyway, pass")
        print("the port explicitly and tell us the USB id so we can add it.")


if __name__ == "__main__":
    main()

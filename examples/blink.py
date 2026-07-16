"""Blink an LED.

    python examples/blink.py              # autodetect
    VER_BACKEND=mock python examples/blink.py

Note what's NOT in this file: no serial port, no COM3, no /dev/ttyUSB0,
no `import RPi.GPIO`. This same file will run against the ESP32 backend
untouched. That's the whole thesis.
"""

import time

from ver import PinMode, PinState, Runtime

LED_PIN = 13


def main() -> None:
    with Runtime() as rt:
        print(f"running on: {rt}")

        with rt.gpio() as gpio:
            gpio.setup(LED_PIN, PinMode.OUTPUT)

            for i in range(10):
                state = PinState.HIGH if i % 2 == 0 else PinState.LOW
                gpio.write(LED_PIN, state)
                print(f"  pin {LED_PIN} -> {state.name}")
                time.sleep(0.25)


if __name__ == "__main__":
    main()

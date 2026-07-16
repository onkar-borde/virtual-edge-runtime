"""Blink the ESP32's on-board LED. Real pin, real photons.

    python examples/blink_esp32.py

Wiring: none. GPIO 2 is the blue LED soldered onto most ESP32 devkits.
If yours doesn't blink, your board may use a different pin -- try 13 or 5,
or wire an external LED (long leg to the pin, short leg through a 220-330
ohm resistor to GND).

Compare this file to examples/blink.py. The logic is identical. The only
difference is that this one pins the backend to esp32 instead of letting
autodetect choose, so you get a clear error if the board is missing rather
than a silent fallback to mock.
"""

import time

from ver import PinMode, PinState, Runtime, VERError

LED_PIN = 2  # on-board blue LED on most ESP32 devkits


def main() -> None:
    try:
        rt = Runtime("esp32")
    except VERError as exc:
        print(f"no ESP32: {exc}\n")
        print("run  python -m ver.tools.ports  to see what's connected.")
        return

    with rt:
        print(f"running on: {rt}")

        try:
            gpio = rt.gpio()
            gpio.open()
        except VERError as exc:
            print(f"\ncouldn't talk to the board:\n  {exc}")
            return

        print(f"link: {gpio.info().transport}")
        print(f"blinking GPIO {LED_PIN} -- watch the board\n")

        with gpio:
            gpio.setup(LED_PIN, PinMode.OUTPUT)
            for i in range(20):
                state = PinState.HIGH if i % 2 == 0 else PinState.LOW
                gpio.write(LED_PIN, state)
                print(f"  pin {LED_PIN} -> {state.name}")
                time.sleep(0.25)

        print("\ndone. the firmware watchdog will have shut the pin down too.")


if __name__ == "__main__":
    main()

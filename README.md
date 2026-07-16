# Virtual Edge Runtime

![tests](https://github.com/onkar-borde/virtual-edge-runtime/actions/workflows/ci.yml/badge.svg)

Write edge AI and robotics code once. Run it on a laptop, a Raspberry Pi, a Jetson, an Android phone, or a laptop bridged to an ESP32 over USB — without changing a line of application code.

```python
from ver import Runtime, PinMode, PinState

with Runtime() as rt:          # picks the best backend for this machine
    with rt.gpio() as gpio:
        gpio.setup(13, PinMode.OUTPUT)
        gpio.write(13, PinState.HIGH)
```

No `import RPi.GPIO`. No `COM3`. No `/dev/ttyUSB0`. The runtime figures that out.

## Why

An SBC is the assumed entry point for physical AI — but a Raspberry Pi 5 (8GB) runs ~₹18,000, and in raw compute it's modest: quad-core Cortex-A76, no discrete GPU. Meanwhile a mid-range laptop with a CUDA GPU beats it comfortably at exactly the workloads that matter here (ONNX inference, OpenCV, vision models).

The gap isn't compute. It's **physical I/O**. A Pi has a 40-pin header; a laptop has USB.

So: laptop as the brain, a ~₹400 ESP32 as the nervous system, USB in between. This split (host does perception + decisions, MCU does hard real-time control) is standard practice on dedicated SBC robots too — a general-purpose OS is bad at microsecond-accurate PWM regardless of how fast the CPU is.

What's missing from the ecosystem isn't the pattern. It's that nobody packages it as a **reusable abstraction** — every project rebuilds it, hardcoded, single-robot, unportable. That's the gap this fills.

## Scope: this does not emulate a Raspberry Pi

It isn't trying to. A Pi has memory-mapped GPIO at microsecond latency, runs off a battery on the robot itself, and costs ₹18k to destroy. A tethered laptop will never be those things, and chasing them means aiming at something worse than what you already have.

The goal is narrower and testable: **the same application code runs on both.** Laptop for development, Pi or Jetson for deployment, no rewrite in between. Nothing is emulated — the runtime just makes the swap free.

A platform is "supported" when it passes the HAL conformance suite. Not when it feels close enough.

### Pi-parity capability map

| Capability | Laptop + ESP32 |
|---|---|
| GPIO digital in/out | ✅ |
| PWM | ✅ |
| ADC | ✅ |
| Camera | ✅ |
| I2C (IMUs, most sensors) | ❌ |
| SPI (displays, fast ADCs) | ❌ |
| Encoders / interrupt counting | ❌ |
| UART passthrough (LiDAR, GPS) | ❌ |
| ROS2 bridge | ❌ |

## Status

| Component | State |
|---|---|
| HAL interfaces (GPIO, IMU, Camera, Motor) | ✅ Done |
| Mock backend | ✅ Done |
| Backend registry + autodetect | ✅ Done |
| Laptop backend (camera via OpenCV) | ✅ Done |
| ESP32 bridge firmware + serial backend | ✅ Done |
| IMU over I2C (MPU6050) | 🚧 Next |
| Raspberry Pi backend | 📋 Planned |
| Jetson backend | 📋 Planned |
| Android backend | 📋 Planned |

## Install

```bash
git clone <repo> && cd virtual-edge-runtime
pip install -e ".[dev]"
pytest                      # 69 tests, no hardware required
python examples/blink.py
```

## Architecture

```
Application code
       │
       ▼
   Runtime  ──────────────► picks a backend (or VER_BACKEND=mock)
       │
       ▼
  HAL contract           VirtualGPIO · VirtualIMU · VirtualCamera · VirtualMotor
       │
   ┌───┴────────┬──────────────┬───────────┐
   ▼            ▼              ▼           ▼
 mock       laptop+ESP32    RPi.GPIO    Jetson
 (dict)     (USB serial)    (header)    (TensorRT)
```

The rule: **if an app imports a backend directly, the abstraction has failed.**

## The mock backend is not a toy

It's the reference implementation. It enforces the same rules real hardware does — write to an unconfigured pin and it raises, same as an ESP32 would. If a real backend behaves differently from mock for identical calls, one of them has a bug. It also means the full test suite runs in CI with nothing plugged in.

## Flashing the ESP32

Open `firmware/ver_bridge/ver_bridge.ino` in the Arduino IDE, select **ESP32 Dev Module**, pick the port, upload. The firmware compiles on both Arduino-ESP32 v2.x and v3.x — the LEDC API changed in v3, and a shim at the top of the `.ino` covers both. Then:

```bash
python -m ver.tools.ports      # find the board
python examples/blink_esp32.py # blink a real LED
```

The firmware runs a **1-second watchdog**: if the host stops talking, every output goes low and every PWM channel goes to zero. A crashed Python script cannot leave a motor running.

## The DTR/RTS trap

On an ESP32 devkit, DTR and RTS aren't serial signalling — an auto-reset circuit wires `RTS → EN` (reset) and `DTR → IO0` (bootloader select). pyserial asserts both when it opens a port, which holds the chip in reset for as long as you're connected. The board goes silent and every error points at the firmware, which is innocent.

`SerialTransport` deasserts both *before* `open()`, then pulses RTS to boot the board cleanly and waits for the `RDY` banner. `tests/test_serial_transport.py` fakes pyserial and asserts on the control lines, because this bug is invisible without hardware and costs an hour every time it reappears.

## An ESP32 is a peripheral, not a platform

The laptop backend owns the camera **and** the pins — reaching the pins through an ESP32 on USB. It does not compete with an `esp32` backend for autodetect, because "laptop with a board plugged in" is still a laptop; it just grew hands.

This matters more than it sounds. When `esp32` outranked `laptop` in autodetect, plugging in a microcontroller silently broke the webcam demo — `Runtime()` landed on a backend with no camera. A peripheral must never change which platform you're on.

The upshot: `examples/blink.py` — written against the mock backend before any hardware existed — blinks a **real LED** when a board is present, and falls back to simulation when it isn't. Same file. No flags.

## Three backends, one contract

The laptop backend is deliberately honest about what a laptop *isn't*. Ask it for GPIO and it raises `UnsupportedCapability` with instructions — it does not quietly fake a pin. Faking is mock's job, and mock announces itself. A backend that lies about its hardware is worse than no backend.

## Design notes

- **Motors stop on close.** `VirtualMotor.close()` calls `stop()` first, and `Runtime.shutdown()` stops every motor before closing anything. A crashed app should not leave wheels spinning.
- **Units are SI.** IMU accel in m/s², gyro in rad/s. Speed is normalised −1.0…1.0. Duty cycle is 0.0…1.0. No backend-specific units leak upward.
- **Errors are HAL errors.** Apps catch `TransportError`, never `serial.SerialException`.
- **Everything is a context manager**, so hardware gets released even on exceptions.

## Safety

- Motor/actuator power comes from a **separate supply** — never through the laptop's USB port. Signal and ground only.
- The MCU is the only thing touching raw voltage. A dropped serial packet stalls the link; it doesn't damage the host.
- Physical risks (stall current, back-EMF, shorts) are identical to any Pi-based build: use a motor driver IC, fuse it, current-limit the supply.

## License

MIT

# Virtual Edge Runtime

![tests](https://github.com/USER-NAME/virtual-edge-runtime/actions/workflows/ci.yml/badge.svg)

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

## Drivers vs backends

`ver/drivers/mpu6050.py` is written against `VirtualI2C` and nothing else. It has no idea whether the bus is an ESP32 on USB, a Raspberry Pi's `/dev/i2c-1`, or a simulation.

**A backend knows a platform. A driver knows a chip.** Ship a Pi backend tomorrow and every driver works on it unchanged, with no port and no review. That's where the portability claim actually pays off — not in blinking an LED, but in never rewriting a sensor driver.

```bash
python -m ver.tools.i2c     # what's on the bus?
python examples/read_imu.py # real tilt from a real MPU6050
```

`read_imu.py` hasn't changed since it was written against a *simulated* IMU, before any I2C code existed. The tilt maths doesn't know whether gravity came from `random.gauss()` or a real accelerometer.

### A sleeping chip drops your writes and says OK

Found the hard way, on real silicon, after the code had been read three times and looked correct.

An MPU with the SLEEP bit set **ACKs writes to configuration registers on the bus and silently discards them**. No error, no NACK, no clue — the register just doesn't change. Only `PWR_MGMT_1` still answers, which makes sense: otherwise nothing could ever wake it.

So `MPU6050.open()` wakes before configuring, and that ordering is load-bearing rather than stylistic. Configure first and every setting evaporates while the code looks perfect.

The simulation used to accept writes in any state — **more permissive than the hardware**, which is the exact failure fakes exist to prevent: the test passed against the fake and failed on the board. It models the gating now, so `test_config_writes_are_ignored_while_asleep` fails on `mock` too if anyone regresses it.

`python -m ver.tools.i2cdebug` walks the bus one operation at a time and prints every line in both directions. `VER_DEBUG=1` does the same for any script. This is what the readable ASCII protocol was chosen for: when the code looks right and the hardware disagrees, the wire is the only witness that isn't guessing.

### Chips lie about their names

Boards sold as "MPU6050" routinely carry an MPU-6500 (`WHO_AM_I` 0x70), MPU-9250 (0x71), or MPU-9255 (0x73). They're register-compatible for accel and gyro — Linux's own `inv_mpu6050` driver treats the mismatch as a warning, not an error, for the same reason.

The driver accepts the family and special-cases only the temperature formula, which is the one thing that genuinely differs (`/340 + 36.53` on a 6050, `/333.87 + 21.0` on a 6500 — get it wrong and you're off by ~15°C, plausibly enough to ship).

It still **rejects unknown IDs** rather than trying anyway. Point an IMU driver at an OLED and "try anyway" hands you 14 bytes of display memory parsed as gravity. Plausible-looking wrong numbers are the worst failure mode there is.

## The demo: perception -> actuation

```bash
pip install -e ".[vision]"
python examples/track_and_actuate.py
```

Webcam -> YOLOv8 on the GPU -> PWM an LED by detection confidence. Wave at the laptop, the LED brightens.

An ESP32 has 520KB of RAM; YOLOv8n's weights alone are ~6MB — it will never run this. A Pi 5 manages ~3-5 fps at 640px, and fixing that means a Hailo accelerator for roughly the price of the Pi again. A mid-range laptop GPU does 30+ fps and already exists.

**The compute and the pins sit on opposite sides of the HAL, and neither knows the other exists.** `track_and_actuate.py` contains no `serial`, no COM port, no mention of an ESP32 — it asks the runtime for a camera and some pins. That's the difference between this and an Arduino sketch: not the blinking, the fact that a neural net on a CUDA GPU is driving the pin and nothing had to be rewritten to make that true.

### Measured latency, and why the architecture is shaped this way

Profiled on a CP210x ESP32 devkit under Windows:

| stage | ms/frame |
|---|---|
| camera wait | 0.0 |
| YOLOv8n inference (GTX 1650 Ti, 480px) | 11.8 |
| preview draw | 1.4 |
| **PWM round-trip over USB** | **40.0** |

The serial payload is 15 bytes — 1.3ms of transmission at 115200. The other ~38ms is USB latency, because Windows defaults the CP210x latency timer to 16ms per direction. **Raising the baud rate does nothing**; the wire is idle almost the whole time.

Two consequences, and they're the whole design:

1. **Actuation belongs off the perception loop.** Blocking a 12ms GPU on a 40ms round-trip drags a 53fps pipeline to 22fps. `track_and_actuate.py` runs the writes on their own thread — perception rate and control rate are different problems with different deadlines.
2. **You cannot close a control loop over this link.** A PID at 25Hz will oscillate a motor. That isn't a flaw in the bridge — it's exactly why real-time control lives *on the microcontroller* and the host does perception and decisions. The measurement doesn't undermine the architecture; it's the reason for it.

## Conformance: the abstraction is measured, not claimed

`tests/test_conformance.py` runs **one set of assertions against every backend**:

| | mock | esp32-fake | esp32-real |
|---|---|---|---|
| what it is | a Python dict | the firmware's rules in Python | an actual board on USB |
| needs hardware | no | no | yes (`--hardware`) |

```bash
pytest tests/test_conformance.py              # simulations
pytest tests/test_conformance.py --hardware   # + your real ESP32
```

If `write(13, HIGH)` then `read(13)` returns `HIGH` on all three, the abstraction is a *measured property*, not a design intention. Errors are part of the contract too: the same mistake must raise the same exception type everywhere, or application error handling isn't portable and the abstraction only holds on the happy path.

**A backend is supported when it passes this file.** Not when it feels close.

It earned its keep on the first run by catching a divergence: `setup(999)` raised `PinError` on mock but `TransportError` on the ESP32, because the Python fake validated pins in a different place than the C++ firmware does. The fake was lying about the hardware — the one thing a fake must never do.

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
| I2C (IMUs, most sensors) | ✅ |
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
| IMU over I2C (MPU6050) | ✅ Done |
| Raspberry Pi backend | 📋 Planned |
| Jetson backend | 📋 Planned |
| Android backend | 📋 Planned |

## Install

```bash
git clone <repo> && cd virtual-edge-runtime
pip install -e ".[dev]"
pytest                      # 201 tests, no hardware required
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

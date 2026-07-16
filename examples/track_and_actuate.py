"""Perception -> actuation. The demo this whole project exists for.

    webcam -> YOLOv8 on the GPU -> PWM an LED by detection confidence

Wave at your laptop; the LED brightens. Walk away; it fades.

    python examples/track_and_actuate.py
    python examples/track_and_actuate.py --class-name "cell phone"
    VER_BACKEND=mock python examples/track_and_actuate.py   # no hardware

Why this is the demo, and not blink:

An Arduino cannot run YOLO. An ESP32 has 520KB of RAM; YOLOv8n's weights
alone are ~6MB. A Raspberry Pi 5 manages roughly 3-5 fps at 640px, and
fixing that means buying a Hailo accelerator for about the price of the Pi
itself.

Your laptop already owns a CUDA GPU that does this at 30+ fps. What it
lacked was pins. That's the whole thesis: the compute and the pins sit on
opposite sides of the HAL, and neither one knows the other exists.

Nothing below imports serial, names a COM port, or mentions an ESP32. It
asks the runtime for a camera and some pins.
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
import time
from collections import deque

from ver import PinMode, Runtime, VERError

LED_PIN = 2          # on-board LED on most ESP32 devkits
SMOOTHING = 0.35     # 0 = jump instantly, 1 = never move
KEEPALIVE_S = 0.4    # must stay well under the firmware's 1s watchdog


def _has_display() -> bool:
    if platform.system() in ("Windows", "Darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def quiet_ultralytics() -> None:
    """Stop ultralytics logging once per frame.

    Writing a line to a Windows console is slow -- tens of milliseconds when
    it's happening 14 times a second. The deprecation warning about fp16 was
    costing more frame time than the fp16 was saving. Measure before you
    optimise; then measure what your logging costs too.
    """
    import logging

    try:
        from ultralytics.utils import LOGGER

        LOGGER.setLevel(logging.ERROR)
    except Exception:
        pass
    logging.getLogger("ultralytics").setLevel(logging.ERROR)


def pick_precision(model, half: bool, imgsz: int = 480) -> dict:
    """Find the fastest precision option by *timing* them.

    They renamed `half=` to `quantize=`. The obvious probe -- pass a keyword
    and see if it raises -- is worthless here, because ultralytics accepts
    `quantize=True` without complaint and then doesn't enable fp16. The probe
    declared success and inference got 1.8x slower.

    Acceptance is not effect. So don't ask whether the library tolerates a
    keyword; ask whether the keyword makes it faster. A stopwatch cannot be
    fooled by a silently ignored argument.
    """
    if not half:
        return {}

    import numpy as np

    blank = np.zeros((imgsz, imgsz, 3), dtype=np.uint8)
    candidates = [{}, {"half": True}, {"quantize": True}]
    results = []

    for kwargs in candidates:
        try:
            for _ in range(2):                       # warm this path
                model(blank, verbose=False, imgsz=imgsz, **kwargs)
            t0 = time.perf_counter()
            for _ in range(5):
                model(blank, verbose=False, imgsz=imgsz, **kwargs)
            results.append(((time.perf_counter() - t0) / 5 * 1000, kwargs))
        except Exception:
            continue

    if not results:
        return {}

    results.sort(key=lambda r: r[0])
    best_ms, best = results[0]
    label = ", ".join(f"{k}={v}" for k, v in best.items()) or "fp32"
    print(f"precision: {label} at {best_ms:.0f}ms/frame")
    for ms, kwargs in results[1:]:
        name = ", ".join(f"{k}={v}" for k, v in kwargs.items()) or "fp32"
        print(f"           ({name}: {ms:.0f}ms)")
    return best


def load_model(weights: str, half: bool = True, imgsz: int = 480):
    try:
        from ultralytics import YOLO
    except ImportError:
        sys.exit(
            "this demo needs ultralytics:\n"
            "    pip install -e \".[vision]\"\n"
            "(it pulls in torch -- a few hundred MB, one time)"
        )

    quiet_ultralytics()
    model = YOLO(weights)

    try:
        import torch

        if torch.cuda.is_available():
            model.to("cuda")
            print(f"inference on: {torch.cuda.get_device_name(0)}")
            # pick_precision also warms the model up, which matters: the
            # first inference on a cold GPU builds CUDA context and
            # allocates workspace -- seconds, not milliseconds. Averaging
            # that into a live fps counter poisons the number permanently.
            precision = pick_precision(model, half, imgsz=imgsz)
            return model, precision
        else:
            print(
                "inference on: CPU  (slow -- expect single-digit fps)\n"
                "  your GPU is idle because pip installed the CPU-only torch.\n"
                "  fix:  pip uninstall torch torchvision -y\n"
                "        pip install torch torchvision --index-url "
                "https://download.pytorch.org/whl/cu128\n"
                "  (check what your python has wheels for first:\n"
                "   pip index versions torch --index-url "
                "https://download.pytorch.org/whl/cu128)"
            )
    except ImportError:
        print("inference on: CPU")

    return model, {}


class Actuator:
    """Send PWM updates on a background thread at their own rate.

    Measured on a CP210x devkit under Windows: a PWM round-trip costs ~40ms.
    The payload is 15 bytes -- 1.3ms of transmission at 115200 -- wrapped in
    ~38ms of USB latency, because Windows defaults the CP210x latency timer
    to 16ms in each direction. Baud rate is irrelevant; the wire is idle
    almost the whole time.

    Doing that inline blocked a GPU that finishes in 12ms, and dragged a
    53fps pipeline down to 22fps. So the perception loop no longer waits: it
    drops a target value in a box, and this thread delivers whatever the
    latest value is, as fast as the link allows.

    Perception rate and control rate are now independent, which is how it
    should have been from the start. They are different problems with
    different deadlines: seeing at 30Hz and actuating at 25Hz are both fine,
    and neither should be hostage to the other.

    The deeper lesson is in the number itself. 40ms round-trip means you
    cannot close a control loop over this link -- a PID at 25Hz will
    oscillate a motor. That's not a flaw in the bridge; it's precisely why
    the architecture puts real-time control *on the microcontroller* and
    keeps the host for perception and decisions. The measurement doesn't
    undermine the design; it's the reason for it.
    """

    def __init__(self, gpio, pin: int, keepalive: float = 0.4):
        import threading

        self._gpio = gpio
        self._pin = pin
        self._keepalive = keepalive
        self._target = 0.0
        self._lock = threading.Lock()
        self._running = True
        self.error = None
        self.sent = 0
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def set(self, value: float) -> None:
        """Non-blocking. Overwrites any undelivered value -- the newest
        command is the only one worth sending."""
        with self._lock:
            self._target = value

    def _loop(self):
        last_sent = -1.0
        last_at = 0.0
        try:
            while self._running:
                with self._lock:
                    value = self._target
                now = time.time()
                moved = abs(value - last_sent) > 0.02
                stale = (now - last_at) > self._keepalive
                if moved or stale:
                    self._gpio.pwm(self._pin, value)
                    self.sent += 1
                    last_sent = value
                    last_at = now
                else:
                    time.sleep(0.005)
        except Exception as exc:
            self.error = exc

    def stop(self):
        self._running = False
        self._thread.join(timeout=2.0)
        try:
            self._gpio.pwm(self._pin, 0.0)
        except Exception:
            pass


class LatestFrame:
    """Read the camera on its own thread; keep only the newest frame.

    The loop was doing this, sequentially:

        wait 33ms for the webcam     (GPU idle)
        spend 17ms on inference      (camera idle)
        -----
        50ms per frame = 20 fps, on hardware capable of far more

    Neither device was busy; they were taking turns. Run capture in a thread
    and the two overlap, so the frame rate becomes max(33, 17) instead of
    33 + 17.

    Keeping only the latest frame is the other half. A queue would let the
    camera run ahead and hand inference frames from a second ago -- fine for
    recording, useless for control. Old frames are worthless here: dropping
    them is the feature.

    This is application-level pipelining, not a HAL concern -- which is why
    it lives in the example. VirtualCamera's job is to hand over a frame,
    not to have opinions about threading.
    """

    def __init__(self, camera):
        import threading

        self._camera = camera
        self._lock = threading.Lock()
        self._frame = None
        self._running = True
        self._error = None
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()
        # Don't hand out None to the first caller.
        deadline = time.time() + 5.0
        while self._frame is None and time.time() < deadline:
            if self._error:
                raise self._error
            time.sleep(0.01)
        if self._frame is None:
            raise TimeoutError("camera produced no frames within 5s")
        return self

    def _loop(self):
        try:
            while self._running:
                frame = self._camera.read()
                with self._lock:
                    self._frame = frame
        except Exception as exc:
            self._error = exc

    def read(self):
        if self._error:
            raise self._error
        with self._lock:
            return self._frame

    def stop(self):
        self._running = False
        self._thread.join(timeout=2.0)


def confidence_of(result, class_name: str) -> float:
    """Highest confidence among boxes matching class_name. 0.0 if absent."""
    names = result.names
    best = 0.0
    for box in result.boxes:
        label = names[int(box.cls[0])]
        if label == class_name:
            best = max(best, float(box.conf[0]))
    return best


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--class-name", default="person",
                        help="COCO class to track (default: person)")
    parser.add_argument("--weights", default="yolov8n.pt",
                        help="model weights; downloaded on first run")
    parser.add_argument("--pin", type=int, default=LED_PIN,
                        help=f"PWM output pin (default: {LED_PIN})")
    parser.add_argument("--headless", action="store_true",
                        help="no preview window")
    parser.add_argument("--imgsz", type=int, default=480,
                        help="inference resolution; lower is faster (default: 480)")
    parser.add_argument("--no-half", action="store_true",
                        help="disable fp16 (use if you see NaN detections)")
    parser.add_argument("--draw-every", type=int, default=2,
                        help="redraw the preview every Nth frame; the box "
                             "drawing costs more than the inference (default: 2)")
    args = parser.parse_args()

    half = not args.no_half
    model, precision = load_model(args.weights, half=half, imgsz=args.imgsz)

    with Runtime() as rt:
        print(f"runtime: {rt}")

        # --- perception: whatever camera this platform has ---
        try:
            cam = rt.camera(index=0)
            cam.open()
        except VERError as exc:
            sys.exit(f"no camera: {exc}")

        # --- actuation: whatever pins this platform has, if any ---
        gpio = None
        try:
            gpio = rt.gpio()
            gpio.open()
            gpio.setup(args.pin, PinMode.PWM)
            print(f"actuating: PWM on pin {args.pin} via {gpio.info().transport}")
        except VERError as exc:
            print(f"\nno pins, running perception only:\n  {exc}\n")
            gpio = None

        show = _has_display() and not args.headless
        if show:
            import cv2

        print(f"tracking '{args.class_name}' -- press q to quit\n")

        brightness = 0.0
        frames = 0
        started = time.time()
        # Rolling window, not a cumulative average: this reports what the
        # loop is doing *now*, which is the only number that means anything
        # once you start optimising.
        recent = deque(maxlen=30)
        infer_ms = deque(maxlen=30)
        # Per-stage timing. I guessed at this bottleneck three times and was
        # wrong three times. A profiler is cheaper than a fourth guess.
        stages = {k: deque(maxlen=30) for k in
                  ("wait", "infer", "draw", "serial", "other")}

        source = LatestFrame(cam).start()
        actuator = Actuator(gpio, args.pin).start() if gpio is not None else None

        try:
            with cam:
                while True:
                    tick = time.perf_counter()

                    t0 = time.perf_counter()
                    frame = source.read()
                    stages["wait"].append((time.perf_counter() - t0) * 1000)

                    t0 = time.perf_counter()
                    result = model(frame.data, verbose=False,
                                   imgsz=args.imgsz, **precision)[0]
                    dt = (time.perf_counter() - t0) * 1000
                    infer_ms.append(dt)
                    stages["infer"].append(dt)

                    target = confidence_of(result, args.class_name)

                    # Raw confidence flickers frame to frame, and a flickering
                    # PWM value is a visibly stuttering LED -- or, on a servo,
                    # a twitching one. Smooth it. This is the sort of thing
                    # that belongs above the HAL: it's control logic, not
                    # hardware, so it works identically on every backend.
                    brightness += (target - brightness) * (1.0 - SMOOTHING)

                    # Every pwm() call is a USB round-trip: write, wait, read
                    # the reply. That's frame budget spent telling the board
                    # something it already knows, and an LED cannot show a 1%
                    # change -- so skip the ones that don't matter.
                    #
                    # But the firmware watchdog zeroes every output after 1s
                    # of silence, on purpose, so a crashed host can't leave a
                    # motor running. Stand still in frame, brightness stops
                    # changing, nothing gets sent, and the watchdog correctly
                    # kills a *perfectly healthy* session. So: keepalive.
                    # Silence has to mean "the host is gone", not "the host
                    # has nothing new to say".
                    value = min(1.0, max(0.0, brightness))
                    t0 = time.perf_counter()
                    if actuator is not None:
                        actuator.set(value)
                        if actuator.error:
                            raise actuator.error
                    stages["serial"].append((time.perf_counter() - t0) * 1000)

                    frames += 1
                    recent.append(time.perf_counter() - tick)
                    fps = len(recent) / sum(recent) if recent else 0.0
                    avg_infer = sum(infer_ms) / len(infer_ms) if infer_ms else 0.0

                    t_draw = time.perf_counter()
                    if show and frames % args.draw_every == 0:
                        display = result.plot()
                        cv2.putText(
                            display,
                            f"{args.class_name}: {target:.2f} | led {brightness:.2f} "
                            f"| {fps:.1f} fps | infer {avg_infer:.0f}ms "
                            f"| {rt.backend_name}",
                            (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                            (0, 255, 0), 2,
                        )
                        cv2.imshow("Virtual Edge Runtime - perception to actuation",
                                   display)
                        stages["draw"].append(
                            (time.perf_counter() - t_draw) * 1000)
                        if cv2.waitKey(1) & 0xFF == ord("q"):
                            break
                    elif frames % 10 == 0:
                        print(f"  {args.class_name}={target:.2f}  "
                              f"led={brightness:.2f}  {fps:.1f} fps  "
                              f"infer={avg_infer:.0f}ms")
        finally:
            source.stop()
            # Leave the pin dark. The firmware watchdog would do this anyway
            # a second later, but relying on the watchdog for normal shutdown
            # is how you end up relying on it for abnormal shutdown too.
            if actuator is not None:
                actuator.stop()
            if show:
                import cv2

                cv2.destroyAllWindows()

        elapsed = time.time() - started
        print(f"\n{frames} frames in {elapsed:.1f}s "
              f"({frames / elapsed:.1f} fps)")

        def avg(key):
            values = stages[key]
            return sum(values) / len(values) if values else 0.0

        total = elapsed / frames * 1000 if frames else 0.0
        measured = sum(avg(k) for k in ("wait", "infer", "draw", "serial"))
        print("\nwhere each frame goes:")
        for key in ("wait", "infer", "draw", "serial"):
            ms = avg(key)
            share = (ms / total * 100) if total else 0
            print(f"  {key:8s} {ms:6.1f} ms  {share:4.0f}%")
        print(f"  {'unknown':8s} {max(0.0, total - measured):6.1f} ms")
        print(f"  {'TOTAL':8s} {total:6.1f} ms")
        if actuator is not None and elapsed:
            print(f"\nactuation: {actuator.sent} pwm writes "
                  f"({actuator.sent / elapsed:.0f} Hz) on its own thread")


if __name__ == "__main__":
    main()

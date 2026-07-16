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


def _has_display() -> bool:
    if platform.system() in ("Windows", "Darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def load_model(weights: str, half: bool = True):
    try:
        from ultralytics import YOLO
    except ImportError:
        sys.exit(
            "this demo needs ultralytics:\n"
            "    pip install -e \".[vision]\"\n"
            "(it pulls in torch -- a few hundred MB, one time)"
        )

    model = YOLO(weights)

    try:
        import torch

        if torch.cuda.is_available():
            model.to("cuda")
            print(f"inference on: {torch.cuda.get_device_name(0)}"
                  f"{' (fp16)' if half else ''}")
            # Warm up before anyone starts a stopwatch. The first inference
            # builds CUDA context and allocates workspace -- seconds, not
            # milliseconds. Averaging that into a live fps counter poisons
            # the number permanently, which is exactly the bug this line
            # and the rolling window below exist to kill.
            import numpy as np

            blank = np.zeros((480, 640, 3), dtype=np.uint8)
            for _ in range(3):
                model(blank, verbose=False, half=half)
            print("warmed up")
        else:
            print(
                "inference on: CPU  (slow -- expect single-digit fps)\n"
                "  your GPU is idle because pip installed the CPU-only torch.\n"
                "  fix:  pip uninstall torch torchvision\n"
                "        pip install torch torchvision --index-url "
                "https://download.pytorch.org/whl/cu124"
            )
    except ImportError:
        print("inference on: CPU")

    return model


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
    args = parser.parse_args()

    half = not args.no_half
    model = load_model(args.weights, half=half)

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
        last_sent = -1.0
        frames = 0
        started = time.time()
        # Rolling window, not a cumulative average: this reports what the
        # loop is doing *now*, which is the only number that means anything
        # once you start optimising.
        recent = deque(maxlen=30)
        infer_ms = deque(maxlen=30)

        try:
            with cam:
                for frame in cam.stream():
                    tick = time.perf_counter()

                    t0 = time.perf_counter()
                    result = model(frame.data, verbose=False,
                                   imgsz=args.imgsz, half=half)[0]
                    infer_ms.append((time.perf_counter() - t0) * 1000)

                    target = confidence_of(result, args.class_name)

                    # Raw confidence flickers frame to frame, and a flickering
                    # PWM value is a visibly stuttering LED -- or, on a servo,
                    # a twitching one. Smooth it. This is the sort of thing
                    # that belongs above the HAL: it's control logic, not
                    # hardware, so it works identically on every backend.
                    brightness += (target - brightness) * (1.0 - SMOOTHING)

                    # Every pwm() call is a USB round-trip: write, wait,
                    # read the reply. That's milliseconds of the frame budget
                    # spent telling the board something it already knows.
                    # An LED cannot show a 1% change; don't pay for it.
                    value = min(1.0, max(0.0, brightness))
                    if gpio is not None and abs(value - last_sent) > 0.02:
                        gpio.pwm(args.pin, value)
                        last_sent = value

                    frames += 1
                    recent.append(time.perf_counter() - tick)
                    fps = len(recent) / sum(recent) if recent else 0.0
                    avg_infer = sum(infer_ms) / len(infer_ms) if infer_ms else 0.0

                    if show:
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
                        if cv2.waitKey(1) & 0xFF == ord("q"):
                            break
                    elif frames % 10 == 0:
                        print(f"  {args.class_name}={target:.2f}  "
                              f"led={brightness:.2f}  {fps:.1f} fps  "
                              f"infer={avg_infer:.0f}ms")
        finally:
            # Leave the pin dark. The firmware watchdog would do this anyway
            # a second later, but relying on the watchdog for normal shutdown
            # is how you end up relying on it for abnormal shutdown too.
            if gpio is not None:
                try:
                    gpio.pwm(args.pin, 0.0)
                except VERError:
                    pass
            if show:
                import cv2

                cv2.destroyAllWindows()

        elapsed = time.time() - started
        print(f"\n{frames} frames in {elapsed:.1f}s")
        if infer_ms:
            avg = sum(infer_ms) / len(infer_ms)
            print(f"inference: {avg:.0f}ms/frame ({1000 / avg:.0f} fps ceiling)")
            print("if the loop is much slower than that ceiling, the model is")
            print("not the bottleneck -- the camera or the preview window is.")


if __name__ == "__main__":
    main()

"""Show a live camera feed through the HAL.

    python examples/camera_view.py            # your real webcam
    VER_BACKEND=mock python examples/camera_view.py   # a fake, no hardware

Press 'q' in the window to quit.

The interesting bit: this file never says "webcam". It asks the runtime for
a VirtualCamera and gets whatever this machine has. On a Jetson that will be
a CSI camera; on Android, Camera2; here, your laptop's webcam. Same file.

The one `import cv2` below is for *displaying* the window — that's UI, not
hardware access. The HAL owns the camera; cv2 just draws the pixels.
"""

import os
import platform
import time

from ver import Runtime, VERError


def _has_display() -> bool:
    """Can we open a window here?

    This has to be answered *before* touching cv2.imshow, because on a
    headless Linux box Qt calls abort() at the C level -- a try/except
    around imshow never gets a chance to run. Ask the environment instead.
    """
    if platform.system() in ("Windows", "Darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def main() -> None:
    with Runtime() as rt:
        print(f"running on: {rt}")

        try:
            cam = rt.camera(index=0)
            cam.open()
        except VERError as exc:
            print(f"\ncouldn't open a camera:\n  {exc}")
            return

        print(f"camera: {cam.info().details}")
        print("press 'q' in the window to quit")

        def headless():
            print("reading 30 frames without a window:")
            with cam:
                for i, frame in zip(range(30), cam.stream()):
                    print(f"  frame {i:2d}  {frame.width}x{frame.height}")

        if not _has_display():
            print("no display available, falling back to headless.\n")
            headless()
            return

        try:
            import cv2
        except ImportError:
            print("OpenCV not installed, falling back to headless.\n")
            headless()
            return

        frames = 0
        started = time.time()

        with cam:
            for frame in cam.stream():
                frames += 1
                elapsed = time.time() - started
                fps = frames / elapsed if elapsed > 0 else 0.0

                display = frame.data.copy()
                cv2.putText(
                    display,
                    f"{rt.backend_name} | {frame.width}x{frame.height} | {fps:.1f} fps",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2,
                )
                cv2.imshow("Virtual Edge Runtime - camera", display)

                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

        cv2.destroyAllWindows()
        print(f"\n{frames} frames in {time.time() - started:.1f}s")


if __name__ == "__main__":
    main()

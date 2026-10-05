"""Latest-frame camera. Capture never waits on inference or HTTP."""

from __future__ import print_function

import logging
import os
import threading
import time

import numpy as np

log = logging.getLogger("jetbot.camera")


class LatestFrameBuffer(object):
    """Single-slot latest frame. Readers copy; writers drop stale frames."""

    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None
        self._ts = 0.0
        self._seq = 0
        self._cond = threading.Condition(self._lock)

    def put(self, frame, ts=None):
        if ts is None:
            ts = time.time()
        stored = frame
        if hasattr(frame, "copy"):
            stored = frame.copy()
        with self._lock:
            self._frame = stored
            self._ts = ts
            self._seq += 1
            self._cond.notify_all()
        return self._seq

    def get(self, copy=True, min_seq=0, wait_s=0.0):
        deadline = time.time() + wait_s if wait_s else None
        with self._lock:
            while self._seq <= min_seq:
                if deadline is None:
                    if self._frame is None:
                        return None, 0.0, 0
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            frame = self._frame
            ts = self._ts
            seq = self._seq
        if frame is None:
            return None, 0.0, 0
        if copy:
            return frame.copy(), ts, seq
        return frame, ts, seq

    @property
    def seq(self):
        with self._lock:
            return self._seq

    @property
    def timestamp(self):
        with self._lock:
            return self._ts


class CameraController(object):
    def start(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def snapshot(self):
        return {"backend": "interface", "ok": False, "fps": 0.0}


class _BaseCapture(CameraController):
    def __init__(self, buffer, width, height, fps):
        self.buffer = buffer
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self._running = False
        self._thread = None
        self._ok = False
        self._frames = 0
        self._fps_ts = time.time()
        self._measured_fps = 0.0
        self._error = None
        self.backend = "base"

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="camera-capture")
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._close()

    def _close(self):
        pass

    def _grab(self):
        raise NotImplementedError

    def _loop(self):
        period = 1.0 / max(1, self.fps)
        while self._running:
            t0 = time.time()
            try:
                frame = self._grab()
            except Exception as exc:
                self._error = str(exc)
                self._ok = False
                time.sleep(0.2)
                continue
            if frame is None:
                time.sleep(0.01)
                continue
            self.buffer.put(frame, t0)
            self._ok = True
            self._frames += 1
            now = time.time()
            if now - self._fps_ts >= 1.0:
                self._measured_fps = self._frames / (now - self._fps_ts)
                self._frames = 0
                self._fps_ts = now
            sleep = period - (now - t0)
            if sleep > 0.002:
                time.sleep(sleep)

    def snapshot(self):
        return {
            "backend": self.backend,
            "ok": self._ok,
            "fps": round(self._measured_fps, 2),
            "width": self.width,
            "height": self.height,
            "error": self._error,
            "seq": self.buffer.seq,
        }


class GstArgusCamera(_BaseCapture):
    def __init__(self, buffer, width, height, fps, capture_width, capture_height, sensor_mode=3):
        super(GstArgusCamera, self).__init__(buffer, width, height, fps)
        import cv2
        self.backend = "nvargus"
        pipeline = (
            "nvarguscamerasrc sensor-mode=%d ! "
            "video/x-raw(memory:NVMM), width=%d, height=%d, format=(string)NV12, "
            "framerate=(fraction)%d/1 ! "
            "nvvidconv ! video/x-raw, width=(int)%d, height=(int)%d, format=(string)BGRx ! "
            "videoconvert ! video/x-raw, format=(string)BGR ! appsink drop=true max-buffers=1"
            % (int(sensor_mode), int(capture_width), int(capture_height), int(fps),
               int(width), int(height))
        )
        self.cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
        if not self.cap.isOpened():
            raise RuntimeError("nvargus pipeline failed to open")
        ok, frame = self.cap.read()
        if not ok or frame is None:
            self.cap.release()
            raise RuntimeError("nvargus opened but produced no frame")
        self.buffer.put(frame)

    def _grab(self):
        ok, frame = self.cap.read()
        if not ok:
            return None
        return frame

    def _close(self):
        try:
            if getattr(self, "cap", None) is not None:
                self.cap.release()
        except Exception:
            pass


class V4L2Camera(_BaseCapture):
    def __init__(self, buffer, width, height, fps, device="/dev/video0"):
        super(V4L2Camera, self).__init__(buffer, width, height, fps)
        import cv2
        self.backend = "v4l2"
        self.cap = cv2.VideoCapture(device)
        if not self.cap.isOpened():
            raise RuntimeError("cannot open %s" % device)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_FPS, fps)
        ok, frame = self.cap.read()
        if not ok or frame is None:
            self.cap.release()
            raise RuntimeError("%s opened but produced no frame" % device)
        if frame.shape[1] != width or frame.shape[0] != height:
            frame = cv2.resize(frame, (width, height))
        self.buffer.put(frame)
        self._cv2 = cv2

    def _grab(self):
        ok, frame = self.cap.read()
        if not ok or frame is None:
            return None
        if frame.shape[1] != self.width or frame.shape[0] != self.height:
            frame = self._cv2.resize(frame, (self.width, self.height))
        return frame

    def _close(self):
        try:
            if getattr(self, "cap", None) is not None:
                self.cap.release()
        except Exception:
            pass


class SyntheticCamera(_BaseCapture):
    """Deterministic moving scene so the stack runs without CSI."""

    def __init__(self, buffer, width, height, fps):
        super(SyntheticCamera, self).__init__(buffer, width, height, fps)
        self.backend = "synthetic"
        self._t = 0

    def _grab(self):
        w, h = self.width, self.height
        img = np.zeros((h, w, 3), dtype=np.uint8)
        img[:, :] = (32, 28, 24)
        # floor
        img[h // 2:, :] = (70, 78, 86)
        # moving "obstacle" block
        cx = int((0.5 + 0.35 * np.sin(self._t / 18.0)) * w)
        bw, bh = max(16, w // 7), max(20, h // 4)
        x0 = max(0, cx - bw // 2)
        x1 = min(w, cx + bw // 2)
        y0 = int(h * 0.45)
        y1 = min(h - 4, y0 + bh)
        img[y0:y1, x0:x1] = (40, 90, 210)
        # horizon line
        img[h // 2 - 1:h // 2 + 1, :] = (90, 90, 90)
        self._t += 1
        return img


def try_create_camera(cfg, buffer=None):
    cam_cfg = cfg.get("camera", {})
    width = int(cam_cfg.get("width", 320))
    height = int(cam_cfg.get("height", 240))
    fps = int(cam_cfg.get("fps", 15))
    simulate = cfg.get("robot", {}).get("simulate_if_missing", True)
    if buffer is None:
        buffer = LatestFrameBuffer()
    errors = []
    try:
        cam = GstArgusCamera(
            buffer, width, height, fps,
            capture_width=int(cam_cfg.get("capture_width", 816)),
            capture_height=int(cam_cfg.get("capture_height", 616)),
            sensor_mode=int(cam_cfg.get("sensor_mode", 3)),
        )
        cam.start()
        log.info("Camera backend: nvargus %dx%d@%d", width, height, fps)
        return cam, buffer, "nvargus"
    except Exception as exc:
        errors.append("nvargus: %s" % exc)
        log.warning("nvargus camera failed: %s", exc)
    device = cam_cfg.get("device", "/dev/video0")
    if os.path.exists(device):
        try:
            cam = V4L2Camera(buffer, width, height, fps, device=device)
            cam.start()
            log.info("Camera backend: v4l2 %s", device)
            return cam, buffer, "v4l2"
        except Exception as exc:
            errors.append("v4l2: %s" % exc)
            log.warning("V4L2 camera failed: %s", exc)
    if not simulate:
        raise RuntimeError("camera init failed: %s" % "; ".join(errors))
    cam = SyntheticCamera(buffer, width, height, fps)
    cam.start()
    log.info("Camera backend: synthetic (%s)", "; ".join(errors) if errors else "forced")
    return cam, buffer, "synthetic"

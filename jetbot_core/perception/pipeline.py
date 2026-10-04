"""Perception worker. Consumes latest frames; never blocks capture or HTTP."""

from __future__ import print_function

import logging
import os
import threading
import time

import cv2
import numpy as np

from ..events import EventType
from ..world import TrackedObject
from .depth import GeometricDepthEstimator
from .detect import ContourDetector, HaarDetector, TensorRTDetector
from .track import IoUTracker

log = logging.getLogger("jetbot.perception")


class PerceptionPipeline(object):
    def __init__(self, cfg, frame_buffer, world, bus, overlay_buffer):
        self.cfg = cfg.get("perception", {})
        self.nav_cfg = cfg.get("navigation", {})
        self.frame_buffer = frame_buffer
        self.world = world
        self.bus = bus
        self.overlay_buffer = overlay_buffer
        self._stop = threading.Event()
        self._thread = None
        self.inf_w = int(self.cfg.get("inference_width", 160))
        self.inf_h = int(self.cfg.get("inference_height", 120))
        self.depth = GeometricDepthEstimator(bins=int(self.nav_cfg.get("bins", 5)))
        inner = ContourDetector(min_area=int(self.cfg.get("min_object_area", 400)))
        engine = self.cfg.get("ssd_engine") or ""
        if engine and os.path.isfile(engine):
            self.detector = TensorRTDetector(engine, inner=HaarDetector(self.cfg.get("haar_cascade"), inner))
            backend = "tensorrt"
        else:
            self.detector = HaarDetector(self.cfg.get("haar_cascade"), inner=inner)
            backend = "opencv"
        self.tracker = IoUTracker()
        self.world.set_perception_meta(backend=backend)
        self._last_depth = 0.0
        self._last_det = 0.0
        self._blocked = False
        self._last_overlay = None
        self._det_n = 0
        self._det_t = time.time()
        self.depth_interval = float(self.cfg.get("depth_interval_s", 0.07))
        self.det_interval = float(self.cfg.get("detection_interval_s", 0.20))
        self.enable_depth = bool(self.cfg.get("enable_depth", True))
        self.enable_det = bool(self.cfg.get("enable_detection", True))

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="perception")
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self):
        last_seq = 0
        fps_n = 0
        fps_t = time.time()
        while not self._stop.is_set():
            frame, ts, seq = self.frame_buffer.get(copy=True, min_seq=last_seq, wait_s=0.25)
            if frame is None:
                continue
            last_seq = seq
            now = time.time()
            self.world.set_perception_meta(frame_seq=seq, last_frame_ts=ts, latency_ms=(now - ts) * 1000.0)
            small = cv2.resize(frame, (self.inf_w, self.inf_h))
            overlay = frame
            ran = False
            if self.enable_depth and (now - self._last_depth) >= self.depth_interval:
                depth = self.depth.estimate(small)
                self._last_depth = now
                ran = True
                self.world.update_obstacles(
                    depth["front_m"], depth["bins"], depth["blocked"],
                    depth["direction"], depth["confidence"], depth["source"],
                )
                self.world.set_perception_meta(depth_fps=depth.get("fps", 0.0), last_depth_ts=now, detect_fps=self.world.perception.get("detect_fps", 0.0))
                if depth["blocked"] and not self._blocked:
                    self.bus.emit(EventType.OBSTACLE_DETECTED, "depth", {
                        "distance": depth["front_m"],
                        "direction": depth["direction"],
                    }, confidence=depth["confidence"])
                    self._blocked = True
                elif not depth["blocked"] and self._blocked:
                    self.bus.emit(EventType.OBSTACLE_CLEARED, "depth", {
                        "distance": depth["front_m"],
                    }, confidence=depth["confidence"])
                    self._blocked = False
                overlay = cv2.resize(depth["vis"], (frame.shape[1], frame.shape[0]))
            if self.enable_det and (now - self._last_det) >= self.det_interval:
                dets = self.detector.detect(small)
                tracks = self.tracker.update(dets)
                self._last_det = now
                ran = True
                new_ids = []
                for tr in tracks:
                    obj = TrackedObject(tr.id, tr.cls, tr.bbox, tr.confidence, track_id=tr.id)
                    is_new = self.world.upsert_object(obj)
                    if is_new:
                        new_ids.append(tr.id)
                    overlay = _draw_box(overlay, tr)
                lost = self.world.prune_objects()
                for obj in lost:
                    self.bus.emit(EventType.OBJECT_LOST, "tracker", {"id": obj.id, "class": obj.cls})
                self._det_n += 1
                if now - self._det_t >= 1.0:
                    self.world.set_perception_meta(detect_fps=self._det_n / (now - self._det_t))
                    self._det_n = 0
                    self._det_t = now
                if new_ids:
                    self.bus.emit(EventType.OBJECT_DETECTED, "detector", {"ids": new_ids})
                    self.bus.emit(EventType.WORLD_CHANGED, "perception", {"reason": "new_objects", "ids": new_ids})
            fps_n += 1
            if now - fps_t >= 1.0:
                self.world.set_perception_meta(fps=fps_n / (now - fps_t))
                fps_n = 0
                fps_t = now
            self.overlay_buffer.put(overlay, now)
            if not ran:
                time.sleep(0.005)


def _draw_box(image, track):
    h, w = image.shape[:2]
    x0, y0, x1, y1 = track.bbox
    p0 = (int(x0 * w), int(y0 * h))
    p1 = (int(x1 * w), int(y1 * h))
    color = (80, 220, 255) if track.cls == "person" else (80, 200, 80)
    cv2.rectangle(image, p0, p1, color, 2)
    label = "%s %s %.2f" % (track.id, track.cls, track.confidence)
    cv2.putText(image, label, (p0[0], max(12, p0[1] - 4)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1, cv2.LINE_AA)
    return image

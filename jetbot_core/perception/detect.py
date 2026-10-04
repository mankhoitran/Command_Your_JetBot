"""Object detection backends. TensorRT is optional; OpenCV always works."""

from __future__ import print_function

import logging
import os

import cv2
import numpy as np

log = logging.getLogger("jetbot.detect")


class Detection(object):
    __slots__ = ("cls", "confidence", "bbox")

    def __init__(self, cls, confidence, bbox):
        self.cls = cls
        self.confidence = float(confidence)
        self.bbox = [float(x) for x in bbox]  # normalized x0,y0,x1,y1

    def to_dict(self):
        return {"class": self.cls, "confidence": self.confidence, "bbox": self.bbox}


class DetectorBackend(object):
    def detect(self, bgr):
        return []


class ContourDetector(DetectorBackend):
    def __init__(self, min_area=400, max_objects=8):
        self.min_area = int(min_area)
        self.max_objects = int(max_objects)

    def detect(self, bgr):
        h, w = bgr.shape[:2]
        small = bgr
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blur, 50, 140)
        kernel = np.ones((3, 3), np.uint8)
        edges = cv2.dilate(edges, kernel, iterations=1)
        found = []
        # Color-salient blobs (high saturation)
        sat = hsv[:, :, 1]
        val = hsv[:, :, 2]
        mask = cv2.inRange(sat, 80, 255)
        mask = cv2.bitwise_and(mask, cv2.inRange(val, 40, 255))
        mask = cv2.bitwise_or(mask, edges)
        contours, _ = _find_contours(mask)
        areas = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < self.min_area:
                continue
            x, y, bw, bh = cv2.boundingRect(cnt)
            if bw < 8 or bh < 8:
                continue
            if bw * bh > 0.7 * w * h:
                continue
            areas.append((area, x, y, bw, bh))
        areas.sort(key=lambda t: -t[0])
        for i, (area, x, y, bw, bh) in enumerate(areas[:self.max_objects]):
            conf = min(0.85, 0.35 + area / float(w * h) * 4.0)
            found.append(Detection(
                "object",
                conf,
                [x / float(w), y / float(h), (x + bw) / float(w), (y + bh) / float(h)],
            ))
        return found


class HaarDetector(DetectorBackend):
    def __init__(self, cascade_path, inner=None):
        self.inner = inner or ContourDetector()
        self.cascade = None
        if cascade_path and os.path.isfile(cascade_path):
            self.cascade = cv2.CascadeClassifier(cascade_path)
            if self.cascade.empty():
                self.cascade = None
                log.warning("Haar cascade empty: %s", cascade_path)
        else:
            log.info("Haar cascade not found (%s); contour-only detection", cascade_path)

    def detect(self, bgr):
        found = list(self.inner.detect(bgr))
        if self.cascade is None:
            return found
        h, w = bgr.shape[:2]
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        faces = self.cascade.detectMultiScale(gray, 1.2, 4, minSize=(24, 24))
        for (x, y, bw, bh) in faces:
            found.append(Detection(
                "person",
                0.72,
                [x / float(w), y / float(h), (x + bw) / float(w), (y + bh) / float(h)],
            ))
        return found


class TensorRTDetector(DetectorBackend):
    """Optional adapter around jetbot.ObjectDetector if an engine exists."""

    def __init__(self, engine_path, inner=None):
        self.inner = inner
        self.model = None
        try:
            from jetbot.object_detection import ObjectDetector
            self.model = ObjectDetector(engine_path)
            log.info("Loaded TensorRT SSD engine %s", engine_path)
        except Exception as exc:
            log.warning("TensorRT detector unavailable: %s", exc)
            if self.inner is None:
                self.inner = ContourDetector()

    def detect(self, bgr):
        if self.model is None:
            return self.inner.detect(bgr) if self.inner else []
        try:
            results = self.model(bgr)
        except Exception as exc:
            log.warning("TensorRT detect failed: %s", exc)
            return self.inner.detect(bgr) if self.inner else []
        found = []
        if not results:
            return found
        for det in results[0]:
            bbox = det.get("bbox") or [0, 0, 0, 0]
            found.append(Detection(
                str(det.get("label", "object")),
                float(det.get("confidence", 0.0)),
                bbox,
            ))
        return found


def _find_contours(mask):
    result = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if len(result) == 2:
        return result[0], result[1]
    return result[1], result[0]

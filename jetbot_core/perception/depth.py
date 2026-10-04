"""Lightweight obstacle / free-space estimate from a single RGB frame.

A TensorRT depth network is the right long-term backend, but this image has
no engine and no PyTorch. The geometric estimator is deterministic, cheap,
and good enough to stop before large nearby blobs. Swap via DepthBackend.
"""

from __future__ import print_function

import time

import cv2
import numpy as np


class DepthBackend(object):
    def estimate(self, bgr):
        """Return dict: front_m, bins (near=high), blocked, direction, confidence, vis."""
        raise NotImplementedError


class GeometricDepthEstimator(DepthBackend):
    def __init__(self, bins=5, min_m=0.22, max_m=2.2):
        self.bins = int(bins)
        self.min_m = float(min_m)
        self.max_m = float(max_m)
        self._fps = 0.0
        self._n = 0
        self._t0 = time.time()

    def estimate(self, bgr):
        t0 = time.time()
        h, w = bgr.shape[:2]
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        # Lower 55% is the driving corridor; ignore sky/ceiling.
        y0 = int(h * 0.45)
        roi = gray[y0:, :]
        rh, rw = roi.shape
        blur = cv2.GaussianBlur(roi, (5, 5), 0)
        edges = cv2.Canny(blur, 40, 120)
        # Dark + edgy regions in the near field (bottom of ROI) are obstacles.
        near = blur[int(rh * 0.45):, :]
        near_edges = edges[int(rh * 0.45):, :]
        occupancy = []
        col_w = rw // self.bins
        for i in range(self.bins):
            x0 = i * col_w
            x1 = rw if i == self.bins - 1 else (i + 1) * col_w
            strip = near[:, x0:x1]
            estirp = near_edges[:, x0:x1]
            if strip.size == 0:
                occupancy.append(0.0)
                continue
            darkness = 1.0 - (float(np.mean(strip)) / 255.0)
            edge_d = float(np.mean(estirp)) / 255.0
            # Large saturated color blobs (synthetic orange / real objects)
            sat = bgr[y0 + int(rh * 0.45):, x0:x1, :]
            chroma = np.std(sat.reshape(-1, 3).astype(np.float32), axis=1)
            chroma_score = min(1.0, float(np.mean(chroma)) / 45.0)
            occ = 0.45 * darkness + 0.35 * min(1.0, edge_d * 6.0) + 0.20 * chroma_score
            occupancy.append(max(0.0, min(1.0, occ)))
        bins = occupancy
        center = bins[self.bins // 2] if bins else 0.0
        # Map occupancy to meters (high occupancy = close).
        front_m = self.max_m - center * (self.max_m - self.min_m)
        blocked = center >= 0.55
        # Direction of nearest mass
        idx = int(np.argmax(np.array(bins))) if bins else self.bins // 2
        names = ["far_left", "left", "front", "right", "far_right"]
        if self.bins == 5:
            direction = names[idx]
        elif idx < self.bins / 3.0:
            direction = "left"
        elif idx > 2 * self.bins / 3.0:
            direction = "right"
        else:
            direction = "front"
        confidence = 0.45 + 0.4 * abs(center - 0.3)
        confidence = max(0.2, min(0.9, confidence))
        vis = self._vis(bgr, bins, front_m, blocked)
        now = time.time()
        self._n += 1
        if now - self._t0 >= 1.0:
            self._fps = self._n / (now - self._t0)
            self._n = 0
            self._t0 = now
        return {
            "front_m": float(front_m),
            "bins": [float(x) for x in bins],
            "blocked": bool(blocked),
            "direction": direction,
            "confidence": float(confidence),
            "source": "geometric",
            "vis": vis,
            "latency_ms": (time.time() - t0) * 1000.0,
            "fps": self._fps,
        }

    def _vis(self, bgr, bins, front_m, blocked):
        vis = bgr.copy()
        h, w = vis.shape[:2]
        col_w = w // max(1, len(bins))
        for i, occ in enumerate(bins):
            color = (0, int(255 * (1.0 - occ)), int(255 * occ))
            cv2.rectangle(vis, (i * col_w, h - 12), ((i + 1) * col_w - 1, h - 1), color, -1)
        label = "front %.2fm %s" % (front_m, "BLOCK" if blocked else "ok")
        cv2.putText(vis, label, (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    (0, 0, 255) if blocked else (180, 255, 180), 1, cv2.LINE_AA)
        return vis

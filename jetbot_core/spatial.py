"""Longer-lived spatial knowledge. Not the realtime world state."""

from __future__ import print_function

import threading
import time

from .world import KNOWN, PROBABLE, UNKNOWN, STALE


class SpatialMemory(object):
    def __init__(self):
        self._lock = threading.Lock()
        self.areas = {}
        self.landmarks = {}
        self.explored = []

    def remember_landmark(self, name, kind, pose, confidence, source="perception"):
        with self._lock:
            prev = self.landmarks.get(name)
            now = time.time()
            entry = {
                "name": name,
                "kind": kind,
                "pose": pose,
                "confidence": float(confidence),
                "source": source,
                "last_verified": now,
                "certainty": KNOWN if confidence >= 0.8 else PROBABLE,
                "seen_count": 1 if prev is None else prev.get("seen_count", 0) + 1,
            }
            self.landmarks[name] = entry
            return prev is None

    def remember_area(self, name, status=UNKNOWN, note=""):
        with self._lock:
            self.areas[name] = {
                "name": name,
                "status": status,
                "note": note,
                "updated": time.time(),
            }

    def mark_explored(self, pose):
        with self._lock:
            self.explored.append({"x": pose.get("x", 0), "y": pose.get("y", 0), "t": time.time()})
            if len(self.explored) > 400:
                self.explored = self.explored[-400:]

    def get_landmark(self, name):
        with self._lock:
            return dict(self.landmarks[name]) if name in self.landmarks else None

    def snapshot(self):
        with self._lock:
            return {
                "areas": dict(self.areas),
                "landmarks": dict(self.landmarks),
                "explored_n": len(self.explored),
            }

    def summary(self):
        snap = self.snapshot()
        bits = []
        for lm in list(snap["landmarks"].values())[:8]:
            if isinstance(lm, dict):
                bits.append("%s:%s" % (lm.get("name"), lm.get("certainty")))
        if not bits:
            return "spatial: no landmarks yet"
        return "spatial: " + ", ".join(bits)

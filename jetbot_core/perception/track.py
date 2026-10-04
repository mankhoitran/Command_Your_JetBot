"""Simple IoU tracker. IDs are stable enough for World State, not SLAM."""

from __future__ import print_function


def _iou(a, b):
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)
    iw = max(0.0, ix1 - ix0)
    ih = max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    denom = area_a + area_b - inter
    if denom <= 0:
        return 0.0
    return inter / denom


class Track(object):
    def __init__(self, track_id, det):
        self.id = track_id
        self.cls = det.cls
        self.bbox = list(det.bbox)
        self.confidence = det.confidence
        self.hits = 1
        self.misses = 0

    def update(self, det):
        self.cls = det.cls
        self.bbox = list(det.bbox)
        self.confidence = det.confidence
        self.hits += 1
        self.misses = 0


class IoUTracker(object):
    def __init__(self, iou_thresh=0.3, max_misses=8):
        self.iou_thresh = float(iou_thresh)
        self.max_misses = int(max_misses)
        self._next = 1
        self.tracks = []

    def update(self, detections):
        assigned = set()
        for tr in self.tracks:
            best_i = -1
            best = 0.0
            for i, det in enumerate(detections):
                if i in assigned:
                    continue
                if det.cls != tr.cls and tr.hits >= 3:
                    continue
                score = _iou(tr.bbox, det.bbox)
                if score > best:
                    best = score
                    best_i = i
            if best_i >= 0 and best >= self.iou_thresh:
                tr.update(detections[best_i])
                assigned.add(best_i)
            else:
                tr.misses += 1
        for i, det in enumerate(detections):
            if i in assigned:
                continue
            tid = "obj_%02d" % self._next
            self._next += 1
            self.tracks.append(Track(tid, det))
        self.tracks = [t for t in self.tracks if t.misses <= self.max_misses]
        return [t for t in self.tracks if t.misses == 0]

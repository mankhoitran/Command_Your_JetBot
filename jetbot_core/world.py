"""Operational world state. Fast, no images, no LLM, no A-MEM."""

from __future__ import print_function

import copy
import threading
import time


KNOWN = "KNOWN"
PROBABLE = "PROBABLE"
UNKNOWN = "UNKNOWN"
STALE = "STALE"
CONTRADICTED = "CONTRADICTED"


def _now():
    return time.time()


class TrackedObject(object):
    def __init__(self, object_id, cls, bbox, confidence, track_id=None):
        self.id = object_id
        self.cls = cls
        self.bbox = list(bbox)  # x0,y0,x1,y1 normalized 0-1
        self.confidence = float(confidence)
        self.track_id = track_id if track_id is not None else object_id
        self.position = None  # (x, y, z) robot-relative meters if known
        self.last_seen = _now()
        self.certainty = PROBABLE if confidence < 0.8 else KNOWN
        self.hits = 1

    def to_dict(self):
        return {
            "id": self.id,
            "class": self.cls,
            "bbox": self.bbox,
            "confidence": round(self.confidence, 3),
            "track_id": self.track_id,
            "position": self.position,
            "last_seen": self.last_seen,
            "certainty": self.certainty,
            "hits": self.hits,
        }


class WorldState(object):
    """Thread-safe snapshot of the current operational world."""

    def __init__(self, stale_s=0.9, stop_m=0.18):
        self._lock = threading.RLock()
        self.stale_s = float(stale_s)
        self.stop_m = float(stop_m)
        self.robot = {
            "pose": {"x": 0.0, "y": 0.0, "yaw": 0.0},
            "velocity": {"linear": 0.0, "angular": 0.0},
            "left_speed": 0.0,
            "right_speed": 0.0,
            "battery": None,
            "camera_pan": 0.0,
            "camera_tilt": 0.0,
            "estop": False,
            "hardware": {
                "motors": "unknown",
                "servo": "unknown",
                "camera": "unknown",
            },
        }
        self.objects = {}
        self.obstacles = {
            "front_m": None,
            "bins": [],
            "blocked": False,
            "direction": "none",
            "confidence": 0.0,
            "source": None,
            "last_update": 0.0,
        }
        self.navigation = {
            "mode": "idle",
            "current_target": None,
            "current_path": [],
            "blocked": False,
            "confidence": 0.0,
            "last_command": None,
        }
        self.task = {
            "id": None,
            "name": None,
            "state": "IDLE",
            "action": None,
            "progress": 0.0,
            "failure_reason": None,
        }
        self.perception = {
            "frame_seq": 0,
            "fps": 0.0,
            "depth_fps": 0.0,
            "detect_fps": 0.0,
            "last_frame_ts": 0.0,
            "last_depth_ts": 0.0,
            "last_detect_ts": 0.0,
            "latency_ms": 0.0,
            "backend": "none",
        }
        self.health = {}
        self.uncertainty = []
        self.updated_at = _now()

    def snapshot(self):
        with self._lock:
            data = {
                "robot": copy.deepcopy(self.robot),
                "objects": dict((k, v.to_dict()) for k, v in self.objects.items()),
                "obstacles": copy.deepcopy(self.obstacles),
                "navigation": copy.deepcopy(self.navigation),
                "task": copy.deepcopy(self.task),
                "perception": copy.deepcopy(self.perception),
                "health": copy.deepcopy(self.health),
                "uncertainty": list(self.uncertainty),
                "updated_at": self.updated_at,
            }
        data["freshness"] = self.freshness_dict()
        return data

    def summary_for_llm(self, max_objects=8):
        """Compact text the LLM can reason over. No images. Fits n_ctx=2048."""
        snap = self.snapshot()
        robot = snap["robot"]
        obs = snap["obstacles"]
        nav = snap["navigation"]
        task = snap["task"]
        perc = snap["perception"]
        fresh = snap["freshness"]
        lines = [
            "task: %s state=%s action=%s" % (task.get("name") or "none", task.get("state"), task.get("action")),
            "robot: pose=(%.2f,%.2f,yaw=%.2f) v=%.2f estop=%s motors=%s camera=%s pan=%.0f tilt=%.0f"
            % (
                robot["pose"]["x"],
                robot["pose"]["y"],
                robot["pose"]["yaw"],
                robot["velocity"]["linear"],
                robot["estop"],
                robot["hardware"]["motors"],
                robot["hardware"]["camera"],
                robot["camera_pan"],
                robot["camera_tilt"],
            ),
            "obstacle: front=%s blocked=%s dir=%s conf=%.2f certainty=%s"
            % (
                "%.2fm" % obs["front_m"] if obs["front_m"] is not None else "unknown",
                obs["blocked"],
                obs["direction"],
                obs["confidence"],
                fresh["obstacles"],
            ),
            "nav: mode=%s target=%s blocked=%s" % (nav["mode"], nav["current_target"], nav["blocked"]),
            "perception: fps=%.1f depth_fps=%.1f detect_fps=%.1f stale=%s"
            % (perc["fps"], perc["depth_fps"], perc["detect_fps"], fresh["perception"] == STALE),
        ]
        objs = sorted(snap["objects"].values(), key=lambda o: -o["confidence"])[:max_objects]
        if objs:
            bits = []
            for obj in objs:
                bits.append("%s(%s,c=%.2f,%s)" % (obj["id"], obj["class"], obj["confidence"], obj["certainty"]))
            lines.append("objects: " + ", ".join(bits))
        else:
            lines.append("objects: none")
        if snap["uncertainty"]:
            lines.append("uncertainty: " + "; ".join(snap["uncertainty"][:6]))
        return "\n".join(lines)

    def freshness_dict(self):
        now = _now()
        perc_age = now - self.perception["last_frame_ts"] if self.perception["last_frame_ts"] else 1e9
        depth_age = now - self.perception["last_depth_ts"] if self.perception["last_depth_ts"] else 1e9
        return {
            "perception": STALE if perc_age > self.stale_s else KNOWN,
            "obstacles": STALE if depth_age > self.stale_s else (KNOWN if self.obstacles["front_m"] is not None else UNKNOWN),
            "pose": PROBABLE,
        }

    def is_perception_fresh(self):
        ts = self.perception["last_depth_ts"] or self.perception["last_frame_ts"]
        if not ts:
            return False
        return (_now() - ts) <= self.stale_s

    def update_robot(self, **kwargs):
        with self._lock:
            for key, value in kwargs.items():
                if key == "pose" and isinstance(value, dict):
                    self.robot["pose"].update(value)
                elif key == "velocity" and isinstance(value, dict):
                    self.robot["velocity"].update(value)
                elif key == "hardware" and isinstance(value, dict):
                    self.robot["hardware"].update(value)
                else:
                    self.robot[key] = value
            self.updated_at = _now()

    def update_obstacles(self, front_m, bins, blocked, direction, confidence, source):
        with self._lock:
            if front_m is not None:
                blocked = bool(blocked) or (float(front_m) <= self.stop_m)
            else:
                blocked = bool(blocked)
            self.obstacles = {
                "front_m": front_m,
                "bins": list(bins) if bins is not None else [],
                "blocked": blocked,
                "direction": direction,
                "confidence": float(confidence),
                "source": source,
                "last_update": _now(),
            }
            self.perception["last_depth_ts"] = _now()
            self.updated_at = _now()

    def obstacles_view(self):
        """Cheap consistent read for the 20 Hz safety / 10 Hz planner loops."""
        with self._lock:
            obs = dict(self.obstacles)
            obs["bins"] = list(self.obstacles.get("bins") or [])
            last_cmd = self.navigation.get("last_command")
            nav = {
                "mode": self.navigation.get("mode"),
                "current_target": self.navigation.get("current_target"),
                "blocked": self.navigation.get("blocked"),
                "last_command": dict(last_cmd) if isinstance(last_cmd, dict) else last_cmd,
            }
            robot = {
                "estop": self.robot.get("estop"),
                "camera_pan": self.robot.get("camera_pan", 0.0),
                "camera_tilt": self.robot.get("camera_tilt", 0.0),
            }
            last_depth = self.perception.get("last_depth_ts") or 0.0
        now = _now()
        depth_age = now - last_depth if last_depth else 1e9
        freshness = STALE if depth_age > self.stale_s else (
            KNOWN if obs.get("front_m") is not None else UNKNOWN
        )
        return {
            "obstacles": obs,
            "navigation": nav,
            "robot": robot,
            "freshness": {"obstacles": freshness},
        }

    def objects_view(self):
        with self._lock:
            return dict((k, v.to_dict()) for k, v in self.objects.items())

    def upsert_object(self, obj):
        with self._lock:
            existing = self.objects.get(obj.id)
            if existing is not None:
                existing.bbox = obj.bbox
                existing.confidence = obj.confidence
                existing.cls = obj.cls
                existing.last_seen = _now()
                existing.hits += 1
                existing.position = obj.position
                if existing.hits >= 3 and existing.confidence >= 0.6:
                    existing.certainty = KNOWN
                elif existing.confidence < 0.4:
                    existing.certainty = PROBABLE
            else:
                obj.last_seen = _now()
                self.objects[obj.id] = obj
            self.perception["last_detect_ts"] = _now()
            self.updated_at = _now()
            return existing is None

    def prune_objects(self, max_age=2.5):
        lost = []
        now = _now()
        with self._lock:
            for key in list(self.objects.keys()):
                obj = self.objects[key]
                age = now - obj.last_seen
                if age > max_age:
                    obj.certainty = STALE
                if age > max_age * 3:
                    lost.append(self.objects.pop(key))
            self.updated_at = _now()
        return lost

    def set_task(self, **kwargs):
        with self._lock:
            self.task.update(kwargs)
            self.updated_at = _now()

    def set_navigation(self, **kwargs):
        with self._lock:
            self.navigation.update(kwargs)
            self.updated_at = _now()

    def set_perception_meta(self, **kwargs):
        with self._lock:
            self.perception.update(kwargs)
            self.updated_at = _now()

    def set_health(self, name, status):
        with self._lock:
            self.health[name] = status
            self.updated_at = _now()

    def health_dict(self):
        with self._lock:
            return copy.deepcopy(self.health)

    def set_uncertainty(self, items):
        with self._lock:
            self.uncertainty = list(items)
            self.updated_at = _now()

    def mark_estop(self, active):
        with self._lock:
            self.robot["estop"] = bool(active)
            if active:
                self.robot["left_speed"] = 0.0
                self.robot["right_speed"] = 0.0
                self.robot["velocity"] = {"linear": 0.0, "angular": 0.0}
            self.updated_at = _now()

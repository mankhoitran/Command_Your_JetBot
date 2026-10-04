"""Local planner. Converts semantic goals into short MotionCommands."""

from __future__ import print_function

import logging
import math
import threading
import time

from .events import EventType
from .safety import MotionCommand

log = logging.getLogger("jetbot.nav")


class LocalPlanner(object):
    def __init__(self, cfg, world, safety, bus):
        nav = cfg.get("navigation", {})
        self.cruise = float(nav.get("cruise_speed", 0.18))
        self.turn_speed = float(nav.get("turn_in_place_speed", 0.22))
        self.blocked_center = float(nav.get("blocked_bins_center", 0.35))
        self.world = world
        self.safety = safety
        self.bus = bus
        self._lock = threading.Lock()
        self.mode = "idle"  # idle|forward|left|right|backup|scan|stop
        self.target = None
        self._blocked_emitted = False
        self._stop = threading.Event()
        self._thread = None
        self.hz = float(cfg.get("loops", {}).get("navigation_hz", 10))
        self._until = 0.0
        self._manual_until = 0.0

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="navigation")
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def set_mode(self, mode, duration_s=0.6, target=None):
        with self._lock:
            self.mode = mode
            self.target = target
            self._until = time.time() + float(duration_s)
            if mode in ("forward", "left", "right", "backup", "stop"):
                self._manual_until = self._until
        self.world.set_navigation(mode=mode, current_target=target)

    def halt(self):
        self.set_mode("stop", duration_s=0.2)

    def _loop(self):
        period = 1.0 / max(1.0, self.hz)
        while not self._stop.is_set():
            t0 = time.time()
            cmd = self._compute()
            if cmd is not None:
                self.safety.request(cmd)
            slept = time.time() - t0
            if slept < period:
                time.sleep(period - slept)

    def _compute(self):
        snap = self.world.snapshot()
        if snap["robot"]["estop"]:
            return MotionCommand(0, 0, "nav-estop", 0.05)
        with self._lock:
            mode = self.mode
            until = self._until
        now = time.time()
        if mode != "idle" and now > until and mode not in ("explore", "follow"):
            with self._lock:
                self.mode = "idle"
            mode = "idle"
            self.world.set_navigation(mode="idle")
        obs = snap["obstacles"]
        bins = obs.get("bins") or []
        front = obs.get("front_m")
        blocked = bool(obs.get("blocked"))
        if blocked or (front is not None and front < 0.42):
            if not self._blocked_emitted:
                self.bus.emit(EventType.NAVIGATION_BLOCKED, "nav", {
                    "front_m": front, "direction": obs.get("direction"),
                }, confidence=obs.get("confidence", 0.5))
                self._blocked_emitted = True
            self.world.set_navigation(blocked=True)
            # Recovery: turn toward the more open bin.
            turn_left = True
            if len(bins) >= 2:
                turn_left = bins[0] < bins[-1]
            speed = self.turn_speed
            if turn_left:
                return MotionCommand(-speed, speed, "recovery", 0.25)
            return MotionCommand(speed, -speed, "recovery", 0.25)
        if self._blocked_emitted:
            self.bus.emit(EventType.NAVIGATION_RECOVERED, "nav", {"front_m": front})
            self._blocked_emitted = False
            self.world.set_navigation(blocked=False)

        if mode == "idle" or mode == "stop":
            return MotionCommand(0, 0, "nav-idle", 0.1)
        if mode == "forward" or mode == "explore":
            left, right = self._corridor_speeds(bins, self.cruise)
            return MotionCommand(left, right, "nav-" + mode, 0.25)
        if mode == "left":
            return MotionCommand(-self.turn_speed, self.turn_speed, "nav-left", 0.2)
        if mode == "right":
            return MotionCommand(self.turn_speed, -self.turn_speed, "nav-right", 0.2)
        if mode == "backup":
            return MotionCommand(-self.cruise, -self.cruise, "nav-backup", 0.2)
        if mode == "follow":
            left, right = self._follow(snap)
            return MotionCommand(left, right, "nav-follow", 0.2)
        return MotionCommand(0, 0, "nav-unknown", 0.1)

    def _corridor_speeds(self, bins, cruise):
        if not bins:
            return cruise, cruise
        n = len(bins)
        mid = n // 2
        left_open = 1.0 - (sum(bins[:mid]) / max(1, mid))
        right_open = 1.0 - (sum(bins[mid + 1:]) / max(1, n - mid - 1))
        bias = (right_open - left_open) * 0.35
        left = _clip(cruise * (1.0 - max(0.0, -bias * 2)), 0.0, cruise)
        right = _clip(cruise * (1.0 - max(0.0, bias * 2)), 0.0, cruise)
        center_occ = bins[mid]
        slow = 1.0 - 0.7 * center_occ
        return left * slow, right * slow

    def _follow(self, snap):
        target = snap["navigation"].get("current_target")
        objs = snap.get("objects") or {}
        chosen = None
        if target and target in objs:
            chosen = objs[target]
        else:
            people = [o for o in objs.values() if o.get("class") == "person"]
            if people:
                chosen = sorted(people, key=lambda o: -o["confidence"])[0]
        if chosen is None:
            return 0.0, 0.0
        x0, y0, x1, y1 = chosen["bbox"]
        cx = (x0 + x1) * 0.5
        err = cx - 0.5
        turn = _clip(err * 0.6, -self.turn_speed, self.turn_speed)
        speed = self.cruise * 0.8
        return _clip(speed - turn, -1, 1), _clip(speed + turn, -1, 1)


def _clip(v, lo, hi):
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v

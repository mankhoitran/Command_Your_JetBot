"""Local planner. Converts semantic goals into short MotionCommands."""

from __future__ import print_function

import logging
import threading
import time

from .events import EventType
from .safety import MotionCommand

log = logging.getLogger("jetbot.nav")


class LocalPlanner(object):
    def __init__(self, cfg, world, safety, bus):
        nav = cfg.get("navigation", {})
        safety_cfg = cfg.get("safety", {})
        self.cruise = float(nav.get("cruise_speed", 0.18))
        self.turn_speed = float(nav.get("turn_in_place_speed", 0.22))
        self.blocked_front_m = float(nav.get("blocked_front_m", safety_cfg.get("min_obstacle_m", 0.18)))
        self.recovery_s = float(nav.get("recovery_s", 2.0))
        self.world = world
        self.safety = safety
        self.bus = bus
        self._lock = threading.Lock()
        self.mode = "idle"  # idle|forward|left|right|backup|scan|stop|explore|follow|recovery
        self.target = None
        self._blocked_emitted = False
        self._recovery_failed_emitted = False
        self._blocked_since = None
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
            if mode in ("forward", "left", "right", "backup", "stop", "scan"):
                self._manual_until = self._until
            if mode != "recovery":
                self._recovery_failed_emitted = False
        self.world.set_navigation(mode=mode, current_target=target)

    def halt(self):
        self.set_mode("stop", duration_s=0.2)

    def in_recovery(self):
        with self._lock:
            return self.mode == "recovery"

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
        view = self.world.obstacles_view()
        if view["robot"]["estop"]:
            return MotionCommand(0, 0, "nav-estop", 0.05)
        with self._lock:
            mode = self.mode
            until = self._until
            target = self.target
        now = time.time()
        if mode != "idle" and mode != "recovery" and now > until:
            with self._lock:
                self.mode = "idle"
                self.target = None
            mode = "idle"
            target = None
            self.world.set_navigation(mode="idle", current_target=None)
        obs = view["obstacles"]
        bins = obs.get("bins") or []
        front = obs.get("front_m")
        close = bool(obs.get("blocked")) or (front is not None and front < self.blocked_front_m)
        # Explicit pad / tool turns and stops must not be stolen by recovery.
        honor_command = mode in ("left", "right", "backup", "stop", "idle", "scan")
        if close and not honor_command:
            if self._blocked_since is None:
                self._blocked_since = now
            if not self._blocked_emitted:
                self.bus.emit(EventType.NAVIGATION_BLOCKED, "nav", {
                    "front_m": front, "direction": obs.get("direction"),
                }, confidence=obs.get("confidence", 0.5))
                self._blocked_emitted = True
            self.world.set_navigation(blocked=True, mode="recovery")
            with self._lock:
                if self.mode not in ("idle", "stop", "scan", "left", "right", "backup"):
                    self.mode = "recovery"
            if (now - self._blocked_since) >= self.recovery_s and not self._recovery_failed_emitted:
                self.bus.emit(EventType.NAVIGATION_RECOVERY_FAILED, "nav", {
                    "front_m": front, "recovery_s": self.recovery_s,
                })
                self._recovery_failed_emitted = True
            turn_left = True
            if len(bins) >= 2:
                turn_left = bins[0] < bins[-1]
            speed = self.turn_speed
            if turn_left:
                return MotionCommand(-speed, speed, "recovery", 0.25)
            return MotionCommand(speed, -speed, "recovery", 0.25)
        if self._blocked_emitted and not close:
            self.bus.emit(EventType.NAVIGATION_RECOVERED, "nav", {"front_m": front})
            self._blocked_emitted = False
            self._recovery_failed_emitted = False
            self._blocked_since = None
            self.world.set_navigation(blocked=False)
            with self._lock:
                if self.mode == "recovery":
                    self.mode = "idle"
                    mode = "idle"
            if mode == "recovery":
                mode = "idle"
                self.world.set_navigation(mode="idle")
        elif close:
            self.world.set_navigation(blocked=True)
        else:
            self._blocked_since = None

        if mode == "idle" or mode == "stop" or mode == "scan":
            return MotionCommand(0, 0, "nav-" + mode, 0.1)
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
            left, right = self._follow(target)
            return MotionCommand(left, right, "nav-follow", 0.2)
        if mode == "recovery":
            return MotionCommand(0, 0, "nav-recovery-hold", 0.1)
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

    def _follow(self, target):
        objs = self.world.objects_view()
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

"""Hard safety boundary. Fast loop. No LLM, no network, no memory, no UI."""

from __future__ import print_function

import logging
import threading
import time

from .events import EventType

log = logging.getLogger("jetbot.safety")


def _clamp(value, lo, hi):
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


class MotionCommand(object):
    def __init__(self, left=0.0, right=0.0, source="none", duration_s=0.4):
        self.left = float(left)
        self.right = float(right)
        self.source = source
        self.duration_s = float(duration_s)
        self.ts = time.time()

    def to_dict(self):
        return {
            "left": self.left,
            "right": self.right,
            "source": self.source,
            "duration_s": self.duration_s,
            "ts": self.ts,
        }


class SafetyController(object):
    def __init__(self, cfg, motors, world, bus):
        safety = cfg.get("safety", {})
        motors_cfg = cfg.get("motors", {})
        self.min_obstacle_m = float(safety.get("min_obstacle_m", 0.38))
        self.slow_obstacle_m = float(safety.get("slow_obstacle_m", 0.70))
        self.max_speed = float(safety.get("max_speed", motors_cfg.get("max_speed", 0.25)))
        self.max_turn = float(safety.get("max_turn", 0.35))
        self.stale_s = float(safety.get("stale_perception_s", 0.90))
        self.command_timeout_s = float(safety.get("command_timeout_s", 1.20))
        self.require_fresh = bool(safety.get("require_fresh_perception_to_move", True))
        self.min_command = float(motors_cfg.get("min_command", 0.05))
        self.motors = motors
        self.world = world
        self.bus = bus
        self._lock = threading.Lock()
        self._estop = False
        self._command = MotionCommand(0, 0, "init", 0)
        self._last_applied = 0.0
        self._last_reject = ""
        self._stop = threading.Event()
        self._thread = None
        self.hz = float(cfg.get("loops", {}).get("safety_hz", 20))

    @property
    def estop(self):
        with self._lock:
            return self._estop

    def emergency_stop(self, source="user"):
        with self._lock:
            self._estop = True
            self._command = MotionCommand(0, 0, "estop", 0)
        try:
            self.motors.stop()
        except Exception as exc:
            log.error("motor stop during estop failed: %s", exc)
        self.world.mark_estop(True)
        self.world.update_robot(left_speed=0.0, right_speed=0.0, velocity={"linear": 0.0, "angular": 0.0})
        self.bus.emit(EventType.EMERGENCY_STOP, source, {"reason": "emergency_stop"})
        log.warning("EMERGENCY STOP from %s", source)

    def clear_estop(self, source="user"):
        with self._lock:
            self._estop = False
        self.world.mark_estop(False)
        try:
            self.motors.stop()
        except Exception:
            pass
        log.info("E-STOP cleared by %s", source)

    def request(self, command):
        """Validate and latch a motion command. Returns (ok, reason)."""
        if not isinstance(command, MotionCommand):
            raise TypeError("expected MotionCommand")
        with self._lock:
            if self._estop:
                self._last_reject = "estop"
                self.bus.emit(EventType.COMMAND_REJECTED, "safety", {
                    "reason": "estop", "source": command.source,
                })
                return False, "estop"
        ok, reason, safe = self.validate(command)
        if not ok:
            with self._lock:
                self._last_reject = reason
            self.bus.emit(EventType.COMMAND_REJECTED, "safety", {
                "reason": reason, "source": command.source,
            })
            return False, reason
        with self._lock:
            self._command = safe
        return True, "ok"

    def validate(self, command):
        left = _clamp(command.left, -1.0, 1.0)
        right = _clamp(command.right, -1.0, 1.0)
        left = _clamp(left, -self.max_speed, self.max_speed)
        right = _clamp(right, -self.max_speed, self.max_speed)
        # Turn magnitude
        turn = abs(left - right) * 0.5
        if turn > self.max_turn:
            scale = self.max_turn / max(turn, 1e-6)
            mean = (left + right) * 0.5
            diff = (left - right) * 0.5 * scale
            left = mean + diff
            right = mean - diff
        moving_fwd = (left + right) > 0.04
        snap = self.world.snapshot()
        obs = snap["obstacles"]
        fresh = snap["freshness"]
        if self.require_fresh and moving_fwd and fresh["obstacles"] == "STALE":
            self.bus.emit(EventType.PERCEPTION_STALE, "safety", {"while": "forward"})
            return False, "stale_perception", MotionCommand(0, 0, command.source, 0)
        front = obs.get("front_m")
        if moving_fwd and front is not None and front <= self.min_obstacle_m:
            return False, "obstacle", MotionCommand(0, 0, command.source, 0)
        if moving_fwd and front is not None and front <= self.slow_obstacle_m:
            scale = max(0.15, (front - self.min_obstacle_m) / max(1e-3, self.slow_obstacle_m - self.min_obstacle_m))
            if left > 0:
                left *= scale
            if right > 0:
                right *= scale
        if abs(left) < self.min_command:
            left = 0.0
        if abs(right) < self.min_command:
            right = 0.0
        safe = MotionCommand(left, right, command.source, command.duration_s)
        safe.ts = command.ts
        return True, "ok", safe

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="safety")
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            self.motors.stop()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self):
        period = 1.0 / max(1.0, self.hz)
        while not self._stop.is_set():
            t0 = time.time()
            with self._lock:
                estop = self._estop
                cmd = self._command
            if estop:
                try:
                    self.motors.stop()
                except Exception:
                    pass
                self.world.update_robot(left_speed=0.0, right_speed=0.0,
                                        velocity={"linear": 0.0, "angular": 0.0})
            else:
                expired = (time.time() - cmd.ts) > max(cmd.duration_s, self.command_timeout_s)
                if expired:
                    left = right = 0.0
                    source = "watchdog"
                else:
                    ok, reason, safe = self.validate(cmd)
                    if ok:
                        left, right = safe.left, safe.right
                        source = cmd.source
                    else:
                        left = right = 0.0
                        source = "safety:" + reason
                try:
                    self.motors.set_speeds(left, right)
                except Exception as exc:
                    log.error("motor write failed: %s", exc)
                    left = right = 0.0
                self.world.update_robot(
                    left_speed=left,
                    right_speed=right,
                    velocity={"linear": (left + right) * 0.5, "angular": (right - left) * 0.5},
                )
                self.world.set_navigation(last_command={"left": left, "right": right, "source": source})
            slept = time.time() - t0
            if slept < period:
                time.sleep(period - slept)

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
        self.min_obstacle_m = float(safety.get("min_obstacle_m", 0.18))
        self.slow_obstacle_m = float(safety.get("slow_obstacle_m", 0.36))
        self.turn_obstacle_m = float(safety.get("turn_obstacle_m", 0.06))
        self.max_speed = float(safety.get("max_speed", motors_cfg.get("max_speed", 0.25)))
        self.max_turn = float(safety.get("max_turn", 0.35))
        self.stale_s = float(safety.get("stale_perception_s", 0.90))
        self.command_timeout_s = float(safety.get("command_timeout_s", 1.20))
        self.require_fresh = bool(safety.get("require_fresh_perception_to_move", True))
        self.min_command = float(motors_cfg.get("min_command", 0.05))
        self._allow_motion = bool(cfg.get("robot", {}).get("allow_motion", False))
        self.motors = motors
        self.world = world
        self.bus = bus
        try:
            self.world.update_robot(allow_motion=self._allow_motion)
        except Exception:
            pass
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

    @property
    def allow_motion(self):
        with self._lock:
            return self._allow_motion

    @allow_motion.setter
    def allow_motion(self, value):
        with self._lock:
            self._allow_motion = bool(value)
        try:
            self.world.update_robot(allow_motion=bool(value))
        except Exception:
            pass

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

    def set_allow_motion(self, allowed, source="user"):
        allowed = bool(allowed)
        with self._lock:
            self._allow_motion = allowed
            if not allowed:
                self._command = MotionCommand(0, 0, "motion-lock", 0)
        if not allowed:
            try:
                self.motors.stop()
            except Exception:
                pass
            self.world.update_robot(left_speed=0.0, right_speed=0.0,
                                    velocity={"linear": 0.0, "angular": 0.0})
        try:
            self.world.update_robot(allow_motion=allowed)
        except Exception:
            pass
        log.info("allow_motion=%s from %s", allowed, source)
        return allowed

    def request(self, command):
        """Validate and latch a motion command. Returns (ok, reason)."""
        if not isinstance(command, MotionCommand):
            raise TypeError("expected MotionCommand")
        with self._lock:
            if self._estop:
                self._last_reject = "estop"
                locked = False
                estopped = True
            else:
                estopped = False
                locked = not self._allow_motion
        if estopped:
            self.bus.emit(EventType.COMMAND_REJECTED, "safety", {
                "reason": "estop", "source": command.source,
            })
            return False, "estop"
        if locked and (abs(command.left) > 1e-6 or abs(command.right) > 1e-6):
            held = MotionCommand(0.0, 0.0, command.source, command.duration_s)
            held.ts = command.ts
            with self._lock:
                self._command = held
                self._last_reject = "motion_locked"
            self.bus.emit(EventType.COMMAND_REJECTED, "safety", {
                "reason": "motion_locked",
                "source": command.source,
                "requested": {"left": command.left, "right": command.right},
            })
            log.info("motion locked (charging); accepted %s without spinning", command.source)
            return False, "motion_locked"
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
        net = left + right
        moving_fwd = net > 0.04
        turning = abs(left - right) > 0.08 and abs(net) <= 0.08
        view = self.world.obstacles_view()
        obs = view["obstacles"]
        fresh = view["freshness"]
        if self.require_fresh and moving_fwd and fresh["obstacles"] == "STALE":
            self.bus.emit(EventType.PERCEPTION_STALE, "safety", {"while": "forward"})
            return False, "stale_perception", MotionCommand(0, 0, command.source, 0)
        front = obs.get("front_m")
        blocked = bool(obs.get("blocked"))
        if moving_fwd and (blocked or (front is not None and front <= self.min_obstacle_m)):
            return False, "obstacle", MotionCommand(0, 0, command.source, 0)
        # In-place left/right are low-risk; only stop a spin if nearly touching.
        if turning and front is not None and front <= self.turn_obstacle_m:
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
                allow = self._allow_motion
                cmd = self._command
            if estop or not allow:
                try:
                    self.motors.stop()
                except Exception:
                    pass
                self.world.update_robot(left_speed=0.0, right_speed=0.0,
                                        velocity={"linear": 0.0, "angular": 0.0})
            else:
                moving = abs(cmd.left) > 1e-6 or abs(cmd.right) > 1e-6
                if moving:
                    hold = min(max(cmd.duration_s, 0.05), self.command_timeout_s)
                else:
                    hold = max(cmd.duration_s, 0.05)
                expired = (time.time() - cmd.ts) > hold
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
                with self._lock:
                    if self._estop or not self._allow_motion:
                        left = right = 0.0
                        source = "estop" if self._estop else "motion-lock"
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

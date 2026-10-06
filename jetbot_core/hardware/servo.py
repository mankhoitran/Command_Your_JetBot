"""Camera pan/tilt. Semantic poses hide PWM degrees from the Agent."""

from __future__ import print_function

import logging
import threading
import time

log = logging.getLogger("jetbot.servo")


LOOK_PRESETS = {
    "forward": (0.0, 0.0),
    "left": (45.0, 0.0),
    "right": (-45.0, 0.0),
    "up": (0.0, 18.0),
    "down": (0.0, -12.0),
    "left_up": (45.0, 15.0),
    "right_up": (-45.0, 15.0),
}


def _clamp(value, lo, hi):
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


class ServoController(object):
    enabled = False

    def look(self, pan_deg, tilt_deg):
        raise NotImplementedError

    def look_named(self, name):
        if name not in LOOK_PRESETS:
            raise ValueError("unknown look pose: %s" % name)
        pan, tilt = LOOK_PRESETS[name]
        self.look(pan, tilt)
        return pan, tilt

    def center(self):
        self.look(0.0, 0.0)

    def snapshot(self):
        return {"backend": "interface", "pan": 0.0, "tilt": 0.0, "ok": False, "enabled": False}

    def close(self):
        pass


class FixedServo(ServoController):
    """Bolted camera. look() is a no-op so tools cannot fake pan/tilt."""

    def __init__(self):
        self.enabled = False
        self.pan = 0.0
        self.tilt = 0.0

    def look(self, pan_deg, tilt_deg):
        return 0.0, 0.0

    def look_named(self, name):
        if name not in LOOK_PRESETS:
            raise ValueError("unknown look pose: %s" % name)
        return 0.0, 0.0

    def snapshot(self):
        return {
            "backend": "fixed",
            "pan": 0.0,
            "tilt": 0.0,
            "ok": True,
            "enabled": False,
        }


class SimulatedServo(ServoController):
    def __init__(self, pan_min=-70, pan_max=70, tilt_min=-25, tilt_max=25):
        self.pan_min = pan_min
        self.pan_max = pan_max
        self.tilt_min = tilt_min
        self.tilt_max = tilt_max
        self.pan = 0.0
        self.tilt = 0.0
        self.enabled = True
        self._lock = threading.Lock()

    def look(self, pan_deg, tilt_deg):
        with self._lock:
            self.pan = _clamp(float(pan_deg), self.pan_min, self.pan_max)
            self.tilt = _clamp(float(tilt_deg), self.tilt_min, self.tilt_max)

    def snapshot(self):
        with self._lock:
            return {
                "backend": "simulated",
                "pan": self.pan,
                "tilt": self.tilt,
                "ok": True,
                "enabled": True,
            }


class PCA9685Servo(ServoController):
    """Optional PCA9685 pan/tilt. Pulse mapping is conservative."""

    def __init__(self, i2c_bus=1, address=0x40, pan_ch=0, tilt_ch=1,
                 pan_min=-70, pan_max=70, tilt_min=-25, tilt_max=25):
        from Adafruit_MotorHAT import Adafruit_MotorHAT
        # MotorHAT already owns a PCA9685 at 0x60. A camera pan/tilt board
        # is typically a second PCA9685 at 0x40. We talk via smbus-like
        # registers if Adafruit_PCA9685 is missing.
        self.pan_min = pan_min
        self.pan_max = pan_max
        self.tilt_min = tilt_min
        self.tilt_max = tilt_max
        self.pan_ch = pan_ch
        self.tilt_ch = tilt_ch
        self.pan = 0.0
        self.tilt = 0.0
        self.enabled = True
        self._lock = threading.Lock()
        self._pwm = None
        try:
            from Adafruit_PCA9685 import PCA9685
            self._pwm = PCA9685(address=address, busnum=i2c_bus)
            self._pwm.set_pwm_freq(50)
        except Exception:
            # Fall back to MotorHAT PWM helper if address matches, else fail.
            hat = Adafruit_MotorHAT(addr=address, i2c_bus=i2c_bus)
            self._pwm = hat._pwm
        self.look(0.0, 0.0)

    def _deg_to_tick(self, deg, lo, hi):
        # Map [lo, hi] degrees onto ~1.0ms..2.0ms at 50Hz (4096 ticks / 20ms)
        frac = (float(deg) - lo) / float(hi - lo)
        frac = _clamp(frac, 0.0, 1.0)
        us = 1000.0 + frac * 1000.0
        ticks = int(us * 4096.0 / 20000.0)
        return max(0, min(4095, ticks))

    def _set(self, channel, ticks):
        if hasattr(self._pwm, "set_pwm"):
            self._pwm.set_pwm(channel, 0, ticks)
        else:
            self._pwm.setPWM(channel, 0, ticks)

    def look(self, pan_deg, tilt_deg):
        pan = _clamp(float(pan_deg), self.pan_min, self.pan_max)
        tilt = _clamp(float(tilt_deg), self.tilt_min, self.tilt_max)
        with self._lock:
            self._set(self.pan_ch, self._deg_to_tick(pan, self.pan_min, self.pan_max))
            self._set(self.tilt_ch, self._deg_to_tick(tilt, self.tilt_min, self.tilt_max))
            self.pan = pan
            self.tilt = tilt

    def snapshot(self):
        with self._lock:
            return {"backend": "pca9685", "pan": self.pan, "tilt": self.tilt, "ok": True, "enabled": True}


def try_create_servo(cfg):
    servo_cfg = cfg.get("servo", {})
    if not bool(servo_cfg.get("enabled", False)):
        log.info("Camera servo disabled (fixed chassis camera)")
        return FixedServo(), "fixed"
    simulate = cfg.get("robot", {}).get("simulate_if_missing", True)
    bus = int(servo_cfg.get("i2c_bus", 1))
    address = int(servo_cfg.get("address", 0x40))
    kwargs = dict(
        pan_min=float(servo_cfg.get("pan_min_deg", -70)),
        pan_max=float(servo_cfg.get("pan_max_deg", 70)),
        tilt_min=float(servo_cfg.get("tilt_min_deg", -25)),
        tilt_max=float(servo_cfg.get("tilt_max_deg", 25)),
    )
    present = False
    try:
        import os as _os
        import fcntl
        fd = _os.open("/dev/i2c-%d" % bus, _os.O_RDWR)
        try:
            fcntl.ioctl(fd, 0x0703, address)
            present = True
        finally:
            _os.close(fd)
    except Exception:
        present = False
    if present and not simulate:
        try:
            ctrl = PCA9685Servo(
                i2c_bus=bus,
                address=address,
                pan_ch=int(servo_cfg.get("pan_channel", 0)),
                tilt_ch=int(servo_cfg.get("tilt_channel", 1)),
                **kwargs
            )
            log.info("Using PCA9685 servo at 0x%x", address)
            return ctrl, "pca9685"
        except Exception as exc:
            log.warning("PCA9685 init failed: %s", exc)
            if not simulate:
                raise
    if present:
        try:
            ctrl = PCA9685Servo(
                i2c_bus=bus,
                address=address,
                pan_ch=int(servo_cfg.get("pan_channel", 0)),
                tilt_ch=int(servo_cfg.get("tilt_channel", 1)),
                **kwargs
            )
            log.info("Using PCA9685 servo at 0x%x", address)
            return ctrl, "pca9685"
        except Exception as exc:
            log.warning("PCA9685 init failed, simulating: %s", exc)
    log.info("Using simulated camera servo")
    return SimulatedServo(**kwargs), "simulated"

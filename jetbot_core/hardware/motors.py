"""Motor abstraction. Agent never talks to PWM."""

from __future__ import print_function

import logging
import threading
import time

log = logging.getLogger("jetbot.motors")


def _clamp(value, lo, hi):
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


class MotorController(object):
    """Interface: differential drive in [-1, 1]."""

    def set_speeds(self, left, right):
        raise NotImplementedError

    def stop(self):
        self.set_speeds(0.0, 0.0)

    def snapshot(self):
        return {"backend": "interface", "left": 0.0, "right": 0.0, "ok": False}

    def close(self):
        try:
            self.stop()
        except Exception:
            pass


class SimulatedMotors(MotorController):
    def __init__(self):
        self._lock = threading.Lock()
        self.left = 0.0
        self.right = 0.0
        self.last_command = 0.0

    def set_speeds(self, left, right):
        left = _clamp(float(left), -1.0, 1.0)
        right = _clamp(float(right), -1.0, 1.0)
        with self._lock:
            self.left = left
            self.right = right
            self.last_command = time.time()

    def snapshot(self):
        with self._lock:
            return {
                "backend": "simulated",
                "left": self.left,
                "right": self.right,
                "ok": True,
                "last_command": self.last_command,
            }


class AdafruitMotorHATController(MotorController):
    """Wraps Adafruit MotorHAT / Waveshare JetBot board."""

    def __init__(self, i2c_bus=1, left_channel=1, right_channel=2,
                 left_alpha=1.0, right_alpha=1.0, address=0x60):
        from Adafruit_MotorHAT import Adafruit_MotorHAT
        self._hat = Adafruit_MotorHAT
        self.driver = Adafruit_MotorHAT(addr=address, i2c_bus=i2c_bus)
        self.left_motor = self.driver.getMotor(left_channel)
        self.right_motor = self.driver.getMotor(right_channel)
        self.left_alpha = float(left_alpha)
        self.right_alpha = float(right_alpha)
        self.left_channel = left_channel
        self.right_channel = right_channel
        self._lock = threading.Lock()
        self.left = 0.0
        self.right = 0.0
        self.last_command = 0.0
        # Waveshare INA/INB PWM channels (same mapping as stock jetbot.motor)
        self._left_ina, self._left_inb = (1, 0) if left_channel == 1 else (2, 3)
        self._right_ina, self._right_inb = (1, 0) if right_channel == 1 else (2, 3)
        if left_channel == right_channel:
            raise ValueError("left and right motor channels must differ")
        self.stop()

    def _write_one(self, motor, ina, inb, alpha, value):
        mapped = int(255.0 * (alpha * value))
        speed = min(max(abs(mapped), 0), 255)
        motor.setSpeed(speed)
        if mapped < 0:
            motor.run(self._hat.FORWARD)
            try:
                self.driver._pwm.setPWM(ina, 0, 0)
                self.driver._pwm.setPWM(inb, 0, speed * 16)
            except Exception:
                pass
        elif mapped > 0:
            motor.run(self._hat.BACKWARD)
            try:
                self.driver._pwm.setPWM(ina, 0, speed * 16)
                self.driver._pwm.setPWM(inb, 0, 0)
            except Exception:
                pass
        else:
            motor.run(self._hat.RELEASE)
            try:
                self.driver._pwm.setPWM(ina, 0, 0)
                self.driver._pwm.setPWM(inb, 0, 0)
            except Exception:
                pass

    def set_speeds(self, left, right):
        left = _clamp(float(left), -1.0, 1.0)
        right = _clamp(float(right), -1.0, 1.0)
        with self._lock:
            self._write_one(self.left_motor, self._left_ina, self._left_inb, self.left_alpha, left)
            self._write_one(self.right_motor, self._right_ina, self._right_inb, self.right_alpha, right)
            self.left = left
            self.right = right
            self.last_command = time.time()

    def snapshot(self):
        with self._lock:
            return {
                "backend": "motorhat",
                "left": self.left,
                "right": self.right,
                "ok": True,
                "last_command": self.last_command,
            }

    def close(self):
        try:
            self.stop()
        except Exception:
            pass


def _i2c_present(bus, address):
    try:
        import fcntl
        import os as _os
        I2C_SLAVE = 0x0703
        path = "/dev/i2c-%d" % int(bus)
        fd = _os.open(path, _os.O_RDWR)
        try:
            fcntl.ioctl(fd, I2C_SLAVE, int(address))
            try:
                _os.write(fd, b"\x00")
                return True
            except Exception:
                # Some devices NAK a dummy write but still exist; treat open+ioctl as present.
                return True
        finally:
            _os.close(fd)
    except Exception:
        return False


def try_create_motors(cfg):
    motors_cfg = cfg.get("motors", {})
    simulate = cfg.get("robot", {}).get("simulate_if_missing", True)
    bus = int(motors_cfg.get("i2c_bus", 1))
    address = int(motors_cfg.get("address", 0x60))
    try:
        from Adafruit_MotorHAT import Adafruit_MotorHAT  # noqa: F401
        hat_ok = True
    except Exception as exc:
        log.warning("Adafruit_MotorHAT unavailable: %s", exc)
        hat_ok = False
    present = _i2c_present(bus, address)
    if hat_ok and (present or not simulate):
        try:
            ctrl = AdafruitMotorHATController(
                i2c_bus=bus,
                left_channel=int(motors_cfg.get("left_channel", 1)),
                right_channel=int(motors_cfg.get("right_channel", 2)),
                left_alpha=float(motors_cfg.get("left_alpha", 1.0)),
                right_alpha=float(motors_cfg.get("right_alpha", 1.0)),
                address=address,
            )
            log.info("Using Adafruit MotorHAT on i2c-%s addr 0x%x", bus, address)
            return ctrl, "motorhat"
        except Exception as exc:
            log.warning("MotorHAT init failed: %s", exc)
            if not simulate:
                raise
    log.info("Using simulated motors (hat_ok=%s i2c_present=%s)", hat_ok, present)
    return SimulatedMotors(), "simulated"

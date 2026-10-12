"""Subsystem health / observability."""

from __future__ import print_function

import os
import time


def _read_first(paths):
    for path in paths:
        try:
            with open(path, "r") as handle:
                return handle.read().strip()
        except Exception:
            continue
    return None


_prev_cpu = None


def _cpu_pct():
    """Percent CPU busy since the previous sample. First call returns None."""
    global _prev_cpu
    try:
        with open("/proc/stat") as handle:
            parts = handle.readline().split()
        nums = [int(x) for x in parts[1:8]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums)
        prev = _prev_cpu
        _prev_cpu = (idle, total)
        if prev is None:
            return None
        didle = idle - prev[0]
        dtotal = total - prev[1]
        if dtotal <= 0:
            return 0.0
        busy = 100.0 * (1.0 - (float(didle) / float(dtotal)))
        if busy < 0:
            busy = 0.0
        if busy > 100:
            busy = 100.0
        return round(busy, 1)
    except Exception:
        return None


def host_telemetry():
    mem_total = mem_avail = None
    try:
        info = {}
        with open("/proc/meminfo") as handle:
            for line in handle:
                parts = line.split(":")
                if len(parts) == 2:
                    info[parts[0]] = parts[1].strip()
        def kib(name):
            raw = info.get(name, "0").split()[0]
            return int(raw) / 1024.0
        mem_total = kib("MemTotal")
        mem_avail = kib("MemAvailable") if "MemAvailable" in info else kib("MemFree")
    except Exception:
        pass
    load1 = None
    try:
        with open("/proc/loadavg") as handle:
            load1 = float(handle.read().split()[0])
    except Exception:
        pass
    temp = None
    raw = _read_first(["/sys/class/thermal/thermal_zone0/temp"])
    if raw:
        try:
            val = float(raw)
            temp = val / 1000.0 if val > 200 else val
        except Exception:
            pass
    gpu = None
    raw = _read_first(["/sys/devices/gpu.0/load"])
    if raw:
        try:
            gpu = float(raw) / 10.0  # Jetson reports tenths of a percent
        except Exception:
            pass
    return {
        "load1": load1,
        "cpu_pct": _cpu_pct(),
        "mem_total_mb": mem_total,
        "mem_avail_mb": mem_avail,
        "temp_c": temp,
        "gpu_pct": gpu,
        "ts": time.time(),
    }


class HealthMonitor(object):
    def __init__(self, world):
        self.world = world

    def set(self, name, online, detail="", extra=None):
        status = {"online": bool(online), "detail": detail or "", "ts": time.time()}
        if extra:
            status.update(extra)
        self.world.set_health(name, status)

    def snapshot(self, host_metrics=None):
        host = host_metrics if host_metrics is not None else host_telemetry()
        self.world.set_health("host", {"online": True, "detail": "ok", "ts": time.time()})
        data = self.world.health_dict()
        data["host_metrics"] = host
        return data

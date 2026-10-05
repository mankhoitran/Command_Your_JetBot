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
        load1 = float(open("/proc/loadavg").read().split()[0])
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

"""YAML + environment configuration. Python 3.6 compatible."""

from __future__ import print_function

import copy
import os

try:
    import yaml
except ImportError:
    yaml = None


DEFAULTS = {
    "robot": {"name": "jetbot-nano", "simulate_if_missing": True},
    "web": {"host": "0.0.0.0", "port": 8080, "jpeg_quality": 60, "mjpeg_fps": 12},
    "camera": {
        "width": 320,
        "height": 240,
        "fps": 15,
        "capture_width": 816,
        "capture_height": 616,
        "sensor_mode": 3,
        "device": "/dev/video0",
    },
    "motors": {
        "i2c_bus": 1,
        "address": 96,
        "left_channel": 1,
        "right_channel": 2,
        "left_alpha": 1.0,
        "right_alpha": 1.0,
        "max_speed": 0.28,
        "min_command": 0.05,
    },
    "servo": {
        "i2c_bus": 1,
        "address": 64,
        "pan_channel": 0,
        "tilt_channel": 1,
        "pan_min_deg": -70,
        "pan_max_deg": 70,
        "tilt_min_deg": -25,
        "tilt_max_deg": 25,
        "center_pan": 0,
        "center_tilt": 0,
    },
    "safety": {
        "min_obstacle_m": 0.38,
        "slow_obstacle_m": 0.70,
        "max_speed": 0.25,
        "max_turn": 0.35,
        "stale_perception_s": 0.90,
        "command_timeout_s": 1.20,
        "watchdog_hz": 20,
        "require_fresh_perception_to_move": True,
    },
    "perception": {
        "inference_width": 160,
        "inference_height": 120,
        "enable_depth": True,
        "enable_detection": True,
        "detection_interval_s": 0.20,
        "depth_interval_s": 0.07,
        "min_object_area": 400,
        "haar_cascade": "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml",
        "ssd_engine": "",
    },
    "navigation": {
        "goal_reach_m": 0.45,
        "turn_in_place_speed": 0.22,
        "cruise_speed": 0.18,
        "bins": 5,
        "blocked_bins_center": 0.35,
    },
    "llm": {
        "base_url": "http://192.168.20.150:8008/v1",
        "model": "gemma-4-E4B-it-Q4_K_M",
        "timeout_s": 25,
        "max_tokens": 280,
        "temperature": 0.2,
        "min_reason_interval_s": 6.0,
    },
    "whisper": {
        "base_url": "http://192.168.20.150:8003",
        "timeout_s": 45,
    },
    "typesafe": {
        "enabled": False,
        "api_url": "https://api.typesafe.ai/v1/systemone",
        "model": "jev-latest",
        "timeout_s": 8,
        "api_key": "",
    },
    "memory": {
        "path": "data/amem.json",
        "max_notes": 400,
        "retrieve_k": 5,
    },
    "loops": {
        "safety_hz": 20,
        "navigation_hz": 10,
        "agent_hz": 4,
        "telemetry_hz": 2,
    },
}

_ENV_MAP = {
    "JETBOT_WEB_PORT": ("web", "port", int),
    "JETBOT_WEB_HOST": ("web", "host", str),
    "JETBOT_LLM_URL": ("llm", "base_url", str),
    "JETBOT_LLM_MODEL": ("llm", "model", str),
    "JETBOT_WHISPER_URL": ("whisper", "base_url", str),
    "TYPESAFE_API_KEY": ("typesafe", "api_key", str),
    "TYPESAFE_ENABLED": ("typesafe", "enabled", lambda v: str(v).lower() in ("1", "true", "yes")),
    "JETBOT_SIMULATE": ("robot", "simulate_if_missing", lambda v: str(v).lower() in ("1", "true", "yes")),
}


def _deep_update(base, overlay):
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def _load_yaml(path):
    if not path or not os.path.isfile(path):
        return {}
    if yaml is None:
        raise RuntimeError("PyYAML is required to load %s" % path)
    with open(path, "r") as handle:
        data = yaml.safe_load(handle) or {}
        # Ignore a trailing document separator if an editor added "---".
    if not isinstance(data, dict):
        raise ValueError("config root must be a mapping: %s" % path)
    return data


def load_config(path=None):
    """Return a nested dict. `path` defaults to CYJ/config.yaml."""
    cfg = copy.deepcopy(DEFAULTS)
    if path is None:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(here, "config.yaml")
    _deep_update(cfg, _load_yaml(path))
    for env_name, spec in _ENV_MAP.items():
        if env_name not in os.environ:
            continue
        section, key, caster = spec
        raw = os.environ[env_name]
        try:
            cfg[section][key] = caster(raw)
        except Exception:
            cfg[section][key] = raw
    return cfg


class AttrDict(dict):
    """Nested attribute access for config sections."""

    def __getattr__(self, item):
        try:
            value = self[item]
        except KeyError:
            raise AttributeError(item)
        if isinstance(value, dict) and not isinstance(value, AttrDict):
            value = AttrDict(value)
            self[item] = value
        return value

    def __setattr__(self, key, value):
        self[key] = value

# Development

## Hardware / OS

- Jetson Nano 4GB, L4T R32.5.0 / JetPack 4.5, Ubuntu 18.04
- System Python **3.6.9** (no pip on PATH)
- Available: numpy 1.13, OpenCV 4.1.1 + GStreamer, TensorRT 7.1, PyYAML, requests
- Not available: PyTorch, Flask, FastAPI, pydantic, pytest, jetson-inference, Adafruit_MotorHAT

Do not introduce 3.7-only syntax (`dataclasses`, `list[str]`, f-strings are OK in 3.6? **No** — this code avoids f-strings).

## Run

```
cd /home/jetbot/CYJ
python3 run.py --config /home/jetbot/CYJ/config.yaml
```

Console: `http://<nano-ip>:8080`  
This device: `http://192.168.16.58:8080`

E-STOP is the red button and `POST /api/estop`. It does not wait for the LLM.

## Configuration

`config.yaml` plus env:

| Env | Meaning |
|---|---|
| `JETBOT_WEB_PORT` | HTTP port |
| `JETBOT_LLM_URL` | OpenAI-compatible base, default `http://192.168.20.150:8008/v1` |
| `JETBOT_WHISPER_URL` | whisper.cpp, default `http://192.168.20.150:8003` |
| `OPENROUTER_API_KEY` | OpenRouter key for Jev (`~typesafe/jev-latest`) |
| `TYPESAFE_API_KEY` | alias of OpenRouter key (legacy name) |
| `JETBOT_SIMULATE` | force simulate-if-missing |
| `JETBOT_ALLOW_MOTION` | `1` to unlock wheels (default locked while charging) |

Remote LLM and Whisper URLs must stay on `.150` unless you change the config on purpose.

## Tests

```
cd /home/jetbot/CYJ
python3 tests/test_core.py
```

stdlib `unittest`, hardware mocked. No physical motion.

## Debugging

- `GET /api/status` — world, health, agent, motors backend
- `GET /api/events` — typed timeline
- `GET /api/memory` — A-MEM-like notes
- `GET /video` — MJPEG (overlay if perception has run)
- Logs: stderr from `run.py` (`--log-level DEBUG`)

If the camera is synthetic, CSI failed to open (often because another process
holds nvargus). Stop Jupyter camera notebooks and retry.

If motors show `simulated`, I2C 0x60 was empty. Power the MotorHAT and restart.

## Resource notes

Keep perception at 160×120. Do not add a second heavyweight network while
the geometric depth + contour detector are running. TensorRT SSD is optional
via `perception.ssd_engine` when an engine file exists.

LLM max_tokens is 280 by design (2k context). Do not send images.
---

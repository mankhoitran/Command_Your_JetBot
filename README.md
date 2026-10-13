# JetBot Cognitive Runtime (CYJ)

Event-driven control stack cho **Jetson Nano 4GB**. Nguyên tắc cốt lõi:

> **LLM lập kế hoạch, không điều khiển PWM trực tiếp.**

Mọi lệnh vận động đều phải qua `Navigation → Safety → Hardware`. E-STOP hoạt động ngay cả khi mất LLM / mất mạng / crash Agent.

```bash
cd /home/jetbot/CYJ
python3 run.py
# Console:  https://<nano-ip>:8080   (HTTPS self-signed, accept warning)
# Monitor:  https://<nano-ip>:8090   (Dashboard / Pipeline / Agent Trace)
# Tìm IP: hostname -I  — luôn dùng https://, http:// sẽ bị reset
```

---

## 1. Kiến trúc tổng quan

```
              HUMAN / INPUT
        ┌─────────┼──────────┐
      Voice      Web      Button/Pad/API
        │         │            │
     Whisper   /api/command  /api/manual
        │         │            │
        └────┬────┴────────────┘
             ▼
      ┌─────────────┐
      │    AGENT    │◄── World summary + Spatial + Memory
      │ TaskManager │    (không bao giờ đọc frame thô)
      │ Keyword→    │
      │ TypeSafe→LLM│
      └──────┬──────┘
             │ tools (tối đa 3 / plan)
             ▼
      ┌─────────────┐     ┌──────────────┐
      │ NAVIGATION  │────►│    SAFETY    │────► Motors / Servo
      │ LocalPlanner│ cmd │  Controller  │ 20Hz   (I2C 0x60 / sim)
      └──────▲──────┘     └──────▲───────┘
             │                  │
      ┌──────┴──────┐    ┌──────┴────────┐
      │ WORLD STATE │◄───│  PERCEPTION   │
      │ operational │    │ RGB→Depth     │
      │ truth (fast)│    │ RGB→Detect→   │
      └──────┬──────┘    │ Track         │
             │           └──────▲────────┘
             │                  │ latest-frame
      ┌──────┴──────┐    ┌──────┴────────┐
      │ SPATIAL +   │    │    CAMERA     │
      │ A-MEM memory│    │ nvargus/V4L2/ │
      │ (semantic)  │    │ synthetic 15fps│
      └─────────────┘    └───────────────┘
```

Thứ tự ưu tiên dữ liệu (realtime luôn thắng memory cũ):

```
SENSOR OBSERVATION > WORLD STATE > SPATIAL MAP > SEMANTIC MEMORY
```

## 2. Flow pipeline (3 vòng thời gian)

| Loop | Tần số | Việc làm | Không được làm |
|---|---|---|---|
| **FAST — Safety** | **20 Hz** (`loops.safety_hz`, `watchdog_hz: 20`) | Validate `MotionCommand` → clamp / stale-check / obstacle-check → `motors.set_speeds()` | Gọi LLM, network, memory, UI |
| **MEDIUM — Perception + Nav** | **Nav 10 Hz**, Depth ~14 Hz (`0.07s`), Detect 5 Hz (`0.20s`), Camera capture 15 fps | Frame mới nhất → depth + detect/track → `WorldState` → `LocalPlanner` ra lệnh vi phân ngắn | Gọi LLM mỗi frame |
| **SLOW — Cognition** | **Agent 4 Hz** + event-driven, LLM throttle **6 s** (`min_reason_interval_s`) | `submit(text)` → keyword → TypeSafe → LLM → `ToolRouter.dispatch` → Planner/Safety | PWM trực tiếp, tính geometry tay |

### 2.1. Đường đi chi tiết một lệnh

```
1. INPUT
   Web POST /api/command {text} ──► agent.submit(source=web)
   Mic browser ──► POST /api/whisper ──► whisper.cpp ──► agent.submit(source=whisper)
   Pad/Manual POST /api/manual ──► runtime.handle_manual (đi thẳng Safety/Planner, không tạo task)

2. PERCEPTION (chạy liên tục, không chờ lệnh)
   Camera thread ──► LatestFrameBuffer (1 slot, drop frame cũ)
      ├─► MJPEG ra UI (copy, jpeg q60, 12fps)
      └─► Worker: resize 160×120
            ├─ depth mỗi 0.07s ──► world.update_obstacles(bins, front_m, blocked)
            └─ detect mỗi 0.20s ──► IoUTracker ──► world.upsert_object + events
                OBJECT_DETECTED / OBSTACLE_DETECTED / WORLD_CHANGED ...

3. AGENT (agent.py:_handle)
   bind corr_id → TaskManager.start/transition(PLANNING)
      ├─ _needs_llm? / source==nav → bỏ qua local
      ├─ keyword match (conf 0.9) → dispatch ngay
      ├─ TypeSafe (conf ≥0.72, needs_confirm <0.7) → dispatch ngay
      └─ LLM plan(world_summary + spatial + memory) → _ensure_usable_plan
            → tools.dispatch từng tool (≤3) → Task EXECUTING/COMPLETED/OBSERVING/FAILED

4. NAVIGATION (navigation.py, 10 Hz)
   set_mode(idle|forward|left|right|backup|scan|explore|follow|recovery)
      → _compute(): corridor follow / bám bbox.cx / quay recovery tại chỗ
      → MotionCommand(left, right, source, duration, corr_id) ──► Safety.request()

5. SAFETY (safety.py — hard boundary)
   request() → validate() → latch → loop 20Hz apply
   Reject nếu: estop | motion_locked | stale (>0.90s) | obstacle | timeout (1.20s)
   Clamp: max_speed 0.25, max_turn 0.35, deadband 0.05, slow-scale khi front < 0.36m
      → motors.set_speeds() (jetbot lib → MotorHAT 0x60 → simulated)
      → world.update_robot + MotorLedger(requested/accepted/applied/rejected)

6. MEMORY (chỉ sự kiện đáng nhớ)
   new object / scan_complete / nav failure / user note
      → SemanticMemory.add_note → amem.json (link/evolve, dedup 120s)
      → Agent lần sau retrieve lexical (overlap + recency + confidence)
   Frame thô KHÔNG BAO GIỜ vào memory.
```

### 2.2. Camera không bao giờ bị nghẽn inference

```
CSI/nvargus (816×616 mode 3) / V4L2 /dev/video0 / synthetic
        │ 320×240 @15fps → latest-frame slot
        ├────────► Web MJPEG (luồng riêng)
        └────────► Perception worker (resize 160×120, drop frame cũ)
                     ├ Depth heuristic   └ Detect + Track
```

### 2.3. Active perception (camera bolted forward)

`servo.enabled: false` — không có gimbal. `look_left/right` = **xoay thân robot**, `look_up/down` = no-op. `camera.scan_environment` = survey 7 bước body-yaw (wheels locked) hoặc quay tại chỗ, rồi emit `SCAN_COMPLETED` → Agent `OBSERVING`.

## 3. Models đã sử dụng

| Vai trò | Model / Thuật toán | Cấu hình thực tế (`config.yaml` + code) | Vì sao chọn |
|---|---|---|---|
| **LLM reasoning** | `gemma-4-E4B-it-Q4_K_M` (remote llama.cpp, OpenAI-compatible) | `base_url: http://192.168.20.150:8008/v1`, `POST /chat/completions`, `max_tokens: 400`, `temp: 0.2`, `timeout: 30s`, `enable_thinking: false` (thinking làm tràn JSON), `response_format: {json_object}`, throttle 6s. Env: `JETBOT_LLM_URL`, `JETBOT_LLM_MODEL` | Nano 4GB không chạy LLM local; remote giữ Nano nhẹ, JSON plan `{say≤160c, reason, tools≤3, done/failed}` đủ cho planner |
| **ASR Voice** | `whisper.cpp` (remote service) | `base_url: http://192.168.20.150:8003`, `POST /inference` (multipart audio), `timeout: 90s`, `GET /` health. Env: `JETBOT_WHISPER_URL` | Tách ASR khỏi reasoning: `mic → Whisper → text → Agent` |
| **Depth** | **Không dùng model file.** `GeometricDepthEstimator` (`perception/depth.py`, `source: "geometric"`) | ROI nửa dưới 55% → Gray → Blur 5×5 → Canny(40,120); mỗi bin (5 bins): `occ = 0.45·darkness + 0.35·edge + 0.20·chroma` → map tuyến tính `occ 0→2.2m, 0.55→0.18m, 1→0.05m`; `blocked = front ≤ 0.18m` | TensorRT depth nặng + thiếu engine/PyTorch trên Nano; heuristic deterministic, nhẹ, đủ cho stop/slow ở 160×120 |
| **Detection** | Mặc định `backend: "opencv"`: **Haar + Contour** (`perception/detect.py`) | `haar_cascade: /usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml` → `person conf 0.72`; `ContourDetector`: HSV sat/val + Canny + findContours, lọc `area ≥ 400 (min_object_area)`, tối đa 8 box, `conf ≤ 0.85`, class `object`; + `IoUTracker(iou 0.3, max_misses 8)` → `obj_01…` | Nhẹ, chạy CPU Nano; SSD/TensorRT chỉ khi `perception.ssd_engine` trỏ tới file engine tồn tại (hiện `""` → không dùng) |
| **Decision gate** | `~typesafe/jev-latest` via OpenRouter Decisions API (`typesafe_client.py`) | `api_url: https://openrouter.ai/api/alpha/decisions`, `timeout: 8s`, key từ `OPENROUTER_API_KEY` (xem `.env`), fail-open → `None`; Agent chỉ dùng khi `conf ≥ 0.72` và `needs_confirm < 0.7`, questions `{intent: stop|forward|left|…|unknown, needs_confirm}` | Không phải "bộ não thứ 2": chỉ phân loại intent / cần xác nhận, giảm gọi LLM |

> Ghi chú bộ nhớ: `docs/development.md` — không thêm network thứ 2 khi đang chạy depth + detector; giữ inference 160×120; LLM `n_ctx = 2048` nên prompt chỉ là world summary (8 objects) + vài dòng memory, **không gửi ảnh**.

## 4. Safety boundary + Task + Events

Ngưỡng (`config.yaml:safety` + `navigation`):

| Param | Giá trị | Ý nghĩa |
|---|---|---|
| `min_obstacle_m` | **0.18 m** | Front ≤ này → zero lệnh tiến, `blocked=True` |
| `slow_obstacle_m` | **0.36 m** | Front ≤ này → scale tốc độ `max(0.15, (front−0.18)/(0.36−0.18))` |
| `turn_obstacle_m` | **0.06 m** | Chỉ chặn xoay tại chỗ khi gần chạm |
| `max_speed` / `max_turn` | **0.25 / 0.35** | Clamp mọi lệnh (motors `max_speed: 0.28`, `min_command: 0.05`) |
| `stale_perception_s` | **0.90 s** | Depth cũ hơn → reject lệnh tiến + emit `PerceptionStale` |
| `command_timeout_s` | **1.20 s** | Watchdog hết hạn → dừng, `source=watchdog` |
| `allow_motion` | **`false` mặc định** | Khóa bánh khi sạc/bàn; mở bằng **UNLOCK WHEELS** hoặc `POST /api/motion {"allow": true}` / `JETBOT_ALLOW_MOTION=1` |
| `goal_reach_m` / `cruise` / `turn_in_place` / `recovery_s` | 0.45 / 0.18 / 0.22 / 2.0s | Planner hành lang + recovery quay sau 2s blocked |

Task lifecycle (`task.py` — TaskManager sở hữu, LLM không sở hữu):

```
IDLE → PLANNING → EXECUTING → OBSERVING → VERIFYING → COMPLETED
              ↘ ACTIVE_PERCEPTION ↗        ↘ REPLANNING ↗ (EXECUTING→REPLANNING→EXECUTING)
              EXECUTING → PAUSED / FAILED / CANCELLED
```

Events (`events.py`, bus 250, history cho UI): `ObjectDetected/Updated/Lost`, `ObstacleDetected/Cleared`, `WorldChanged`, `ScanRequested/Completed`, `TargetFound/Lost`, `LowConfidence`, `TaskStarted/Paused/Completed/Failed/Cancelled`, `NavigationBlocked/Recovered/RecoveryFailed`, `EmergencyStop`, `CommandRejected`, `PerceptionStale`, `LlmUnavailable`, `WhisperUnavailable`, `MemoryUpdated`, `UserCommand`, `VoiceTranscript`.

Độ không chắc chắn: `KNOWN | PROBABLE | UNKNOWN | STALE | CONTRADICTED` (object prune sau 2.5s, drop 7.5s).

## 5. Web Console + Pipeline Monitor

| | Console (`web.py` + `web/static`) | Monitor (`monitor.py` + `web/monitor`) |
|---|---|---|
| Port | `:8080` HTTPS | `:8090` HTTPS (cùng cert `data/ssl/`) |
| Xem | Camera MJPEG + overlay, robot status, perception, world, task, memory, events, nút **STOP** đỏ + điều khiển tay | Dashboard / Pipeline (loops, nodes, edges) / Agent Trace |
| API chính | `GET /api/status /api/world /api/memory /api/health /api/task /api/events`, `GET /video`, `POST /api/command /api/manual /api/motion /api/estop /api/whisper /api/scan` | `GET /api/pipeline /api/dashboard /api/motor /api/trace /api/events /api/settings /api/status`, `GET /api/stream` (SSE 0.8s), `POST /api/settings /api/command` |
| An toàn | UI không bypass Safety; STOP = `safety.emergency_stop()` local | Read-only, trừ sửa prompt/tools live và queue lệnh; không bao giờ gọi `set_speeds` / `emergency_stop` / `set_allow_motion` |

## 6. Cài môi trường & chạy

**Hardware/OS (đã chốt trên máy này):** Jetson Nano 4GB, L4T R32.5.0 / JetPack 4.5, Ubuntu 18.04, **Python 3.6.9** (tránh `dataclasses`, `list[str]`; code không dùng f-string).

**Deps có sẵn:** `numpy 1.13`, `OpenCV 4.1.1 + GStreamer`, `TensorRT 7.1`, `PyYAML`, `requests`. **Không có:** PyTorch, Flask/FastAPI, pydantic, pytest, jetson-inference, Adafruit_MotorHAT → code dùng stdlib HTTP + fallback `simulated` khi thiếu I2C/camera.

```bash
# 1. Clone / vào đúng folder
cd /home/jetbot/CYJ

# 2. Key TypeSafe (không commit file thật)
cp .env.example .env   # điền OPENROUTER_API_KEY=sk-or-v1-...

# 3. Kiểm tra config (LLM/Whisper phải ở .150 trừ khi đổi có chủ đích)
cat config.yaml
# llm.base_url: http://192.168.20.150:8008/v1 (gemma-4-E4B-it-Q4_K_M)
# whisper.base_url: http://192.168.20.150:8003
# servo.enabled: false (giữ false tới khi gắn PCA9685 0x40)
# robot.allow_motion: false (mặc định khóa bánh)

# 4. Chạy (từ chối instance thứ 2 nếu 8080/8090 bận)
python3 run.py [--config config.yaml] [--host 0.0.0.0] [--port 8080] [--monitor-port 8090] [--log-level INFO]
# Mở UNLOCK WHEELS trên console khi robot đã đặt đất. Hoặc boot mở sẵn:
JETBOT_ALLOW_MOTION=1 python3 run.py

# 5. Test (stdlib unittest, mock hardware, không chạy motor thật)
python3 tests/test_core.py -v   # hoặc scripts/run_tests.sh
```

Env override (`config.py` + `.env`): `JETBOT_WEB_PORT/HOST`, `JETBOT_MONITOR_PORT`, `JETBOT_LLM_URL`, `JETBOT_LLM_MODEL`, `JETBOT_WHISPER_URL`, `OPENROUTER_API_KEY` / `TYPESAFE_API_KEY`, `JETBOT_SIMULATE`, `JETBOT_ALLOW_MOTION`.

Debug nhanh: `GET /api/status` (world/health/agent/backend), `GET /api/events`, `GET /video` (MJPEG overlay), `:8090/api/pipeline` + `/api/motor` (requested/accepted/applied/rejected), stderr `--log-level DEBUG`. Camera synthetic = CSI bận (tắt notebook chiếm nvargus); motors simulated = I2C 0x60 trống (cấp nguồn MotorHAT rồi restart).

## 7. Layout repo

```
CYJ/
├── run.py                  # entrypoint: load config → Runtime.start → serve 8080 + monitor 8090
├── config.yaml             # mọi threshold/port/model URL (đừng hardcode trong code)
├── .env / .env.example     # OPENROUTER_API_KEY (gitignored)
├── jetbot_core/
│   ├── runtime.py          # dựng + start/stop toàn bộ subsystem
│   ├── agent.py / llm.py / tools.py / task.py   # cognition (slow loop)
│   ├── world.py / spatial.py / memory.py        # world nhanh + spatial + A-MEM semantic
│   ├── perception/ (pipeline.py, depth.py, detect.py, track.py)
│   ├── navigation.py / safety.py / events.py    # medium + fast loop
│   ├── hardware/ (camera.py, motors.py, servo.py)
│   ├── web.py / monitor.py / telemetry.py / trace.py / health.py / live.py
│   ├── whisper_client.py / typesafe_client.py / config.py
├── web/static/ + web/monitor/  # console + dashboard
├── data/ (amem.json, live_settings.json, ssl/) # memory + live prompt + cert tự gen
├── docs/ (architecture, development, gap_analysis, observability, pipeline_audit, ...)
├── tests/test_core.py      # ~946 dòng unittest no-hardware
└── jetbot_construct.md     # design prompt gốc
```

Chi tiết sâu hơn: `docs/architecture.md` (loops, safety, world vs memory), `docs/development.md` (chạy/test/debug), `docs/gap_analysis.md`, `docs/observability.md`, `docs/pipeline_audit.md`, `jetbot_construct.md` (spec gốc 38 mục).

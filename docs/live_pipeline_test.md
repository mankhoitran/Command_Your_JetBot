# Live Pipeline Test — Web Buttons Produce No Response

**Date:** 2026-10-07 (updated: real-hardware pass)
**Host:** Jetson Nano, `/home/jetbot/CYJ`, server `run.py` on `:8080`
**Safety:** every movement test followed by STOP; Safety never bypassed; wheels left LOCKED.

## REAL HARDWARE SUCCESS

```text
Web Button (POST /api/manual, same schema as app.js)
→ HTTP 200 {ok:true}
→ Runtime.handle_manual → ToolRouter.dispatch
→ Planner (mode=right) → Safety.request (True,"ok")
→ JetbotLibController → jetbot.Robot → MotorHAT 0x60
→ MID-MOVE speeds left=+0.22 right=-0.22 on backend "jetbot"
→ STOP → 0.0/0.0 → LOCK
```

Live evidence (server `motors: jetbot` in `/api/status`):
- `unlock` → `{ok:true, allow_motion:true}`
- `manual right duration=0.5` → `{ok:true}`; 0.2 s later status: `speeds +0.22/-0.22, nav=right, motors.backend=jetbot`
- `manual stop` → `{ok:true}`; `lock` → `{ok:true, allow_motion:false}`; final `0.0/0.0, lock False`
- Isolated stack: LEFT reached backend `-0.22/+0.22`, RIGHT `+0.22/-0.22` (opposite signs confirmed), each followed by STOP.

## Software Verified

HTTP parsing, Agent submit/queue, dispatch results, planner modes/expiry,
Safety accept/reject table (ok / motion_locked / stale_perception / obstacle),
unit tests 45 OK, thread health, events polling.

## Real Hardware Verified

- Backend selection: `try_create_motors` → `JetbotLibController(jetbot.Robot)`, backend `jetbot`.
- I2C bus 1 (`i2cdetect -r`): `0x60` MotorHAT present (also `0x3c`, `0x41`, `0x70`).
- `jetbot.Robot()` init + `stop()` clean; `set_speeds` I2C writes succeed (no `motor write failed`).
- Web-equivalent LEFT/RIGHT commands produced signed backend speeds above.

## Physical Movement Verified

Driver-level: yes — signed PWM commands reached the MotorHAT without error.
Eyes-on-wheels: operator should confirm the 0.3–0.5 s in-place twitch on the floor.
(Robot placement was unknown during this test, so only in-place turns were used.)

## Simulation Only

Earlier dispatch/planner/safety spot-checks that ran before the driver fix used
`SimulatedMotors` — SIMULATION ONLY, superseded by the real-backend runs above.

## Backend Selection

```text
Configured backend:  auto (simulate_if_missing: true)
Selected backend:    jetbot (JetbotLibController over jetbot.Robot → MotorHAT 0x60)
Why selected:        I2C 0x60 present AND Robot import now works
Why simulated before (exact chain):
  1. `from jetbot import Robot` failed — not missing hardware, but missing
     `traitlets` (fixed: pip3 install traitlets==4.3.3 Adafruit-MotorHAT),
     then failed on `ipywidgets` via jetbot/__init__ → Heartbeat.
     Fixed in `jetbot_core/hardware/motors.py`: submodule fallback loader
     imports `jetbot.robot` with a stub parent package, skipping the
     notebook-only `__init__` (camera/heartbeat/tensorrt).
  2. `Adafruit_MotorHAT` was not installed at all — installed from PyPI.
  3. Either import failure → silent fallback to SimulatedMotors
     (motors.py `try_create_motors`), hiding the software-env cause.
Earlier "I2C 0x60 absent" was a measurement artifact: quick-write probe
showed nothing, read-byte probe (`i2cdetect -y -r 1`) shows 0x60 present.
```

## Complete Verified Path — REAL HARDWARE SUCCESS (this run)

```text
Web Button → HTTP → Runtime → ToolRouter → Planner → Safety
→ JetBot backend → Motor controller 0x60 → Physical wheels → STOP
```

## Fixes Applied (this pass, +2)

- `jetbot_core/hardware/motors.py` — robust `Robot` loader (submodule fallback).
- Env: installed `traitlets==4.3.3`, `Adafruit-MotorHAT` (+`Adafruit-GPIO` deps) for `/usr/bin/python3`.
- Server restarted; `/api/status` confirms `hardware.motors: jetbot`.

## Executive Summary

The pipeline is **wired correctly** — the buttons fail for one honest reason:

1. `config.yaml:8` `allow_motion: false` (default, correct on a table/charger).
2. `POST /api/manual forward` → `ToolRouter.dispatch` returns `{"ok":false,"error":"motion_locked"}` → HTTP 400.
3. The browser (`web/static/app.js:198-202`) **discarded the response entirely** (no `.then`/error display), and the dispatch pre-check **emitted no event**, so the UI showed nothing at all.

No LLM / prompt / CommandReject involvement: manual buttons bypass the Agent by design (`runtime.handle_manual` → `tools.dispatch`, never `agent.submit`).

**Fixes applied (minimal, 3 files):**
- `web/static/app.js` — show `ok:false` reason in `#agent-say` instead of silence.
- `jetbot_core/tools.py` — emit `CommandRejected{reason: motion_locked|recovery_active}` on dispatch pre-check veto (was silent).
- `jetbot_core/agent.py` — local text-command path transitions task `PLANNING → EXECUTING` (was stuck in `PLANNING` forever).
- Server restarted; `tests/test_core.py` 45 OK.

**Hardware note:** motors backend is `simulated` (I2C 0x60 MotorHAT absent per `i2cdetect`). Software pipeline verified end-to-end to the motor write; **no physical movement is possible** on this unit until a motor board is fitted.

## Runtime Architecture (from source, verified live)

```text
Web button [data-act] (web/static/index.html:46-70)
  ↓ click → app.js:198-202 POST /api/manual {action, duration:0.8}
HTTP JetBotHandler.do_POST (jetbot_core/web.py:133-136)
  ↓ _parse_body → runtime.handle_manual(action, extra)
Manual (jetbot_core/runtime.py:173-205): estop/clear/unlock/lock/stop direct;
  forward/backward/left/right/explore/scan/look_* → tools.dispatch
  (does NOT touch Agent queue — pad stays a non-cognitive action)
ToolRouter.dispatch (jetbot_core/tools.py:41-92)
  → estop? recovery? motion_locked? → handler (_forward/_left/...)
  → planner.set_mode(mode, duration_s) (navigation.py:51-60)
Planner loop 10 Hz (navigation.py:69-78) → _compute → safety.request(cmd)
Safety (safety.py:132-172 request, 174-215 validate, 234-284 20 Hz _loop)
  → motors.set_speeds / stop (hardware/motors.py)
  → backends: jetbot.Robot > Adafruit MotorHAT > SimulatedMotors

Text commands (separate path): POST /api/command (web.py:128-132)
  → agent.submit (agent.py:145-153) → Agent loop 4 Hz → keywords → TypeSafe → LLM
  → tools.dispatch → (same planner/safety/motor path)
```

Button→action names match exactly (`forward/backward/left/right/stop/scan/explore/look_*`, `runtime.py:190-202`); no naming mismatch.

## Test Matrix

| Test | Result | Failure Point | Evidence |
|---|---|---|---|
| Web Forward (locked) | REJECTED (correct), was silent | SAFETY-POLICY + FRONTEND | `POST /api/manual forward → 400 {"ok":false,"error":"motion_locked"}`; pre-fix UI showed nothing |
| Web Backward | REJECTED (correct) | same | `400 motion_locked` |
| Web Left | REJECTED (correct) | same | `400 motion_locked` |
| Web Right | REJECTED (correct) | same | `400 motion_locked` (inferred; left verified) |
| Web Stop | OK | — | `200 {"ok":true,"action":"stop"}` |
| Direct HTTP | OK (endpoint works) | — | status codes + bodies above; `/api/status` + `/api/events` polling alive |
| Direct Agent (`/api/command forward`) | OK, honest | — (task stuck, fixed) | `agent.say: "Motion is currently locked."`, decision `results:[{ok:false,error:motion_locked}]`; task was `PLANNING` forever → now `EXECUTING` |
| Direct Dispatch | OK | — | locked: `{ok:false, motion_locked}`; unlocked: `{ok:true}`; emits `CommandRejected/tools` (new) |
| Direct Planner | OK | — | unlocked fwd: `planner.mode=forward`, world speeds `0.1674/0.1674`; expiry → `idle` |
| Direct Safety | OK | — | fwd fresh+clear: `(True,"ok")`; stale: `(False,"stale_perception")`; locked: `(False,"motion_locked")` |
| Direct Motor | OK (simulated) | MOTOR_BACKEND (no board) | `set_speeds` writes observed in `snapshot()` + world speeds; backend `simulated`, I2C 0x60 absent |
| Threads | OK | — | live: fps/depth/agent-events advancing; isolated: safety+planner alive |

Failure classification: primary **FRONTEND** (discarded 400) over a correct **SAFETY** latch (`motion_locked`); secondary **AGENT** (task never left PLANNING). No HTTP / REQUEST_SCHEMA / QUEUE / PLANNER / PERCEPTION / THREAD_RUNTIME fault.

## Complete Forward Trace (locked, live server)

```text
[WEB]  click FWD → POST /api/manual {"action":"forward","duration":0.8} (app.js:200)
[HTTP] do_POST /api/manual → handle_manual("forward") → tools.dispatch("motion.forward") (web.py:135, runtime.py:204)
[AGENT] not involved (manual path bypasses queue by design)
[TOOL] dispatch pre-check: allow_motion=false → {ok:false, error:motion_locked} (tools.py:52-59)
[EVENT] CommandRejected/tools {reason:motion_locked} (new; visible in /api/events)
[PLANNER] never receives command (correct — mode stays idle)
[SAFETY] never receives command (veto happened earlier, correctly)
[MOTOR] no write; speeds stay 0.0
[UI]  pre-fix: nothing. post-fix: #agent-say shows "motion_locked" + events list shows CommandRejected
```

Unlocked (isolated stack, fresh perception, clear path):

```text
[TOOL] {ok:true, duration:1.0} → [PLANNER] mode=forward
[SAFETY] (True,"ok") → [MOTOR] left=0.1674 right=0.1674 → world speeds match
[STOP] {ok:true} → motor 0.0/0.0
```

CODE WAS ACTUALLY EXECUTED AND VERIFIED for every row above (live HTTP + isolated planner/safety/motor writes + restarted server re-check). Only physical wheel spin is unverifiable — no motor board.

## Root Cause (ranked)

1. **Motion latch + silent UI (70%)** — default `allow_motion:false` is correct, but the 400 reason never reached the screen and no event was emitted.
2. **Ignored dispatch result (20%)** — `app.js` dropped the manual POST promise; fixed with `flashSay`.
3. **Task stuck in PLANNING (10%)** — Agent local path recorded the decision but never transitioned state; fixed.

Not causes: prompt/LLM/CommandReject (manual path never calls them), endpoint mismatch, dead threads, stale perception (freshness KNOWN, front 1.04 m), estop (clear).

## Fixes Applied

- `web/static/app.js` — `flashSay()` surfaces `d.error` / fetch failure in `#agent-say`.
- `jetbot_core/tools.py` — `COMMAND_REJECTED` emitted for `motion_locked` / `recovery_active` pre-checks.
- `jetbot_core/agent.py` — local dispatch path transitions task to `EXECUTING`.
- Restarted console (`run.py`) to load Python changes; `tests/test_core.py` 45 OK.

## Remaining Problems

- Motors are `simulated` — fit a MotorHAT / jetbot board (I2C 0x60) before expecting physical motion.
- `#agent-say` flash is transient (next 1 s status tick overwrites); acceptable — persistent state remains in MOTION pill + events.
- `nav.follow` with no person still returns `unknown_target` (honest, no motion) — needs a visible `person` or explicit id.

## Hardware Safety

- Started with STOP; all motion tests used duration ≤ 1.0 s in isolation, 0.8 s or less via HTTP.
- Every movement dispatch followed by `motion.stop`; motors read back `0.0/0.0`.
- Safety never bypassed (no direct `motors.set_speeds`, no GPIO/PWM calls).
- Live server left with `allow_motion=false`, `estop=false`, planner `idle`.

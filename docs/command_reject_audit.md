# CommandReject / Decision Audit

**Date:** 2026-10-06
**Scope:** `/home/jetbot/CYJ` — full path `user → HTTP → Agent → LLM/parser → Tools → Planner → Safety → motors`
**Constraint:** diagnosis only, no source modified
**Premise correction:** this codebase has **no `CommandReject` decision**. There is only a `CommandRejected` **event** (`jetbot_core/events.py:37`, emitted in `jetbot_core/safety.py:133,143,154`, displayed as bad in `web/static/app.js:89`). The "Agent refuses too easily" symptom is real, but the mechanism is **downstream deterministic gates + silent fallbacks**, not an LLM action competing with `FORWARD`.

---

## 1. Decision pipeline (exact path)

```text
User command
 ↓ POST /api/command {text} | POST /api/manual {action} | POST /api/whisper → agent.submit(text)
   jetbot_core/web.py:128-136,152-186 → jetbot_core/agent.py:92-100
 ↓ Agent._loop (4 Hz, single thread, _busy gate)
   jetbot_core/agent.py:181-193 → _handle:195-311
 ↓ _local_intent: keywords → TypeSafe → None (= abstain)
   jetbot_core/agent.py:332-346,313-330
 ↓ if None or _needs_llm → LLM.plan(world_summary, memory_summary)
   jetbot_core/llm.py:96-143, SYSTEM_PROMPT:16-45
 ↓ parse_plan / _clean_tools / _extract_tools / _recover_plan
   jetbot_core/llm.py:146-336
 ↓ _ensure_usable_plan (blocked → force scan)
   jetbot_core/agent.py:363-389
 ↓ tools.dispatch(name, args)
   jetbot_core/tools.py:41-80
 ↓ planner.set_mode / halt  →  planner._loop 10 Hz → _compute → safety.request
   jetbot_core/navigation.py:51-78,80-160 → jetbot_core/safety.py:120-160
 ↓ safety.validate + 20 Hz _loop → motors.set_speeds / stop
   jetbot_core/safety.py:162-203,222-272 → jetbot_core/hardware/motors.py
```

| Stage | File:function | Input → Output | Validation / reject condition | Fallback |
|---|---|---|---|---|
| HTTP accept | `web.py:do_POST` | text → `agent.submit` | empty text → `None` (dropped silently) | `{"ok":True,"accepted":...}` even if Agent later does nothing |
| Agent queue | `agent.py:_loop/_handle` | item → plan+dispatch | `_aborted` (estop) → return, `last_say="emergency stop"` | queue holds while `_busy`; estop clears `_pending` but not in-flight `_handle` |
| Keywords | `agent.py:_keyword_intent` | lowercased text → intent,0.9 | exact / `startswith(key+" ")` / `endswith(" "+key)` only | miss → TypeSafe/LLM |
| TypeSafe | `agent.py:_local_intent`, `typesafe_client.py:classify_intent:105` | text → `{intent,confidence,needs_confirm}` or `None` | `confidence>=0.72`, `intent!="unknown"`, `needs_confirm<0.7` else `None` | `None` = abstain, **not** reject; falls to LLM |
| LLM | `llm.py:plan` | instruction+world+memory → JSON | timeout 30s (`config.yaml:85`), HTTP≥400, empty → `(None,err)` | `LLM_UNAVAILABLE` event, `motion.stop`, task `FAILED` (`agent.py:260-270`) |
| Parser | `llm.py:parse_plan,_clean_tools` | raw text → `{say,reason,tools[],done,failed}` | unknown tool dropped; `camera*` truncated → `camera.scan_environment`; `motion*` truncated → dropped (`llm.py:270-277,301-305`) | `_empty_plan` / `_recover_plan` with `tools=[]` |
| Repair | `agent.py:_ensure_usable_plan` | plan + blocked flag → plan | if `blocked` or `"block"` in text → force `camera.scan_environment`; if bad parse and no tools → force scan | uncertainty → **scan**, never reject |
| Tools | `tools.py:dispatch` | name → handler result | estop → `ok:False`; `motion.*` during `planner.in_recovery()` → `ok:False recovery_active`; unknown → `ok:False`; `nav.go_to` unknown → `ok:False unknown_target` (+side-effect scan) | caller (`agent.py:235-242,276-279`) ignores `ok` and still says `"OK: <intent>"` |
| Planner | `navigation.py:_compute` | mode+`obstacles_view` → `MotionCommand` | `blocked` or `front<blocked_front_m(0.18)` + mode not in honor-list → recovery spin; `follow` no target → `(0,0)` silently | recovery emits `NAVIGATION_BLOCKED`, after `recovery_s:2.0` emits `NAVIGATION_RECOVERY_FAILED` |
| Safety | `safety.py:request/validate/_loop` | `MotionCommand` → `(ok,reason)` + latched cmd | `estop` / `motion_locked` (`allow_motion=false`) / `stale_perception` / `obstacle` → `(False,reason)` + `COMMAND_REJECTED` event | `_loop` writes `0,0` with `source="safety:<reason>"`; UI sees `CommandRejected` bad pill |
| Task | `task.py:start/transition/ensure_executing` | action → state | illegal transition → `ValueError` (almost all call sites swallow with `except ValueError:pass`) | local intents leave task in `EXECUTING` forever (never `COMPLETED` except `stop`/`status`) |

---

## 2. Every reject / no-op path

`CommandReject*` occurs **5 times** in code, all the same event. Everything else is `ok:False`, `(False,reason)`, `None`, or silent `(0,0)`.

| # | Location | Condition | Why it fires | Threshold / value | Necessary? |
|---|---|---|---|---|---|
| R1 | `safety.py:132-136 request` | `_estop==True` | e-stop latched | — | **Yes** |
| R2 | `safety.py:137-149 request` | `allow_motion==False` and non-zero cmd | **default** `robot.allow_motion:false` (`config.yaml:8`); wheels locked on table/charger | `>1e-6` | Yes as gate, **No as `CommandRejected`** — latch ≠ danger; same event + same red pill as estop is misleading |
| R3 | `safety.py:181-183 validate` | `require_fresh && moving_fwd && freshness==STALE` | `last_depth_ts` older than `stale_perception_s:0.90` (`config.yaml:58`) | `0.9 s` | Yes, but fires on camera stall even for `go forward a little`; human sees "safe" while robot sees `front=unknown/STALE` |
| R4 | `safety.py:186 validate` | `moving_fwd && (blocked \|\| front<=min_obstacle_m)` | `WorldState.stop_m=0.18`, depth `_occupancy_to_m` (`depth.py:103-114`), `update_obstacles` re-derives `blocked` (`world.py:202-207`) | `0.18 m` fwd, `0.06 m` turn (`config.yaml:52-54`) | Yes |
| R5 | `safety.py:189 validate` | `turning && front<=turn_obstacle_m` | in-place spin nearly touching | `0.06 m` | Yes |
| T1 | `tools.py:44 dispatch` | `robot.estop` and tool not in `(motion.stop,task.fail,task.complete)` | late plan after estop | — | Yes |
| T2 | `tools.py:46 dispatch` | `motion.*` (not stop) and `planner.in_recovery()` | recovery owns wheels | `mode=="recovery"` (`navigation.py:65-67`) | Yes, but `agent.py` does not check before saying `OK`; looks like reject-after-accept |
| T3 | `tools.py:69 dispatch` | unknown tool name | LLM hallucinated tool; `_clean_tools` should have dropped it | — | Yes |
| T4 | `tools.py:268 _go_to` | target not in `objects_view` | `nav.go_to` with name never seen; pose is always `(0,0,0)`, no metric map | — | **Unclear** — returns `ok:False unknown_target` **and** starts a scan as side effect; caller cannot tell which happened |
| T5 | `tools.py:154 _scan` | `scan already running` | `_scan_lock` / `_scanning` | — | Yes, but second `scan the room` looks refused |
| A1 | `agent.py:342-345 _local_intent` | TypeSafe `confidence<0.72` or `intent==unknown` or `needs_confirm>=0.7` or no key | abstain | `0.72 / 0.7` (`agent.py:342-343`) | **No-reject** (falls to LLM); but adds 8 s OpenRouter block before LLM 30 s block |
| A2 | `agent.py:260-270 _handle` | `llm.plan` returns `(None,err)` | LLM down / timeout / bad HTTP | `timeout_s:30` | Emits `LLM_UNAVAILABLE`, stops, task `FAILED` — correct fail-safe, but `last_say` buries reason in UI `agent-say` |
| A3 | `agent.py:288-300 _handle` | `plan.failed` or `done and no tools` or else | LLM `task.fail` / empty plan | — | `task.fail` is the **closest thing to `CommandReject` the LLM can express**, and it is indistinguishable in UI from safety reject |
| P1 | `navigation.py:102-125 _compute` | `close && mode not in (left,right,backup,stop,idle,scan)` | recovery steals `forward/explore/follow` | `blocked \|\| front<0.18` | Yes, but `go forward a little` becomes a spin; human asked translate, robot rotates |
| P2 | `navigation.py:176-192 _follow` | no `target` and no `person` | `follow` / `go_to` with no visible person | — | **Silent `(0,0)`** — no error, no event; worst no-op in the stack |
| K1 | `agent.py:313-330 _keyword_intent` | paraphrase miss | exact/prefix/suffix match only; no stemming, no containment | — | **Over-narrow**: `move toward the chair`, `go through the open space`, `move closer to the target` never match → slow LLM path |

No path scores `CommandReject` against `FORWARD`. No logits, no ranking, no reject threshold exist. The audit premise (reject as competing action) does not apply.

---

## 3. Prompt audit (exact text)

Full `SYSTEM_PROMPT` is `jetbot_core/llm.py:16-45` (44 lines). Verbatim operative sentences:

- `"You are NOT the realtime controller. Never invent PWM, GPIO, or raw motor values."`
- `"The camera is bolted to the chassis. ... To face a new direction, use motion.left / motion.right."`
- `"Reply with ONE JSON object only. No markdown fences. No extra text."`
- `"If blocked, prefer a turn or scan, not forcing forward."`
- `"If you do not know, say so and use camera.scan_environment."`

Counts in prompt + schema + `ALLOWED_TOOLS` (`llm.py:47-54`):

| Token | Count | Note |
|---|---|---|
| `reject` / `CommandReject` / `refuse` / `cannot` / `never assume` / `must be certain` / `ambiguous` / `uncertain` | **0** | no explicit reject instruction anywhere |
| `uncertain`-adjacent | 1 (`"If you do not know"`) | maps to **scan**, not reject |
| `blocked` | 1 | maps to turn/scan |
| `fail` | 2 (`task.complete / task.fail`, `"failed": false`) | only reject-like tool; schema gives no criteria for when to use it |
| `scan` | 3 (tool list + do-not-know + blocked fallback) | most-reinforced action |
| `stop` | 1 (tool list) | no "prefer stop when unsure" language |

Few-shot examples: **none**. Tool descriptions: one line each, no preconditions (`duration 0.2-3`, `follow if visible else scan`). Output format: strict JSON, `tools<=4` enforced in code not prompt (`llm.py:263` slices `[:4]`).

Semantic effect: the prompt teaches `uncertainty → scan`, and code reinforces it twice (`_ensure_usable_plan` forces scan on `blocked` or `"block"` in text, and on any bad parse with no tools). This is the opposite of `uncertainty → reject`. If the robot appears unwilling, it is not because the prompt says "be careful"; it is because scan / stop / `(0,0)` are the only things the downstream layers let through, and they look like doing nothing.

A separate prompt defect matters more than bias: the model is never told the wheels may be **locked**. `summary_for_llm` (`world.py:126-171`) sends `estop, motors backend, obstacle, nav, perception stale, objects` but **not** `allow_motion`, not `recovery_active`, not `turn_obstacle_m`. The LLM therefore plans `motion.forward` in exactly the state where `safety.py:137` guarantees `motion_locked`. The Agent then reports `"OK: forward"` (`agent.py:241`) while Safety emits `CommandRejected`. That single missing line explains a large share of "reasonable command, agent said OK, nothing moved, UI shows CommandRejected".

---

## 4. Human-vs-Agent safety test (10 commands)

Assumes defaults: `allow_motion=false`, camera working, `front=1.5 m`, no block, LLM reachable. Change one fact per row where noted.

| # | Command | Human expects | Agent likely does | Allowed? | Could look rejected? | Why | Justified? | Sufficient World State |
|---|---|---|---|---|---|---|---|---|
| 1 | `go forward a little` | creep ~0.5 m | keyword `forward` → `motion.forward(1.0)` → planner `forward` → safety pass → moves **iff unlocked**; else `motion_locked` | yes if unlocked | **yes (default)** — `R2` | default lock; agent still says OK | latch yes, UX no | `allow_motion` in prompt + UI pill already exists but plan ignores it |
| 2 | `turn right` | yaw in place | `motion.right(0.6)` → honored even when `close` (`navigation.py:101`) → moves unless `front<=0.06` | yes | only if `front<=0.06` (`R5`) or locked/stale | nearly touching or stale | yes | `front_m` + freshness already sent |
| 3 | `turn left` | yaw in place | same as 2 | yes | same as 2 | same | yes | same |
| 4 | `move toward the chair` | approach visible chair | **no keyword** → TypeSafe (8 s, likely `unknown`/low-conf) → LLM (30 s) → `nav.go_to{target:chair}`? `objects_view` keys are `obj_01`, not `chair` → `unknown_target` + stray scan | no (name ≠ id) | yes — `T4` + 38 s silence | detector emits class `object`/`person` only (`detect.py`), tracker ids `obj_NN`; LLM cannot guess id from class | no — naming gap, not danger | object list with `id+class` is sent, but no alias resolution |
| 5 | `go to the table` | drive to table | same as 4; no table landmark, `SpatialMemory` has pose `None` | no | yes — `T4` | same naming + no metric map | partially (honest fail, wrong error) | need `unknown_target` surfaced as say, not `ok:False` buried in decisions |
| 6 | `look around` / `scan the room` | survey | keyword `scan` → `camera.scan_environment` → halt + body yaw survey (`tools.py:212-224`) | yes | only if scan already running (`T5`) or estop (`T1`) | lock is per-scan | yes | — |
| 7 | `stop` | halt now | keyword `stop` → `motion.stop` (bypasses `T2` recovery gate) → `planner.halt` + zero cmd → task `COMPLETED` | yes | no | — | — | — |
| 8 | `move closer to the target` | creep toward current target | **no keyword** (`closer` not in map) → LLM → likely `motion.forward` without target grounding | yes if unlocked+clear | yes if locked/stale/blocked | same as 1 + vague reference | no — vagueness should narrow to safest feasible (short forward), not stall 38 s | need target id in `nav.current_target` (sent) but LLM not told to prefer it |
| 9 | `go through the open space` | corridor follow | **no keyword** → LLM → likely `motion.explore` → `_corridor_speeds` on bins | yes if clear | yes if any `close` → recovery spin (`P1`) | center bin occupancy maps to slow/stop | yes if truly blocked; no if heuristic false-positive on dark floor | bins are **not** in LLM summary (only `front/blocked/dir`), so LLM cannot pick the open side |
| 10 | `go forward a little` **while blocked** | human would turn first | keyword `forward` → planner steals into `recovery` spin; safety would have rejected forward anyway | no (forward) | yes — `P1+R4` | correct arbitration, wrong expectation | **yes** — the one case where "reject forward" is right; UI should say `recovery` not `CommandRejected` | `nav.mode=recovery` is sent; say text does not use it |

---

## 5. Collapsed states

| Collapsed pair | Where | Evidence | Effect |
|---|---|---|---|
| `motion_locked` = `estop` = `obstacle` | `safety.py:133,143,154` all emit `COMMAND_REJECTED`; `app.js:89` all render `bad` | same event type, same red pill | charging-latch looks like danger refusal |
| `unknown` (TypeSafe) = "ask LLM" = 38 s stall | `agent.py:342-346` | `unknown` and low-conf and `needs_confirm` all → `None` → full LLM path | ambiguous text pays the same latency as complex planning |
| `STALE` = `blocked` for forward | `safety.py:181` rejects forward on stale even with `front=1.5 m` last-known | `freshness_dict` (`world.py:173-181`): `STALE` if `depth_age>0.9` | camera stall reads as wall |
| `PROBABLE`/`UNKNOWN`/`STALE` object = absent for `go_to` | `tools.py:260-268` requires exact id in `objects_view`; `world.py:275-287` marks stale then deletes | no alias, no "last seen over there" | `move toward the chair` fails even with a `PROBABLE` chair in memory |
| `INVALID` (bad tool) = dropped silently | `llm.py:270-277` `continue` on non-camera unknown tools | no event, no say | LLM thinks it acted; nothing dispatched |
| `FAILED` (task) = rejected (safety) in UI | `TASK_FAILED` and `CommandRejected` both `bad` (`app.js:87-88`); `agent.py:283-287` sets `FAILED` on `plan.failed` | same color, adjacent timeline | operator cannot tell "LLM gave up" from "wheels refused" |
| `EXECUTING` forever = in-progress | `agent.py:293-300` moves to `EXECUTING` after LLM tools; only `stop`/`status`/`done-no-tools` complete | local `forward` never completes | next command inherits stale `REPLANNING`/`EXECUTING` context |

`UNKNOWN / UNCERTAIN / AMBIGUOUS / UNSAFE / IMPOSSIBLE / INVALID / UNSUPPORTED / REJECT` are **not** distinguished anywhere in the decision path. There is no enum, no threshold, and no mapping — which is precisely why every stall surfaces under the same red `CommandRejected`/`TaskFailed` banner.

---

## 6. `CommandReject` as a decision — verdict: **safety gate, not competing action**

Evidence:

- No `CommandReject` string in `ALLOWED_TOOLS`, `INTENT_TOOLS`, `SYSTEM_PROMPT`, or `Agent`. `grep CommandReject` hits only `app.js:89` (CSS class map) and the `CommandRejected` event.
- No scoring/ranking/logits anywhere. Intent is keyword exact-match (`agent.py:328`) or TypeSafe choice-then-threshold (`agent.py:342-343`), then single-plan LLM. Nothing competes; first hit wins.
- Thresholds that exist gate **abstention**, not rejection: TypeSafe `0.72/0.7` → fall through to LLM, never refuse.
- The only LLM-native refusal is `task.fail` (`tools.py:291-298` → task `FAILED`), which halts the planner. It has no criteria, no threshold, and no UI distinction from a safety veto.
- Structural bias therefore is not "reject outscores forward". It is **accept-then-veto**: Agent says `OK`, Safety says no, UI shows only the veto. Sections 2 (R2/R3/T2/P2) and 4 (#1,4,8) are the instances.

---

## 7. Safety vs decision responsibility

| Question | Current owner | Correct owner | Conflict? |
|---|---|---|---|
| "What does the user want?" | Agent keywords → TypeSafe → LLM (`agent.py:332-346,255`) | Agent | none, but paraphrase coverage (K1) is so narrow the LLM pays for simple synonyms |
| "Is the command supported?" | Nobody: unknown tool → `ok:False` in `tools.py:69`, swallowed by `agent.py:235-242` | Agent (validate before dispatch; say what is supported) | **yes** — LLM never learns its tool was dropped |
| "Is it physically safe now?" | Safety `validate` + planner recovery (`safety.py:162-203`, `navigation.py:102-125`) | Safety/planner | none — correct placement |
| "Are wheels allowed at all?" | Safety latch (`safety.py:137`), invisible to planner and LLM | Safety enforces; **Agent must know** | **yes** — `allow_motion` missing from `summary_for_llm`; plan-then-veto guaranteed |
| "Blocked → what now?" | Planner recovers, Agent replans only on `RECOVERY_FAILED` (`agent.py:134-145`) | as built | none — correct split, but UI copy still says rejected instead of recovering |
| Servo/pose grounding | `tools.py:139-151` + fixed-servo no-op | Tools | none since fixed-camera rewrite; LLM prompt correctly updated (`llm.py:18-21`) |

The Agent is **not** doing safety reasoning it shouldn't. If anything it does too little validity reasoning: it never pre-checks `estop / allow_motion / blocked / recovery_active / unknown_target` before dispatch, so every predictable veto arrives as a surprise event instead of a sentence.

---

## 8. World State audit (what the LLM actually sees)

`summary_for_llm` (`world.py:126-171`, ~7 lines, fits `n_ctx=2048`):

```text
task: <name> state=<S> action=<a>
robot: pose=(0.00,0.00,yaw=0.00) v=<v> estop=<b> motors=<backend> camera=<backend> pan=0 tilt=0
obstacle: front=<m|unknown> blocked=<b> dir=<d> conf=<c> certainty=<KNOWN|UNKNOWN|STALE>
nav: mode=<m> target=<id> blocked=<b>
perception: fps=.. depth_fps=.. detect_fps=.. stale=<bool>
objects: id(class,c=..,CERTAINTY), ... | objects: none
[uncertainty: ...]  (always empty — set_uncertainty has no writers)
```

Adequate: `blocked/front/stale/estop/mode/objects` are present and tiny. Defective:

1. **Missing `allow_motion`** — the #1 veto is invisible (see §3).
2. **Missing `recovery_active`** — `nav.mode=recovery` is sent, but nothing tells the model "do not send motion until recovered".
3. **Missing bins** — `go through the open space` cannot be grounded; only center `front` is sent while planner steers on 5 bins (`navigation.py:162-174`).
4. **Missing name→id alias** — `objects` lists `obj_01(object,c=0.6)`; user says "chair". `go_to` needs exact id (`tools.py:261`). No class→id map is offered.
5. **`pose` is constant `(0,0,0)`** — correctly useless (no odometry), but its presence invites the LLM to believe metric `go_to` works.
6. **`uncertainty` always empty** — `set_uncertainty` (`world.py:313`) has no callers; the `UNKNOWN/PROBABLE` vocabulary exists but is never populated, so the model cannot distinguish "unseen" from "seen-uncertain".
7. **Depth confidence is decorative** — `0.45+0.4*|center-0.3|` (`depth.py:82-83`) never reflects illumination, motion blur, or floor texture; planner and safety correctly ignore it, but the summary prints it as `conf=`.

Representation is not too large; it is too narrow in exactly the three fields (`allow_motion`, `recovery`, `bins/alias`) that would let the Agent pick the safest feasible action first.

---

## 9. Five concrete failure cases

### Case 1 — Default table lock looks like refusal
```text
Command:  go forward a little (fresh boot, robot on table)
Expected: creep, or "wheels locked — unlock to drive"
Actual:   Agent says "OK: forward"; Safety emits CommandRejected(motion_locked); wheels silent
Relevant code: agent.py:235-242 (ignores dispatch ok) + safety.py:137-149 + config.yaml:8
Why: allow_motion=false by default and invisible to LLM (world.py:126-171)
Root cause: missing validity pre-check + missing allow_motion in prompt context
Severity: HIGH — every first-run motion command fails this way
```

### Case 2 — Paraphrase pays 38 s then scans
```text
Command:  move toward the chair (chair visible as obj_03)
Expected: short forward / follow obj_03
Actual:   keyword miss → TypeSafe ~8 s (likely unknown) → LLM ~30 s → nav.go_to{target:chair}
          → ok:False unknown_target (+ stray scan side effect)
Relevant code: agent.py:313-330,342-346 + typesafe_client.py:105-153 + tools.py:258-268
Why: K1 exact-match + T4 exact-id + no class→id alias
Root cause: tool/schema grounding (names ≠ ids)
Severity: HIGH — the canonical "reasonable" command always fails
```

### Case 3 — Stale camera vetoes a clear floor
```text
Command:  go forward a little (camera stalled 1.2 s, last front=1.5 m)
Expected: creep, or "camera stale — holding"
Actual:   "OK: forward" then CommandRejected(stale_perception)
Relevant code: safety.py:181-183 + world.py:173-181 (STALE>0.9 s)
Why: R3 treats unknown as blocked for forward (correct fail-safe, wrong message)
Root cause: safety policy correct; Agent message wrong (claims OK before validate)
Severity: MEDIUM — safe behavior, misreported as agent refusal
```

### Case 4 — Recovery gate vetoes the replan it triggered
```text
Command:  (named explore) Path blocked → recovery spin → Agent replan "turn left"
Expected: turn after recovery clears
Actual:   tools.dispatch(motion.left) → ok:False recovery_active; Agent still records OK + plan
Relevant code: tools.py:46 + navigation.py:65-67,102-125 + agent.py:276-279 (no ok check)
Why: T2 gate is correct (one driver) but Agent plans motion without checking in_recovery
Root cause: planner/agent interface — no pre-check, no wait/retry, result ignored
Severity: MEDIUM — recovery works, replan silently dropped
```

### Case 5 — Follow nobody = silent standstill
```text
Command:  follow me (no person in frame; contour detector emits class "object")
Expected: "I don't see a person — scanning" or approach obj_NN
Actual:   nav.follow accepted; _follow returns (0,0); no event, no say change beyond "OK: follow"
Relevant code: navigation.py:176-192 + tools.py:270-275 + detect.py (contour→"object", Haar→"person" only if cascade present)
Why: P2 silent (0,0); detector rarely emits "person" on Nano without cascade/engine
Root cause: world-state interpretation (class vocabulary) + silent no-op
Severity: MEDIUM — looks like the agent is "unwilling to follow"
```

---

## 10. Prompt bias diagnosis

**Severity: LOW** (as a cause of over-rejection).

- Exact instruction causing bias: none. There is no "reject if unsure / must be certain / never assume" sentence. The closest — `"If you do not know, say so and use camera.scan_environment"` — biases toward a low-risk **action**, not refusal.
- Why an LLM might still read conservatively: `task.fail` exists in the schema with zero criteria, and `"prefer a turn or scan, not forcing forward"` plus the forced-scan repair (`agent.py:378-388`) make `scan` the attractor for any `blocked`/truncated/uncertain input. A log reader sees `scan, scan, scan` and infers timidity, but the mechanism is fallback, not fear.
- Examples reinforce bias: no few-shots, so nothing to reinforce either way.
- Schema reinforces bias: mildly — `failed` boolean with no definition invites the model to set `failed:true` for "I can't fully satisfy this" (e.g. unknown chair) instead of partial progress (approach nearest blob).
- Guarantee safety: the prompt does **not** ask the model to guarantee safety. Safety is correctly delegated (`Never invent PWM... NOT the realtime controller`).
- Reasonable assumptions discouraged: mildly — with no alias map and no `allow_motion` context, the model cannot assume "chair≈obj_03" or "wheels free", so it guesses tool names that downstream drops.

Prompt is at most a contributor via **omission** (missing latch/recovery/alias context, undefined `task.fail` criteria), not commission.

---

## 11. Prompt principles (no rewrite yet)

1. **State the latch first.** `wheels: LOCKED/FREE` as line 1 of World. Fixes Case 1: model says "unlock first" instead of planning motion into a guaranteed veto.
2. **Reject only invalid/impossible/unsupported; name the category.** Define `task.fail` criteria (`unknown_target`, `unsupported`, `needs_person_not_visible`). Fixes UI conflation (§5) and gives Case 2/5 an honest sentence instead of a red veto.
3. **Prefer the safest feasible action, and say the substitution.** e.g. blocked-forward → in-place turn; `look left` → body yaw (already true in code, absent from old mental model). Fixes Case 4 perception ("I turned instead of ramming" vs "rejected").
4. **Let Safety validate physics; Agent validates meaning.** Forbid the model from second-guessing `front_m`; require it to send the best-intent action and let Safety veto. Already true in code — write it down so future prompts don't add "only act when certain".
5. **No certainty threshold for ordinary motion.** Short forward/turn with fresh perception and no block is always sendable. Fixes any temptation to add `confidence>=X` to the prompt (no such threshold exists today — keep it that way).
6. **Ground references before acting.** If user names a class ("chair") and multiple ids exist, rule: pick highest-confidence id, or scan once then pick. Fixes Case 2 without code: needs the alias line in World (§8.4).
7. **One honest sentence on every veto.** If dispatch returns `ok:False`, the say text must carry `reason` (estop/locked/stale/blocked/recovery/unknown_target). Fixes the accept-then-veto pattern across Cases 1–4. This is an Agent `_handle` rule, not model prose.

---

## 12. Final diagnosis

### Root cause ranking (hypotheses)

```text
Safety/policy + default latch visibility:  30%
  motion_locked default + stale veto + obstacle veto all surface as CommandRejected,
  and allow_motion is missing from the LLM context.

Tool/schema grounding (names≠ids, silent drops, go_to side effect): 25%
  move toward X / go to Y cannot succeed; unknown tools vanish; follow-nobody is (0,0).

Decision architecture (accept-then-veto, ignored ok, EXECUTING forever): 20%
  Agent claims OK before validate, ignores dispatch results, never completes local tasks.

World State completeness (no latch/recovery/bins/alias, empty uncertainty): 12%
  narrow summary forces the model to guess the three fields that decide success.

Prompt bias: 8%
  LOW — no reject instruction; only omissions (undefined task.fail, missing latch context).

Thresholds (TypeSafe 0.72/0.7, keyword exact-match): 5%
  cause latency (8 s + 30 s), not refusal; TypeSafe abstains, never rejects.
```

### Most important finding

**The Agent does not over-select `CommandReject` — it over-promises and Safety over-reports.** `Agent._handle` says `"OK: <intent>"` before or regardless of validation (`agent.py:241`, `tools.py:41-80` results ignored), while `SafetyController` emits the same `CommandRejected` event for a charging latch, a stalled camera, and a real wall (`safety.py:133,143,154`). A human watching the console sees one red veto for three unrelated causes and attributes it to an unwilling Agent. The single highest-leverage fact: with stock `config.yaml` (`allow_motion:false`) the robot **cannot** honor any motion command, and the LLM is never told.

### Minimal fix recommendation

Without redesign, in this order:

1. Add `allow_motion` (and `recovery_active`) to `summary_for_llm` (`world.py:126-171`) + one prompt line: `"If wheels are LOCKED, say so and do not plan motion."` — kills Case 1 and the dominant veto.
2. Check `dispatch` results in `Agent._handle` (`agent.py:235-242,276-279`): if `ok:False`, set `last_say` from `result.error` (`motion_locked / stale_perception / obstacle / recovery_active / unknown_target`) instead of `"OK"`. Surfaces §5 distinctions with zero architecture change.
3. Resolve class→id for `go_to`/`follow` (prefer exact id, else highest-confidence same-class object, else honest `task.fail: unknown_target` with no stray scan). Fixes Case 2/5.
4. Keep prompt as-is otherwise. Do not add certainty language, a second planner, or a reject action — the current `uncertainty → scan` + deterministic Safety veto is the correct split; it only needs honest reporting.

---

## Appendix — files consulted

`jetbot_core/agent.py`, `jetbot_core/llm.py`, `jetbot_core/tools.py`, `jetbot_core/safety.py`, `jetbot_core/navigation.py`, `jetbot_core/world.py`, `jetbot_core/task.py`, `jetbot_core/typesafe_client.py`, `jetbot_core/events.py`, `jetbot_core/runtime.py`, `jetbot_core/web.py`, `jetbot_core/perception/depth.py`, `jetbot_core/perception/pipeline.py`, `jetbot_core/perception/detect.py`, `config.yaml`, `web/static/app.js`, `docs/architecture.md`, `docs/pipeline_audit.md`, `docs/implementation_plan.md`.

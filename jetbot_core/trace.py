"""Bounded pipeline tracer for the monitor. Not on the safety path."""

from __future__ import print_function

import json
import threading
import time
import uuid

from .telemetry import get_corr_id, redact


def _clip(value, limit=3500):
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return "<bytes %d>" % len(value)
    if isinstance(value, (dict, list, tuple)):
        value = redact(value)
        try:
            text = json.dumps(value, default=str)
        except Exception:
            text = str(value)
        if len(text) <= limit:
            return value
        return {"_truncated": True, "preview": text[:limit], "chars": len(text)}
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "…[%d chars]" % len(text)


class TraceRecorder(object):
    """Thread-local current run + bounded history of pipeline steps."""

    def __init__(self, max_runs=80, max_steps=48):
        self._lock = threading.RLock()
        self._local = threading.local()
        self._runs = []
        self._max_runs = int(max_runs)
        self._max_steps = int(max_steps)
        self._seq = 0
        self._active = None
        self.last_asr = None
        self.last_llm = None
        self.last_tool = None

    def begin_run(self, source, text, kind="command", corr_id=None):
        run = {
            "id": uuid.uuid4().hex[:10],
            "ts": time.time(),
            "source": source or "unknown",
            "kind": kind,
            "input": (text or "")[:400],
            "status": "running",
            "say": "",
            "model": None,
            "route": None,
            "steps": [],
            "ended_ts": None,
            "corr_id": corr_id or get_corr_id(),
        }
        with self._lock:
            self._seq += 1
            run["seq"] = self._seq
            self._runs.append(run)
            if len(self._runs) > self._max_runs:
                self._runs = self._runs[-self._max_runs:]
            self._active = run["id"]
        self._local.run_id = run["id"]
        self.step("input", source or "user", input={"text": text, "source": source})
        return run["id"]

    def current_run_id(self):
        return getattr(self._local, "run_id", None)

    def step(self, stage, name, input=None, output=None, ok=None, error=None,
             model=None, latency_s=None, extra=None, source=None):
        run_id = self.current_run_id()
        orphan = False
        if not run_id:
            # Standalone observation (ASR, pad tool) — one-shot run, no extra input step.
            run = {
                "id": uuid.uuid4().hex[:10],
                "ts": time.time(),
                "source": source or stage,
                "kind": stage,
                "input": str(name or stage)[:400],
                "status": "running",
                "say": "",
                "model": None,
                "route": None,
                "steps": [],
                "ended_ts": None,
                "corr_id": get_corr_id(),
            }
            with self._lock:
                self._seq += 1
                run["seq"] = self._seq
                self._runs.append(run)
                if len(self._runs) > self._max_runs:
                    self._runs = self._runs[-self._max_runs:]
                self._active = run["id"]
            self._local.run_id = run["id"]
            run_id = run["id"]
            orphan = True
        item = {
            "ts": time.time(),
            "stage": stage,
            "name": name,
            "ok": ok,
            "error": error,
            "model": model,
            "latency_s": None if latency_s is None else round(float(latency_s), 4),
            "input": _clip(input, 3500),
            "output": _clip(output, 3500),
            "extra": extra or {},
            "corr_id": get_corr_id() or (extra or {}).get("corr_id"),
        }
        with self._lock:
            self._seq += 1
            item["seq"] = self._seq
            run = self._find(run_id)
            if run is not None:
                run["steps"].append(item)
                if len(run["steps"]) > self._max_steps:
                    run["steps"] = run["steps"][-self._max_steps:]
                if model:
                    run["model"] = model
                if stage in ("intent", "route"):
                    run["route"] = name
                if stage == "say" and output:
                    run["say"] = str(output)[:240]
            if stage == "asr":
                self.last_asr = item
            elif stage == "llm":
                self.last_llm = item
            elif stage == "tool":
                self.last_tool = item
        if orphan:
            self.end_run(status="ok" if ok is not False else "error",
                         say=str(output)[:240] if output and not isinstance(output, (dict, list)) else "",
                         error=error)
        return item

    def end_run(self, status="ok", say="", error=None):
        run_id = self.current_run_id()
        with self._lock:
            run = self._find(run_id) if run_id else None
            if run is not None and run.get("status") == "running":
                run["status"] = status or "ok"
                run["ended_ts"] = time.time()
                if say:
                    run["say"] = str(say)[:240]
                if error:
                    run["error"] = str(error)[:240]
            if self._active == run_id:
                self._active = None
        try:
            del self._local.run_id
        except Exception:
            self._local.run_id = None
        return run_id

    def _find(self, run_id):
        if not run_id:
            return None
        for run in reversed(self._runs):
            if run.get("id") == run_id:
                return run
        return None

    def snapshot(self, limit=40, after_seq=0):
        with self._lock:
            runs = list(self._runs)
            seq = self._seq
            active = self._active
            last_asr = self.last_asr
            last_llm = self.last_llm
            last_tool = self.last_tool
        if after_seq:
            runs = [r for r in runs if r.get("seq", 0) > after_seq]
        if limit:
            runs = runs[-int(limit):]
        return {
            "seq": seq,
            "active": active,
            "runs": list(reversed(runs)),
            "last_asr": last_asr,
            "last_llm": last_llm,
            "last_tool": last_tool,
        }

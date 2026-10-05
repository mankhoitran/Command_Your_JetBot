"""Task lifecycle. The Task Manager owns this, not the LLM."""

from __future__ import print_function

import threading
import time
import uuid

from .events import EventType

IDLE = "IDLE"
PLANNING = "PLANNING"
EXECUTING = "EXECUTING"
OBSERVING = "OBSERVING"
VERIFYING = "VERIFYING"
REPLANNING = "REPLANNING"
ACTIVE_PERCEPTION = "ACTIVE_PERCEPTION"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
PAUSED = "PAUSED"

LEGAL = {
    IDLE: (PLANNING, CANCELLED),
    PLANNING: (EXECUTING, ACTIVE_PERCEPTION, COMPLETED, FAILED, CANCELLED, IDLE),
    EXECUTING: (OBSERVING, VERIFYING, REPLANNING, ACTIVE_PERCEPTION, COMPLETED, FAILED, CANCELLED, PAUSED),
    OBSERVING: (PLANNING, EXECUTING, VERIFYING, ACTIVE_PERCEPTION, FAILED, CANCELLED),
    VERIFYING: (COMPLETED, REPLANNING, EXECUTING, FAILED, CANCELLED),
    REPLANNING: (EXECUTING, ACTIVE_PERCEPTION, FAILED, CANCELLED, IDLE),
    ACTIVE_PERCEPTION: (PLANNING, EXECUTING, OBSERVING, FAILED, CANCELLED),
    PAUSED: (EXECUTING, CANCELLED, IDLE),
    COMPLETED: (IDLE, PLANNING),
    FAILED: (IDLE, PLANNING),
    CANCELLED: (IDLE, PLANNING),
}


class TaskManager(object):
    def __init__(self, world, bus):
        self.world = world
        self.bus = bus
        self._lock = threading.Lock()
        self.state = IDLE
        self.task_id = None
        self.name = None
        self.goal = None
        self.action = None
        self.progress = 0.0
        self.failure_reason = None
        self.history = []
        self.updated_at = time.time()

    def _set(self, state, action=None, progress=None, failure=None):
        prev = self.state
        if state != prev and state not in LEGAL.get(prev, ()):
            raise ValueError("illegal task transition %s -> %s" % (prev, state))
        self.state = state
        if action is not None:
            self.action = action
        if progress is not None:
            self.progress = float(progress)
        if failure is not None:
            self.failure_reason = failure
        self.updated_at = time.time()
        self.history.append({"t": self.updated_at, "from": prev, "to": state, "action": self.action})
        if len(self.history) > 80:
            self.history = self.history[-80:]
        self.world.set_task(
            id=self.task_id, name=self.name, state=self.state,
            action=self.action, progress=self.progress,
            failure_reason=self.failure_reason,
        )

    def start(self, name, goal=None):
        with self._lock:
            if self.state not in (IDLE, COMPLETED, FAILED, CANCELLED):
                self._force_idle()
            self.task_id = uuid.uuid4().hex[:8]
            self.name = name
            self.goal = goal or name
            self.failure_reason = None
            self.progress = 0.0
            self.state = IDLE
            self._set(PLANNING, action="understand", progress=0.05)
        self.bus.emit(EventType.TASK_STARTED, "task", {"id": self.task_id, "name": name})
        return self.task_id

    def _force_idle(self):
        self.state = IDLE
        self.task_id = None
        self.name = None
        self.action = None
        self.progress = 0.0

    def note_action(self, action, progress=None):
        """Record a pad/tool action without forcing a lifecycle jump."""
        with self._lock:
            self.action = action
            if progress is not None:
                self.progress = float(progress)
            self.updated_at = time.time()
            self.world.set_task(
                id=self.task_id, name=self.name, state=self.state,
                action=self.action, progress=self.progress,
                failure_reason=self.failure_reason,
            )
        return self.state

    def ensure_executing(self, action=None, progress=None):
        """Pad/manual tools may fire while IDLE; promote through PLANNING."""
        with self._lock:
            if self.state == IDLE:
                if not self.task_id:
                    self.task_id = uuid.uuid4().hex[:8]
                    self.name = action or "manual"
                    self.goal = self.name
                self._set(PLANNING, action=action or "manual", progress=0.05)
            if self.state != EXECUTING:
                try:
                    self._set(EXECUTING, action=action, progress=progress)
                except ValueError:
                    if action is not None:
                        self.action = action
                    self.updated_at = time.time()
                    self.world.set_task(
                        id=self.task_id, name=self.name, state=self.state,
                        action=self.action, progress=self.progress,
                        failure_reason=self.failure_reason,
                    )
                    return self.state
            elif action is not None:
                self.action = action
                if progress is not None:
                    self.progress = float(progress)
                self.updated_at = time.time()
                self.world.set_task(
                    id=self.task_id, name=self.name, state=self.state,
                    action=self.action, progress=self.progress,
                    failure_reason=self.failure_reason,
                )
        return self.state

    def transition(self, state, action=None, progress=None, failure=None):
        with self._lock:
            self._set(state, action=action, progress=progress, failure=failure)
        if state == COMPLETED:
            self.bus.emit(EventType.TASK_COMPLETED, "task", {"id": self.task_id, "name": self.name})
        elif state == FAILED:
            self.bus.emit(EventType.TASK_FAILED, "task", {
                "id": self.task_id, "name": self.name, "reason": failure,
            })
        elif state == CANCELLED:
            self.bus.emit(EventType.TASK_CANCELLED, "task", {"id": self.task_id})
        elif state == PAUSED:
            self.bus.emit(EventType.TASK_PAUSED, "task", {"id": self.task_id})
        elif state == REPLANNING:
            pass
        return self.state

    def cancel(self, reason="user"):
        with self._lock:
            if self.state in (COMPLETED, CANCELLED, IDLE):
                self._force_idle()
                self.world.set_task(id=None, name=None, state=IDLE, action=None, progress=0.0, failure_reason=None)
                return
            try:
                self._set(CANCELLED, action="cancel", failure=reason)
            except ValueError:
                self.state = CANCELLED
                self.failure_reason = reason
                self.world.set_task(state=CANCELLED, failure_reason=reason)
        self.bus.emit(EventType.TASK_CANCELLED, "task", {"reason": reason})

    def snapshot(self):
        with self._lock:
            return {
                "id": self.task_id,
                "name": self.name,
                "goal": self.goal,
                "state": self.state,
                "action": self.action,
                "progress": self.progress,
                "failure_reason": self.failure_reason,
                "history": list(self.history[-12:]),
            }

"""Typed in-process event bus. Safety-critical events never depend on the Agent."""

from __future__ import print_function

import threading
import time
import uuid

try:
    import enum
except ImportError:
    enum = None


class EventType(object):
    OBJECT_DETECTED = "ObjectDetected"
    OBJECT_UPDATED = "ObjectUpdated"
    OBJECT_LOST = "ObjectLost"
    OBSTACLE_DETECTED = "ObstacleDetected"
    OBSTACLE_CLEARED = "ObstacleCleared"
    WORLD_CHANGED = "WorldChanged"
    SCAN_REQUESTED = "ScanRequested"
    SCAN_COMPLETED = "ScanCompleted"
    TARGET_FOUND = "TargetFound"
    TARGET_LOST = "TargetLost"
    LOW_CONFIDENCE = "LowConfidence"
    UNCERTAINTY_DETECTED = "UncertaintyDetected"
    TASK_STARTED = "TaskStarted"
    TASK_PAUSED = "TaskPaused"
    TASK_COMPLETED = "TaskCompleted"
    TASK_FAILED = "TaskFailed"
    TASK_CANCELLED = "TaskCancelled"
    NAVIGATION_BLOCKED = "NavigationBlocked"
    NAVIGATION_RECOVERED = "NavigationRecovered"
    EMERGENCY_STOP = "EmergencyStop"
    COMMAND_REJECTED = "CommandRejected"
    PERCEPTION_STALE = "PerceptionStale"
    LLM_UNAVAILABLE = "LlmUnavailable"
    WHISPER_UNAVAILABLE = "WhisperUnavailable"
    MEMORY_UPDATED = "MemoryUpdated"
    HEALTH = "Health"
    USER_COMMAND = "UserCommand"
    VOICE_TRANSCRIPT = "VoiceTranscript"


class Event(object):
    __slots__ = ("id", "type", "timestamp", "source", "payload", "confidence")

    def __init__(self, event_type, source, payload=None, confidence=1.0, timestamp=None):
        self.id = uuid.uuid4().hex[:12]
        self.type = event_type
        self.timestamp = time.time() if timestamp is None else timestamp
        self.source = source
        self.payload = payload if payload is not None else {}
        self.confidence = float(confidence)

    def to_dict(self):
        return {
            "id": self.id,
            "type": self.type,
            "timestamp": self.timestamp,
            "source": self.source,
            "confidence": self.confidence,
            "payload": self.payload,
        }


class EventBus(object):
    """Synchronous dispatch under a lock, plus a bounded history for the UI."""

    def __init__(self, history=200):
        self._subs = {}
        self._any = []
        self._lock = threading.RLock()
        self._history = []
        self._history_limit = int(history)
        self._seq = 0

    def subscribe(self, event_type, callback):
        with self._lock:
            if event_type is None:
                self._any.append(callback)
            else:
                self._subs.setdefault(event_type, []).append(callback)
        return callback

    def unsubscribe(self, event_type, callback):
        with self._lock:
            if event_type is None:
                if callback in self._any:
                    self._any.remove(callback)
            else:
                listeners = self._subs.get(event_type, [])
                if callback in listeners:
                    listeners.remove(callback)

    def publish(self, event):
        if not isinstance(event, Event):
            raise TypeError("EventBus.publish expects Event, got %r" % type(event))
        with self._lock:
            self._seq += 1
            self._history.append(event)
            if len(self._history) > self._history_limit:
                self._history = self._history[-self._history_limit:]
            listeners = list(self._subs.get(event.type, [])) + list(self._any)
        errors = []
        for cb in listeners:
            try:
                cb(event)
            except Exception as exc:
                errors.append((cb, exc))
        return errors

    def emit(self, event_type, source, payload=None, confidence=1.0):
        event = Event(event_type, source, payload=payload, confidence=confidence)
        self.publish(event)
        return event

    def recent(self, limit=50, after_ts=0):
        with self._lock:
            items = [e for e in self._history if e.timestamp > after_ts]
        if limit:
            items = items[-int(limit):]
        return [e.to_dict() for e in items]

    @property
    def seq(self):
        with self._lock:
            return self._seq

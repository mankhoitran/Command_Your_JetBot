"""A-MEM-inspired semantic memory. JSON notes, lexical retrieval.

ChromaDB / sentence-transformers cannot run on this Python 3.6 Nano image.
The note schema, linking, and evolution policy follow A-MEM; embeddings do not.
Realtime obstacles never live here.
"""

from __future__ import print_function

import json
import logging
import os
import re
import threading
import time
import uuid

from .events import EventType

log = logging.getLogger("jetbot.memory")

_TOKEN = re.compile(r"[a-z0-9_]+")


def _tokens(text):
    return set(_TOKEN.findall((text or "").lower()))


class MemoryNote(object):
    def __init__(self, content, keywords=None, tags=None, context="",
                 location=None, source="agent", confidence=0.6, note_id=None,
                 links=None, timestamp=None):
        self.id = note_id or uuid.uuid4().hex[:10]
        self.content = content
        self.keywords = list(keywords or [])
        self.tags = list(tags or [])
        self.context = context or ""
        self.location = location
        self.source = source
        self.confidence = float(confidence)
        self.links = list(links or [])
        self.timestamp = timestamp or time.time()
        self.last_verified = self.timestamp
        self.hits = 0
        self.evolution = []

    def to_dict(self):
        return {
            "id": self.id,
            "content": self.content,
            "keywords": self.keywords,
            "tags": self.tags,
            "context": self.context,
            "location": self.location,
            "source": self.source,
            "confidence": self.confidence,
            "links": self.links,
            "timestamp": self.timestamp,
            "last_verified": self.last_verified,
            "hits": self.hits,
            "evolution": self.evolution[-6:],
        }

    @classmethod
    def from_dict(cls, data):
        note = cls(
            content=data.get("content", ""),
            keywords=data.get("keywords"),
            tags=data.get("tags"),
            context=data.get("context", ""),
            location=data.get("location"),
            source=data.get("source", "agent"),
            confidence=data.get("confidence", 0.6),
            note_id=data.get("id"),
            links=data.get("links"),
            timestamp=data.get("timestamp"),
        )
        note.last_verified = data.get("last_verified", note.timestamp)
        note.hits = int(data.get("hits", 0))
        note.evolution = list(data.get("evolution") or [])
        return note

    def blob(self):
        return " ".join([
            self.content or "",
            " ".join(self.keywords),
            " ".join(self.tags),
            self.context or "",
        ])


class SemanticMemory(object):
    MEANINGFUL_REASONS = (
        "new_object", "object_moved", "new_area", "target_found", "target_lost",
        "nav_failure", "nav_success", "scan_complete", "user_note", "confirmation",
    )

    def __init__(self, path, max_notes=400, retrieve_k=5, bus=None):
        self.path = path
        self.max_notes = int(max_notes)
        self.retrieve_k = int(retrieve_k)
        self.bus = bus
        self._lock = threading.RLock()
        self.notes = {}
        self._load()

    def _load(self):
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r") as handle:
                text = handle.read().strip()
            if not text:
                return
            raw = json.loads(text)
            for item in raw.get("notes", []):
                note = MemoryNote.from_dict(item)
                self.notes[note.id] = note
        except Exception as exc:
            log.warning("memory load failed: %s", exc)

    def _save(self):
        if not self.path:
            return
        directory = os.path.dirname(self.path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        payload = {"notes": [n.to_dict() for n in self.notes.values()]}
        tmp = self.path + ".tmp"
        with open(tmp, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.rename(tmp, self.path)

    def should_write(self, reason, content):
        if reason not in self.MEANINGFUL_REASONS:
            return False
        if not content or len(content.strip()) < 8:
            return False
        # Dedup near-identical recent notes
        tokens = _tokens(content)
        now = time.time()
        for note in self.notes.values():
            if now - note.timestamp > 120:
                continue
            overlap = len(tokens & _tokens(note.content))
            if overlap >= max(3, 0.8 * max(1, len(tokens))):
                note.last_verified = now
                note.hits += 1
                note.confidence = min(0.95, note.confidence + 0.05)
                return False
        return True

    def add_note(self, content, reason="user_note", keywords=None, tags=None,
                 context="", location=None, source="agent", confidence=0.6):
        with self._lock:
            if not self.should_write(reason, content):
                return None
            if not keywords:
                keywords = sorted(list(_tokens(content)))[:8]
            if not tags:
                tags = [reason]
            note = MemoryNote(
                content=content, keywords=keywords, tags=tags, context=context,
                location=location, source=source, confidence=confidence,
            )
            self._link_and_evolve(note)
            self.notes[note.id] = note
            if len(self.notes) > self.max_notes:
                oldest = sorted(self.notes.values(), key=lambda n: n.timestamp)[0]
                self.notes.pop(oldest.id, None)
            try:
                self._save()
            except Exception as exc:
                log.warning("memory save failed: %s", exc)
        if self.bus is not None:
            self.bus.emit(EventType.MEMORY_UPDATED, "memory", {
                "id": note.id, "reason": reason,
            }, confidence=confidence)
        return note.id

    def _link_and_evolve(self, note):
        tokens = _tokens(note.blob())
        scored = []
        for other in self.notes.values():
            overlap = tokens & _tokens(other.blob())
            if len(overlap) >= 2:
                scored.append((len(overlap), other))
        scored.sort(key=lambda t: -t[0])
        for _, other in scored[:4]:
            if other.id not in note.links:
                note.links.append(other.id)
            if note.id not in other.links:
                other.links.append(note.id)
            # Evolution: merge a keyword / tag if missing
            for kw in note.keywords:
                if kw not in other.keywords and len(other.keywords) < 12:
                    other.keywords.append(kw)
                    other.evolution.append({
                        "t": time.time(),
                        "from": note.id,
                        "change": "keyword:" + kw,
                    })

    def search(self, query, k=None):
        k = int(k or self.retrieve_k)
        q = _tokens(query)
        if not q:
            return []
        ranked = []
        with self._lock:
            for note in self.notes.values():
                blob = _tokens(note.blob())
                overlap = len(q & blob)
                if overlap <= 0:
                    continue
                recency = 1.0 / (1.0 + (time.time() - note.timestamp) / 3600.0)
                score = overlap + 0.3 * recency + 0.2 * note.confidence
                ranked.append((score, note))
        ranked.sort(key=lambda t: -t[0])
        out = []
        for score, note in ranked[:k]:
            d = note.to_dict()
            d["score"] = round(score, 3)
            note.hits += 1
            out.append(d)
        return out

    def recent(self, limit=12):
        with self._lock:
            notes = sorted(self.notes.values(), key=lambda n: n.timestamp, reverse=True)
            return [n.to_dict() for n in notes[:int(limit)]]

    def snapshot(self):
        with self._lock:
            return {"count": len(self.notes), "recent": self.recent(8)}

    def summary_for_llm(self, query, k=4):
        hits = self.search(query, k=k)
        if not hits:
            hits = self.recent(3)
        if not hits:
            return "memory: none"
        bits = []
        for h in hits:
            bits.append("- [%s c=%.2f] %s" % (h.get("id"), h.get("confidence", 0), h.get("content", "")[:160]))
        return "memory:\n" + "\n".join(bits)

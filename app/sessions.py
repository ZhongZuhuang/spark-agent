from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from pydantic_ai.messages import ModelMessage

from .config import SESSION_FILE

_HISTORY_ADAPTER: TypeAdapter[list[ModelMessage]] = TypeAdapter(list[ModelMessage])
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _blank() -> dict[str, Any]:
    return {
        "title": "新对话",
        "history": [],
        "messages": [],
        "plan": [],
        "updated_at": _now(),
    }


def _serialize_session(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": session.get("title") or "新对话",
        "history": _HISTORY_ADAPTER.dump_python(session.get("history", []), mode="json"),
        "messages": session.get("messages", []),
        "plan": session.get("plan", []),
        "updated_at": session.get("updated_at", _now()),
    }


def _deserialize_session(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": raw.get("title") or "新对话",
        "history": _HISTORY_ADAPTER.validate_python(raw.get("history", [])),
        "messages": raw.get("messages", []),
        "plan": raw.get("plan", []),
        "updated_at": raw.get("updated_at", _now()),
    }


def load_sessions() -> dict[str, dict[str, Any]]:
    with _LOCK:
        if not SESSION_FILE.exists():
            return {}
        try:
            raw = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return {}
            return {
                str(key): _deserialize_session(value)
                for key, value in raw.items()
                if isinstance(value, dict)
            }
        except Exception:
            backup = SESSION_FILE.with_suffix(".corrupt.json")
            try:
                SESSION_FILE.replace(backup)
            except OSError:
                pass
            return {}


def save_sessions(sessions: dict[str, dict[str, Any]]) -> None:
    with _LOCK:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {key: _serialize_session(value) for key, value in sessions.items()}
        tmp = SESSION_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(SESSION_FILE)


def get_or_create(
    sessions: dict[str, dict[str, Any]], session_id: str
) -> dict[str, Any]:
    with _LOCK:
        if session_id not in sessions:
            sessions[session_id] = _blank()
        return sessions[session_id]


def generate_title(text: str) -> str:
    clean = " ".join(str(text or "").strip().split())
    if not clean:
        return "新对话"
    return clean[:48] + ("…" if len(clean) > 48 else "")


def public_view(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": session.get("title") or "新对话",
        "messages": session.get("messages", []),
        "plan": session.get("plan", []),
        "updated_at": session.get("updated_at", _now()),
        "history_count": len(session.get("history", [])),
        "persisted": True,
    }


def context_view(session: dict[str, Any]) -> dict[str, Any]:
    history = session.get("history", [])
    return {
        "history": _HISTORY_ADAPTER.dump_python(history, mode="json"),
        "message_count": len(session.get("messages", [])),
        "history_count": len(history),
        "updated_at": session.get("updated_at", _now()),
        "storage": str(SESSION_FILE),
    }

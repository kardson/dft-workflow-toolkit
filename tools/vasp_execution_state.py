"""Stable execution-state names and durable local notification receipts."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Iterator


NOT_STARTED = "NOT_STARTED"
STARTING = "STARTING"
RUNNING = "RUNNING"
COMPLETED_PENDING_REVIEW = "COMPLETED_PENDING_REVIEW"
FAILED_OR_INCOMPLETE = "FAILED_OR_INCOMPLETE"
SUPERVISOR_ERROR = "SUPERVISOR_ERROR"
UNKNOWN = "UNKNOWN"

ACTIVE_STATES = frozenset({STARTING, RUNNING})
TERMINAL_STATES = frozenset({
    COMPLETED_PENDING_REVIEW,
    FAILED_OR_INCOMPLETE,
    SUPERVISOR_ERROR,
})
KNOWN_STATES = ACTIVE_STATES | TERMINAL_STATES | {NOT_STARTED, UNKNOWN}

# Preserve the old label in evidence, but expose one canonical machine value.
LEGACY_STATE_ALIASES = {
    "COMPLETED_PENDING_SOL_REVIEW": COMPLETED_PENDING_REVIEW,
}


def normalize_state(value: Any) -> tuple[str, str | None]:
    """Return (canonical, original alias); reject unknown labels fail-closed."""

    if not isinstance(value, str):
        raise ValueError("status must be a string")
    if value in KNOWN_STATES:
        return value, None
    canonical = LEGACY_STATE_ALIASES.get(value)
    if canonical is not None:
        return canonical, value
    raise ValueError(f"Unknown status: {value}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def notification_identity(
    task_id: str,
    execution_id: str,
    terminal_state: str,
) -> dict[str, str]:
    canonical, _alias = normalize_state(terminal_state)
    if canonical not in TERMINAL_STATES:
        raise ValueError("notification receipts require a terminal execution state")
    if not task_id.strip() or not execution_id.strip():
        raise ValueError("task_id and execution_id are required for durable dedupe")
    return {
        "task_id": task_id,
        "execution_id": execution_id,
        "terminal_state": canonical,
    }


def notification_receipt_path(
    ledger_dir: Path | str,
    task_id: str,
    execution_id: str,
    terminal_state: str,
) -> Path:
    identity = notification_identity(task_id, execution_id, terminal_state)
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return Path(ledger_dir) / f"{digest}.jsonl"


@contextmanager
def _receipt_lock(path: Path) -> Iterator[None]:
    """Serialize receipt transitions across watcher and popup processes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read_receipt_unlocked(receipt_path: Path) -> dict[str, Any]:
    if not receipt_path.is_file():
        return {
            "available": False, "state": UNKNOWN, "events": [],
            "user_acknowledged": False, "user_status_confirmed": False,
            "user_closed": False,
            "path": str(receipt_path),
        }
    events: list[dict[str, Any]] = []
    with receipt_path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid receipt JSON at line {line_number}: {error.msg}") from error
            if not isinstance(value, dict) or not isinstance(value.get("event"), str):
                raise ValueError(f"invalid receipt record at line {line_number}")
            events.append(value)
    event_names = [event["event"] for event in events]
    explicit_closed = any(
        event.get("event") == "USER_CLOSED_NOTIFICATION"
        and event.get("confirmation_method") in {
            "askokcancel-explicit-ok", "persistent-window-explicit-ok"}
        and event.get("dialog_result") == "ok"
        for event in events)
    explicit_confirmed = any(
        event.get("event") == "USER_CONFIRMED_STATUS"
        and event.get("confirmation_method") in {
            "askokcancel-explicit-ok", "persistent-window-explicit-ok"}
        and event.get("dialog_result") == "ok"
        for event in events)
    if explicit_closed:
        state = "USER_CLOSED_NOTIFICATION"
    elif explicit_confirmed:
        state = "USER_CONFIRMED_STATUS"
    elif any(name in {"USER_CONFIRMED_STATUS", "USER_CLOSED_NOTIFICATION"}
             for name in event_names):
        state = "USER_CONFIRMATION_CALLBACK_UNVERIFIED"
    else:
        state = next((name for name in reversed(event_names)
                      if name.endswith("ERROR") or name.endswith("FAILED")),
                     event_names[-1] if event_names else UNKNOWN)
    return {
        "available": True,
        "state": state,
        "events": events,
        "user_acknowledged": explicit_closed,
        "user_status_confirmed": explicit_confirmed,
        "user_closed": explicit_closed,
        "path": str(receipt_path),
    }


def _write_receipt_record(path: Path, event: dict[str, Any], mode: str = "a") -> None:
    with path.open(mode, encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        stream.flush()


def reserve_notification(
    path: Path,
    identity: dict[str, str],
    *,
    process_is_alive: Callable[[int, str], bool | None] | None = None,
    max_attempts: int = 2,
    **details: Any,
) -> bool:
    """Claim a notice, or safely retry an unacknowledged dead/failed attempt."""

    path = Path(path)
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least one")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _receipt_lock(path):
        if not path.exists():
            _write_receipt_record(path, {
                "event": "ATTEMPT_CLAIMED",
                "time_utc": utc_now(),
                "identity": identity,
                "attempt": 1,
                **details,
            }, mode="x")
            return True

        receipt = _read_receipt_unlocked(path)
        events = receipt["events"]
        if not events:
            attempts = 0
        else:
            recorded_identity = next(
                (event.get("identity") for event in events if "identity" in event), None)
            if recorded_identity != identity:
                raise ValueError("notification receipt identity conflicts with requested notice")
            event_names = [event["event"] for event in events]
            if receipt["user_acknowledged"]:
                return False

            attempt_events = [event for event in events
                              if event["event"] in {"ATTEMPT_CLAIMED", "ATTEMPT_RETRY_CLAIMED"}]
            attempts = len(attempt_events)
            latest_attempt = attempt_events[-1] if attempt_events else None
            latest_attempt_index = (events.index(latest_attempt) if latest_attempt else -1)
            later_events = events[latest_attempt_index + 1:]

            child_event = next((event for event in reversed(later_events)
                                if event["event"] in {"NOTIFICATION_CHILD_STARTED",
                                                      "POPUP_CHILD_STARTED"}), None)
            if child_event is not None:
                child_pid = child_event.get("pid")
                alive = (process_is_alive(child_pid, "popup_child")
                         if process_is_alive and isinstance(child_pid, int) else None)
                if alive is not False:
                    return False
            else:
                failure = next((event for event in reversed(later_events)
                                if event["event"].endswith(("ERROR", "FAILED"))), None)
                if failure is not None:
                    child_pid = failure.get("child_pid", failure.get("pid"))
                    if isinstance(child_pid, int):
                        alive = (process_is_alive(child_pid, "popup_child")
                                 if process_is_alive else None)
                        if alive is not False:
                            return False
                    elif (failure.get("process_exited") is not True
                          and failure.get("launch_succeeded") is not False):
                        return False
                else:
                    watcher_pid = latest_attempt.get("watcher_pid") if latest_attempt else None
                    alive = (process_is_alive(watcher_pid, "watcher")
                             if process_is_alive and isinstance(watcher_pid, int) else None)
                    if alive is not False:
                        return False

        if attempts >= max_attempts:
            return False
        _write_receipt_record(path, {
            "event": "ATTEMPT_RETRY_CLAIMED",
            "time_utc": utc_now(),
            "identity": identity,
            "attempt": attempts + 1,
            **details,
        })
        return True


def append_notification_receipt(path: Path | str, event: str, **details: Any) -> None:
    path = Path(path)
    record = {"event": event, "time_utc": utc_now(), **details}
    with _receipt_lock(path):
        _write_receipt_record(path, record)


def read_notification_receipt(path: Path | str) -> dict[str, Any]:
    receipt_path = Path(path)
    if not receipt_path.is_file():
        return {"available": False, "state": UNKNOWN, "events": [], "path": str(receipt_path)}
    with _receipt_lock(receipt_path):
        return _read_receipt_unlocked(receipt_path)

#!/usr/bin/env python3
"""Local VASP status and dry-run recovery planning.

The injectable recovery seam is intended for deterministic fixtures. There is
no production SSH, transfer, calculation, process-control, or retry adapter.
Execution status is mechanical evidence only; scientific acceptance is
independent and remains with VASP Sol.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import shutil
import tempfile
import sys
from typing import Any, Callable

import progress_evidence
from progress_evidence import EvidenceError, load_execution_identity
from vasp_execution_state import (
    TERMINAL_STATES,
    UNKNOWN,
    normalize_state,
    notification_receipt_path,
    read_notification_receipt,
)


RECOVERY_REQUIRED = (
    "OUTCAR",
    "OSZICAR",
    "CONTCAR",
    "vasprun.xml",
    "run_timing.txt",
    "status.json",
)
RECOVERY_OPTIONAL = ("vasp.stdout", "vasp.stderr")
RECOVERY_WHITELIST = RECOVERY_REQUIRED + RECOVERY_OPTIONAL
RECOVERY_EXCLUDED = ("POTCAR", "CHGCAR", "WAVECAR")
RECOVERY_OUTPUT_ROLES = frozenset(
    {"OUTCAR", "OSZICAR", "CONTCAR", "vasprun.xml", "vasp.stdout", "vasp.stderr"}
)
TIME_PAIR_TOLERANCE_SECONDS = 1.0
STATUS_FILE_SOURCE = ".job_watch/status.json"


class OperationError(ValueError):
    """A local operation cannot establish a safe identity or destination."""


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise OperationError(f"file is missing: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise OperationError(f"cannot read JSON at {path}: {error}") from error
    if not isinstance(value, dict):
        raise OperationError(f"JSON root must be an object: {path}")
    return value


def _json_bytes(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OperationError(f"{label} is not valid UTF-8 JSON: {error}") from error
    if not isinstance(value, dict):
        raise OperationError(f"{label} JSON root must be an object")
    return value


def _parse_epoch(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    epoch = float(value)
    if not math.isfinite(epoch):
        return None
    try:
        return datetime.fromtimestamp(epoch, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_iso_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        return None
    return result.astimezone(timezone.utc)


def _time_pair(
    document: dict[str, Any],
    name: str,
    *,
    required: bool,
    now: datetime,
) -> tuple[datetime | None, dict[str, Any], list[str]]:
    epoch_key = f"{name}_epoch"
    utc_key = f"{name}_utc"
    has_epoch = epoch_key in document
    has_utc = utc_key in document
    blockers: list[str] = []
    epoch_value = _parse_epoch(document.get(epoch_key)) if has_epoch else None
    utc_value = _parse_iso_utc(document.get(utc_key)) if has_utc else None
    if has_epoch and epoch_value is None:
        blockers.append(f"{epoch_key} is present but invalid")
    if has_utc and utc_value is None:
        blockers.append(f"{utc_key} is present but invalid")
    if not has_epoch and not has_utc:
        if required:
            blockers.append(f"{name} timestamp is missing")
        return None, {"epoch_key": None, "utc_key": None, "value": None}, blockers
    values = [value for value in (epoch_value, utc_value) if value is not None]
    if not values:
        return None, {
            "epoch_key": epoch_key if has_epoch else None,
            "utc_key": utc_key if has_utc else None,
            "value": None,
        }, blockers
    if len(values) == 2 and abs((values[0] - values[1]).total_seconds()) > TIME_PAIR_TOLERANCE_SECONDS:
        blockers.append(f"{name} epoch and UTC values conflict")
    chosen = epoch_value or utc_value
    if chosen > now:
        blockers.append(f"{name} timestamp is in the future")
    return chosen, {
        "epoch_key": epoch_key if has_epoch else None,
        "utc_key": utc_key if has_utc else None,
        "value": chosen.isoformat().replace("+00:00", "Z"),
    }, blockers


def _manifest_started_at(manifest: dict[str, Any], now: datetime) -> tuple[datetime | None, list[str]]:
    anchor: datetime | None = None
    blockers: list[str] = []
    for key, parser in (
        ("execution_started_epoch", _parse_epoch),
        ("execution_started_utc", _parse_iso_utc),
    ):
        if key not in manifest:
            continue
        value = parser(manifest.get(key))
        if value is None:
            blockers.append(f"manifest {key} is present but invalid")
        elif value > now:
            blockers.append(f"manifest {key} is in the future")
        elif anchor is not None and abs((anchor - value).total_seconds()) > TIME_PAIR_TOLERANCE_SECONDS:
            blockers.append("manifest execution start epoch and UTC values conflict")
        else:
            anchor = value
    return anchor, blockers


def _manifest_binding(
    manifest: dict[str, Any],
    identity: dict[str, Any],
    status: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    blockers: list[str] = []
    for key in ("task_id", "unit_id"):
        if status.get(key) != identity.get(key):
            blockers.append(f"status {key} is absent or differs from execution manifest")
    expected_execution = manifest.get("execution_id")
    observed_execution = status.get("execution_id")
    if not isinstance(expected_execution, str) or not expected_execution.strip():
        blockers.append("manifest execution_id is absent; only weak task/unit binding is possible")
        binding = {
            "strength": "WEAK",
            "source": "manifest task_id+unit_id only",
            "expected_execution_id": None,
            "observed_execution_id": observed_execution,
        }
    elif observed_execution != expected_execution:
        blockers.append("status execution_id is absent or differs from execution manifest")
        binding = {
            "strength": "MISMATCH",
            "source": "manifest execution_id",
            "expected_execution_id": expected_execution,
            "observed_execution_id": observed_execution,
        }
    else:
        binding = {
            "strength": "STRONG",
            "source": "manifest task_id+unit_id+execution_id",
            "expected_execution_id": expected_execution,
            "observed_execution_id": observed_execution,
        }
    return binding, blockers


def _parse_code(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _timing_code(text: str | None) -> tuple[int | None, list[str]]:
    if text is None:
        return None, ["run_timing.txt is missing or unreadable"]
    found: list[int] = []
    malformed = False
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "exit_code":
            parsed = _parse_code(value.strip())
            if parsed is None:
                malformed = True
            else:
                found.append(parsed)
    if malformed:
        return None, ["run_timing.txt exit_code is invalid"]
    if len(found) != 1:
        return None, ["run_timing.txt must contain exactly one integer exit_code"]
    return found[0], []


def _exit_evidence(
    status: dict[str, Any],
    timing_text: str | None,
    expected_case: str,
    *,
    required: bool,
    state: str,
) -> tuple[dict[str, Any], list[str]]:
    values: list[dict[str, Any]] = []
    blockers: list[str] = []
    runner_raw = status.get("runner_exit_code")
    runner_code = _parse_code(runner_raw)
    if runner_code is not None:
        values.append({
            "scope": "runner",
            "value": runner_code,
            "source": "status.json.runner_exit_code",
        })
    elif required:
        blockers.append("status runner_exit_code is missing or invalid")

    legacy_runner = _parse_code(status.get("exit_code"))
    if "exit_code" in status and legacy_runner is not None:
        values.append({
            "scope": "runner_legacy",
            "value": legacy_runner,
            "source": "status.json.exit_code",
        })
    elif "exit_code" in status:
        blockers.append("legacy status exit_code is invalid")

    cases = status.get("cases")
    matched = []
    if isinstance(cases, list):
        matched = [
            item for item in cases
            if isinstance(item, dict) and item.get("path") == expected_case
        ]
    if required and len(matched) != 1:
        blockers.append("status must contain exactly one case record matching the manifest case")
    if len(matched) == 1:
        case = matched[0]
        case_code = _parse_code(case.get("exit_code"))
        if case_code is not None:
            values.append({
                "scope": expected_case,
                "value": case_code,
                "source": "status.json.cases[].exit_code",
            })
        elif required:
            blockers.append("manifest case exit_code is missing or invalid")
        if required and not isinstance(case.get("ok"), bool):
            blockers.append("manifest case ok evidence is missing or invalid")

    timing_exit, timing_blockers = _timing_code(timing_text)
    if timing_exit is not None:
        values.append({
            "scope": "case_timing",
            "value": timing_exit,
            "source": "run_timing.txt.exit_code",
        })
    if required:
        blockers.extend(timing_blockers)
    distinct_codes = {item["value"] for item in values}
    if required and len(distinct_codes) > 1:
        blockers.append("runner, case, and run_timing exit codes conflict")
    if required and runner_code is not None and len(matched) == 1:
        case_ok = matched[0].get("ok")
        if state == "COMPLETED_PENDING_REVIEW" and (
            runner_code != 0 or case_ok is not True
        ):
            blockers.append("completed status conflicts with runner/case success evidence")
        if state == "FAILED_OR_INCOMPLETE" and runner_code == 0 and case_ok is True:
            blockers.append("failed status conflicts with successful runner/case evidence")
    return {
        "values": values,
        "available": len(values) >= 3 and len(distinct_codes) == 1,
        "complete": not blockers,
        "reason": None if not blockers else "; ".join(dict.fromkeys(blockers)),
    }, blockers


def _inspect_status(
    manifest_path: Path,
    status_data: dict[str, Any],
    timing_text: str | None,
    *,
    status_source: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise OperationError("now must be timezone-aware")
    current = current.astimezone(timezone.utc)
    manifest = _read_object(manifest_path)
    identity = load_execution_identity(manifest_path)
    raw_state = status_data.get("status")
    try:
        reported_state, alias = normalize_state(raw_state)
        state_reason = None
    except ValueError as error:
        reported_state, alias, state_reason = UNKNOWN, None, str(error)
    binding, blockers = _manifest_binding(manifest, identity, status_data)
    is_terminal = reported_state in TERMINAL_STATES
    if reported_state == UNKNOWN:
        blockers.append(state_reason or "status is unknown")

    started, started_record, time_blockers = _time_pair(
        status_data,
        "started",
        required=is_terminal or reported_state == "RUNNING",
        now=current,
    )
    blockers.extend(time_blockers)
    ended, ended_record, time_blockers = _time_pair(
        status_data, "ended", required=is_terminal, now=current
    )
    blockers.extend(time_blockers)
    updated, updated_record, time_blockers = _time_pair(
        status_data,
        "updated",
        required=is_terminal or reported_state == "RUNNING",
        now=current,
    )
    blockers.extend(time_blockers)
    if started is not None and ended is not None and ended < started:
        blockers.append("ended timestamp precedes started timestamp")
    if started is not None and updated is not None and updated < started:
        blockers.append("updated timestamp precedes started timestamp")
    if ended is not None and updated is not None and updated < ended:
        blockers.append("updated timestamp precedes ended timestamp")
    if not is_terminal and reported_state not in {UNKNOWN, "NOT_STARTED"} and ended is not None:
        blockers.append("nonterminal status contains an ended timestamp")

    manifest_start, manifest_time_blockers = _manifest_started_at(manifest, current)
    blockers.extend(manifest_time_blockers)
    if manifest_start is not None and started is not None and abs(
        (manifest_start - started).total_seconds()
    ) > TIME_PAIR_TOLERANCE_SECONDS:
        blockers.append("status started timestamp differs from manifest execution start")

    exit_evidence, exit_blockers = _exit_evidence(
        status_data,
        timing_text,
        identity["case"],
        required=is_terminal,
        state=reported_state,
    )
    blockers.extend(exit_blockers)
    blockers = list(dict.fromkeys(blockers))
    execution_state = UNKNOWN if blockers else reported_state
    return {
        "report_type": "execution_status_short",
        "status_source": status_source,
        "observed_utc": current.isoformat().replace("+00:00", "Z"),
        "manifest_identity": identity,
        "manifest_binding": binding,
        "status_timestamp": {
            "started": started_record,
            "ended": ended_record,
            "updated": updated_record,
        },
        "task_id": status_data.get("task_id"),
        "job_label": status_data.get("job"),
        "unit_id": status_data.get("unit_id"),
        "execution_id": status_data.get("execution_id"),
        "execution_state": execution_state,
        "reported_execution_state": reported_state,
        "raw_status": raw_state,
        "legacy_alias": alias,
        "state_reason": state_reason,
        "evidence_status": "COMPLETE" if not blockers else "HOLD",
        "evidence_blockers": blockers,
        "exit_evidence": exit_evidence,
        "scientific_acceptance": status_data.get("scientific_acceptance") or UNKNOWN,
        "science_review_performed": False,
    }


def _read_timing(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def short_status(
    manifest_path: Path,
    status_path: Path,
    timing_path: Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Summarize local metadata only, bound to one execution manifest."""

    timing = timing_path or status_path.with_name("run_timing.txt")
    return _inspect_status(
        manifest_path,
        _read_object(status_path),
        _read_timing(timing),
        status_source=str(status_path.resolve()),
        now=now,
    )


def local_preflight(manifest_path: Path) -> dict[str, Any]:
    """Validate local manifest identity and expose actions deliberately not run."""

    identity = load_execution_identity(manifest_path)
    manifest = _read_object(manifest_path)
    expected_execution = manifest.get("execution_id")
    binding = (
        "STRONG" if isinstance(expected_execution, str) and expected_execution.strip()
        else "WEAK_EXECUTION_ID_ABSENT"
    )
    return {
        "report_type": "local_preflight",
        "result": "IDENTITY_VALID",
        "identity": identity,
        "execution_binding": binding,
        "checks": {
            "manifest_identity": "PASS",
            "remote_target_syntax": "PASS",
            "runtime_input_declaration": "PASS",
        },
        "remote_environment": "NOT_CHECKED",
        "ssh_attempted": False,
        "process_or_tmux_changed": False,
        "execution_authorized": False,
        "scientific_acceptance": "NOT_EVALUATED",
    }


def recovery_plan(
    manifest_path: Path,
    status_path: Path,
    timing_path: Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    identity = load_execution_identity(manifest_path)
    report = short_status(manifest_path, status_path, timing_path, now=now)
    state = report["execution_state"]
    ready = (
        state in TERMINAL_STATES
        and report["evidence_status"] == "COMPLETE"
        and report["manifest_binding"]["strength"] == "STRONG"
        and report["exit_evidence"]["complete"]
    )
    return {
        "report_type": "terminal_recovery_plan",
        "disposition": "READY_FOR_SEPARATE_AUTHORIZATION" if ready else "HOLD",
        "identity": identity,
        "manifest_binding": report["manifest_binding"],
        "status_source": report["status_source"],
        "observed_utc": report["observed_utc"],
        "status_timestamp": report["status_timestamp"],
        "execution_id": report["execution_id"],
        "execution_state": state,
        "reported_execution_state": report["reported_execution_state"],
        "evidence_status": report["evidence_status"],
        "evidence_blockers": report["evidence_blockers"],
        "exit_evidence": report["exit_evidence"],
        "identity_conflicts": list(report["evidence_blockers"]),
        "candidate_files": list(RECOVERY_WHITELIST) if ready else [],
        "required_files": list(RECOVERY_REQUIRED),
        "optional_files": list(RECOVERY_OPTIONAL),
        "excluded_files": list(RECOVERY_EXCLUDED),
        "transfer_performed": False,
        "automatic_retry_or_restart": False,
        "scientific_acceptance": report["scientific_acceptance"],
        "next_step": (
            "Only a separately authorized exact-member extraction may proceed; scientific review remains separate."
            if ready
            else "Resolve every identity, timestamp, or exit-evidence blocker; no extraction is proposed."
        ),
    }


def _path_is_symlink_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    junction_check = getattr(path, "is_junction", None)
    if not callable(junction_check):
        # Older Windows Python may lack junction inspection entirely.
        return sys.platform == "win32"
    try:
        return bool(junction_check())
    except (OSError, NotImplementedError, ValueError):
        # Path safety is fail-closed when a reparse-point check cannot complete.
        return True


def _has_symlink_component(path: Path) -> bool:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if _path_is_symlink_or_junction(current):
            return True
    return False


def _recovery_snapshot_name() -> str:
    name = progress_evidence.compact_utc()
    if not isinstance(name, str) or re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", name) is None:
        raise OperationError("generated recovery snapshot name is not a single UTC leaf")
    return name


def _safe_target(target: Path, safety_root: Path) -> tuple[Path, Path]:
    if not target.is_absolute() or not safety_root.is_absolute():
        raise OperationError("target and safety_root must be absolute paths")
    if ".." in target.parts or ".." in safety_root.parts:
        raise OperationError("path traversal component is forbidden")
    if _has_symlink_component(safety_root):
        raise OperationError("safety_root contains a symlink or junction")
    try:
        root = safety_root.resolve(strict=True)
    except OSError as error:
        raise OperationError(f"safety_root cannot be resolved: {error}") from error
    if not root.is_dir():
        raise OperationError("safety_root must be an existing directory")
    if target.name in {"", ".", ".."} or target.parent.resolve() != root:
        raise OperationError("target must be a direct child of safety_root")
    if _has_symlink_component(target.parent):
        raise OperationError("target parent contains a symlink or junction")
    if _path_is_symlink_or_junction(target) or target.exists():
        raise OperationError("target already exists; overwrite is forbidden")
    return root, target


def _expected_remote_source(identity: dict[str, Any], name: str) -> str:
    if name == "status.json":
        return f"{identity['remote_batch_dir']}/{STATUS_FILE_SOURCE}"
    if name == "run_timing.txt":
        return f"{identity['remote_case_dir']}/run_timing.txt"
    return progress_evidence.expected_source(identity, name)


def _validate_stats(member: dict[str, Any], content: bytes, name: str) -> None:
    before = member.get("read_before")
    after = member.get("read_after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise OperationError(f"{name}: read-before/read-after evidence is missing")
    expected_bytes = len(content)
    for label, record in (("read_before", before), ("read_after", after)):
        if record.get("present") is not True:
            raise OperationError(f"{name}: {label} does not confirm a present file")
        if record.get("bytes") != expected_bytes:
            raise OperationError(f"{name}: {label} byte count conflicts with content")
        if not isinstance(record.get("mtime_utc"), str) or not record["mtime_utc"].strip():
            raise OperationError(f"{name}: {label} mtime is missing")
    if before.get("mtime_utc") != after.get("mtime_utc"):
        raise OperationError(f"{name}: source changed during read")


def _validate_payload(
    payload: Any,
    manifest: dict[str, Any],
    identity: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(payload, dict):
        raise OperationError("transport returned no recovery payload object")
    remote_identity = payload.get("manifest_identity")
    if not isinstance(remote_identity, dict):
        raise OperationError("transport payload has no canonical manifest identity")
    expected_execution = manifest.get("execution_id")
    expected = dict(identity)
    expected["execution_id"] = expected_execution
    identity_keys = (
        "task_id", "unit_id", "remote_batch_dir", "case", "remote_case_dir",
        "remote_input_dir", "runtime_input_source", "host", "execution_id",
    )
    for key in identity_keys:
        if remote_identity.get(key) != expected.get(key):
            raise OperationError(f"transport manifest identity conflicts at {key}")
    if not isinstance(expected_execution, str) or not expected_execution.strip():
        raise OperationError("manifest execution_id is required for extraction")

    members = payload.get("members")
    if not isinstance(members, list):
        raise OperationError("transport payload member list is missing")
    names: list[str] = []
    by_name: dict[str, dict[str, Any]] = {}
    for member in members:
        if not isinstance(member, dict):
            raise OperationError("transport payload contains a malformed member")
        name = member.get("name")
        if (
            not isinstance(name, str)
            or not name
            or "/" in name
            or "\\" in name
            or name in {".", ".."}
        ):
            raise OperationError("transport member name is unsafe")
        if name not in RECOVERY_WHITELIST:
            raise OperationError(f"transport member is outside the exact whitelist: {name}")
        if name in by_name:
            raise OperationError(f"transport member is duplicated: {name}")
        if member.get("is_symlink") is not False:
            raise OperationError(f"{name}: source is a symlink or symlink status is unknown")
        content = member.get("content")
        if not isinstance(content, bytes):
            raise OperationError(f"{name}: transport content must be bytes")
        if name in RECOVERY_REQUIRED and not content:
            raise OperationError(f"{name}: required member is empty")
        if member.get("source_path") != _expected_remote_source(identity, name):
            raise OperationError(f"{name}: source path is not manifest-derived")
        _validate_stats(member, content, name)
        names.append(name)
        by_name[name] = member
    missing = sorted(set(RECOVERY_REQUIRED) - set(names))
    if missing:
        raise OperationError("required recovery members are missing: " + ", ".join(missing))
    return remote_identity, members, by_name


def _frames_for_payload(
    identity: dict[str, Any],
    members: list[dict[str, Any]],
    by_name: dict[str, dict[str, Any]],
    started_utc: str,
    ended_utc: str,
) -> list[str]:
    frames: list[dict[str, Any]] = [{
        "type": "start",
        "collection_start_utc": started_utc,
        "manifest_identity": identity,
    }]
    for member in members:
        name = member["name"]
        content = member["content"]
        encoded = base64.b64encode(content).decode("ascii")
        if name in {"status.json", "run_timing.txt"}:
            role = "job_status" if name == "status.json" else "run_timing"
            frames.append({
                "type": "metadata_file",
                "role": role,
                "source_path": member["source_path"],
                "content_b64": encoded,
                "read_content": True,
                "read_before": member["read_before"],
                "read_after": member["read_after"],
                "coverage": "full",
            })
        else:
            frames.append({
                "type": "file",
                "role": name,
                "kind": "recovery_full_output",
                "fragment_id": "recovery-full",
                "source_path": member["source_path"],
                "content_b64": encoded,
                "coverage": "full",
                "original_start_line": 1,
                "original_end_line": max(
                    1, len(content.decode("utf-8", errors="replace").splitlines())
                ),
                "original_start_byte": 0,
                "original_end_byte": len(content),
                "read_before": member["read_before"],
                "read_after": member["read_after"],
            })
    frames.append({
        "type": "metadata",
        "identity": identity,
        "operation": "mockable_exact_whitelist_recovery",
    })
    frames.append({"type": "end", "collection_end_utc": ended_utc, "exit_code": 0})
    return [json.dumps(item, ensure_ascii=False) for item in frames]


def _verify_imported_recovery(
    bundle: Path,
    identity: dict[str, Any],
    by_name: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    document = progress_evidence.read_json(bundle / "evidence.json")
    if not isinstance(document, dict) or document.get("evidence_type") != "primary_raw_evidence_bundle":
        raise OperationError("progress_evidence importer did not create a canonical evidence bundle")
    imported_identity = document.get("manifest_identity")
    if not isinstance(imported_identity, dict):
        raise OperationError("imported bundle lost manifest identity")
    for key in ("task_id", "unit_id", "remote_batch_dir", "case", "remote_case_dir", "host"):
        if imported_identity.get(key) != identity.get(key):
            raise OperationError(f"imported manifest identity conflicts at {key}")
    if document.get("collection", {}).get("status") != "COMPLETE":
        raise OperationError("imported evidence bundle is not complete")

    expected_roles = set(by_name) & RECOVERY_OUTPUT_ROLES
    file_map = document.get("files")
    if not isinstance(file_map, dict) or set(file_map) != expected_roles:
        raise OperationError("imported output roles do not exactly match extracted whitelist members")
    for role in expected_roles:
        records = file_map.get(role)
        if not isinstance(records, list) or len(records) != 1:
            raise OperationError(f"imported {role} does not have exactly one full record")
        record = records[0]
        if (
            record.get("coverage") != "full"
            or record.get("source_path") != _expected_remote_source(identity, role)
            or record.get("read_consistent") is not True
        ):
            raise OperationError(f"imported {role} is incomplete or has the wrong source path")
        snapshot = progress_evidence.safe_relative(bundle, record.get("snapshot_path"))
        if snapshot.read_bytes() != by_name[role]["content"]:
            raise OperationError(f"imported {role} content differs from transported bytes")

    metadata_files = document.get("metadata_files")
    if not isinstance(metadata_files, list):
        raise OperationError("imported metadata member list is missing")
    metadata_roles = {item.get("role") for item in metadata_files if isinstance(item, dict)}
    if metadata_roles != {"job_status", "run_timing"} or len(metadata_files) != 2:
        raise OperationError("imported status/timing metadata members are incomplete or excessive")
    for item in metadata_files:
        name = "status.json" if item.get("role") == "job_status" else "run_timing.txt"
        if item.get("source_path") != _expected_remote_source(identity, name):
            raise OperationError(f"imported {name} source path is not manifest-derived")
        if item.get("read_consistent") is not True:
            raise OperationError(f"imported {name} changed during read")
        snapshot = progress_evidence.safe_relative(bundle, item.get("snapshot_path"))
        if snapshot.read_bytes() != by_name[name]["content"]:
            raise OperationError(f"imported {name} content differs from transported bytes")
    unexpected_sensitive = set(document.get("files", {})) & set(RECOVERY_EXCLUDED)
    if unexpected_sensitive:
        raise OperationError("sensitive/restart file role appeared in imported bundle")
    return document


def recover_outputs(
    manifest_path: Path,
    status_path: Path,
    target: Path,
    safety_root: Path,
    *,
    timing_path: Path | None = None,
    execute: bool = False,
    authority_confirmed: bool = False,
    authorization_ref: str | None = None,
    transport: Callable[[dict[str, Any], tuple[str, ...]], Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Dry-run by default; injected transports are for fixtures, not live SSH."""

    root, destination = _safe_target(target, safety_root)
    plan = recovery_plan(manifest_path, status_path, timing_path, now=now)
    result = {
        **plan,
        "target": str(destination),
        "safety_root": str(root),
        "operation_mode": "EXECUTE" if execute else "DRY_RUN",
        "transfer_performed": False,
        "authorization_record": {
            "authority_confirmed_by_caller": authority_confirmed is True,
            "reference": authorization_ref,
            "reference_is_not_authority": True,
        },
    }
    if not execute:
        return result
    if (
        authority_confirmed is not True
        or not isinstance(authorization_ref, str)
        or not authorization_ref.strip()
    ):
        raise OperationError(
            "execute requires authority_confirmed=True and a nonempty external authorization_ref"
        )
    if transport is None:
        raise OperationError(
            "no real transport is configured; execution is available only through an injected fixture"
        )
    if plan["disposition"] != "READY_FOR_SEPARATE_AUTHORIZATION":
        raise OperationError("status/manifest evidence is not ready for extraction")

    snapshot_name = _recovery_snapshot_name()
    manifest = _read_object(manifest_path)
    identity = load_execution_identity(manifest_path)
    payload = transport(identity, tuple(RECOVERY_WHITELIST))
    remote_identity, members, by_name = _validate_payload(payload, manifest, identity)

    remote_status = _json_bytes(by_name["status.json"]["content"], "remote status.json")
    remote_timing = by_name["run_timing.txt"]["content"].decode("utf-8")
    remote_report = _inspect_status(
        manifest_path,
        remote_status,
        remote_timing,
        status_source="transport:status.json",
        now=now,
    )
    if (
        remote_report["execution_state"] not in TERMINAL_STATES
        or remote_report["evidence_status"] != "COMPLETE"
    ):
        raise OperationError(
            "transport status is nonterminal, stale, or lacks consistent exit evidence"
        )
    for key in ("task_id", "unit_id", "execution_id"):
        expected_value = (
            plan["identity"].get(key)
            if key in {"task_id", "unit_id"}
            else plan.get(key)
        )
        if remote_report.get(key) != expected_value:
            raise OperationError(f"transport status identity differs from dry-run plan at {key}")
    if remote_report.get("reported_execution_state") != plan.get("reported_execution_state"):
        raise OperationError("transport terminal state differs from dry-run status")
    if remote_report.get("status_timestamp") != plan.get("status_timestamp"):
        raise OperationError("transport status timestamps differ from dry-run status")
    if remote_report.get("exit_evidence", {}).get("values") != plan.get("exit_evidence", {}).get("values"):
        raise OperationError("transport exit evidence differs from dry-run status")

    root, destination = _safe_target(destination, root)
    stage_root = Path(tempfile.mkdtemp(prefix=".vasp-recovery-stage-", dir=root))
    published = False
    try:
        started_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        ended_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        frames = _frames_for_payload(identity, members, by_name, started_utc, ended_utc)
        bundle = progress_evidence.ingest_export(
            frames,
            stage_root,
            manifest_path,
            snapshot_name=snapshot_name,
            source_mode="injected-transport",
            command_mode="injected-transport",
            command_argv=["injected-transport"],
        )
        imported = _verify_imported_recovery(bundle, identity, by_name)
        receipt = {
            "schema": "vasp-terminal-recovery-receipt/v1",
            "kind": "MECHANICAL_WHITELIST_EXTRACTION",
            "manifest_identity": remote_identity,
            "execution_id": remote_report["execution_id"],
            "reported_terminal_state": remote_report["reported_execution_state"],
            "status_timestamp": remote_report["status_timestamp"],
            "exit_evidence": remote_report["exit_evidence"],
            "extracted_members": sorted(by_name),
            "required_members": sorted(RECOVERY_REQUIRED),
            "optional_members_present": sorted(set(by_name) & set(RECOVERY_OPTIONAL)),
            "excluded_members": list(RECOVERY_EXCLUDED),
            "authorization_record": result["authorization_record"],
            "progress_evidence_bundle": str(bundle),
            "importer_collection_status": imported["collection"]["status"],
            "scientific_acceptance": "NOT_EVALUATED",
            "scientific_review_performed": False,
            "transfer_adapter": "injected fixture; no live transport implementation",
        }
        progress_evidence.write_json(bundle / "recovery_receipt.json", receipt)
        root, destination = _safe_target(destination, root)
        bundle.rename(destination)
        published = True
    finally:
        if stage_root.exists():
            resolved = stage_root.resolve()
            if resolved.parent == root and stage_root.name.startswith(".vasp-recovery-stage-"):
                shutil.rmtree(stage_root)

    return {
        **result,
        "disposition": "EXTRACTED_MECHANICAL_CANDIDATE",
        "target": str(destination),
        "bundle": str(destination),
        "transfer_performed": True,
        "published": published,
        "scientific_acceptance": "NOT_EVALUATED",
        "next_step": "VASP Sol reviews the returned raw evidence; no scientific conclusion is made here.",
    }


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    status_parser = subparsers.add_parser("status", help="manifest-bound local execution status")
    status_parser.add_argument("manifest", type=Path)
    status_parser.add_argument("status_json", type=Path)
    status_parser.add_argument("--timing", type=Path)
    preflight_parser = subparsers.add_parser("preflight", help="local manifest identity preflight")
    preflight_parser.add_argument("manifest", type=Path)
    recovery_parser = subparsers.add_parser("recovery-plan", help="dry-run terminal recovery plan")
    recovery_parser.add_argument("manifest", type=Path)
    recovery_parser.add_argument("status_json", type=Path)
    recovery_parser.add_argument("--timing", type=Path)
    recover_parser = subparsers.add_parser("recover", help="dry-run exact whitelist recovery")
    recover_parser.add_argument("manifest", type=Path)
    recover_parser.add_argument("status_json", type=Path)
    recover_parser.add_argument("target", type=Path)
    recover_parser.add_argument("safety_root", type=Path)
    recover_parser.add_argument("--timing", type=Path)
    recover_parser.add_argument("--execute", action="store_true")
    recover_parser.add_argument("--authority-confirmed", action="store_true")
    recover_parser.add_argument("--authorization-ref")
    receipt_parser = subparsers.add_parser("receipt", help="read one durable notification receipt")
    receipt_parser.add_argument("ledger_dir", type=Path)
    receipt_parser.add_argument("task_id")
    receipt_parser.add_argument("execution_id")
    receipt_parser.add_argument("terminal_state")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            result = short_status(args.manifest, args.status_json, args.timing)
        elif args.command == "preflight":
            result = local_preflight(args.manifest)
        elif args.command == "recovery-plan":
            result = recovery_plan(args.manifest, args.status_json, args.timing)
        elif args.command == "recover":
            result = recover_outputs(
                args.manifest,
                args.status_json,
                args.target,
                args.safety_root,
                timing_path=args.timing,
                execute=args.execute,
                authority_confirmed=args.authority_confirmed,
                authorization_ref=args.authorization_ref,
            )
        else:
            path = notification_receipt_path(
                args.ledger_dir, args.task_id, args.execution_id, args.terminal_state
            )
            result = read_notification_receipt(path)
        _emit(result)
    except (EvidenceError, OSError, OperationError, ValueError) as error:
        print(json.dumps({"result": "ERROR", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.command == "status" and result.get("execution_state") == UNKNOWN:
        return 1
    if args.command == "receipt" and not result.get("available"):
        return 1
    return 0 if result.get("disposition") not in {"HOLD"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

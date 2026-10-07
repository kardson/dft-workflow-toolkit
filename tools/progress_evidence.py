#!/usr/bin/env python3
"""Build and verify bounded, source-traceable VASP progress evidence bundles.

This module is deliberately separate from progress_snapshot.py:

* collection is a one-shot operation and writes raw evidence before parsing;
* remote collection is represented by a read-only stdout export script;
* local fixtures can exercise the same bundle builder without SSH;
* output fragments are never concatenated into a pretend continuous OUTCAR or
  OSZICAR, so unfinished ionic-step numbers remain unknown;
* licensed POTCAR/CHGCAR/WAVECAR bytes and embedded PAW bodies are refused.

The command has no retry, wait, restart, notification, or remote-write path.
"""

from __future__ import annotations

import argparse
import base64
import binascii
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, TextIO

try:
    from progress_snapshot import (
        OUTCAR_PARAMETER_FIELDS,
        compare_parameter_sources,
        load_approved_manifest,
        parse_incar,
        parse_outcar_parameters,
        parse_poscar,
        incar_float,
        incar_int,
    )
    from vasp_execution_state import (
        COMPLETED_PENDING_REVIEW,
        FAILED_OR_INCOMPLETE,
        NOT_STARTED,
        RUNNING,
        STARTING,
        SUPERVISOR_ERROR,
        TERMINAL_STATES,
        normalize_state,
    )
except ImportError:  # pragma: no cover - package import fallback
    from .progress_snapshot import (
        OUTCAR_PARAMETER_FIELDS,
        compare_parameter_sources,
        load_approved_manifest,
        parse_incar,
        parse_outcar_parameters,
        parse_poscar,
        incar_float,
        incar_int,
    )
    from .vasp_execution_state import (
        COMPLETED_PENDING_REVIEW,
        FAILED_OR_INCOMPLETE,
        NOT_STARTED,
        RUNNING,
        STARTING,
        SUPERVISOR_ERROR,
        TERMINAL_STATES,
        normalize_state,
    )


SCHEMA = "vasp-progress-evidence/v1"
QUICK_STATUS_SCHEMA = "vasp-quick-status/v1"
REMOTE_BATCH_RE = re.compile(r"^/srv/dft/calculations/[A-Za-z0-9_.-]+$")
CASE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
TMUX_SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
FLOAT_RE = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?"
ALLOWED_COVERAGE = {"full", "range", "head", "tail", "unknown"}
SENSITIVE_FILES = {"POTCAR", "CHGCAR", "WAVECAR"}
PRIMARY_INPUTS = ("POSCAR", "INCAR", "KPOINTS")
OUTPUT_ROLES = (
    "OUTCAR",
    "OSZICAR",
    "CONTCAR",
    "vasprun.xml",
    "vasp.stdout",
    "vasp.stderr",
)
OUTCAR_KINDS = {
    "outcar_identity",
    "outcar_parameters",
    "outcar_force",
    "outcar_marker",
    "outcar_timing",
    "outcar_diagnostics",
}
OSZICAR_KINDS = {"oszicar", "oszicar_tail"}

# These markers are not needed for a progress claim and can expose licensed
# PAW data or its radial-potential body. Refuse before writing a raw file.
PAW_BODY_RE = re.compile(
    r"\b(?:TITEL|VRHFIN|LEXCH|POMASS|ZVAL|ENMAX|PSCTR|"
    r"PAW[_ -]PBE|pseudopotential|radial\s+(?:grid|potential)|"
    r"atomic\s+pseudo)\b",
    re.IGNORECASE,
)


class EvidenceError(ValueError):
    """A manifest or evidence package violates a collection invariant."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def compact_utc(value: str | None = None) -> str:
    if value is None:
        value = utc_now()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y%m%dT%H%M%SZ")


def parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def iso_mtime(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def stat_record(path: Path) -> dict[str, Any]:
    try:
        item = path.stat()
    except FileNotFoundError:
        return {"present": False, "bytes": None, "mtime_utc": None}
    except OSError as error:
        return {
            "present": None,
            "bytes": None,
            "mtime_utc": None,
            "error": f"{type(error).__name__}: {error}",
        }
    return {
        "present": True,
        "bytes": item.st_size,
        "mtime_utc": iso_mtime(item.st_mtime),
    }


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise EvidenceError(f"JSON file is missing: {path}") from error
    except json.JSONDecodeError as error:
        raise EvidenceError(f"invalid JSON {path}: line {error.lineno}: {error.msg}") from error


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def safe_relative(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise EvidenceError(f"path escapes source directory: {relative}") from error
    return candidate


def _host_from_value(value: Any, port: Any = None) -> dict[str, Any]:
    if isinstance(value, dict):
        user = value.get("user")
        address = value.get("address")
        value_port = value.get("port", port)
    elif isinstance(value, str):
        if "@" not in value:
            raise EvidenceError("execution manifest host string must be user@address")
        user, address = value.split("@", 1)
        value_port = port
    else:
        raise EvidenceError("execution manifest host object/string is missing")
    if (
        not isinstance(user, str)
        or not user.strip()
        or any(character.isspace() for character in user)
        or not isinstance(address, str)
        or not address.strip()
        or any(character.isspace() for character in address)
        or "/" in address
        or "@" in address
    ):
        raise EvidenceError("execution manifest host user/address is invalid")
    if (
        not isinstance(value_port, int)
        or isinstance(value_port, bool)
        or not 1 <= value_port <= 65535
    ):
        raise EvidenceError("execution manifest host port is invalid")
    return {"user": user, "address": address, "port": value_port}


def _manifest_namespaces(manifest: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    remote_value = manifest.get("remote")
    remote = remote_value if isinstance(remote_value, dict) else {}
    if "remote_target" in manifest and not isinstance(manifest["remote_target"], dict):
        raise EvidenceError("execution manifest remote_target must be an object")
    remote_target = manifest.get("remote_target", {})
    return [("remote", remote), ("remote_target", remote_target)]


def _host_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    sources = [("", manifest)] + _manifest_namespaces(manifest)
    normalized: list[tuple[str, dict[str, Any]]] = []
    for namespace, block in sources:
        host_present = "host" in block
        port_present = "port" in block
        label = f"{namespace}.host" if namespace else "host"
        port_label = f"{namespace}.port" if namespace else "port"
        if not host_present:
            if port_present:
                raise EvidenceError(f"execution manifest {port_label} is present without host")
            continue
        host_value = block["host"]
        embedded_port = host_value.get("port") if isinstance(host_value, dict) else None
        separate_port = block.get("port") if port_present else None
        if (
            embedded_port is not None
            and separate_port is not None
            and embedded_port != separate_port
        ):
            raise EvidenceError(f"execution manifest {label} port conflicts with {port_label}")
        value_port = separate_port if separate_port is not None else embedded_port
        normalized.append((label, _host_from_value(host_value, value_port)))
    if not normalized:
        raise EvidenceError("execution manifest host object/string is missing")
    expected = normalized[0][1]
    for source, host in normalized[1:]:
        if host != expected:
            raise EvidenceError(
                f"execution manifest {normalized[0][0]} and {source} conflict"
            )
    return expected


def _compatible_manifest_value(
    manifest: dict[str, Any],
    direct_key: str,
    nested_sources: list[tuple[str, dict[str, Any]]],
    nested_key: str,
    label: str,
) -> tuple[Any, str]:
    values: list[tuple[str, Any]] = []
    if direct_key in manifest:
        values.append((direct_key, manifest[direct_key]))
    for namespace, source in nested_sources:
        if nested_key in source:
            values.append((f"{namespace}.{nested_key}", source[nested_key]))
    if not values:
        raise EvidenceError(f"execution manifest {label} is missing")
    source, value = values[0]
    for other_source, other_value in values[1:]:
        if other_value != value:
            raise EvidenceError(
                f"execution manifest {source} and {other_source} conflict"
            )
    return value, source


def load_execution_identity(manifest_path: Path) -> dict[str, Any]:
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("route") not in {None, "vasp"}:
        raise EvidenceError("execution manifest is not a VASP manifest")
    task_id = manifest.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise EvidenceError("execution manifest task_id is missing")
    flat_unit_present = "unit_id" in manifest
    workflow_unit_present = "workflow_unit_id" in manifest
    flat_unit = manifest.get("unit_id")
    workflow_unit = manifest.get("workflow_unit_id")
    for name, value in (("unit_id", flat_unit), ("workflow_unit_id", workflow_unit)):
        if (name == "unit_id" and not flat_unit_present) or (
            name == "workflow_unit_id" and not workflow_unit_present
        ):
            continue
        if not isinstance(value, str) or not value.strip():
            raise EvidenceError(f"execution manifest {name} is invalid")
    if flat_unit_present and workflow_unit_present and flat_unit != workflow_unit:
        raise EvidenceError("execution manifest unit_id and workflow_unit_id conflict")
    unit_id = flat_unit if flat_unit_present else workflow_unit
    if not isinstance(unit_id, str) or not unit_id:
        raise EvidenceError("execution manifest unit_id/workflow_unit_id is missing")
    nested_sources = _manifest_namespaces(manifest)
    batch, batch_source = _compatible_manifest_value(
        manifest, "remote_batch_dir", nested_sources, "batch_dir", "remote_batch_dir"
    )
    case, case_source = _compatible_manifest_value(
        manifest, "case", nested_sources, "case_id", "case"
    )
    if not isinstance(batch, str) or not REMOTE_BATCH_RE.fullmatch(batch):
        raise EvidenceError("execution manifest remote_batch_dir is unsafe or missing")
    if not isinstance(case, str) or not CASE_RE.fullmatch(case) or case in {".", ".."}:
        raise EvidenceError("execution manifest case is unsafe or missing")
    case_dir = f"{batch}/{case}"
    case_dir_sources = [("", manifest)] + nested_sources
    for namespace, source in case_dir_sources:
        if "case_dir" not in source:
            continue
        recorded_case_dir = source["case_dir"]
        label = f"{namespace}.case_dir" if namespace else "case_dir"
        if not isinstance(recorded_case_dir, str) or recorded_case_dir != case_dir:
            raise EvidenceError(
                f"execution manifest {label} does not equal batch_dir/case_id"
            )
    runtime_input_dir = manifest.get("runtime_input_dir")
    if runtime_input_dir is None:
        runtime_input_dir = case_dir
        runtime_input_source = "case"
    elif runtime_input_dir == case_dir:
        runtime_input_source = "case"
    elif runtime_input_dir == batch and manifest.get("runtime_input_verified") is True:
        runtime_input_source = "verified_batch"
    else:
        raise EvidenceError(
            "execution manifest runtime_input_dir must be the case directory "
            "or an explicitly verified batch directory"
        )
    return {
        "task_id": task_id,
        "unit_id": unit_id,
        "workflow_unit_id": workflow_unit if workflow_unit_present else None,
        "identity_sources": {
            "unit_id": (
                "unit_id+workflow_unit_id"
                if flat_unit_present and workflow_unit_present
                else "unit_id"
                if flat_unit_present
                else "workflow_unit_id"
            ),
            "remote_batch_dir": batch_source,
            "case": case_source,
            "case_dir": [
                f"{namespace}.case_dir" if namespace else "case_dir"
                for namespace, source in case_dir_sources
                if "case_dir" in source
            ],
        },
        "host": _host_identity(manifest),
        "remote_batch_dir": batch,
        "case": case,
        "remote_case_dir": case_dir,
        "remote_input_dir": runtime_input_dir,
        "runtime_input_source": runtime_input_source,
        "manifest_path": str(manifest_path.resolve()),
    }


def _optional_manifest_identity_value(
    manifest: dict[str, Any],
    direct_key: str,
    nested_key: str,
    label: str,
) -> tuple[Any, str | None]:
    values: list[tuple[str, Any]] = []
    if direct_key in manifest:
        values.append((direct_key, manifest[direct_key]))
    for namespace, source in _manifest_namespaces(manifest):
        if nested_key in source:
            values.append((f"{namespace}.{nested_key}", source[nested_key]))
    if not values:
        return None, None
    source, value = values[0]
    for other_source, other_value in values[1:]:
        if other_value != value:
            raise EvidenceError(
                f"execution manifest {source} and {other_source} conflict"
            )
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise EvidenceError(f"execution manifest {label} is invalid")
    return value, source


def _load_quick_status_identity(manifest_path: Path | str) -> dict[str, Any]:
    path = Path(manifest_path).resolve()
    base = load_execution_identity(path)
    manifest = read_json(path)
    if not isinstance(manifest, dict):
        raise EvidenceError("execution manifest must be an object")
    execution_id, execution_source = _optional_manifest_identity_value(
        manifest, "execution_id", "execution_id", "execution_id"
    )
    if execution_id is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]+", execution_id):
        raise EvidenceError("execution manifest execution_id is unsafe")
    tmux_session, tmux_source = _optional_manifest_identity_value(
        manifest, "tmux_session", "tmux_session", "tmux_session"
    )
    if tmux_session is not None and not TMUX_SESSION_RE.fullmatch(tmux_session):
        raise EvidenceError("execution manifest tmux_session is unsafe")
    return {
        **base,
        "execution_id": execution_id,
        "tmux_session": tmux_session,
        "quick_identity_sources": {
            "execution_id": execution_source,
            "tmux_session": tmux_source,
        },
    }


def build_quick_status_script(manifest_path: Path | str) -> str:
    """Render a one-shot, read-only remote status/tail probe for one manifest."""

    identity = _load_quick_status_identity(manifest_path)
    remote_identity = {
        "task_id": identity["task_id"],
        "unit_id": identity["unit_id"],
        "execution_id": identity["execution_id"],
        "remote_batch_dir": identity["remote_batch_dir"],
        "case": identity["case"],
        "remote_case_dir": identity["remote_case_dir"],
        "tmux_session": identity["tmux_session"],
    }
    identity_literal = repr(json.dumps(remote_identity, ensure_ascii=False, separators=(",", ":")))
    template = r'''#!/usr/bin/env python3
import json, os, re, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

IDENTITY = json.loads(__QUICK_STATUS_IDENTITY__)
FRESH_SECONDS = 300
MAX_PID_PROBES = 8
SAFE_SESSION = re.compile(r"^[A-Za-z0-9_.-]+$")
IONIC = re.compile(r"^\s*(\d+)\s+F\s*=\s*([-+0-9.EeDd]+)\s+E0\s*=\s*([-+0-9.EeDd]+)")
ELECTRONIC = re.compile(r"^\s*(DAV|RMM):\s*(\d+)\b", re.I)
OUTCAR_MARKER = re.compile(
    r"WARNING|FATAL|MPI_ABORT|SEGMENTATION\s+FAULT|VERY\s+SERIOUS|"
    r"reached required accuracy|aborting loop because EDIFF is reached|"
    r"general timing and accounting informations|LOOP\+?\s*:", re.I
)
TAIL_LIMITS = {
    "OUTCAR": 64 * 1024,
    "OSZICAR": 32 * 1024,
    "vasp.stdout": 16 * 1024,
    "vasp.stderr": 16 * 1024,
}
BATCH = Path(IDENTITY["remote_batch_dir"])
CASE = Path(IDENTITY["remote_case_dir"])
NOW = datetime.now(timezone.utc)
NOW_UTC = NOW.isoformat().replace("+00:00", "Z")

def mtime_utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")

def stat_file(path):
    try:
        item = path.stat()
    except FileNotFoundError:
        return {"present": False, "bytes": None, "mtime_utc": None}
    except OSError as error:
        return {"present": None, "bytes": None, "mtime_utc": None,
                "error": type(error).__name__}
    return {"present": True, "bytes": item.st_size, "mtime_utc": mtime_utc(item.st_mtime)}

def age_seconds(timestamp):
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return max(0, int((NOW - parsed.astimezone(timezone.utc)).total_seconds()))
    except (ValueError, TypeError):
        return None

def freshness(timestamp):
    age = age_seconds(timestamp)
    if age is None:
        return {"state": "UNKNOWN", "age_seconds": None}
    return {"state": "FRESH" if age <= FRESH_SECONDS else "STALE",
            "age_seconds": age, "fresh_window_seconds": FRESH_SECONDS}

def capture(path, limit, tail):
    before = stat_file(path)
    result = {"source_path": str(path), "read_before": before,
              "read_after": None, "read_bytes": 0, "byte_offset": None,
              "coverage": "unknown", "truncated_prefix": False,
              "stable": False, "read_error": None}
    data = b""
    if before.get("present") is not True:
        result["reason"] = "missing" if before.get("present") is False else "stat_failed"
        return result, data
    size = before.get("bytes")
    offset = max(0, size - limit) if tail and isinstance(size, int) else 0
    if not tail and isinstance(size, int) and size > limit:
        result["read_error"] = "metadata_exceeds_limit"
    else:
        try:
            with path.open("rb") as stream:
                stream.seek(offset)
                data = stream.read(limit)
            result["read_bytes"] = len(data)
            result["byte_offset"] = offset
            result["coverage"] = "tail" if offset else "full"
            result["truncated_prefix"] = bool(offset)
        except OSError as error:
            result["read_error"] = type(error).__name__
    after = stat_file(path)
    result["read_after"] = after
    result["stable"] = (
        before.get("present") is True and after.get("present") is True
        and before.get("bytes") == after.get("bytes")
        and before.get("mtime_utc") == after.get("mtime_utc")
    )
    if not result["stable"]:
        result["reason"] = "changed_during_read"
    return result, data

def identity_binding(task_id, unit_id, execution_id=None):
    task_match = task_id == IDENTITY["task_id"]
    unit_match = unit_id == IDENTITY["unit_id"]
    expected_execution = IDENTITY.get("execution_id")
    if not task_match or not unit_match:
        return {"state": "MISMATCH", "task_match": task_match,
                "unit_match": unit_match, "execution_match": None}
    if expected_execution is not None:
        execution_match = execution_id == expected_execution
        return {"state": "MATCH" if execution_match else "MISMATCH",
                "task_match": True, "unit_match": True,
                "execution_match": execution_match}
    return {"state": "WEAK", "task_match": True, "unit_match": True,
            "execution_match": None}

def positive_pid(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,9}", value.strip()):
        return int(value.strip())
    return None

def timing_values(text):
    values, conflicts = {}, []
    allowed = {
        "task_id", "workflow_unit_id", "unit_id", "execution_id", "case_id",
        "status", "session", "start_utc", "started_utc", "end_utc", "ended_utc",
        "exit_code", "runner_exit_code", "supervisor_exit_code", "postcheck_exit_code",
        "supervisor_pid", "runner_pid", "mpi_pid", "mpi_launcher_pid",
    }
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in allowed:
            continue
        if key in values and values[key] != value:
            conflicts.append(key)
        else:
            values[key] = value
    return values, sorted(set(conflicts))

def selected_lines(data, byte_offset, pattern, limit):
    selected, cursor = [], byte_offset or 0
    for local_line, chunk in enumerate(data.splitlines(keepends=True), 1):
        line = chunk.rstrip(b"\r\n").decode("utf-8", "replace")
        if pattern.search(line):
            selected.append({"local_line": local_line,
                             "byte_offset": cursor,
                             "raw": line[:360]})
        cursor += len(chunk)
    return selected[-limit:]

def read_text_entry(entry, data):
    if not data and entry.get("read_bytes", 0) == 0:
        return ""
    return data.decode("utf-8", "replace")

def probe_pid(pid, declarations):
    result = {"pid": pid, "declared_by": declarations,
              "state": "UNKNOWN", "identity_match": "PID_ONLY_UNVERIFIED"}
    try:
        checked = subprocess.run(
            ["ps", "-p", str(pid), "-o", "pid=,stat=,etime=,comm="],
            capture_output=True, text=True, check=False, timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        result["reason"] = type(error).__name__
        return result
    line = checked.stdout.strip().splitlines()
    if checked.returncode == 0 and line:
        parts = line[0].split(None, 3)
        if len(parts) >= 4:
            state = "ZOMBIE" if parts[1].startswith("Z") else "PRESENT"
            result.update({"state": state, "pid": int(parts[0]),
                           "process_state": parts[1], "elapsed": parts[2],
                           "comm": parts[3]})
        else:
            result["reason"] = "unparseable_ps_record"
    elif checked.returncode == 1 and not line:
        result["state"] = "ABSENT"
    else:
        result["reason"] = "ps_probe_failed"
    return result

def main():
    status_path = BATCH / ".job_watch" / "status.json"
    timing_path = CASE / "run_timing.txt"
    entries, data = {}, {}
    entries["status.json"], data["status.json"] = capture(status_path, 64 * 1024, False)
    entries["run_timing.txt"], data["run_timing.txt"] = capture(timing_path, 32 * 1024, False)
    for name, limit in TAIL_LIMITS.items():
        entries[name], data[name] = capture(CASE / name, limit, True)

    status_doc, status_error = None, None
    if entries["status.json"].get("read_error"):
        status_error = entries["status.json"]["read_error"]
    elif entries["status.json"].get("read_bytes", 0):
        try:
            status_doc = json.loads(data["status.json"].decode("utf-8"))
            if not isinstance(status_doc, dict):
                status_doc, status_error = None, "status_not_object"
        except (UnicodeDecodeError, json.JSONDecodeError):
            status_error = "status_json_invalid_or_truncated"
    timing_doc, timing_error = {}, None
    if entries["run_timing.txt"].get("read_error"):
        timing_error = entries["run_timing.txt"]["read_error"]
    elif entries["run_timing.txt"].get("read_bytes", 0):
        timing_doc, timing_conflicts = timing_values(
            data["run_timing.txt"].decode("utf-8", "replace")
        )
        if timing_conflicts:
            timing_error = "duplicate_conflicting_keys:" + ",".join(timing_conflicts)

    status_doc = status_doc if isinstance(status_doc, dict) else {}
    status_task = status_doc.get("task_id")
    if status_task is None and status_doc.get("job") == IDENTITY["task_id"]:
        status_task = status_doc.get("job")
    status_unit = status_doc.get("unit_id") or status_doc.get("workflow_unit_id")
    status_binding = identity_binding(status_task, status_unit, status_doc.get("execution_id"))
    status_binding["legacy_job_alias"] = status_doc.get("task_id") is None and status_task is not None
    status_updated = status_doc.get("updated_utc")
    if status_updated is None and isinstance(status_doc.get("updated_epoch"), (int, float)):
        status_updated = mtime_utc(status_doc["updated_epoch"])
    status_file_freshness = freshness(status_updated or (entries["status.json"].get("read_after") or {}).get("mtime_utc"))
    status_info = {
        "available": bool(status_doc), "identity_binding": status_binding,
        "raw_status": status_doc.get("status"),
        "execution_id": status_doc.get("execution_id"),
        "started_utc": status_doc.get("started_utc"),
        "updated_utc": status_updated, "ended_utc": status_doc.get("ended_utc"),
        "state_changed_utc": status_doc.get("state_changed_utc"),
        "record_written_utc": status_doc.get("record_written_utc"),
        "temporal_semantics": "LAST_STATUS_WRITE_NOT_HEARTBEAT",
        "freshness": status_file_freshness,
        "exit_codes": {key: status_doc.get(key) for key in (
            "exit_code", "runner_exit_code", "supervisor_exit_code", "postcheck_exit_code"
        ) if key in status_doc},
        "expected_tmux_session": status_doc.get("expected_tmux_session"),
        "pids": {key: status_doc.get(key) for key in (
            "supervisor_pid", "runner_pid", "mpi_launcher_pid", "mpi_pid"
        ) if key in status_doc},
        "error": status_error,
    }
    timing_unit = timing_doc.get("workflow_unit_id") or timing_doc.get("unit_id")
    timing_binding = identity_binding(
        timing_doc.get("task_id"), timing_unit, timing_doc.get("execution_id")
    )
    if timing_doc.get("case_id") is not None and timing_doc.get("case_id") != IDENTITY["case"]:
        timing_binding["state"] = "MISMATCH"
        timing_binding["case_match"] = False
    else:
        timing_binding["case_match"] = True if timing_doc.get("case_id") is not None else None
    timing_mtime = (entries["run_timing.txt"].get("read_after") or {}).get("mtime_utc")
    timing_info = {
        "available": bool(timing_doc), "identity_binding": timing_binding,
        "raw_status": timing_doc.get("status"),
        "execution_id": timing_doc.get("execution_id"),
        "started_utc": timing_doc.get("start_utc") or timing_doc.get("started_utc"),
        "ended_utc": timing_doc.get("end_utc") or timing_doc.get("ended_utc"),
        "freshness": freshness(timing_mtime),
        "exit_codes": {key: timing_doc.get(key) for key in (
            "exit_code", "runner_exit_code", "supervisor_exit_code", "postcheck_exit_code"
        ) if key in timing_doc},
        "tmux_session": timing_doc.get("session"),
        "pids": {key: timing_doc.get(key) for key in (
            "supervisor_pid", "runner_pid", "mpi_launcher_pid", "mpi_pid"
        ) if key in timing_doc},
        "error": timing_error,
    }

    pid_declarations = {}
    if status_binding["state"] in {"MATCH", "WEAK"}:
        for role, value in status_info["pids"].items():
            pid = positive_pid(value)
            if pid is not None:
                pid_declarations.setdefault(pid, []).append("status.json." + role)
    if timing_binding["state"] in {"MATCH", "WEAK"}:
        for role, value in timing_info["pids"].items():
            pid = positive_pid(value)
            if pid is not None:
                pid_declarations.setdefault(pid, []).append("run_timing.txt." + role)
    pid_items = list(pid_declarations.items())
    process_checks = [probe_pid(pid, sources) for pid, sources in pid_items[:MAX_PID_PROBES]]
    pid_probe_limit_reached = len(pid_items) > MAX_PID_PROBES

    session_sources = []
    if IDENTITY.get("tmux_session") is not None:
        session_sources.append({"source": "execution_manifest", "value": IDENTITY["tmux_session"]})
    if status_binding["state"] in {"MATCH", "WEAK"} and status_info.get("expected_tmux_session"):
        session_sources.append({"source": "status.json", "value": status_info["expected_tmux_session"]})
    if timing_binding["state"] in {"MATCH", "WEAK"} and timing_info.get("tmux_session"):
        session_sources.append({"source": "run_timing.txt", "value": timing_info["tmux_session"]})
    session_values = sorted(set(item["value"] for item in session_sources))
    tmux_info = {"state": "UNKNOWN", "session": None, "pane_dead": None,
                 "pane_pid": None, "declared_by": session_sources}
    if len(session_values) > 1:
        tmux_info.update({"state": "CONFLICT", "reason": "declared_session_values_conflict"})
    elif session_values:
        session = session_values[0]
        tmux_info["session"] = session
        if not SAFE_SESSION.fullmatch(session):
            tmux_info.update({"state": "CONFLICT", "reason": "unsafe_session_name"})
        else:
            try:
                checked = subprocess.run(
                    ["tmux", "display-message", "-p", "-t", session,
                     "#{session_name}\t#{pane_dead}\t#{pane_pid}"],
                    capture_output=True, text=True, check=False, timeout=1,
                )
                fields = checked.stdout.strip().split("\t")
                if checked.returncode == 0 and len(fields) == 3 and fields[0] == session:
                    tmux_info.update({"state": "PRESENT", "pane_dead": fields[1] == "1",
                                      "pane_pid": positive_pid(fields[2])})
                elif checked.returncode == 1:
                    tmux_info.update({"state": "ABSENT", "reason": "session_not_found"})
                else:
                    tmux_info.update({"state": "UNKNOWN", "reason": "tmux_probe_failed"})
            except (OSError, subprocess.TimeoutExpired) as error:
                tmux_info.update({"state": "UNKNOWN", "reason": type(error).__name__})

    oszicar_entry = entries["OSZICAR"]
    oszicar_text = read_text_entry(oszicar_entry, data["OSZICAR"])
    oszicar_lines = oszicar_text.splitlines()
    ionic_rows = []
    electronic_rows = []
    last_ionic_line = -1
    for index, line in enumerate(oszicar_lines):
        ionic_match = IONIC.match(line)
        if ionic_match:
            last_ionic_line = index
            ionic_rows.append({"ionic_step": int(ionic_match.group(1)),
                               "F_raw": ionic_match.group(2),
                               "E0_raw": ionic_match.group(3), "raw": line[:360],
                               "tail_local_line": index + 1})
        electronic_match = ELECTRONIC.match(line)
        if electronic_match:
            electronic_rows.append({"algorithm": electronic_match.group(1).upper(),
                                    "iteration": int(electronic_match.group(2)),
                                    "raw": line[:360], "tail_local_line": index + 1})
    last_complete_ionic_line = last_ionic_line + 1
    for row in electronic_rows:
        row["after_last_complete_ionic_row"] = (
            row["tail_local_line"] > last_complete_ionic_line
        )
    progress = {
        "oszicar": {
            "tail": oszicar_entry,
            "last_visible_complete_ionic_row": ionic_rows[-1] if ionic_rows else None,
            "visible_complete_ionic_rows": len(ionic_rows),
            "tail_visible_electronic_rows": electronic_rows[-8:],
        },
        "outcar": {
            "tail": entries["OUTCAR"],
            "marker_lines": selected_lines(data["OUTCAR"], entries["OUTCAR"].get("byte_offset"), OUTCAR_MARKER, 8),
        },
        "stdout": {
            "tail": entries["vasp.stdout"],
            "electronic_rows": selected_lines(data["vasp.stdout"], entries["vasp.stdout"].get("byte_offset"), ELECTRONIC, 5),
            "ionic_rows": selected_lines(data["vasp.stdout"], entries["vasp.stdout"].get("byte_offset"), IONIC, 3),
            "tail_lines": [line[:360] for line in read_text_entry(entries["vasp.stdout"], data["vasp.stdout"]).splitlines()[-4:]],
        },
        "stderr": {
            "tail": entries["vasp.stderr"],
            "tail_lines": [line[:360] for line in read_text_entry(entries["vasp.stderr"], data["vasp.stderr"]).splitlines()[-8:]],
        },
    }
    output = {
        "schema": "vasp-quick-status/v1",
        "observed_utc": NOW_UTC,
        "identity": IDENTITY,
        "sources": {"status_json": status_info, "run_timing": timing_info},
        "files": entries,
        "processes": process_checks,
        "process_probe_limit_reached": pid_probe_limit_reached,
        "tmux": tmux_info,
        "progress": progress,
        "limits": {"outcar_tail_bytes": 64 * 1024,
                   "oszicar_tail_bytes": 32 * 1024,
                   "stdout_tail_bytes": 16 * 1024,
                   "stderr_tail_bytes": 16 * 1024,
                   "pid_probes_are_exact_and_unverified_beyond_pid": True,
                   "no_directory_scan": True},
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n")

if __name__ == "__main__":
    main()
'''
    return template.replace("__QUICK_STATUS_IDENTITY__", identity_literal)


def _normalise_quick_state(value: Any) -> tuple[str, str | None]:
    try:
        return normalize_state(value)
    except ValueError:
        return "UNKNOWN", None


def _quick_status_canonical(remote: dict[str, Any]) -> tuple[str, str, list[str]]:
    reasons: list[str] = []
    sources = remote.get("sources")
    if not isinstance(sources, dict):
        return "UNKNOWN", "UNKNOWN", ["remote status sources are missing"]
    status = sources.get("status_json")
    timing = sources.get("run_timing")
    if not isinstance(status, dict) or not isinstance(timing, dict):
        return "UNKNOWN", "UNKNOWN", ["status.json or run_timing source is missing"]
    status_binding_doc = status.get("identity_binding")
    timing_binding_doc = timing.get("identity_binding")
    status_binding = (
        status_binding_doc.get("state") if isinstance(status_binding_doc, dict) else None
    )
    timing_binding = (
        timing_binding_doc.get("state") if isinstance(timing_binding_doc, dict) else None
    )
    if status_binding != "MATCH":
        reasons.append("status.json identity binding is not strong and exact")
    if timing_binding != "MATCH":
        reasons.append("run_timing identity binding is not exact")
    if status_binding != "MATCH" or timing_binding != "MATCH":
        return "UNKNOWN", "UNKNOWN", reasons
    status_state, status_alias = _normalise_quick_state(status.get("raw_status"))
    timing_state, timing_alias = _normalise_quick_state(timing.get("raw_status"))
    if status_alias is not None:
        status["legacy_alias"] = status_alias
    if timing_alias is not None:
        timing["legacy_alias"] = timing_alias
    if status_state == "UNKNOWN" or timing_state == "UNKNOWN":
        reasons.append("one or more reported execution states are absent or unknown")
        return "UNKNOWN", "UNKNOWN", reasons
    if status_state != timing_state:
        reasons.append("status.json and run_timing report conflicting execution states")
        return "UNKNOWN", "CONFLICT", reasons

    processes = remote.get("processes")
    tmux = remote.get("tmux")
    processes = processes if isinstance(processes, list) else []
    tmux = tmux if isinstance(tmux, dict) else {}
    if remote.get("process_probe_limit_reached"):
        reasons.append("task-declared PID probe limit was reached")
        return "UNKNOWN", "UNKNOWN", reasons
    process_states = [item.get("state") for item in processes if isinstance(item, dict)]
    tmux_state = tmux.get("state")
    if status_state == RUNNING:
        if any(state in {"ABSENT", "ZOMBIE"} for state in process_states) or tmux_state == "ABSENT" or (
            tmux_state == "PRESENT" and tmux.get("pane_dead") is True
        ):
            reasons.append("reported RUNNING conflicts with an absent process or dead/missing tmux session")
            return "UNKNOWN", "CONFLICT", reasons
        if not process_states or any(state != "PRESENT" for state in process_states) or tmux_state != "PRESENT" or tmux.get("pane_dead") is not False:
            reasons.append("live PID and tmux evidence is incomplete")
            return "UNKNOWN", "UNKNOWN", reasons
    elif status_state in TERMINAL_STATES:
        tmux_terminated = tmux_state == "ABSENT" or (
            tmux_state == "PRESENT" and tmux.get("pane_dead") is True
        )
        if any(state == "PRESENT" for state in process_states) or (
            tmux_state == "PRESENT" and tmux.get("pane_dead") is False
        ):
            reasons.append("reported terminal state conflicts with a live declared PID or tmux pane")
            return "UNKNOWN", "CONFLICT", reasons
        if (
            not process_states
            or any(state not in {"ABSENT", "ZOMBIE"} for state in process_states)
            or not tmux_terminated
        ):
            reasons.append("terminal process/tmux evidence is incomplete")
            return "UNKNOWN", "UNKNOWN", reasons
    elif status_state == STARTING:
        if not process_states or not any(state == "PRESENT" for state in process_states):
            reasons.append("STARTING has no live task-declared PID evidence")
            return "UNKNOWN", "UNKNOWN", reasons
    elif status_state == NOT_STARTED:
        if any(state == "PRESENT" for state in process_states) or (
            tmux_state == "PRESENT" and tmux.get("pane_dead") is False
        ):
            reasons.append("NOT_STARTED conflicts with a live task-declared process/session")
            return "UNKNOWN", "CONFLICT", reasons
        if (
            any(state not in {"ABSENT", "ZOMBIE"} for state in process_states)
            or not (
                tmux_state == "ABSENT"
                or (tmux_state == "PRESENT" and tmux.get("pane_dead") is True)
            )
        ):
            reasons.append("NOT_STARTED process/tmux evidence is incomplete")
            return "UNKNOWN", "UNKNOWN", reasons
    else:
        reasons.append("execution state is not covered by the quick-status consistency rules")
        return "UNKNOWN", "UNKNOWN", reasons
    return status_state, "AGREED", reasons


def quick_status(
    manifest_path: Path | str,
    *,
    runner: Any = None,
) -> dict[str, Any]:
    """Issue exactly one bounded read-only SSH quick-status query."""

    observed_local_start = utc_now()
    start_clock = time.monotonic()
    try:
        identity = _load_quick_status_identity(manifest_path)
        script = build_quick_status_script(manifest_path)
    except (EvidenceError, OSError, ValueError) as error:
        return {
            "schema": QUICK_STATUS_SCHEMA,
            "query_status": "PARTIAL",
            "canonical_status": "UNKNOWN",
            "state_consistency": "UNKNOWN",
            "observed_local_utc": observed_local_start,
            "identity": None,
            "ssh": {"attempted": False, "exit_code": None, "timed_out": False,
                    "reason": "manifest_identity_rejected"},
            "reasons": [f"{type(error).__name__}: {error}"],
            "scientific_acceptance": "NOT_ASSESSED",
        }
    argv = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        "-p", str(identity["host"]["port"]),
        f"{identity['host']['user']}@{identity['host']['address']}",
        "python3", "-",
    ]
    invoke = runner if runner is not None else subprocess.run
    ended = utc_now()
    try:
        completed = invoke(
            argv,
            input=script.encode("utf-8"),
            capture_output=True,
            check=False,
            timeout=30,
        )
        ended = utc_now()
        ssh_exit = completed.returncode
        stdout = completed.stdout or b""
        stderr = completed.stderr or b""
        timed_out = False
    except subprocess.TimeoutExpired as error:
        ended = utc_now()
        ssh_exit = None
        stdout = error.stdout or b""
        stderr = error.stderr or b""
        timed_out = True
    except OSError as error:
        ended = utc_now()
        ssh_exit = None
        stdout = b""
        stderr = str(error).encode("utf-8", errors="replace")
        timed_out = False
    duration = max(0.0, time.monotonic() - start_clock)
    base = {
        "schema": QUICK_STATUS_SCHEMA,
        "query_status": "PARTIAL",
        "canonical_status": "UNKNOWN",
        "state_consistency": "UNKNOWN",
        "observed_local_utc": ended,
        "identity": {
            "task_id": identity["task_id"],
            "unit_id": identity["unit_id"],
            "execution_id": identity["execution_id"],
            "execution_identity_strength": "STRONG" if identity["execution_id"] else "WEAK",
            "host": identity["host"],
            "remote_batch_dir": identity["remote_batch_dir"],
            "remote_case_dir": identity["remote_case_dir"],
            "tmux_session": identity["tmux_session"],
        },
        "ssh": {
            "attempted": True,
            "argv": argv,
            "exit_code": ssh_exit,
            "timed_out": timed_out,
            "timeout_seconds": 30,
            "started_utc": observed_local_start,
            "ended_utc": ended,
            "duration_seconds": round(duration, 3),
            "stderr_text": stderr.decode("utf-8", errors="replace")[:2048],
            "stderr_truncated": len(stderr) > 2048,
        },
        "scientific_acceptance": "NOT_ASSESSED",
        "reasons": [],
    }
    if timed_out:
        base["reasons"].append("SSH quick-status exceeded the single 30-second timeout")
        return base
    if ssh_exit != 0:
        base["reasons"].append(f"SSH quick-status exited with code {ssh_exit}")
        return base
    try:
        remote = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        base["reasons"].append("remote quick-status stdout is not valid JSON")
        base["remote_stdout_excerpt"] = stdout.decode("utf-8", errors="replace")[:1024]
        return base
    if not isinstance(remote, dict) or remote.get("schema") != QUICK_STATUS_SCHEMA:
        base["reasons"].append("remote quick-status schema is missing or unsupported")
        return base
    remote_identity = remote.get("identity")
    if not isinstance(remote_identity, dict):
        base["reasons"].append("remote quick-status identity is missing")
        return base
    expected_identity = {
        "task_id": identity["task_id"],
        "unit_id": identity["unit_id"],
        "remote_batch_dir": identity["remote_batch_dir"],
        "remote_case_dir": identity["remote_case_dir"],
    }
    if identity["execution_id"] is not None:
        expected_identity["execution_id"] = identity["execution_id"]
    if identity["tmux_session"] is not None:
        expected_identity["tmux_session"] = identity["tmux_session"]
    if any(remote_identity.get(key) != value for key, value in expected_identity.items()):
        base["reasons"].append("remote quick-status identity differs from the local manifest")
        base["remote_identity"] = remote_identity
        return base
    canonical, consistency, reasons = _quick_status_canonical(remote)
    remote_sources = remote.get("sources")
    remote_sources = remote_sources if isinstance(remote_sources, dict) else {}
    base.update({
        "canonical_status": canonical,
        "state_consistency": consistency,
        "remote_observed_utc": remote.get("observed_utc"),
        "sources": remote.get("sources"),
        "processes": remote.get("processes"),
        "tmux": remote.get("tmux"),
        "progress": remote.get("progress"),
        "files": remote.get("files"),
        "limits": remote.get("limits"),
    })
    base["reasons"].extend(reasons)
    for source_name in ("status_json", "run_timing"):
        source = remote_sources.get(source_name)
        if not isinstance(source, dict):
            base["reasons"].append(f"{source_name} is missing")
            continue
        freshness_info = source.get("freshness")
        if not isinstance(freshness_info, dict) or freshness_info.get("state") == "UNKNOWN":
            base["reasons"].append(f"{source_name} freshness is unknown")
        elif freshness_info.get("state") == "STALE" and source_name != "status_json":
            base["reasons"].append(f"{source_name} is stale")
        if source.get("error"):
            base["reasons"].append(f"{source_name}: {source['error']}")
    files = remote.get("files")
    if not isinstance(files, dict):
        base["reasons"].append("remote file metadata is missing")
    else:
        for role in ("status.json", "run_timing.txt", "OUTCAR", "OSZICAR", "vasp.stdout", "vasp.stderr"):
            item = files.get(role)
            if not isinstance(item, dict):
                base["reasons"].append(f"{role} metadata is missing")
                continue
            read_before = item.get("read_before")
            if not isinstance(read_before, dict) or read_before.get("present") is not True:
                base["reasons"].append(f"{role} is missing or unreadable")
            if item.get("stable") is not True:
                base["reasons"].append(f"{role} changed or was unstable during the read")
            if item.get("read_error"):
                base["reasons"].append(f"{role}: {item['read_error']}")
            if item.get("truncated_prefix"):
                base["reasons"].append(f"{role} reports a bounded tail; earlier bytes were not inspected")
    if remote.get("process_probe_limit_reached"):
        base["reasons"].append("task-declared PID probe limit reached")
    base["query_status"] = "PARTIAL" if base["reasons"] else "OK"
    return base


def expected_source(identity: dict[str, Any], role: str) -> str:
    if role in PRIMARY_INPUTS:
        return f"{identity['remote_input_dir']}/{role}"
    return f"{identity['remote_case_dir']}/{role}"


def source_ref(record: dict[str, Any], local_line: int | None = None) -> dict[str, Any]:
    result = {
        "snapshot_path": record.get("snapshot_path"),
        "source_path": record.get("source_path"),
    }
    if local_line is not None:
        result["snapshot_line"] = local_line
        start = record.get("original_start_line")
        if isinstance(start, int) and start >= 1:
            result["original_line"] = start + local_line - 1
        else:
            result["original_line"] = None
            result["original_line_reason"] = "original_start_line is absent"
    return result


def finite(value: str) -> float | None:
    try:
        parsed = float(value.replace("D", "E").replace("d", "e"))
    except (AttributeError, TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _line_count(data: bytes) -> int:
    return len(data.decode("utf-8", errors="replace").splitlines())


def _offsets(
    descriptor: dict[str, Any],
    data: bytes,
    *,
    full_default: bool = False,
) -> dict[str, Any]:
    coverage = descriptor.get("coverage")
    if coverage is None and full_default:
        coverage = "full"
    if coverage not in ALLOWED_COVERAGE:
        coverage = "unknown"
    result: dict[str, Any] = {
        "coverage": coverage,
        "original_start_line": descriptor.get("original_start_line"),
        "original_end_line": descriptor.get("original_end_line"),
        "original_start_byte": descriptor.get("original_start_byte"),
        "original_end_byte": descriptor.get("original_end_byte"),
    }
    if full_default and coverage == "full":
        result["original_start_line"] = 1
        result["original_end_line"] = _line_count(data)
        result["original_start_byte"] = 0
        result["original_end_byte"] = len(data)
    return result


def offsets_valid(record: dict[str, Any]) -> tuple[bool, str | None]:
    coverage = record.get("coverage")
    if coverage not in ALLOWED_COVERAGE or coverage == "unknown":
        return False, "coverage is absent or unknown"
    line_start = record.get("original_start_line")
    line_end = record.get("original_end_line")
    byte_start = record.get("original_start_byte")
    byte_end = record.get("original_end_byte")
    line_ok = (
        isinstance(line_start, int)
        and isinstance(line_end, int)
        and line_start >= 1
        and line_end >= line_start
    )
    byte_ok = (
        isinstance(byte_start, int)
        and isinstance(byte_end, int)
        and byte_start >= 0
        and byte_end >= byte_start
    )
    if not (line_ok or byte_ok):
        return False, "original line or byte offsets are missing/invalid"
    if coverage == "full" and line_start != 1 and byte_start != 0:
        return False, "full coverage must start at original line 1 or byte 0"
    return True, None


def _stat_pair_consistent(record: dict[str, Any]) -> bool:
    before = record.get("read_before")
    after = record.get("read_after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    if before.get("present") is not True or after.get("present") is not True:
        return False
    return (
        before.get("bytes") == after.get("bytes")
        and before.get("mtime_utc") == after.get("mtime_utc")
    )


def _descriptor_list(role: str, spec: Any) -> list[dict[str, Any]]:
    if not isinstance(spec, dict):
        return []
    fragments = spec.get("fragments")
    if isinstance(fragments, list):
        result = []
        for index, item in enumerate(fragments, 1):
            if isinstance(item, dict):
                result.append({**item, "role": role, "fragment_index": index})
        return result
    return [{**spec, "role": role, "fragment_index": 1}]


def _diagnostic_category(line: str) -> tuple[str, str] | None:
    clean = line.strip()
    upper = clean.upper()
    warning = re.search(r"\bWARNING\b\s*:?\s*(.*)", clean, re.IGNORECASE)
    if warning:
        detail = re.sub(r"\s+", " ", warning.group(1)).strip()
        return "warning", detail[:180] or "WARNING"
    if re.search(
        r"FATAL|SEGMENTATION\s+FAULT|MPI_ABORT|INTERNAL\s+ERROR|"
        r"\bERROR\b|ZBRENT|VERY\s+SERIOUS|BRMIX",
        upper,
    ):
        detail = re.sub(r"\s+", " ", clean)
        return "error_or_suspicion", detail[:180] or "error_or_suspicion"
    return None


def _contains_paw_body(text: str) -> bool:
    return bool(PAW_BODY_RE.search(text))


def _remote_stat_from_meta(meta: dict[str, Any] | None, key: str, fallback: dict[str, Any]) -> dict[str, Any]:
    if isinstance(meta, dict) and isinstance(meta.get(key), dict):
        return dict(meta[key])
    return fallback


def _record_metadata(
    descriptor: dict[str, Any],
    data: bytes,
    before: dict[str, Any],
    after: dict[str, Any],
    remote_meta: dict[str, Any] | None,
    snapshot_path: str | None,
) -> dict[str, Any]:
    before_value = _remote_stat_from_meta(remote_meta, "read_before", before)
    after_value = _remote_stat_from_meta(remote_meta, "read_after", after)
    result = {
        "role": descriptor["role"],
        "kind": descriptor.get("kind", descriptor["role"]),
        "fragment_id": descriptor.get(
            "fragment_id",
            f"{descriptor['role']}-{descriptor['fragment_index']:03d}",
        ),
        "source_path": descriptor.get("source_path"),
        "snapshot_path": snapshot_path,
        "read_content": snapshot_path is not None,
        "read_before": before_value,
        "read_after": after_value,
        "read_consistent": (
            before_value.get("bytes") == after_value.get("bytes")
            and before_value.get("mtime_utc") == after_value.get("mtime_utc")
        ),
    }
    result.update(_offsets(descriptor, data, full_default=descriptor["role"] in PRIMARY_INPUTS))
    if snapshot_path is not None:
        result["snapshot_start_line"] = 1
        result["snapshot_end_line"] = _line_count(data)
        result["snapshot_bytes"] = len(data)
    if descriptor.get("context") is not None:
        result["context"] = descriptor["context"]
    for key in ("parameter_tag", "parameter_section", "parameter_center"):
        if key in descriptor:
            result[key] = descriptor[key]
    return result


def _remote_meta_for(
    capture_meta: dict[str, Any],
    role: str,
    fragment_id: str | None = None,
) -> dict[str, Any] | None:
    files = capture_meta.get("files")
    if not isinstance(files, dict):
        return None
    item = files.get(role)
    if isinstance(item, dict):
        if fragment_id and isinstance(item.get("fragments"), dict):
            nested = item["fragments"].get(fragment_id)
            return nested if isinstance(nested, dict) else item
        return item
    return None


def capture_bundle(
    task_dir: Path | str,
    manifest_path: Path | str,
    source_dir: Path | str,
    *,
    capture_metadata_path: Path | str | None = None,
    snapshot_name: str | None = None,
) -> Path:
    """Copy an already-collected local source into a new immutable evidence bundle.

    source_dir is deliberately a fixture/staging abstraction. The reviewed SSH
    path emits the same descriptor records and is ingested separately; this
    function itself never opens a network connection.
    """

    task_root = Path(task_dir).resolve()
    manifest = Path(manifest_path).resolve()
    source_root = Path(source_dir).resolve()
    if not task_root.is_dir() or not source_root.is_dir():
        raise EvidenceError("task_dir and source_dir must be existing directories")
    identity = load_execution_identity(manifest)
    if capture_metadata_path is None:
        capture_meta: dict[str, Any] = {}
    else:
        raw_meta = read_json(Path(capture_metadata_path).resolve())
        capture_meta = raw_meta if isinstance(raw_meta, dict) else {}
    collection_start = capture_meta.get("collection_start_utc") or utc_now()
    collection_end = capture_meta.get("collection_end_utc") or utc_now()
    command = capture_meta.get("command")
    if not isinstance(command, dict):
        command = {
            "mode": "local_fixture",
            "read_only": True,
            "argv": ["local-fixture-source"],
            "exit_code": 0,
        }
    snapshots_root = task_root / "snapshots"
    snapshots_root.mkdir(parents=True, exist_ok=True)
    name = snapshot_name or compact_utc(collection_start)
    if not re.fullmatch(r"\d{8}T\d{6}Z", name):
        raise EvidenceError(f"snapshot name is not a UTC timestamp: {name}")
    bundle = snapshots_root / name
    bundle.mkdir(parents=False, exist_ok=False)
    raw_root = bundle / "raw"
    raw_root.mkdir()
    records_by_role: dict[str, list[dict[str, Any]]] = defaultdict(list)
    failures: list[str] = []
    sensitive_stats: dict[str, Any] = {}
    specs = capture_meta.get("files", {})
    if not isinstance(specs, dict):
        specs = {}

    # The local source descriptor is explicit. A remote path is never inferred
    # from a user-supplied batch/case string.
    for role in PRIMARY_INPUTS + OUTPUT_ROLES + tuple(sorted(SENSITIVE_FILES)):
        role_spec = specs.get(role)
        descriptors = _descriptor_list(role, role_spec)
        if not descriptors and role in PRIMARY_INPUTS:
            descriptors = [{
                "role": role,
                "kind": role,
                "fragment_index": 1,
                "input_path": role,
                "source_path": expected_source(identity, role),
                "coverage": "full",
            }]
        for descriptor in descriptors:
            descriptor["role"] = role
            source_path = descriptor.get("source_path")
            if role in SENSITIVE_FILES:
                if source_path != expected_source(identity, role):
                    failures.append(f"{role}: source_path does not match manifest-derived path")
                    continue
                input_path = descriptor.get("input_path")
                if isinstance(input_path, str):
                    source_file = safe_relative(source_root, input_path)
                    before_sensitive = stat_record(source_file)
                    after_sensitive = stat_record(source_file)
                    sensitive_stats[role] = {
                        "source_path": expected_source(identity, role),
                        "read_content": False,
                        "read_before": before_sensitive,
                        "read_after": after_sensitive,
                    }
                else:
                    sensitive_stats[role] = {
                        "source_path": expected_source(identity, role),
                        "read_content": False,
                        "present": None,
                        "bytes": None,
                        "mtime_utc": None,
                        "read_before": {"present": None, "bytes": None, "mtime_utc": None},
                        "read_after": {"present": None, "bytes": None, "mtime_utc": None},
                        "reason": "stat-only record did not supply local content path",
                    }
                if descriptor.get("snapshot_path") or descriptor.get("content_b64"):
                    failures.append(f"{role}: content export is forbidden")
                continue
            if source_path != expected_source(identity, role):
                failures.append(
                    f"{role}/{descriptor.get('fragment_id')}: source_path is not manifest-derived"
                )
                continue
            input_path = descriptor.get("input_path")
            if not isinstance(input_path, str):
                failures.append(f"{role}/{descriptor.get('fragment_id')}: input_path is missing")
                continue
            try:
                source_file = safe_relative(source_root, input_path)
                before_local = stat_record(source_file)
                data = source_file.read_bytes()
                after_local = stat_record(source_file)
            except (OSError, EvidenceError) as error:
                failures.append(f"{role}/{descriptor.get('fragment_id')}: read failed: {error}")
                continue
            text = data.decode("utf-8", errors="replace")
            if role == "OUTCAR" and _contains_paw_body(text):
                failures.append(
                    f"{role}/{descriptor.get('fragment_id')}: embedded PAW body refused"
                )
                continue
            fragment_id = descriptor.get(
                "fragment_id",
                f"{role}-{descriptor['fragment_index']:03d}",
            )
            safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(fragment_id))
            destination = raw_root / role / f"{safe_id}.txt"
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                with destination.open("xb") as stream:
                    stream.write(data)
            except OSError as error:
                failures.append(f"{role}/{fragment_id}: bundle write failed: {error}")
                continue
            remote_meta = _remote_meta_for(capture_meta, role, str(fragment_id))
            record = _record_metadata(
                descriptor,
                data,
                before_local,
                after_local,
                remote_meta,
                str(destination.relative_to(bundle)),
            )
            records_by_role[role].append(record)

    for role in SENSITIVE_FILES:
        sensitive_stats.setdefault(
            role,
            {
                "source_path": expected_source(identity, role),
                "read_content": False,
                "read_before": {"present": False, "bytes": None, "mtime_utc": None},
                "read_after": {"present": False, "bytes": None, "mtime_utc": None},
                "reason": "no content path supplied; stat-only absence is recorded",
            },
        )
    metadata = capture_meta.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    collection_status = "COMPLETE" if not failures else "PARTIAL"
    if command.get("exit_code") not in {0, None}:
        collection_status = "FAILED"
    document = {
        "schema": SCHEMA,
        "evidence_type": "primary_raw_evidence_bundle",
        "bundle_directory": str(bundle),
        "manifest_identity": identity,
        "collection": {
            "status": collection_status,
            "collection_start_utc": collection_start,
            "collection_end_utc": collection_end,
            "command": command,
            "failures": failures,
            "source_mode": capture_meta.get("source_mode", "local_fixture"),
            "non_atomic_read_note": (
                "per-file read_before/read_after are recorded; a changing file is partial "
                "and is never silently completed from an earlier sample"
            ),
        },
        "files": {key: value for key, value in records_by_role.items()},
        "sensitive_files_stat_only": sensitive_stats,
        "sensitive_files_not_read": sorted(SENSITIVE_FILES),
        "metadata": metadata,
    }
    write_json(bundle / "evidence.json", document)
    return bundle


def _records(document: dict[str, Any], role: str) -> list[dict[str, Any]]:
    values = document.get("files", {}).get(role)
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def _record_text(bundle: Path, record: dict[str, Any]) -> str | None:
    relative = record.get("snapshot_path")
    if not isinstance(relative, str):
        return None
    path = safe_relative(bundle, relative)
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _verify_record(
    bundle: Path,
    identity: dict[str, Any],
    role: str,
    record: dict[str, Any],
    issues: list[str],
    gaps: list[str],
) -> str | None:
    expected = expected_source(identity, role)
    if record.get("source_path") != expected:
        issues.append(f"{role}/{record.get('fragment_id')}: source path mismatch")
    valid_offsets, reason = offsets_valid(record)
    empty_optional_stderr = (
        role == "vasp.stderr"
        and record.get("snapshot_bytes") == 0
        and record.get("read_content") is True
    )
    if not valid_offsets and not empty_optional_stderr:
        gaps.append(f"{role}/{record.get('fragment_id')}: {reason}")
    if not _stat_pair_consistent(record):
        gaps.append(f"{role}/{record.get('fragment_id')}: read_before/read_after missing or changed")
    relative = record.get("snapshot_path")
    if not isinstance(relative, str):
        gaps.append(f"{role}/{record.get('fragment_id')}: raw snapshot path is absent")
        return None
    try:
        path = safe_relative(bundle, relative)
    except EvidenceError as error:
        issues.append(str(error))
        return None
    if not path.is_file():
        gaps.append(f"{role}/{record.get('fragment_id')}: raw snapshot file is absent")
        return None
    try:
        data = path.read_bytes()
    except OSError as error:
        gaps.append(f"{role}/{record.get('fragment_id')}: raw read failed: {error}")
        return None
    if not data and role != "vasp.stderr":
        gaps.append(f"{role}/{record.get('fragment_id')}: raw evidence is empty")
    if role == "OUTCAR" and _contains_paw_body(data.decode("utf-8", errors="replace")):
        issues.append(f"{role}/{record.get('fragment_id')}: PAW body is present in bundle")
    return data.decode("utf-8", errors="replace")


def _parse_force_fragment(
    text: str,
    record: dict[str, Any],
    nions: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    marker = re.compile(r"POSITION\s+TOTAL-FORCE", re.IGNORECASE)
    row_pattern = re.compile(
        rf"^\s*({FLOAT_RE})\s+({FLOAT_RE})\s+({FLOAT_RE})\s+"
        rf"({FLOAT_RE})\s+({FLOAT_RE})\s+({FLOAT_RE})"
    )
    lines = text.splitlines()
    complete: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    headers = [i for i, line in enumerate(lines) if marker.search(line)]
    for block_number, header_index in enumerate(headers, 1):
        rows: list[dict[str, Any]] = []
        j = header_index + 1
        while j < len(lines) and len(rows) < nions:
            raw = lines[j]
            match = row_pattern.match(raw)
            if match:
                values = [finite(item) for item in match.groups()]
                if all(item is not None for item in values):
                    rows.append({
                        "position_cart_A": [float(item) for item in values[:3]],
                        "force_eV_A": [float(item) for item in values[3:6]],
                        "raw": raw.strip(),
                        "source": source_ref(record, j + 1),
                    })
                    j += 1
                    continue
            stripped = raw.strip()
            if stripped and not set(stripped) <= {"-", "=", " "}:
                break
            j += 1
        block = {
            "fragment_id": record.get("fragment_id"),
            "block_number": block_number,
            "marker_source": source_ref(record, header_index + 1),
            "row_count": len(rows),
            "expected_nions": nions,
            "rows": rows,
            "complete": len(rows) == nions,
            "step_alignment": "uncertain",
            "ionic_step": None,
        }
        if block["complete"]:
            complete.append(block)
        else:
            incomplete.append(block)
    return complete, incomplete


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def _max_force(
    block: dict[str, Any],
    atoms: list[dict[str, Any]],
    allowed: set[int] | None,
    selection: str,
) -> dict[str, Any] | None:
    candidates = []
    for atom, row in zip(atoms, block["rows"]):
        index = atom["index_1based"]
        if allowed is not None and index not in allowed:
            continue
        vector = row["force_eV_A"]
        candidates.append({
            "index_1based": index,
            "species": atom.get("species"),
            "force_eV_A": vector,
            "norm_eV_A": _norm(vector),
            "selection": selection,
            "ionic_step": None,
            "step_alignment": "uncertain",
            "source": row["source"],
        })
    return max(candidates, key=lambda item: item["norm_eV_A"]) if candidates else None


def _parse_oszicar_fragment(
    text: str,
    record: dict[str, Any],
    nelm: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ionic_pattern = re.compile(
        rf"^\s*(\d+)\s+F\s*=\s*({FLOAT_RE})\s+E0\s*=\s*({FLOAT_RE})(.*)$",
        re.IGNORECASE,
    )
    electronic_pattern = re.compile(r"^\s*(DAV|RMM):\s*(\d+)(.*)$", re.IGNORECASE)
    float_pattern = re.compile(FLOAT_RE)
    completed: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for line_number, raw in enumerate(text.splitlines(), 1):
        electronic = electronic_pattern.match(raw)
        if electronic:
            first = float_pattern.search(electronic.group(3))
            pending.append({
                "algorithm": electronic.group(1).upper(),
                "iteration": int(electronic.group(2)),
                "energy_like_eV": finite(first.group(0)) if first else None,
                "raw": raw.strip(),
                "source": source_ref(record, line_number),
            })
            continue
        ionic = ionic_pattern.match(raw)
        if ionic:
            de = re.search(rf"d\s*E\s*=\s*({FLOAT_RE})", ionic.group(4), re.IGNORECASE)
            completed.append({
                "ionic_step": int(ionic.group(1)),
                "F_eV": finite(ionic.group(2)),
                "E0_eV": finite(ionic.group(3)),
                "dE_eV": finite(de.group(1)) if de else None,
                "raw": raw.strip(),
                "source": source_ref(record, line_number),
                "electronic_steps": pending,
                "electronic_step_count": len(pending),
                "max_electronic_iteration": (
                    max(item["iteration"] for item in pending) if pending else None
                ),
                "NELM": nelm,
                "NELM_reached": (
                    None if nelm is None or not pending
                    else max(item["iteration"] for item in pending) >= nelm
                ),
            })
            pending = []
    return completed, pending


def _scan_diagnostics(
    text: str,
    record: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line_number, raw in enumerate(text.splitlines(), 1):
        category = _diagnostic_category(raw)
        if category is None:
            continue
        kind, label = category
        grouped[f"{kind}:{label}"].append({
            "kind": kind,
            "category": label,
            "raw": raw.rstrip(),
            "source": source_ref(record, line_number),
        })
    return grouped


def _marker_records(text: str, record: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line_number, raw in enumerate(text.splitlines(), 1):
        lower = raw.lower()
        item = {"raw": raw.rstrip(), "source": source_ref(record, line_number)}
        if "aborting loop because ediff is reached" in lower:
            result["electronic_ediff"].append(item)
        if "reached required accuracy" in lower or "stopping structural energy" in lower:
            result["ionic_convergence"].append(item)
        if "general timing and accounting informations" in lower:
            result["normal_end"].append(item)
        loop = re.search(
            rf"\b(LOOP\+?):\s+cpu time\s+({FLOAT_RE}):\s+real time\s+({FLOAT_RE})",
            raw,
            re.IGNORECASE,
        )
        if loop:
            result[loop.group(1).upper()].append({
                **item,
                "cpu_seconds": finite(loop.group(2)),
                "real_seconds": finite(loop.group(3)),
            })
    return result


def _first_last(values: list[dict[str, Any]]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "first": None, "recent": None}
    return {
        "count": len(values),
        "first": values[0],
        "recent": values[-1],
    }


def _check_collection(
    document: dict[str, Any],
    gaps: list[str],
    issues: list[str],
) -> dict[str, Any]:
    collection = document.get("collection")
    if not isinstance(collection, dict):
        issues.append("collection metadata is missing")
        return {"status": "INVALID"}
    start = parse_utc(collection.get("collection_start_utc"))
    end = parse_utc(collection.get("collection_end_utc"))
    if start is None or end is None:
        gaps.append("collection start/end UTC is missing or invalid")
    elif end < start:
        issues.append("collection end precedes collection start")
    command = collection.get("command")
    if not isinstance(command, dict):
        issues.append("collection command metadata is missing")
        command = {}
    if command.get("read_only") is not True:
        issues.append("collection command is not explicitly read_only")
    if command.get("exit_code") not in {0, None}:
        issues.append(f"collection command exit_code={command.get('exit_code')}")
    if collection.get("status") in {"FAILED", "INTERRUPTED"}:
        gaps.append(f"collection status is {collection.get('status')}")
    return {
        "status": collection.get("status"),
        "start_utc": collection.get("collection_start_utc"),
        "end_utc": collection.get("collection_end_utc"),
        "command": command,
    }


def verify_bundle(bundle_dir: Path | str) -> dict[str, Any]:
    bundle = Path(bundle_dir).resolve()
    issues: list[str] = []
    gaps: list[str] = []
    evidence_path = bundle / "evidence.json"
    try:
        document = read_json(evidence_path)
    except EvidenceError as error:
        result = {
            "schema": SCHEMA,
            "evidence_status": "PARTIAL_EVIDENCE",
            "bundle": str(bundle),
            "issues": [str(error)],
            "gaps": ["evidence.json is unreadable"],
            "claims": {
                "max_free_force": {"status": "SUPPRESSED", "reason": "evidence index unavailable"},
                "diagnostics": {"status": "PENDING", "reason": "evidence index unavailable"},
            },
        }
        result["report"] = make_report(result)
        return result
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        result = {
            "schema": SCHEMA,
            "evidence_status": "PARTIAL_EVIDENCE",
            "bundle": str(bundle),
            "issues": ["evidence schema is missing or unsupported"],
            "gaps": [],
            "claims": {
                "max_free_force": {"status": "SUPPRESSED", "reason": "unsupported schema"},
                "diagnostics": {"status": "PENDING", "reason": "unsupported schema"},
            },
            "report": [],
        }
        return result
    identity = document.get("manifest_identity")
    if not isinstance(identity, dict):
        issues.append("manifest-derived identity is missing")
        identity = {}
    else:
        if not isinstance(identity.get("remote_batch_dir"), str) or not REMOTE_BATCH_RE.fullmatch(identity.get("remote_batch_dir", "")):
            issues.append("manifest-derived remote_batch_dir is invalid")
        case = identity.get("case")
        if not isinstance(case, str) or not CASE_RE.fullmatch(case or "") or case in {".", ".."}:
            issues.append("manifest-derived case is invalid")
        if identity.get("remote_case_dir") != (
            f"{identity.get('remote_batch_dir')}/{identity.get('case')}"
        ):
            issues.append("remote_case_dir is not derived from batch/case")
        remote_input_dir = identity.get("remote_input_dir")
        expected_case_dir = identity.get("remote_case_dir")
        if remote_input_dir not in {identity.get("remote_batch_dir"), expected_case_dir}:
            issues.append("remote_input_dir is not the case directory or batch directory")
        if (
            remote_input_dir == identity.get("remote_batch_dir")
            and identity.get("runtime_input_source") != "verified_batch"
        ):
            issues.append("batch runtime input is not marked verified")
        if (
            remote_input_dir == expected_case_dir
            and identity.get("runtime_input_source") != "case"
        ):
            issues.append("case runtime input source is not identified as case")
        manifest_source = identity.get("manifest_path")
        if isinstance(manifest_source, str) and Path(manifest_source).is_file():
            try:
                manifest_identity = load_execution_identity(Path(manifest_source))
            except EvidenceError as error:
                issues.append(f"manifest identity cannot be reloaded: {error}")
            else:
                for key in (
                    "task_id",
                    "unit_id",
                    "workflow_unit_id",
                    "identity_sources",
                    "remote_batch_dir",
                    "case",
                    "remote_case_dir",
                    "remote_input_dir",
                    "runtime_input_source",
                    "host",
                ):
                    if key in identity and identity.get(key) != manifest_identity.get(key):
                        issues.append(f"bundle identity differs from execution manifest at {key}")
    collection = _check_collection(document, gaps, issues)

    all_raw_records = []
    for role in PRIMARY_INPUTS + OUTPUT_ROLES:
        role_records = _records(document, role)
        all_raw_records.extend(role_records)
        for record in role_records:
            _verify_record(bundle, identity, role, record, issues, gaps)
    if not all_raw_records:
        gaps.append("no primary raw records are present; summary-only material is not evidence")

    poscar_records = _records(document, "POSCAR")
    incar_records = _records(document, "INCAR")
    kpoints_records = _records(document, "KPOINTS")
    poscar_text = _record_text(bundle, poscar_records[0]) if len(poscar_records) == 1 else None
    incar_text = _record_text(bundle, incar_records[0]) if len(incar_records) == 1 else None
    kpoints_text = _record_text(bundle, kpoints_records[0]) if len(kpoints_records) == 1 else None
    if len(poscar_records) != 1 or poscar_records[0].get("coverage") != "full":
        gaps.append("POSCAR is not present as one full raw record")
    if len(incar_records) != 1 or incar_records[0].get("coverage") != "full":
        gaps.append("INCAR is not present as one full raw record")
    if len(kpoints_records) != 1 or kpoints_records[0].get("coverage") != "full":
        gaps.append("KPOINTS is not present as one full raw record")

    poscar = parse_poscar(poscar_text, bundle / "raw" / "POSCAR" / "POSCAR.txt") if poscar_text is not None else {
        "valid": False,
        "nions": None,
        "atoms": [],
        "mask_status": "UNAVAILABLE",
        "fixed_indices_1based": [],
        "free_indices_1based": [],
        "errors": ["POSCAR raw evidence is unavailable"],
    }
    incar = parse_incar(incar_text, bundle / "raw" / "INCAR" / "INCAR.txt") if incar_text is not None else {
        "parameters": {},
        "errors": ["INCAR raw evidence is unavailable"],
    }
    if not poscar.get("valid"):
        issues.extend(f"POSCAR: {error}" for error in poscar.get("errors", []))
    outcar_records = _records(document, "OUTCAR")
    identity_values: dict[str, dict[str, Any]] = {}
    marker_values: dict[str, list[dict[str, Any]]] = defaultdict(list)
    diagnostics: dict[str, list[dict[str, Any]]] = defaultdict(list)
    complete_blocks: list[dict[str, Any]] = []
    incomplete_blocks: list[dict[str, Any]] = []
    parameter_fields: dict[str, dict[str, Any]] = {
        tag: {"tag": tag, "kind": kind, "observations": []}
        for tag, kind in OUTCAR_PARAMETER_FIELDS.items()
    }
    parameter_section_markers: list[dict[str, Any]] = []
    parameter_fragments_seen = 0
    actual_nions = None
    for record in outcar_records:
        text = _record_text(bundle, record)
        if text is None:
            continue
        if record.get("kind") in {"outcar_identity", "outcar_parameters"}:
            for local_line, raw in enumerate(text.splitlines(), 1):
                patterns = {
                    "NIONS": (rf"\bNIONS\s*=\s*(\d+)", int),
                    "NELECT": (rf"\bNELECT\s*=\s*({FLOAT_RE})", finite),
                    "NKPTS": (rf"\bNKPTS\s*=\s*(\d+)", int),
                    "NBANDS": (rf"\bNBANDS\s*=\s*(\d+)", int),
                    "KPAR": (rf"\bKPAR\s*=\s*(\d+)", int),
                    "NCORE": (rf"\bNCORE\s*=\s*(\d+)", int),
                    "MPI_RANKS": (r"running\s+(\d+)\s+mpi-ranks", int),
                }
                for key, (pattern, converter) in patterns.items():
                    match = re.search(pattern, raw, re.IGNORECASE)
                    if match:
                        try:
                            value = converter(match.group(1))
                        except (TypeError, ValueError):
                            value = None
                        identity_values[key] = {
                            "value": value,
                            "raw": raw.strip(),
                            "source": source_ref(record, local_line),
                        }
                        if key == "NIONS":
                            actual_nions = value
        if record.get("kind") in {"outcar_marker", "outcar_timing", "outcar_diagnostics"}:
            found_markers = _marker_records(text, record)
            for key, values in found_markers.items():
                marker_values[key].extend(values)
        if record.get("kind") == "outcar_diagnostics":
            found_diagnostics = _scan_diagnostics(text, record)
            for key, values in found_diagnostics.items():
                diagnostics[key].extend(values)
        if record.get("kind") == "outcar_parameters":
            parameter_fragments_seen += 1
            parsed_parameters = parse_outcar_parameters(
                text,
                bundle / str(record.get("snapshot_path", "raw/OUTCAR/parameter.txt")),
                default_section=str(record.get("parameter_section") or "unknown"),
            )
            parameter_section_markers.extend(parsed_parameters.get("section_markers", []))
            for tag, field_data in parsed_parameters.get("fields", {}).items():
                if tag not in parameter_fields or not isinstance(field_data, dict):
                    continue
                for observation in field_data.get("observations", []):
                    if not isinstance(observation, dict):
                        continue
                    observation = dict(observation)
                    local_source = observation.get("source")
                    local_line = local_source.get("line") if isinstance(local_source, dict) else None
                    observation["source"] = source_ref(record, local_line)
                    observation["fragment_id"] = record.get("fragment_id")
                    parameter_fields[tag]["observations"].append(observation)
        if record.get("kind") == "outcar_force":
            if isinstance(poscar.get("nions"), int) and poscar.get("nions") > 0:
                complete, incomplete = _parse_force_fragment(text, record, poscar["nions"])
                complete_blocks.extend(complete)
                incomplete_blocks.extend(incomplete)

    if actual_nions is not None and poscar.get("nions") != actual_nions:
        issues.append(f"POSCAR nions={poscar.get('nions')} differs from OUTCAR NIONS={actual_nions}")
    if not identity_values.get("NIONS"):
        gaps.append("OUTCAR identity/parameter raw fragment lacks NIONS")
    if not outcar_records:
        gaps.append("OUTCAR raw fragments are absent")
    outcar_parameters = {
        "available": bool(parameter_fragments_seen),
        "source": "selected OUTCAR parameter fragments",
        "coverage": "selected_fragments" if parameter_fragments_seen else "unknown",
        "section_markers": parameter_section_markers,
        "fields": parameter_fields,
        "errors": (
            []
            if parameter_fragments_seen
            else ["OUTCAR parameter fragments are absent; effective values are UNKNOWN"]
        ),
    }
    execution_manifest_source = identity.get("manifest_path")
    approved_manifest_path = None
    if isinstance(execution_manifest_source, str) and execution_manifest_source:
        approved_manifest_path = Path(execution_manifest_source).resolve().parent / "input_manifest.json"
    approved_manifest, parameter_manifest_error = load_approved_manifest(approved_manifest_path)
    parameter_comparison = compare_parameter_sources(
        approved_manifest,
        incar,
        outcar_parameters,
        manifest_source=(
            str(approved_manifest_path)
            if approved_manifest is not None and approved_manifest_path is not None
            else None
        ),
    )
    parameter_comparison["approval_manifest"] = {
        "status": "SUPPLIED" if approved_manifest is not None else "UNKNOWN",
        "path": str(approved_manifest_path) if approved_manifest_path is not None else None,
        "reason": (
            None
            if parameter_manifest_error is None
            else parameter_manifest_error.get("reason", "approved manifest is unavailable")
        ),
    }

    mask_ok = poscar.get("valid") and poscar.get("mask_status") == "OK"
    force_history = []
    for block in complete_blocks:
        max_all = _max_force(block, poscar.get("atoms", []), None, "all")
        max_free = (
            _max_force(
                block,
                poscar.get("atoms", []),
                set(poscar.get("free_indices_1based", [])),
                "free",
            )
            if mask_ok and (actual_nions is None or actual_nions == poscar.get("nions"))
            else None
        )
        force_history.append({
            "fragment_id": block.get("fragment_id"),
            "block_number": block.get("block_number"),
            "row_count": block.get("row_count"),
            "ionic_step": None,
            "step_alignment": "uncertain",
            "max_all": max_all,
            "max_free": max_free,
            "marker_source": block.get("marker_source"),
        })
    if force_history:
        last_force = force_history[-1]
        force_claim = {
            "status": "VERIFIED" if last_force.get("max_free") is not None else "SUPPRESSED",
            "value": last_force.get("max_free"),
            "reason": (
                None
                if last_force.get("max_free") is not None
                else "POSCAR mask/NIONS alignment is unavailable or invalid"
            ),
        }
    else:
        last_force = None
        force_claim = {
            "status": "SUPPRESSED",
            "value": None,
            "reason": "no complete NIONS force block was captured",
        }
    if incomplete_blocks:
        gaps.append(
            "one or more incomplete OUTCAR force fragments were captured; "
            "the preceding complete block is retained"
        )

    oszicar_records = _records(document, "OSZICAR")
    nelm = incar_int(incar, "NELM")
    completed_ionic_steps: list[dict[str, Any]] = []
    current_scf_candidates: list[dict[str, Any]] = []
    for record in oszicar_records:
        text = _record_text(bundle, record)
        if text is None:
            continue
        completed, pending = _parse_oszicar_fragment(text, record, nelm)
        completed_ionic_steps.extend(completed)
        if pending or record.get("kind") == "oszicar_tail":
            current_scf_candidates.append({
                "state": "IN_PROGRESS" if pending else "NO_UNASSOCIATED_ELECTRONIC_ROWS",
                "ionic_step": None,
                "step_alignment": "uncertain" if pending else "not_applicable",
                "electronic_steps": pending,
                "electronic_step_count": len(pending),
                "last_electronic_step": pending[-1] if pending else None,
                "NELM": nelm,
                "NELM_reached": (
                    None if nelm is None or not pending
                    else max(item["iteration"] for item in pending) >= nelm
                ),
                "source_fragment": record.get("fragment_id"),
            })
    if len(completed_ionic_steps) > 5:
        gaps.append("OSZICAR evidence contains more than the allowed five completed ionic steps")
    oszicar_claim = {
        "completed_ionic_steps": completed_ionic_steps[-5:],
        "current_scf": current_scf_candidates[-1] if current_scf_candidates else {
            "state": (
                "NO_UNASSOCIATED_ELECTRONIC_ROWS"
                if completed_ionic_steps
                else "UNKNOWN"
            ),
            "ionic_step": None,
            "step_alignment": "not_applicable" if completed_ionic_steps else "unknown",
            "reason": (
                "snapshot ends after a completed F/E0 row"
                if completed_ionic_steps
                else "no OSZICAR DAV/RMM or tail fragment was captured"
            ),
        },
        "source_fragments_separate": True,
    }
    if not oszicar_records:
        gaps.append("OSZICAR raw fragments are absent")

    stdout_records = _records(document, "vasp.stdout")
    stdout_observation: dict[str, Any] = {
        "status": "ABSENT",
        "source_fragments": [],
        "independent_from_oszicar": True,
    }
    for record in stdout_records:
        text = _record_text(bundle, record)
        if text is None:
            continue
        dav = []
        f_rows = []
        for line_number, raw in enumerate(text.splitlines(), 1):
            match = re.match(r"^\s*(DAV|RMM):\s*(\d+)(.*)$", raw, re.IGNORECASE)
            if match:
                dav.append({
                    "algorithm": match.group(1).upper(),
                    "iteration": int(match.group(2)),
                    "raw": raw.strip(),
                    "source": source_ref(record, line_number),
                })
            if re.match(r"^\s*\d+\s+F\s*=", raw):
                f_rows.append({"raw": raw.strip(), "source": source_ref(record, line_number)})
        stdout_observation = {
            "status": "PRESENT",
            "fragment_id": record.get("fragment_id"),
            "dav_rmm_rows": dav,
            "f_rows": f_rows,
            "independent_from_oszicar": True,
        }
    if not stdout_records:
        gaps.append("vasp.stdout tail is absent; OSZICAR/stdout lag cannot be compared")

    metadata_records = document.get("metadata_files")
    if not isinstance(metadata_records, list):
        metadata_records = []
    runtime_evidence = {
        "status_file": {"state": "UNKNOWN", "reason": "status.json was not captured"},
        "timing_file": {"state": "UNKNOWN", "reason": "run_timing.txt was not captured"},
        "watcher": {"state": "UNKNOWN", "reason": "watcher state is not inferred"},
    }
    expected_metadata_sources = {
        "job_status": f"{identity.get('remote_batch_dir')}/.job_watch/status.json",
        "run_timing": f"{identity.get('remote_case_dir')}/run_timing.txt",
    }
    for record in metadata_records:
        if not isinstance(record, dict):
            gaps.append("metadata file record is not an object")
            continue
        role = record.get("role")
        if role not in expected_metadata_sources:
            issues.append(f"unsupported metadata file role: {role}")
            continue
        if record.get("source_path") != expected_metadata_sources[role]:
            issues.append(f"{role}: metadata source path mismatch")
        valid_offsets, reason = offsets_valid(record)
        metadata_unavailable = (
            record.get("snapshot_bytes") == 0
        )
        if not valid_offsets and not metadata_unavailable:
            gaps.append(f"{role}: {reason}")
        if not _stat_pair_consistent(record) and not metadata_unavailable:
            gaps.append(f"{role}: read_before/read_after missing or changed")
        content = _record_text(bundle, record)
        if content is None:
            gaps.append(f"{role}: raw metadata file is absent")
            continue
        state = "SUPPLIED" if record.get("read_content") is True and content.strip() else "UNKNOWN"
        runtime_evidence["status_file" if role == "job_status" else "timing_file"] = {
            "state": state,
            "source_path": record.get("source_path"),
            "snapshot_path": record.get("snapshot_path"),
            "bytes": record.get("snapshot_bytes"),
        }
        if state == "UNKNOWN":
            runtime_evidence["status_file" if role == "job_status" else "timing_file"]["reason"] = (
                "metadata file is empty, unread, or unavailable"
            )

    bundle_metadata = document.get("metadata")
    if not isinstance(bundle_metadata, dict):
        bundle_metadata = {}
    outcar_scan = bundle_metadata.get("outcar_scan")
    if not isinstance(outcar_scan, dict):
        outcar_scan = {}
    diagnostic_scan = outcar_scan.get("diagnostic_scan")
    if not isinstance(diagnostic_scan, dict):
        diagnostic_scan = {}
    diagnostic_categories = {
        key: _first_last(values) for key, values in sorted(diagnostics.items())
    }
    for key, scan in diagnostic_scan.items():
        if not isinstance(scan, dict):
            continue
        item = diagnostic_categories.setdefault(key, {
            "count": 0,
            "first": None,
            "recent": None,
        })
        if isinstance(scan.get("full_stream_count"), int):
            item["count"] = scan["full_stream_count"]
            item["full_stream_count"] = scan["full_stream_count"]
        item["selected_center_lines"] = scan.get("selected_center_lines", [])
        item["context_radius"] = scan.get("context_radius")
        item["scan_coverage"] = scan.get("scan_coverage", "unknown")
        if item.get("first") is None:
            gaps.append(f"diagnostic context is missing for {key}")
    diagnostic_claim = {
        "status": "VERIFIED" if diagnostics and not any(
            isinstance(scan, dict)
            and scan.get("full_stream_count", 0) > 0
            and key not in diagnostics
            for key, scan in diagnostic_scan.items()
        ) else "PENDING",
        "categories": diagnostic_categories,
        "reason": (
            None
            if diagnostics
            else "diagnostic context fragments were not captured"
        ),
    }
    marker_summary = {
        key: {
            "count_in_selected_fragments": len(values),
            "records": values[-20:],
            "scope": "selected_fragments_only",
        }
        for key, values in sorted(marker_values.items())
    }
    marker_scan = outcar_scan.get("marker_scan")
    if isinstance(marker_scan, dict):
        marker_key_map = {
            "electronic_ediff": "electronic_ediff",
            "ionic_convergence": "ionic_convergence",
            "normal_end": "normal_end",
            "LOOP": "LOOP",
            "LOOP+": "LOOP+",
        }
        for key, scan in marker_scan.items():
            if not isinstance(scan, dict):
                continue
            local_key = marker_key_map.get(key, key)
            item = marker_summary.setdefault(local_key, {
                "count_in_selected_fragments": 0,
                "records": [],
                "scope": "selected_fragments_only",
            })
            item["full_stream_count"] = scan.get("full_stream_count")
            item["selected_count"] = scan.get("selected_count")
            item["selected_original_lines"] = scan.get("selected_original_lines", [])
            item["scan_coverage"] = scan.get("scan_coverage", "unknown")
    sensitive = document.get("sensitive_files_not_read")
    if sorted(sensitive or []) != sorted(SENSITIVE_FILES):
        issues.append("sensitive_files_not_read does not cover POTCAR/CHGCAR/WAVECAR")
    if document.get("sensitive_files_stat_only") and any(
        isinstance(value, dict) and value.get("read_content") is True
        for value in document["sensitive_files_stat_only"].values()
    ):
        issues.append("sensitive stat-only record claims content was read")
    sensitive_stat_only = document.get("sensitive_files_stat_only")
    if isinstance(sensitive_stat_only, dict):
        for role in SENSITIVE_FILES:
            item = sensitive_stat_only.get(role)
            if not isinstance(item, dict):
                gaps.append(f"{role}: stat-only record is absent")
                continue
            if item.get("source_path") != expected_source(identity, role):
                issues.append(f"{role}: stat-only source path mismatch")
            before = item.get("read_before")
            after = item.get("read_after")
            if not isinstance(before, dict) or not isinstance(after, dict):
                gaps.append(f"{role}: stat-only read_before/read_after is missing")
            elif (
                before.get("bytes") != after.get("bytes")
                or before.get("mtime_utc") != after.get("mtime_utc")
            ):
                gaps.append(f"{role}: stat-only file changed during collection")
    else:
        gaps.append("sensitive stat-only records are absent")
    if any(role in document.get("files", {}) for role in SENSITIVE_FILES):
        issues.append("sensitive file content is present in the primary file records")
    raw_root = bundle / "raw"
    if raw_root.is_dir():
        for path in raw_root.rglob("*"):
            if path.is_file() and path.name.upper().split(".")[0] in SENSITIVE_FILES:
                issues.append(f"sensitive filename appears under raw evidence: {path.name}")

    hard_requirements = [
        not issues,
        not gaps,
        collection.get("status") == "COMPLETE",
        all(
            len(_records(document, role)) == 1
            and _records(document, role)[0].get("coverage") == "full"
            for role in PRIMARY_INPUTS
        ),
        bool(poscar.get("valid")) and bool(incar_text) and bool(kpoints_text),
        bool(identity_values.get("NIONS")),
        bool(force_history) and force_claim["status"] == "VERIFIED",
        bool(oszicar_records),
        bool(stdout_records),
    ]
    status = "READY_FOR_SOL_REVIEW" if all(hard_requirements) else "PARTIAL_EVIDENCE"
    result = {
        "schema": SCHEMA,
        "evidence_status": status,
        "bundle": str(bundle),
        "task": {
            "task_id": identity.get("task_id"),
            "unit_id": identity.get("unit_id"),
            "remote_batch_dir": identity.get("remote_batch_dir"),
            "remote_case_dir": identity.get("remote_case_dir"),
            "remote_input_dir": identity.get("remote_input_dir"),
            "runtime_input_source": identity.get("runtime_input_source"),
            "host": identity.get("host"),
        },
        "collection": collection,
        "identity": {
            "POSCAR_nions": poscar.get("nions"),
            "POSCAR_mask_status": poscar.get("mask_status"),
            "POSCAR_fixed_indices_1based": poscar.get("fixed_indices_1based", []),
            "POSCAR_free_indices_1based": poscar.get("free_indices_1based", []),
            "OUTCAR": identity_values,
        },
        "progress": {
            "oszicar": oszicar_claim,
            "stdout_independent": stdout_observation,
        },
        "runtime_evidence": runtime_evidence,
        "metadata_files": metadata_records,
        "parameters": {
            "outcar": outcar_parameters,
            "comparison": parameter_comparison,
        },
        "forces": {
            "complete_block_count": len(complete_blocks),
            "incomplete_block_count": len(incomplete_blocks),
            "incomplete_blocks": incomplete_blocks,
            "history": force_history,
            "last_complete": last_force,
            "step_alignment": "uncertain",
        },
        "markers": marker_summary,
        "claims": {
            "max_free_force": force_claim,
            "diagnostics": diagnostic_claim,
        },
        "gaps": gaps,
        "issues": issues,
        "sensitive_files_not_read": sorted(SENSITIVE_FILES),
        "metadata": document.get("metadata", {}),
    }
    result["report"] = make_report(result)
    return result


def make_report(result: dict[str, Any]) -> list[str]:
    task = result.get("task", {})
    collection = result.get("collection", {})
    status = result.get("evidence_status", "PARTIAL_EVIDENCE")
    oszicar = result.get("progress", {}).get("oszicar", {})
    force = result.get("claims", {}).get("max_free_force", {})
    diagnostics = result.get("claims", {}).get("diagnostics", {})
    parameters = result.get("parameters", {}).get("comparison", {})
    metadata = result.get("metadata")
    watcher = metadata.get("watcher") if isinstance(metadata, dict) else None
    completed = oszicar.get("completed_ionic_steps", [])
    last_step = completed[-1].get("ionic_step") if completed else None
    current = oszicar.get("current_scf", {})
    current_state = current.get("state", "UNKNOWN") if isinstance(current, dict) else "UNKNOWN"
    force_value = force.get("value") if isinstance(force, dict) else None
    force_text = "suppressed"
    if isinstance(force_value, dict):
        force_text = (
            f"{force_value.get('norm_eV_A')} eV/A at atom "
            f"{force_value.get('index_1based')}"
        )
    warning_text = diagnostics.get("status", "PENDING") if isinstance(diagnostics, dict) else "PENDING"
    watcher_text = "not supplied"
    if isinstance(watcher, dict):
        watcher_text = str(watcher.get("status") or watcher.get("event") or "supplied")
    elif isinstance(metadata, dict):
        process_watch = metadata.get("process_watch_evidence")
        if isinstance(process_watch, dict):
            watcher_text = str(process_watch.get("watcher") or "UNKNOWN")
    gaps = result.get("gaps", [])
    gap_text = "none" if not gaps else "; ".join(str(item) for item in gaps[:2])
    parameter_status = parameters.get("status", "UNKNOWN") if isinstance(parameters, dict) else "UNKNOWN"
    return [
        f"{status}: {task.get('task_id')} @ {collection.get('start_utc')}",
        f"remote case: {task.get('remote_case_dir')}; collection={collection.get('status')}",
        f"progress: OSZICAR completed_steps={len(completed)} last_step={last_step}; current_scf={current_state}",
        f"force: {force_text}; ionic_step=null; step_alignment=uncertain",
        f"diagnostics: {warning_text}; warning/error categories remain source-labelled",
        f"parameters: {parameter_status}; effective OUTCAR evidence is separate from INCAR/input echo",
        f"watcher: {watcher_text}; watcher evidence is separate from primary outputs",
        f"evidence: {result.get('bundle')}; gaps={gap_text}",
    ]


def build_remote_export_script(manifest_path: Path | str) -> str:
    """Render the reviewed read-only remote stdout exporter for one manifest identity.

    The generated program is intended to be sent as stdin to python3 - over the
    already authorized SSH connection. It never writes remotely and only emits
    framed JSON records. The local ingest path is separate so this function can
    be unit-tested without opening SSH.
    """

    identity = load_execution_identity(Path(manifest_path).resolve())
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    return f'''#!/usr/bin/env python3
import base64, json, os, re, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

IDENTITY = json.loads({encoded!r})
BATCH = Path(IDENTITY["remote_batch_dir"])
CASE = Path(IDENTITY["remote_case_dir"])
INPUT = Path(IDENTITY["remote_input_dir"])
REMOTE_BATCH = Path(IDENTITY["remote_batch_dir"])
REMOTE_CASE = Path(IDENTITY["remote_case_dir"])
NUM = r"[-+]?(?:\\d+(?:\\.\\d*)?|\\.\\d+)(?:[EeDd][-+]?\\d+)?"
PAW = re.compile(r"\\b(?:TITEL|VRHFIN|LEXCH|POMASS|ZVAL|ENMAX|PSCTR|PAW[_ -]PBE|pseudopotential|radial)\\b", re.I)
ROW = re.compile(r"^\\s*(" + NUM + r")\\s+(" + NUM + r")\\s+(" + NUM + r")\\s+(" + NUM + r")\\s+(" + NUM + r")\\s+(" + NUM + r")")
IONIC = re.compile(r"^\\s*(\\d+)\\s+F\\s*=.*E0\\s*=.*", re.I)
PARAMETER_TAGS = (
    "ENCUT", "EDIFF", "EDIFFG", "PREC", "ISPIN", "ISTART", "ICHARG",
    "ISMEAR", "SIGMA", "NELM", "NELMIN", "NSW", "IBRION", "ISIF",
    "LDIPOL", "IDIPOL", "DIPOL",
)
PARAMETER_PATTERNS = {{
    tag: re.compile(r"(?<![A-Za-z0-9_])" + re.escape(tag) + r"\\s*=\\s*(.*)$", re.I)
    for tag in PARAMETER_TAGS
}}

def parameter_section_marker(line):
    lowered = line.lower()
    if re.search(
        r"parameters?\\s+from\\s+incar|incar\\s*:\\s*$|"
        r"(?:input|incar)\\s+(?:file|echo)|input\\s+parameters?",
        lowered,
    ):
        return "input_echo"
    if re.search(
        r"(?:effective|actual|applied|used)\\s+(?:incar|parameters?)|"
        r"(?:incar|parameters?).*(?:effective|actual|applied|used)|"
        r"startparameter\\s+for\\s+this\\s+run",
        lowered,
    ):
        return "effective_parameter"
    return None

def source_path_for(path):
    try:
        return str(REMOTE_BATCH / path.relative_to(BATCH)).replace("\\\\", "/")
    except ValueError:
        return str(REMOTE_CASE / path.relative_to(CASE)).replace("\\\\", "/")

def stat(path):
    try:
        s = path.stat()
        return {{"present": True, "bytes": s.st_size, "mtime_utc": datetime.fromtimestamp(s.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")}}
    except OSError as e:
        return {{"present": False, "error": type(e).__name__ + ": " + str(e)}}

def emit(role, kind, fragment_id, source_path, data, coverage, start_line=None, end_line=None, before=None, after=None, extra=None):
    text = data.decode("utf-8", "replace")
    if role == "OUTCAR" and PAW.search(text):
        sys.stdout.write(json.dumps({{"type": "gap", "role": role, "kind": kind, "fragment_id": fragment_id, "source_path": source_path, "original_start_line": start_line, "original_end_line": end_line, "reason": "selected OUTCAR fragment contains a filtered PAW marker"}}) + "\\n")
        return
    item = {{
        "type": "file",
        "role": role,
        "kind": kind,
        "fragment_id": fragment_id,
        "source_path": source_path,
        "coverage": coverage,
        "original_start_line": start_line,
        "original_end_line": end_line,
        "read_before": before,
        "read_after": after,
        "content_b64": base64.b64encode(data).decode("ascii"),
    }}
    if isinstance(extra, dict):
        item.update(extra)
    sys.stdout.write(json.dumps(item, ensure_ascii=False) + "\\n")

def read_small(role, path):
    before = stat(path)
    try:
        data = path.read_bytes()
    except OSError:
        data = b""
    after = stat(path)
    emit(role, role, role, source_path_for(path), data, "full", 1, len(data.decode("utf-8", "replace").splitlines()), before, after)

def read_tail(role, path, limit=120):
    before = stat(path)
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        lines = []
    after = stat(path)
    data = ("\\n".join(lines[-limit:]) + ("\\n" if lines else "")).encode()
    start = max(1, len(lines) - len(lines[-limit:]) + 1)
    emit(role, role + "_tail", role + "-tail", source_path_for(path), data, "tail", start, len(lines), before, after)

def read_metadata_file(role, path):
    before = stat(path)
    try:
        data = path.read_bytes()
    except OSError:
        data = b""
    after = stat(path)
    item = {{
        "type": "metadata_file",
        "role": role,
        "source_path": source_path_for(path),
        "read_content": before.get("present") is True,
        "read_before": before,
        "read_after": after,
        "bytes": len(data),
        "coverage": "full",
        "original_start_line": 1,
        "original_end_line": len(data.decode("utf-8", "replace").splitlines()),
        "content_b64": base64.b64encode(data).decode("ascii"),
    }}
    sys.stdout.write(json.dumps(item, ensure_ascii=False) + "\\n")
    return item

def emit_context_window(path, lines, center, before, after, kind, prefix, extra=None, radius=2):
    filtered = 0
    first = max(1, center - radius)
    last = min(len(lines), center + radius)
    for no in range(first, last + 1):
        line = lines[no - 1]
        if PAW.search(line):
            filtered += 1
            continue
        fields = dict(extra or {{}})
        fields.update({{
            "context_id": prefix,
            "context_center_line": center,
            "context_offset": no - center,
        }})
        emit(
            "OUTCAR",
            kind,
            "%s-%04d" % (prefix, no),
            source_path_for(path),
            (line + "\\n").encode(),
            "range",
            no,
            no,
            before,
            after,
            fields,
        )
    return filtered

def export_outcar(path):
    before = stat(path)
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        lines = []
    after = stat(path)
    identity_patterns = [
        ("version", re.compile(r"vasp\\.", re.I)),
        ("MPI_RANKS", re.compile(r"running\\s+\\d+\\s+mpi-ranks", re.I)),
        ("NIONS", re.compile(r"\\bNIONS\\s*=\\s*(\\d+)", re.I)),
        ("NELECT", re.compile(r"\\bNELECT\\s*=\\s*" + NUM, re.I)),
        ("NKPTS", re.compile(r"\\bNKPTS\\s*=\\s*\\d+", re.I)),
        ("NBANDS", re.compile(r"\\bNBANDS\\s*=\\s*\\d+", re.I)),
        ("KPAR", re.compile(r"\\bKPAR\\s*=\\s*\\d+", re.I)),
        ("NCORE", re.compile(r"\\bNCORE\\s*=\\s*\\d+", re.I)),
    ]
    identity_hits, nions = {{}}, None
    filtered_context_lines = 0
    for no, line in enumerate(lines, 1):
        for field, pattern in identity_patterns:
            if field in identity_hits or not pattern.search(line):
                continue
            identity_hits[field] = (no, line)
            if field == "NIONS":
                match = pattern.search(line)
                nions = int(match.group(1)) if match else None
    for field, (no, line) in identity_hits.items():
        filtered_context_lines += emit_context_window(
            path,
            lines,
            no,
            before,
            after,
            "outcar_identity",
            "identity-" + field.lower(),
            {{"identity_field": field, "identity_center_line": no}},
            radius=1,
        )

    blocks, current, start = [], [], None
    for no, line in enumerate(lines, 1):
        if "TOTAL-FORCE" in line.upper():
            if current:
                blocks.append((start, no - 1, current, nions is not None and sum(1 for x in current if ROW.match(x)) >= nions))
            start, current = no, [line]
        elif current:
            current.append(line)
            if nions and sum(1 for x in current if ROW.match(x)) >= nions:
                blocks.append((start, no, current, True)); current = []; start = None
    if current:
        blocks.append((start, len(lines), current, False))
    complete_blocks = [item for item in blocks if item[3]]
    incomplete_blocks = [item for item in blocks if not item[3]]
    selected_blocks = [(item, True) for item in complete_blocks[-3:]]
    if incomplete_blocks:
        selected_blocks.append((incomplete_blocks[-1], False))
    for index, (block, complete) in enumerate(selected_blocks, 1):
        first, last, chunk = block[:3]
        fragment_id = "force-%03d" % index if complete else "force-tail"
        emit(
            "OUTCAR",
            "outcar_force",
            fragment_id,
            source_path_for(path),
            ("\\n".join(chunk) + "\\n").encode(),
            "range",
            first,
            last,
            before,
            after,
            {{"force_complete": complete, "force_row_count": sum(1 for x in chunk if ROW.match(x))}},
        )

    iteration_seen = set()
    for block, complete in selected_blocks:
        first, last = block[0], block[1]
        for no in range(max(1, first - 12), min(len(lines), last + 3) + 1):
            if no in iteration_seen or not re.search(r"\\bIteration\\b", lines[no - 1], re.I):
                continue
            iteration_seen.add(no)
            emit(
                "OUTCAR",
                "outcar_marker",
                "iteration-%04d" % no,
                source_path_for(path),
                (lines[no - 1] + "\\n").encode(),
                "range",
                no,
                no,
                before,
                after,
                {{"marker_kind": "iteration", "force_complete": complete}},
            )

    marker_patterns = [
        ("electronic_ediff", re.compile(r"aborting\\s+loop\\s+because\\s+ediff\\s+is\\s+reached", re.I)),
        ("ionic_convergence", re.compile(r"reached\\s+required\\s+accuracy|stopping\\s+structural\\s+energy", re.I)),
        ("normal_end", re.compile(r"general\\s+timing\\s+and\\s+accounting\\s+informations", re.I)),
        ("LOOP+", re.compile(r"\\bLOOP\\+:", re.I)),
        ("LOOP", re.compile(r"\\bLOOP:", re.I)),
    ]
    marker_hits = defaultdict(list)
    for no, line in enumerate(lines, 1):
        for key, pattern in marker_patterns:
            if pattern.search(line):
                marker_hits[key].append((no, line))
    marker_scan = {{}}
    for key, values in marker_hits.items():
        selected_values = values if len(values) <= 20 else values[:10] + values[-10:]
        marker_scan[key] = {{
            "full_stream_count": len(values),
            "selected_count": len(selected_values),
            "selected_original_lines": [item[0] for item in selected_values],
            "scan_coverage": "full",
        }}
        for no, line in selected_values:
            emit(
                "OUTCAR",
                "outcar_marker",
                "marker-%s-%04d" % (key.lower().replace("+", "plus"), no),
                source_path_for(path),
                (line + "\\n").encode(),
                "range",
                no,
                no,
                before,
                after,
                {{"marker_kind": key}},
            )

    parameter_hits = defaultdict(list)
    parameter_section_markers = []
    parameter_section = "unknown"
    for no, line in enumerate(lines, 1):
        marker = parameter_section_marker(line)
        if marker is not None:
            parameter_section = marker
            parameter_section_markers.append({{
                "section": marker,
                "original_line": no,
                "raw": line.strip()[:240],
            }})
        for tag, pattern in PARAMETER_PATTERNS.items():
            match = pattern.search(line)
            if not match:
                continue
            section = parameter_section
            if section == "unknown" and re.search(r"\\b(?:effective|actual|applied|used)\\b", line, re.I):
                section = "effective_parameter"
            parameter_hits[tag].append((no, line, section))
    parameter_scan = {{}}
    for tag in PARAMETER_TAGS:
        values = parameter_hits.get(tag, [])
        selected_values = values if len(values) <= 4 else values[:2] + values[-2:]
        parameter_scan[tag] = {{
            "full_stream_count": len(values),
            "selected_count": len(selected_values),
            "selected_original_lines": [item[0] for item in selected_values],
            "selected_sections": [item[2] for item in selected_values],
            "scan_coverage": "full",
        }}
        for no, line, section in selected_values:
            emit(
                "OUTCAR",
                "outcar_parameters",
                "parameter-%s-%04d" % (tag.lower(), no),
                source_path_for(path),
                (line + "\\n").encode(),
                "range",
                no,
                no,
                before,
                after,
                {{
                    "parameter_tag": tag,
                    "parameter_section": section,
                    "parameter_center": True,
                }},
            )

    diagnostic_groups = defaultdict(list)
    for no, line in enumerate(lines, 1):
        clean = line.strip()
        warning = re.search(r"\\bWARNING\\b\\s*:?\\s*(.*)", clean, re.I)
        if warning:
            detail = re.sub(r"\\s+", " ", warning.group(1)).strip()[:180] or "WARNING"
            diagnostic_groups["warning:" + detail].append((no, line))
        elif re.search(r"FATAL|SEGMENTATION\\s+FAULT|MPI_ABORT|INTERNAL\\s+ERROR|\\bERROR\\b|ZBRENT|VERY\\s+SERIOUS|BRMIX", clean, re.I):
            detail = re.sub(r"\\s+", " ", clean)[:180]
            diagnostic_groups["error_or_suspicion:" + detail].append((no, line))
    diagnostic_scan = {{}}
    for category, values in diagnostic_groups.items():
        centers = [values[0]]
        if values[-1][0] != values[0][0]:
            centers.append(values[-1])
        selected_lines = []
        for no, line in centers:
            selected_lines.append(no)
            filtered_context_lines += emit_context_window(
                path,
                lines,
                no,
                before,
                after,
                "outcar_diagnostics",
                "diagnostic-%04d" % no,
                {{"diagnostic_category": category, "diagnostic_center_line": no}},
                radius=2,
            )
        diagnostic_scan[category] = {{
            "full_stream_count": len(values),
            "selected_center_lines": selected_lines,
            "context_radius": 2,
            "scan_coverage": "full",
        }}
    return {{
        "identity_fields": sorted(identity_hits),
        "marker_scan": marker_scan,
        "parameter_scan": parameter_scan,
        "parameter_section_markers": parameter_section_markers,
        "diagnostic_scan": diagnostic_scan,
        "complete_force_blocks_total": len(complete_blocks),
        "incomplete_force_blocks_total": len(incomplete_blocks),
        "paw_filtered_context_lines": filtered_context_lines,
    }}

def export_oszicar(path):
    before = stat(path)
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        lines = []
    after = stat(path)
    chunks, current = [], []
    for no, line in enumerate(lines, 1):
        current.append((no, line))
        if IONIC.match(line):
            chunks.append(current); current = []
    for index, chunk in enumerate(chunks[-5:], 1):
        first, last = chunk[0][0], chunk[-1][0]
        emit("OSZICAR", "oszicar", "ionic-%03d" % index, source_path_for(path), ("\\n".join(x[1] for x in chunk) + "\\n").encode(), "range", first, last, before, after)
    if current:
        first, last = current[0][0], current[-1][0]
        emit("OSZICAR", "oszicar_tail", "tail", source_path_for(path), ("\\n".join(x[1] for x in current) + "\\n").encode(), "range", first, last, before, after)

def main():
    started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    sys.stdout.write(json.dumps({{"type": "start", "collection_start_utc": started}}) + "\\n")
    for role, name in [("POSCAR", "POSCAR"), ("INCAR", "INCAR"), ("KPOINTS", "KPOINTS")]:
        read_small(role, INPUT / name)
    outcar_scan = export_outcar(CASE / "OUTCAR")
    export_oszicar(CASE / "OSZICAR")
    read_tail("vasp.stdout", CASE / "vasp.stdout")
    read_tail("vasp.stderr", CASE / "vasp.stderr")
    status = BATCH / ".job_watch" / "status.json"
    timing = CASE / "run_timing.txt"
    status_item = read_metadata_file("job_status", status)
    timing_item = read_metadata_file("run_timing", timing)
    sys.stdout.write(json.dumps({{
        "type": "metadata",
        "status_path": str(status),
        "timing_path": str(timing),
        "input_source_dir": str(CASE),
        "outcar_scan": outcar_scan,
        "process_watch_evidence": {{
            "status_file": "SUPPLIED" if status_item.get("bytes", 0) > 0 else "UNKNOWN",
            "timing_file": "SUPPLIED" if timing_item.get("bytes", 0) > 0 else "UNKNOWN",
            "watcher": "UNKNOWN",
            "note": "process/tmux/watcher state is not inferred when its status file is absent",
        }},
    }}) + "\\n")
    for role in ["POTCAR", "CHGCAR", "WAVECAR"]:
        path = CASE / role
        before = stat(path)
        after = stat(path)
        sys.stdout.write(json.dumps({{"type": "sensitive_stat", "role": role, "source_path": source_path_for(path), "read_content": False, "read_before": before, "read_after": after}}) + "\\n")
    ended = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    sys.stdout.write(json.dumps({{"type": "end", "exit_code": 0, "collection_end_utc": ended}}) + "\\n")

if __name__ == "__main__":
    main()
'''


def ingest_export(
    stream: Iterable[str] | TextIO,
    task_dir: Path | str,
    manifest_path: Path | str,
    *,
    snapshot_name: str | None = None,
    source_mode: str = "ssh-stdout",
    command_mode: str = "ssh-stdin-read-only",
    command_argv: list[str] | None = None,
    command_exit_code: int | None = None,
    command_started_utc: str | None = None,
    command_ended_utc: str | None = None,
    command_stderr: str | None = None,
) -> Path:
    """Ingest framed stdout from build_remote_export_script into a new bundle.

    The exporter supplies original offsets and remote stat pairs. The ingestion
    path never assembles adjacent fragments; it writes each frame separately
    and then leaves verification to verify_bundle.
    """

    task_root = Path(task_dir).resolve()
    manifest = Path(manifest_path).resolve()
    identity = load_execution_identity(manifest)
    lines = stream
    frames: list[dict[str, Any]] = []
    start_frame: dict[str, Any] | None = None
    metadata_frame: dict[str, Any] | None = None
    sensitive_stats: dict[str, Any] = {}
    end_frame: dict[str, Any] | None = None
    exporter_gaps: list[str] = []
    metadata_frames: list[dict[str, Any]] = []
    for raw in lines:
        try:
            item = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(item, dict) and item.get("type") == "start":
            start_frame = item
        elif isinstance(item, dict) and item.get("type") == "file":
            frames.append(item)
        elif isinstance(item, dict) and item.get("type") == "metadata":
            metadata_frame = item
        elif isinstance(item, dict) and item.get("type") == "metadata_file":
            metadata_frames.append(item)
        elif isinstance(item, dict) and item.get("type") == "gap":
            exporter_gaps.append(
                "remote exporter gap %s/%s: %s"
                % (item.get("role"), item.get("fragment_id"), item.get("reason", "unspecified"))
            )
        elif isinstance(item, dict) and item.get("type") == "sensitive_stat":
            role = item.get("role")
            if role in SENSITIVE_FILES:
                if item.get("source_path") != expected_source(identity, role):
                    exporter_gaps.append(f"remote sensitive stat source mismatch for {role}")
                if item.get("read_content") is True:
                    exporter_gaps.append(f"remote sensitive stat claims content was read for {role}")
                sensitive_stats[role] = {
                    "source_path": item.get("source_path"),
                    "read_content": item.get("read_content", False),
                    "read_before": item.get("read_before"),
                    "read_after": item.get("read_after"),
                }
        elif isinstance(item, dict) and item.get("type") == "end":
            end_frame = item
    snapshots_root = task_root / "snapshots"
    snapshots_root.mkdir(parents=True, exist_ok=True)
    name = snapshot_name or compact_utc()
    bundle = snapshots_root / name
    bundle.mkdir(parents=False, exist_ok=False)
    raw_root = bundle / "raw"
    raw_root.mkdir()
    files: dict[str, list[dict[str, Any]]] = defaultdict(list)
    failures: list[str] = list(exporter_gaps)
    metadata_files: list[dict[str, Any]] = []
    for index, frame in enumerate(metadata_frames, 1):
        role = frame.get("role")
        if role not in {"job_status", "run_timing"}:
            failures.append(f"metadata frame {index}: unsupported role {role}")
            continue
        expected = (
            f"{identity['remote_batch_dir']}/.job_watch/status.json"
            if role == "job_status"
            else f"{identity['remote_case_dir']}/run_timing.txt"
        )
        if frame.get("source_path") != expected:
            failures.append(f"metadata frame {index}: source path mismatch")
            continue
        try:
            data = base64.b64decode(frame.get("content_b64", ""), validate=True)
        except (TypeError, ValueError):
            failures.append(f"metadata frame {index}: invalid base64")
            continue
        safe_role = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(role))
        destination = raw_root / "metadata" / f"{safe_role}.txt"
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with destination.open("xb") as stream_out:
                stream_out.write(data)
        except OSError as error:
            failures.append(f"metadata frame {index}: raw write failed: {error}")
            continue
        metadata_files.append({
            "role": role,
            "source_path": frame["source_path"],
            "snapshot_path": str(destination.relative_to(bundle)),
            "read_content": frame.get("read_content", False),
            "read_before": frame.get("read_before"),
            "read_after": frame.get("read_after"),
            "read_consistent": (
                isinstance(frame.get("read_before"), dict)
                and isinstance(frame.get("read_after"), dict)
                and frame["read_before"].get("bytes") == frame["read_after"].get("bytes")
                and frame["read_before"].get("mtime_utc") == frame["read_after"].get("mtime_utc")
            ),
            "coverage": frame.get("coverage", "unknown"),
            "original_start_line": frame.get("original_start_line"),
            "original_end_line": frame.get("original_end_line"),
            "snapshot_start_line": 1,
            "snapshot_end_line": _line_count(data),
            "snapshot_bytes": len(data),
        })
    for index, frame in enumerate(frames, 1):
        role = frame.get("role")
        if role not in PRIMARY_INPUTS + OUTPUT_ROLES:
            failures.append(f"frame {index}: unsupported role {role}")
            continue
        if frame.get("source_path") != expected_source(identity, role):
            failures.append(f"frame {index}: source path mismatch")
            continue
        try:
            data = base64.b64decode(frame.get("content_b64", ""), validate=True)
        except (TypeError, ValueError):
            failures.append(f"frame {index}: invalid base64")
            continue
        if role == "OUTCAR" and _contains_paw_body(data.decode("utf-8", errors="replace")):
            failures.append(f"frame {index}: embedded PAW body refused")
            continue
        fragment_id = str(frame.get("fragment_id") or f"{role}-{index:03d}")
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", fragment_id)
        destination = raw_root / role / f"{safe_id}.txt"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream_out:
            stream_out.write(data)
        record = {
            "role": role,
            "kind": frame.get("kind", role),
            "fragment_id": fragment_id,
            "source_path": frame["source_path"],
            "snapshot_path": str(destination.relative_to(bundle)),
            "read_content": True,
            "read_before": frame.get("read_before"),
            "read_after": frame.get("read_after"),
            "read_consistent": (
                isinstance(frame.get("read_before"), dict)
                and isinstance(frame.get("read_after"), dict)
                and frame["read_before"].get("bytes") == frame["read_after"].get("bytes")
                and frame["read_before"].get("mtime_utc") == frame["read_after"].get("mtime_utc")
            ),
            "coverage": frame.get("coverage", "unknown"),
            "original_start_line": frame.get("original_start_line"),
            "original_end_line": frame.get("original_end_line"),
            "original_start_byte": frame.get("original_start_byte"),
            "original_end_byte": frame.get("original_end_byte"),
            "snapshot_start_line": 1,
            "snapshot_end_line": _line_count(data),
            "snapshot_bytes": len(data),
        }
        for key in (
            "identity_field",
            "force_complete",
            "force_row_count",
            "marker_kind",
            "context_id",
            "context_center_line",
            "context_offset",
            "diagnostic_category",
            "diagnostic_center_line",
            "parameter_tag",
            "parameter_section",
            "parameter_center",
        ):
            if key in frame:
                record[key] = frame[key]
        files[role].append(record)
    if not isinstance(source_mode, str) or not source_mode.strip():
        raise EvidenceError("source_mode must be a nonempty string")
    if not isinstance(command_mode, str) or not command_mode.strip():
        raise EvidenceError("command_mode must be a nonempty string")
    if command_argv is not None and (
        not isinstance(command_argv, list)
        or not all(isinstance(item, str) for item in command_argv)
    ):
        raise EvidenceError("command_argv must be a list of strings")
    if command_exit_code is not None and (
        not isinstance(command_exit_code, int) or isinstance(command_exit_code, bool)
    ):
        raise EvidenceError("command_exit_code must be an integer")
    for label, value in (
        ("command_started_utc", command_started_utc),
        ("command_ended_utc", command_ended_utc),
    ):
        if value is not None and parse_utc(value) is None:
            raise EvidenceError(f"{label} must be a valid UTC timestamp")
    if command_stderr is not None and not isinstance(command_stderr, str):
        raise EvidenceError("command_stderr must be a string")
    remote_export_exit_code = (end_frame or {}).get("exit_code")
    recorded_exit_code = (
        command_exit_code if command_exit_code is not None else remote_export_exit_code
    )
    if command_exit_code not in (None, 0):
        failures.append(f"SSH transport exited with code {command_exit_code}")
    if remote_export_exit_code not in (None, 0):
        failures.append(f"remote exporter exited with code {remote_export_exit_code}")
    command_document = {
        "mode": command_mode,
        "read_only": True,
        "argv": command_argv if command_argv is not None else [
            "ssh",
            "-p",
            str(identity["host"]["port"]),
            f"{identity['host']['user']}@{identity['host']['address']}",
            "python3",
            "-",
        ],
        "exit_code": recorded_exit_code,
        "remote_export_exit_code": remote_export_exit_code,
        "started_utc": command_started_utc,
        "ended_utc": command_ended_utc,
    }
    if command_stderr is not None:
        stderr_limit = 16 * 1024
        command_document["stderr_text"] = command_stderr[:stderr_limit]
        command_document["stderr_truncated"] = len(command_stderr) > stderr_limit
    document = {
        "schema": SCHEMA,
        "evidence_type": "primary_raw_evidence_bundle",
        "bundle_directory": str(bundle),
        "manifest_identity": identity,
        "collection": {
            "status": "COMPLETE" if not failures and remote_export_exit_code == 0 and recorded_exit_code == 0 else "PARTIAL",
            "collection_start_utc": (start_frame or {}).get("collection_start_utc") or utc_now(),
            "collection_end_utc": (end_frame or {}).get("collection_end_utc") or utc_now(),
            "command": command_document,
            "failures": failures,
            "source_mode": source_mode,
        },
        "files": dict(files),
        "metadata_files": metadata_files,
        "sensitive_files_stat_only": sensitive_stats,
        "sensitive_files_not_read": sorted(SENSITIVE_FILES),
        "metadata": metadata_frame or {},
    }
    write_json(bundle / "evidence.json", document)
    return bundle


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build/verify a bounded, source-traceable VASP evidence bundle."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture", help="stage a local fixture/source into a new UTC bundle")
    capture.add_argument("--task-dir", required=True)
    capture.add_argument("--manifest", required=True)
    capture.add_argument("--source-dir", required=True)
    capture.add_argument("--capture-metadata")
    capture.add_argument("--snapshot-name")
    verify = sub.add_parser("verify", help="verify one evidence bundle and emit JSON")
    verify.add_argument("bundle_dir")
    script = sub.add_parser("ssh-script", help="emit the read-only remote stdout exporter")
    script.add_argument("--manifest", required=True)
    ingest = sub.add_parser("ingest-ssh-stdout", help="ingest framed read-only SSH stdout")
    ingest.add_argument("--task-dir", required=True)
    ingest.add_argument("--manifest", required=True)
    ingest.add_argument("--snapshot-name")
    ingest.add_argument("--command-argv-json")
    ingest.add_argument("--ssh-exit-code", type=int)
    ingest.add_argument("--ssh-started-utc")
    ingest.add_argument("--ssh-ended-utc")
    ingest.add_argument("--ssh-stderr-base64")
    quick = sub.add_parser(
        "quick-status",
        help="issue one bounded read-only SSH status query and emit compact JSON",
    )
    quick.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "quick-status":
            result = quick_status(args.manifest)
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            if not result.get("ssh", {}).get("attempted"):
                return 2
            return 0 if result.get("query_status") == "OK" else 3
        if args.command == "capture":
            bundle = capture_bundle(
                args.task_dir,
                args.manifest,
                args.source_dir,
                capture_metadata_path=args.capture_metadata,
                snapshot_name=args.snapshot_name,
            )
            print(bundle)
            return 0
        if args.command == "verify":
            result = verify_bundle(args.bundle_dir)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "ssh-script":
            sys.stdout.write(build_remote_export_script(args.manifest))
            return 0
        command_argv = None
        if args.command_argv_json is not None:
            command_argv = json.loads(args.command_argv_json)
        command_stderr = None
        if args.ssh_stderr_base64 is not None:
            command_stderr = base64.b64decode(
                args.ssh_stderr_base64,
                validate=True,
            ).decode("utf-8", errors="replace")
        bundle = ingest_export(
            sys.stdin,
            args.task_dir,
            args.manifest,
            snapshot_name=args.snapshot_name,
            command_argv=command_argv,
            command_exit_code=args.ssh_exit_code,
            command_started_utc=args.ssh_started_utc,
            command_ended_utc=args.ssh_ended_utc,
            command_stderr=command_stderr,
        )
        print(bundle)
        return 0
    except (EvidenceError, OSError, ValueError, binascii.Error) as error:
        print(json.dumps({
            "schema": SCHEMA,
            "error": f"{type(error).__name__}: {error}",
        }, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())

"""Map supported existing JSON receipts to a compact, provenance-linked sidecar."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
from pathlib import Path

SCHEMA = "vasp-tool-evidence/v1"
KNOWN = {
    "vasp-practical-preparation/v1": "preparation",
    "vasp-practical-analysis/v1": "analysis",
    "vasp-result-record/v1": "result_record",
    "vasp-executor-check/v1": "executor_check",
    "vasp-practical-case-preparation/v1": "case_preparation",
    "vasp-execution-state/v1": "execution_state",
    "vasp-execution-receipt/v1": "execution_receipt",
    "vasp-practical-execution/v1": "runtime_plan",
}
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.-]+$")
SAFE_SCHEMA = re.compile(r"^[A-Za-z0-9_./-]+$")
MAX_TOKEN_LENGTH = 128


def _ptr(path: str, key: str | int) -> str:
    escaped = str(key).replace("~", "~0").replace("/", "~1")
    return f"{path}/{escaped}"


def _ref(path: str, pointer: str, obj: dict, key: str):
    if isinstance(obj, dict) and key in obj:
        return {"path": path, "json_pointer": _ptr(pointer, key)}
    return None


def _normalized_absolute(path: str | Path) -> Path:
    return Path(os.path.normpath(str(Path(path).expanduser().absolute())))


def _is_bool_or_none(value):
    return value if type(value) is bool else None


def _short_token(value):
    if isinstance(value, str) and len(value) <= MAX_TOKEN_LENGTH and SAFE_TOKEN.fullmatch(value):
        return value
    return None


def _short_schema(value):
    if isinstance(value, str) and len(value) <= MAX_TOKEN_LENGTH and SAFE_SCHEMA.fullmatch(value):
        return value
    return None


def _measured_number(value):
    if type(value) not in (int, float) or value < 0:
        return None
    try:
        if not math.isfinite(value):
            return None
    except OverflowError:
        return None
    return value


def _measured_count(value):
    if type(value) is not int or value < 0:
        return None
    return value


def map_receipt(receipt: dict, receipt_path: Path) -> dict:
    source_path = str(_normalized_absolute(receipt_path))
    schema = receipt.get("schema")
    family = KNOWN.get(schema) if isinstance(schema, str) else None
    base = {
        "schema": SCHEMA,
        "operation": family or "unknown",
        "tool_outcome": "UNKNOWN",
        "reason_code": "MISSING_RUN_EVIDENCE",
        "existing_receipt": {"path": source_path, "schema": _short_schema(schema)},
        "execution_id": None,
        "case_id": None,
        "attempt": None,
        "run_state": "UNKNOWN",
        "result_state": "UNKNOWN",
        "electronic_converged": None,
        "ionic_converged": None,
        "ionic_convergence_context": "NOT_EVALUATED",
        "geometry_relaxation_status": "NOT_EVALUATED",
        "scientific_acceptance": "NOT_EVALUATED",
        "metrics": {"human_time_seconds": None, "manual_repairs": None, "tool_elapsed_seconds": None},
        "checks_performed": [],
        "missing": [],
        "evidence_refs": [],
        "effects_observed": {"source_read": None, "local_artifact_write": None,
                             "remote_tool_call": None, "remote_compute_control": None},
        "next_action": "Review the referenced source receipt; do not infer scientific acceptance.",
    }
    if family is None:
        base["tool_outcome"] = "UNSUPPORTED_SCHEMA"
        base["reason_code"] = "UNSUPPORTED_SCHEMA"
        base["missing"] = ["supported receipt schema"]
        return base

    def ref(pointer, obj, key):
        item = _ref(source_path, pointer, obj, key)
        if item:
            base["evidence_refs"].append(item)
        return item

    def top(key, default=None):
        return receipt.get(key, default)

    base["checks_performed"].append("receipt JSON parsed; schema dispatched by exact schema identifier")
    acceptance = _short_token(top("scientific_acceptance"))
    base["scientific_acceptance"] = acceptance or "NOT_EVALUATED"
    ref("", receipt, "scientific_acceptance")
    base["metrics"]["human_time_seconds"] = _measured_number(top("active_human_seconds"))
    base["metrics"]["manual_repairs"] = _measured_count(top("manual_repairs"))
    wall = top("preparation_wall_seconds")
    wall_key = "preparation_wall_seconds"
    if wall is None:
        wall, wall_key = top("elapsed_seconds"), "elapsed_seconds"
    base["metrics"]["tool_elapsed_seconds"] = _measured_number(wall)
    for key in ("active_human_seconds", "manual_repairs", "preparation_wall_seconds", "elapsed_seconds"):
        ref("", receipt, key)

    if family == "preparation":
        passed = _is_bool_or_none(top("passed"))
        base["tool_outcome"] = "PREPARED" if passed is True else "FAILED" if passed is False else "UNKNOWN"
        base["reason_code"] = "PREPARATION_PASSED" if passed is True else "PREPARATION_FAILED" if passed is False else "PREPARATION_STATUS_MISSING"
        base["result_state"] = "PREPARED_LOCAL" if passed is True else "PREPARATION_FAILED" if passed is False else "UNKNOWN"
        execution = top("execution")
        if _short_token(execution) in {"NOT_SUBMITTED", "NOT_AUTHORIZED", "SUBMITTED", "RUNNING", "COMPLETED", "FAILED"}:
            base["run_state"] = execution
        else:
            base["missing"].append("direct run-state evidence")
        rt = top("runtime_package")
        if isinstance(rt, dict):
            base["execution_id"] = _short_token(rt.get("execution_id"))
            case = rt.get("case")
            base["case_id"] = _short_token(case)
            ref("/runtime_package", rt, "execution_id")
            ref("/runtime_package", rt, "case")
        ref("", receipt, "execution")
        backends = top("backend_receipts")
        backend_mode = _short_token(top("backend_evidence_mode"))
        live_observation = backend_mode == "LIVE_VASPKIT" and isinstance(backends, list) and any(
            isinstance(item, dict)
            and type(item.get("returncode")) is int
            and isinstance(item.get("stdout"), str)
            and isinstance(item.get("command"), list)
            and all(isinstance(part, str) for part in item["command"])
            and any("vaspkit" in part.casefold() for part in item["command"])
            for item in backends
        )
        if live_observation:
            base["effects_observed"]["remote_tool_call"] = True
            for index, item in enumerate(backends):
                if isinstance(item, dict) and type(item.get("returncode")) is int:
                    ref(_ptr("/backend_receipts", index), item, "returncode")
                    ref(_ptr("/backend_receipts", index), item, "command")
        ref("", receipt, "backend_evidence_mode")
        ref("", receipt, "backend_receipts")
        if not live_observation:
            base["missing"].append("reliable observed remote_tool_call evidence")
        base["effects_observed"]["local_artifact_write"] = True if passed is True and isinstance(top("delivery_scope"), str) else None
        ref("", receipt, "passed")
        ref("", receipt, "delivery_scope")
        if passed is None:
            base["missing"].append("passed")
        base["next_action"] = "Review the closed execution gate and independent checks before any separate authorization." if passed is True else "Inspect the failure evidence; no automatic retry is implied."

    elif family == "analysis":
        status = _short_token(top("status"))
        base["tool_outcome"] = "ANALYZED" if status in {"COMPLETE_XML", "PARTIAL"} else "FAILED" if status == "FAILED" else "UNKNOWN"
        base["reason_code"] = "ANALYSIS_RECEIPT" if base["tool_outcome"] in {"ANALYZED", "FAILED"} else "ANALYSIS_STATUS_UNKNOWN"
        base["result_state"] = status or "UNKNOWN"
        base["electronic_converged"] = _is_bool_or_none(top("electronic_converged"))
        base["ionic_converged"] = _is_bool_or_none(top("ionic_converged"))
        parameters = top("parameters")
        nsw = parameters.get("NSW") if isinstance(parameters, dict) else None
        if type(nsw) is int and nsw == 0 or isinstance(nsw, str) and nsw.strip() == "0":
            base["ionic_convergence_context"] = "NSW_0_STATIC_PARSER_FLAG_NOT_GEOMETRY_RELAXATION"
        elif nsw is not None:
            base["ionic_convergence_context"] = "IONIC_CONVERGENCE_PARSER_FIELD_ONLY"
        else:
            base["ionic_convergence_context"] = "NSW_NOT_RECORDED"
        base["geometry_relaxation_status"] = "NOT_EVALUATED"
        ref("", receipt, "status")
        ref("", receipt, "electronic_converged")
        ref("", receipt, "ionic_converged")
        if isinstance(parameters, dict):
            ref("/parameters", parameters, "NSW")
        source = top("source")
        base["effects_observed"]["source_read"] = True if isinstance(source, str) and source else None
        base["effects_observed"]["local_artifact_write"] = True if isinstance(top("output_dir"), str) and top("output_dir") else None
        ref("", receipt, "source")
        ref("", receipt, "output_dir")
        base["missing"] = ["status"] if status is None else []
        if top("source") is None:
            base["missing"].append("source")
        if base["ionic_convergence_context"].startswith("NSW_0_STATIC"):
            base["next_action"] = "The parser ionic_converged field is retained as reported; NSW=0 is static and does not establish geometry-relaxation completion or scientific acceptance."
        else:
            base["next_action"] = "Use explicit convergence fields and their referenced source; COMPLETE_XML is parser completeness, not run completion or scientific acceptance."

    elif family == "result_record":
        for key, target in (("execution_id", "execution_id"), ("case_id", "case_id"), ("attempt", "attempt")):
            value = top(key)
            if key == "attempt":
                if type(value) is int and value > 0:
                    base[target] = value
            else:
                base[target] = _short_token(value)
            ref("", receipt, key)
        for key in ("run_state", "result_state"):
            token = _short_token(top(key))
            if token:
                base[key] = token
            ref("", receipt, key)
        base["tool_outcome"] = "RECORDED"
        base["reason_code"] = "RESULT_RECORD_FIELDS_COPIED"
        base["electronic_converged"] = _is_bool_or_none(top("electronic_converged"))
        base["ionic_converged"] = _is_bool_or_none(top("ionic_converged"))
        for key in ("electronic_converged", "ionic_converged", "source"):
            ref("", receipt, key)
        base["effects_observed"]["source_read"] = True if isinstance(top("source"), str) and top("source") else None
        base["missing"] = [key for key in ("execution_id", "case_id", "attempt", "run_state", "result_state") if base[key] in (None, "UNKNOWN")]

    elif family == "executor_check":
        passed = _is_bool_or_none(top("passed"))
        mode = _short_token(top("mode"))
        base["tool_outcome"] = "CHECK_PASSED" if passed is True else "CHECK_FAILED" if passed is False else "UNKNOWN"
        base["reason_code"] = "EXECUTOR_CHECK_RESULT" if passed is not None else "CHECK_STATUS_MISSING"
        base["result_state"] = (mode.upper() + "_CHECK_" + ("PASSED" if passed else "FAILED")) if passed is not None and mode else "UNKNOWN"
        program = top("program_exit")
        if isinstance(program, dict) and type(program.get("raw")) is int:
            base["reason_code"] = "PROGRAM_EXIT_ZERO" if program["raw"] == 0 else "PROGRAM_EXIT_NONZERO"
            ref("/program_exit", program, "raw")
        ref("", receipt, "passed")
        ref("", receipt, "mode")
        conv = top("convergence")
        if isinstance(conv, dict):
            for key in ("electronic_scf", "ionic_convergence"):
                value = _is_bool_or_none(conv.get(key))
                if value is not None:
                    base["electronic_converged" if key == "electronic_scf" else "ionic_converged"] = value
                ref("/convergence", conv, key)
        base["missing"] = [] if passed is not None else ["passed"]
        if mode is None:
            base["missing"].append("mode")
        base["next_action"] = "Interpret postcheck and program exit separately from run state, convergence, and scientific acceptance."

    elif family == "case_preparation":
        case_dir = top("case_dir")
        case = top("case")
        execution_id = top("execution_id")
        prepared = isinstance(case_dir, str) and bool(case_dir)
        base["tool_outcome"] = "CASE_PREPARED" if prepared else "UNKNOWN"
        base["reason_code"] = "CASE_PREPARATION_RECEIPT" if prepared else "CASE_IDENTITY_MISSING"
        base["result_state"] = "PREPARED_CASE" if prepared else "UNKNOWN"
        direct_execution = _short_token(top("execution"))
        if direct_execution in {"NOT_SUBMITTED", "NOT_AUTHORIZED", "SUBMITTED", "RUNNING", "COMPLETED", "FAILED"}:
            base["run_state"] = direct_execution
        base["execution_id"] = _short_token(execution_id)
        base["case_id"] = _short_token(case)
        for key in ("execution_id", "case", "case_dir", "execution"):
            ref("", receipt, key)
        base["effects_observed"]["local_artifact_write"] = True if prepared else None
        base["missing"] = [] if prepared else ["case_dir"]
        for key in ("execution_id", "case_id"):
            if base[key] is None:
                base["missing"].append(key)
        if base["run_state"] == "UNKNOWN":
            base["missing"].append("direct run-state evidence")
        base["next_action"] = "Case preparation does not mean a run was submitted."

    elif family == "execution_state":
        status = _short_token(top("status"))
        base["tool_outcome"] = "STATE_RECORDED" if status is not None else "UNKNOWN"
        base["reason_code"] = "EXECUTION_STATE_RECEIPT" if status is not None else "STATE_MISSING"
        base["run_state"] = status or "UNKNOWN"
        execution_id = top("execution_id")
        base["execution_id"] = _short_token(execution_id)
        base["result_state"] = "NOT_EVALUATED"
        for key in ("status", "execution_id", "runner_exit_code", "cases"):
            ref("", receipt, key)
        base["missing"] = ["status"] if status is None else []
        if base["execution_id"] is None:
            base["missing"].append("execution_id")

    elif family == "execution_receipt":
        status = _short_token(top("status"))
        base["tool_outcome"] = "EXECUTION_RECEIPT" if status is not None else "UNKNOWN"
        base["reason_code"] = "EXECUTION_RECEIPT_STATUS" if status is not None else "STATE_MISSING"
        base["run_state"] = status or "UNKNOWN"
        execution_id = top("execution_id")
        case = top("case")
        base["execution_id"] = _short_token(execution_id)
        base["case_id"] = _short_token(case)
        for key in ("status", "execution_id", "case", "program_exit"):
            ref("", receipt, key)
        base["missing"] = ["status"] if status is None else []
        if base["execution_id"] is None:
            base["missing"].append("execution_id")
        if base["case_id"] is None:
            base["missing"].append("case")

    elif family == "runtime_plan":
        actions = top("actions")
        launch = actions.get("launch") if isinstance(actions, dict) else None
        base["tool_outcome"] = "PLAN_DECLARED"
        base["reason_code"] = "EXECUTION_PLAN_ONLY"
        base["result_state"] = "PREPARED_LOCAL_CLOSED" if top("status") == "PREPARED_LOCAL_CLOSED" else "PLAN_ONLY"
        execution_id = top("execution_id")
        case = top("case")
        base["execution_id"] = _short_token(execution_id)
        base["case_id"] = _short_token(case)
        base["plan_scope"] = {"launch_enabled": launch if type(launch) is bool else None}
        ref("", receipt, "status")
        ref("", receipt, "execution_id")
        ref("", receipt, "case")
        ref("/actions", actions, "launch")
        if _short_token(top("status")) is None:
            base["missing"].append("status")
        if base["execution_id"] is None:
            base["missing"].append("execution_id")
        if base["case_id"] is None:
            base["missing"].append("case")
        if type(launch) is not bool:
            base["missing"].append("actions.launch")
        base["next_action"] = "A plan is not evidence that a run was submitted."

    if "scientific_acceptance" in receipt and acceptance is None:
        base["missing"].append("valid scientific_acceptance")
    return base


def _reject_link_components(path: Path):
    current = path.absolute()
    while True:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("paths cannot traverse a symlink or junction")
        if current.parent == current:
            break
        current = current.parent


def _source_receipt_path(path: str | Path) -> Path:
    candidate = _normalized_absolute(path)
    _reject_link_components(candidate)
    if not candidate.is_file():
        raise ValueError("receipt must be an existing regular JSON file")
    return candidate


def _receipt_source_values(receipt: dict):
    for key in ("source", "source_dir", "case_dir", "calculation_dir", "output_dir"):
        value = receipt.get(key)
        if isinstance(value, str):
            yield value
    for section_name, path_keys in (("program_exit", ("source",)), ("input", ("input_dir", "source_dir", "case_dir"))):
        section = receipt.get(section_name)
        if isinstance(section, dict):
            for key in path_keys:
                value = section.get(key)
                if isinstance(value, str):
                    yield value
    cases = receipt.get("cases")
    if isinstance(cases, list):
        for case in cases:
            if isinstance(case, dict):
                for key in ("case_dir", "calculation_dir", "source_dir", "path"):
                    value = case.get(key)
                    if isinstance(value, str):
                        yield value


def validate_output_path(output: Path, receipt_path: Path, receipt: dict) -> Path:
    candidate = _normalized_absolute(output)
    if candidate.exists() or candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
        raise ValueError("output must be a new file; overwrite is forbidden")
    if not candidate.parent.is_dir():
        raise ValueError("output parent directory must already exist")
    _reject_link_components(candidate.parent)
    target = candidate
    source = _normalized_absolute(receipt_path)
    if target == source:
        raise ValueError("output cannot overwrite its input receipt")
    forbidden = set()
    for value in _receipt_source_values(receipt):
        if not value:
            continue
        if value.startswith("fixture://"):
            continue
        referenced = Path(value).expanduser()
        if not referenced.is_absolute():
            referenced = (receipt_path.parent / referenced)
        existing = referenced if referenced.exists() else referenced.parent if referenced.parent.is_dir() else None
        if existing is not None:
            _reject_link_components(existing)
            existing = _normalized_absolute(existing)
            forbidden.add(existing if existing.is_dir() else existing.parent)
    if not forbidden:
        forbidden.add(source.parent)
    for root in forbidden:
        if target == root or root in target.parents:
            raise ValueError("output must be outside the source calculation directory")
    return target


def summarize(receipt_path: str | Path, output_path: str | Path | None = None) -> dict:
    source = _source_receipt_path(receipt_path)
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("receipt JSON root must be an object")
    output = validate_output_path(Path(output_path), source, payload) if output_path is not None else None
    result = map_receipt(payload, source)
    if output is not None:
        # All input, output-path, and mapping checks happen before the first artifact.
        serialized = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        with output.open("x", encoding="utf-8") as stream:
            stream.write(serialized + "\n")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    result = summarize(args.receipt, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

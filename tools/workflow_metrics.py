"""Aggregate only explicitly referenced VASP workflow observations.

The module reads JSON receipts listed in a manifest. It never scans directories,
opens calculation outputs, launches a workflow, or infers timings from paths.
"""
from __future__ import annotations

import argparse
import os
import csv
import json
import math
from pathlib import Path, PurePosixPath


TOOLS = Path(__file__).resolve().parent
PROJECT_ROOT = Path(os.environ.get("DFT_WORKSPACE_ROOT", str(TOOLS.parent))).resolve()
MANIFEST_SCHEMA = "vasp-workflow-observations/v1"
METRICS_SCHEMA = "vasp-workflow-observation/v1"
FORBIDDEN_PATH_PART = "excluded_reference_trial"

METRIC_SPECS = {
    "preparation_command_elapsed_seconds": {
        "unit": "s", "scope": "preparation command/workflow only",
        "description": "Receipt elapsed_seconds; never approval-to-delivery or human time.",
        "improvement_direction": "lower_is_better",
    },
    "preparation_wall_seconds": {
        "unit": "s", "scope": "preparation wrapper only",
        "description": "Explicit preparation_wall_seconds; never end-to-end or human time.",
        "improvement_direction": "lower_is_better",
    },
    "active_operations": {"unit": "operations", "scope": "observed workflow activity",
                           "improvement_direction": None},
    "approved_to_delivery_wall_seconds": {"unit": "s", "scope": "approval through delivery",
                                           "improvement_direction": "lower_is_better"},
    "active_human_seconds": {"unit": "s", "scope": "measured human activity",
                              "improvement_direction": "lower_is_better"},
    "mechanical_repairs_events": {"unit": "events", "scope": "explicitly recorded mechanical repair events",
                                   "improvement_direction": "lower_is_better"},
    "first_pass": {"unit": "boolean", "scope": "attempt-linked first-pass outcome",
                    "improvement_direction": "higher_is_better"},
    "failed_attempts": {"unit": "attempts", "scope": "explicit execution/result attempts",
                         "improvement_direction": "lower_is_better"},
    "stopped_attempts": {"unit": "attempts", "scope": "explicit execution/result stop states",
                         "improvement_direction": None},
    "observable_tool_calls": {"unit": "calls", "scope": "actual, explicitly classified tool calls",
                              "improvement_direction": None},
    "vasp_runtime_seconds": {"unit": "s", "scope": "observed VASP runtime",
                             "improvement_direction": "lower_is_better"},
    "resource_core_hours": {"unit": "core-hours", "scope": "observed compute resource cost",
                            "improvement_direction": "lower_is_better"},
}

COMPARISON_FIELDS = (
    "template", "approved_input_identity", "environment", "generation_scope",
    "analysis_scope", "metric_definition",
)

# Read-only source/test audit. NOT_OBSERVED means the implementation has not
# been executed against a live host, scheduler, or production notification path.
CONTRACT_COVERAGE = [
    {
        "area": "host/directory/task-unit-execution-case-cwd",
        "status": "CODE_AND_GUARD_TESTS_COVERED; LIVE_RUNTIME_NOT_OBSERVED",
        "fields": "host, remote_batch_dir, task_id, unit_id, execution_id, case, case_dir/runtime_input_dir",
        "code_refs": ["practical_execution_builder.py#describe", "practical_execution_builder.py#verify_source_contract", "practical_runtime.py#run", "practical_runtime.py#execute_case"],
        "test_refs": ["test_practical_runtime.py#test_unique_execution_does_not_inherit_authorization", "test_practical_runtime.py#test_launcher_uses_exact_case_cwd_and_lock", "test_job_watch.py#test_execution_receipt_has_identity_timestamps_and_stage_durations"],
        "not_observed": "No live host submission, remote directory, or production cwd event is measured by the 27 preparation receipts.",
    },
    {
        "area": "environment/PAW/input-mask",
        "status": "CODE_AND_GUARD_TESTS_COVERED; LIVE_RUNTIME_NOT_OBSERVED",
        "fields": "environment_id, VASP/MPI/OMP profile, PAW family/order/component identity, approved input and selective-dynamics mask",
        "code_refs": ["practical_execution_builder.py#describe", "practical_execution_builder.py#verify_source_contract", "practical_runtime.py#materialize_case", "practical_runtime.py#verify_case_receipt"],
        "test_refs": ["test_practical_runtime.py#test_unsafe_environment_not_rendered_into_shell", "test_practical_runtime.py#test_conflicting_execution_descriptors_rejected", "test_practical_runtime.py#test_fresh_stage_preserves_exact_inputs_and_paw_order", "test_practical_runtime.py#test_bad_paw_preserves_failed_stage"],
        "not_observed": "The audit reuses synthetic/local guards; no new environment probe or live PAW read was run.",
    },
    {
        "area": "fresh/warm restart source and mutation",
        "status": "CODE_AND_GUARD_TESTS_COVERED; LIVE_RUNTIME_NOT_OBSERVED",
        "fields": "restart mode, exact source case, source file size/stability, independent target copy, fresh fallback refusal",
        "code_refs": ["practical_execution_builder.py#verify_source_contract", "practical_runtime.py#copy_restart", "practical_runtime.py#materialize_case", "practical_runtime.py#verify_case_receipt"],
        "test_refs": ["test_practical_runtime.py#test_warm_stage_copies_independent_files_and_checks_mutation", "test_practical_runtime.py#test_restart_size_mismatch_does_not_copy"],
        "not_observed": "No restart contents or calculation directories were opened for B5; receipt-only baseline has no runtime copy event.",
    },
    {
        "area": "output contract and capped-run purpose",
        "status": "CODE_AND_GUARD_TESTS_COVERED; LIVE_RUNTIME_NOT_OBSERVED",
        "fields": "required outputs, program/electronic markers, ionic observation-only markers, finite NSW purpose",
        "code_refs": ["practical_execution_builder.py#output_requirements", "practical_execution_builder.py#describe", "practical_runtime.py#execute_case", "job_watch.py#terminal-state classification"],
        "test_refs": ["test_practical_runtime.py#test_cap_preserves_ionic_observation_policy", "test_practical_runtime.py#test_full_capped_runner_uses_existing_preflight_and_postcheck", "test_job_watch.py#test_relaxation_not_converged"],
        "not_observed": "Preparation receipts do not show a completed runtime or scientific acceptance.",
    },
    {
        "area": "resources and upload whitelist",
        "status": "CODE_COVERED; GUARD_EVIDENCE_REUSED; LIVE_RESOURCE_USE_NOT_OBSERVED",
        "fields": "approved MPI/OMP ranks, physical core check, immutable upload list and excluded restart/PAW/geometry artifacts",
        "code_refs": ["practical_execution_builder.py#describe", "practical_execution_builder.py#build", "practical_runtime.py#runtime_environment"],
        "test_refs": ["test_practical_runtime.py#test_conflicting_execution_descriptors_rejected", "practical_rollout_validation_20260927.md#119-of-119-affected-suite"],
        "not_observed": "Declared MPI ranks are planned resources only; no actual resource cost or live upload is inferred.",
    },
    {
        "area": "closed gate/one-time lock/preflight/no-overwrite",
        "status": "CODE_AND_GUARD_TESTS_COVERED; LIVE_RUNTIME_NOT_OBSERVED",
        "fields": "user/Sol/gate booleans, unique execution identity, preflight-before-lock, exact cwd, one-run lock, existing-case protection",
        "code_refs": ["practical_runtime.py#authorized", "practical_runtime.py#launch_once", "practical_runtime.py#execute_case", "practical_execution_builder.py#build"],
        "test_refs": ["test_practical_runtime.py#test_closed_gate_never_enters_runtime", "test_practical_runtime.py#test_unique_execution_does_not_inherit_authorization", "test_practical_runtime.py#test_launcher_uses_exact_case_cwd_and_lock", "test_practical_runtime.py#test_rejected_preflight_does_not_write_or_overwrite_case_outputs"],
        "not_observed": "No gate was opened or runtime package migrated in B5.",
    },
    {
        "area": "receipt/unknown/notification semantics",
        "status": "CODE_AND_GUARD_TESTS_COVERED; LIVE_NOTIFICATION_NOT_OBSERVED",
        "fields": "execution identity, stage timings, unknown/missing outputs, supervisor status, notification event and user acknowledgement",
        "code_refs": ["job_watch.py#receipt/status writers", "practical_runtime.py#execute_case"],
        "test_refs": ["test_job_watch.py#test_runner_failure", "test_job_watch.py#test_truncated_output", "test_job_watch.py#test_notification_failure_preserves_result", "test_job_watch.py#test_zero_exit_adapter_is_not_user_acknowledgement", "test_job_watch.py#test_execution_receipt_has_identity_timestamps_and_stage_durations"],
        "not_observed": "No notification service, user acknowledgement, or live supervisor event was triggered for B5.",
    },
]


def _finite_number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(float(value)) and value >= 0


def _workspace_path(source_ref: str, workspace_root: Path) -> Path:
    if not isinstance(source_ref, str) or not source_ref.strip():
        raise ValueError("source_ref must be a nonempty workspace-relative JSON path")
    posix = PurePosixPath(source_ref.replace("\\", "/"))
    if posix.is_absolute() or any(part in {".", ".."} for part in posix.parts):
        raise ValueError("source_ref must not be absolute or traverse outside the workspace")
    if FORBIDDEN_PATH_PART.casefold() in {part.casefold() for part in posix.parts}:
        raise ValueError("the excluded previous-results trial directory is not an allowed source")
    if posix.suffix.casefold() != ".json":
        raise ValueError("only explicitly referenced JSON receipts are supported")
    root = workspace_root.resolve()
    path = root.joinpath(*posix.parts).resolve()
    if not path.is_relative_to(root):
        raise ValueError("source_ref resolves outside the workspace")
    return path


def _read_manifest(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"observation manifest must use {MANIFEST_SCHEMA}")
    if not isinstance(data.get("observations"), list):
        raise ValueError("observation manifest observations must be a list")
    for group_name in ("result_records", "metrics_receipts"):
        if group_name in data and not isinstance(data[group_name], list):
            raise ValueError(f"{group_name} must be a list")
    return data


def _entry_rows(manifest: dict, manifest_ref: str | None = None) -> list[dict]:
    rows = []
    for key, default_kind in (("observations", "practical_preparation"),
                              ("result_records", "result_record"),
                              ("metrics_receipts", "metrics_observation")):
        for row_index, row in enumerate(manifest.get(key, [])):
            if not isinstance(row, dict):
                raise ValueError(f"{key} entries must be objects")
            item = dict(row)
            item.setdefault("kind", default_kind)
            item.setdefault("collection", key)
            if item.get("metric_definition") is not None and manifest_ref:
                item["_metric_definition_source_ref"] = (
                    f"{manifest_ref}#/{key}/{row_index}/metric_definition")
            rows.append(item)
    return rows


def _expected_schema(kind: str):
    return {
        "practical_preparation": "vasp-practical-preparation/v1",
        "result_record": "vasp-result-record/v1",
        "execution_receipt": "vasp-execution-receipt/v1",
        "metrics_observation": METRICS_SCHEMA,
    }.get(kind)


def _identity(payload: dict, entry: dict, source_ref: str) -> dict:
    if isinstance(payload.get("identity"), dict):
        source = payload["identity"]
        source_pointer = "/identity"
    else:
        source = payload
        source_pointer = ""
    package = payload.get("package_report") if isinstance(payload.get("package_report"), dict) else {}

    def pick(key, fallback=None):
        if key in source and source[key] not in (None, "", {}):
            return source[key], f"{source_ref}#{source_pointer}/{key}"
        if fallback and fallback[0] in package and package[fallback[0]] not in (None, "", {}):
            return package[fallback[0]], f"{source_ref}#/package_report/{fallback[0]}"
        return None, None

    template, template_ref = pick("template", ("template",))
    approved, approved_ref = pick("approved_input_identity")
    if approved is None:
        source_hash = package.get("source_spec_sha256")
        generated_hashes = package.get("generated_input_hashes")
        if source_hash and isinstance(generated_hashes, dict) and generated_hashes:
            approved = {"source_spec_sha256": source_hash, "generated_input_hashes": generated_hashes}
            approved_ref = f"{source_ref}#/package_report"
    environment, environment_ref = pick("environment", ("environment",))
    if environment is None:
        environment_id, environment_ref = pick("environment_id", ("environment_id",))
        if environment_id is not None:
            environment = environment_id
    generation_scope, generation_ref = pick("generation_scope", ("generation_scope",))
    if generation_scope is None:
        generation_scope, generation_ref = pick("delivery_scope", ("delivery_scope",))
    analysis_scope, analysis_ref = pick("analysis_scope", ("analysis_scope",))
    metric_definition = entry.get("metric_definition")
    metric_ref = entry.get("_metric_definition_source_ref") if metric_definition is not None else None
    if metric_definition is None:
        metric_definition = source.get("metric_definition")
        metric_ref = (f"{source_ref}#{source_pointer}/metric_definition"
                      if metric_definition is not None else None)
    return {
        "template": {"value": template, "source_ref": template_ref},
        "approved_input_identity": {"value": approved, "source_ref": approved_ref},
        "environment": {"value": environment, "source_ref": environment_ref},
        "generation_scope": {"value": generation_scope, "source_ref": generation_ref},
        "analysis_scope": {"value": analysis_scope, "source_ref": analysis_ref},
        "metric_definition": {"value": metric_definition, "source_ref": metric_ref},
    }


def _comparison_key(identity: dict):
    if any(not item.get("value") or not item.get("source_ref") for item in identity.values()):
        return None
    return json.dumps({name: identity[name]["value"] for name in COMPARISON_FIELDS},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _preparation_values(payload: dict, source_ref: str) -> dict:
    values = {}

    def add(name, value, pointer, reason=None):
        valid = type(value) is bool if name == "first_pass" else _finite_number(value)
        if value is not None and not valid:
            value = None
            reason = "invalid_or_non_numeric_value"
        values[name] = {"value": value, "unit": METRIC_SPECS[name]["unit"],
                        "source_ref": f"{source_ref}#{pointer}" if value is not None else None,
                        "missing_reason": reason if value is None else None}

    for name, field in (
        ("preparation_command_elapsed_seconds", "elapsed_seconds"),
        ("preparation_wall_seconds", "preparation_wall_seconds"),
        ("active_operations", "active_operations"),
        ("approved_to_delivery_wall_seconds", "approved_to_delivery_wall_seconds"),
        ("active_human_seconds", "active_human_seconds"),
    ):
        value = payload.get(field)
        if field not in payload:
            reason = "field_absent"
        elif value is None:
            reason = "explicit_null"
        else:
            reason = None
        add(name, value, f"/{field}", reason)

    if payload.get("manual_repairs_measurement") == "RECORDED_EVENTS" and "manual_repairs" in payload:
        add("mechanical_repairs_events", payload.get("manual_repairs"), "/manual_repairs")
    else:
        old_zero_unmarked = payload.get("manual_repairs") == 0 and "manual_repairs_measurement" not in payload
        reason = "unmarked_legacy_zero_not_a_measurement" if old_zero_unmarked else "RECORDED_EVENTS_marker_absent"
        add("mechanical_repairs_events", None, "/manual_repairs", reason)

    if payload.get("first_pass_measurement") == "RECORDED_EVENTS" and type(payload.get("first_pass")) is bool:
        add("first_pass", payload["first_pass"], "/first_pass")
    else:
        add("first_pass", None, "/first_pass", "attempt-linked_first-pass_observation_absent")

    mode = payload.get("backend_evidence_mode")
    backend_rows = payload.get("backend_receipts")
    if isinstance(mode, str) and mode in {"LIVE_VASPKIT", "ACTUAL_VASPKIT"} and isinstance(backend_rows, list):
        valid = [item for item in backend_rows if isinstance(item, dict) and
                 isinstance(item.get("command"), list) and item.get("command") and
                 type(item.get("returncode")) is int and isinstance(item.get("stage"), str) and item.get("stage")]
        if valid:
            values["observable_tool_calls"] = {
                "value": len(valid), "unit": "calls",
                "source_ref": f"{source_ref}#/backend_receipts",
                "missing_reason": None,
            }
        else:
            values["observable_tool_calls"] = {
                "value": None, "unit": "calls", "source_ref": None,
                "missing_reason": "live_mode_without_complete_call_receipt",
            }
    else:
        reason = "frozen_backend_receipt_excluded" if mode == "FROZEN_RECEIPT_VALIDATION_ONLY" else "backend_provenance_mode_not_explicit"
        values["observable_tool_calls"] = {"value": None, "unit": "calls", "source_ref": None,
                                           "missing_reason": reason}
    return values


def _metrics_payload_values(payload: dict, source_ref: str) -> dict:
    raw = payload.get("metrics")
    values = {}
    if not isinstance(raw, dict):
        return values
    for name, spec in METRIC_SPECS.items():
        item = raw.get(name)
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        unit = item.get("unit")
        evidence_ref = item.get("evidence_ref")
        valid_value = value is None or (type(value) is bool if name == "first_pass" else _finite_number(value))
        if unit != spec["unit"] or not valid_value or (value is not None and not isinstance(evidence_ref, str)):
            values[name] = {"value": None, "unit": spec["unit"], "source_ref": None,
                            "missing_reason": "invalid_unit_value_or_evidence_ref"}
        elif value is None:
            values[name] = {"value": None, "unit": unit, "source_ref": None,
                            "missing_reason": item.get("missing_reason", "explicit_null")}
        else:
            values[name] = {"value": value, "unit": unit,
                            "source_ref": f"{source_ref}#{evidence_ref.lstrip('#')}",
                            "missing_reason": None}
    return values


def _load_rows(manifest_path: Path, manifest: dict, workspace_root: Path):
    rows = []
    errors = []
    manifest_path = Path(manifest_path).resolve()
    root = Path(workspace_root).resolve()
    try:
        manifest_ref = manifest_path.relative_to(root).as_posix()
    except ValueError:
        manifest_ref = manifest_path.as_posix()
    for index, entry in enumerate(_entry_rows(manifest, manifest_ref), 1):
        source_ref = entry.get("source_ref")
        observation_id = entry.get("observation_id") or f"observation-{index:03d}"
        kind = entry.get("kind")
        row = {"observation_id": observation_id, "source_ref": source_ref,
               "kind": kind, "collection": entry.get("collection"), "payload": None,
               "identity": None, "metrics": {}, "schema_status": "UNKNOWN",
               "source_status": "PRESENT" if source_ref else "MISSING_REFERENCE",
               "comparison_role": entry.get("comparison_role")}
        if not source_ref:
            row["source_status"] = "INSUFFICIENT_EVIDENCE"
            errors.append({"observation_id": observation_id, "reason": "source_ref_missing"})
            rows.append(row)
            continue
        try:
            path = _workspace_path(source_ref, workspace_root)
        except ValueError as error:
            row["source_status"] = "REJECTED_SOURCE_REF"
            errors.append({"observation_id": observation_id, "source_ref": source_ref,
                           "reason": type(error).__name__, "detail": str(error)})
            rows.append(row)
            continue
        if not path.is_file():
            row["source_status"] = "MISSING_SOURCE"
            errors.append({"observation_id": observation_id, "source_ref": source_ref,
                           "reason": "explicit_source_file_not_found"})
            rows.append(row)
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            row["source_status"] = "INSUFFICIENT_EVIDENCE"
            errors.append({"observation_id": observation_id, "source_ref": source_ref,
                           "reason": type(error).__name__})
            rows.append(row)
            continue
        if not isinstance(payload, dict):
            row["source_status"] = "UNSUPPORTED_SCHEMA"
            errors.append({"observation_id": observation_id, "source_ref": source_ref,
                           "reason": "receipt_root_not_object"})
            rows.append(row)
            continue
        expected = _expected_schema(kind)
        schema = payload.get("schema")
        row["schema"] = schema
        if not expected or schema != expected:
            row["schema_status"] = "UNSUPPORTED_SCHEMA"
            row["source_status"] = "INSUFFICIENT_EVIDENCE"
            errors.append({"observation_id": observation_id, "source_ref": source_ref,
                           "expected_schema": expected, "actual_schema": schema})
            rows.append(row)
            continue
        row["schema_status"] = "SUPPORTED"
        row["payload"] = payload
        row["identity"] = _identity(payload, entry, source_ref)
        if row["comparison_role"] not in {"baseline", "candidate"}:
            role = payload.get("comparison_role")
            row["comparison_role"] = role if role in {"baseline", "candidate"} else row["comparison_role"]
        if kind == "practical_preparation":
            row["metrics"] = _preparation_values(payload, source_ref)
        elif kind == "metrics_observation":
            row["metrics"] = _metrics_payload_values(payload, source_ref)
            role = payload.get("comparison_role")
            row["comparison_role"] = role if role in {"baseline", "candidate"} else row["comparison_role"]
        rows.append(row)
    return rows, errors


def _attempt_failure_value(payload):
    state = payload.get("run_state") or payload.get("status")
    if not isinstance(state, str):
        return None, "attempt_status_absent"
    state = state.strip().upper()
    if state == "STOPPED":
        return None, "stopped_attempt_recorded_separately"
    if state in {"FAILED", "FAILED_OR_INCOMPLETE", "ABORTED"}:
        return 1, None
    if state in {"COMPLETED", "COMPLETED_PENDING_REVIEW", "SUCCEEDED"}:
        return 0, None
    return None, "attempt_status_not_terminal_or_unrecognized"


def _metric_rows(rows, manifest):
    prep_rows = [row for row in rows if row["kind"] == "practical_preparation" and row["schema_status"] == "SUPPORTED"]
    result_rows = [row for row in rows if row["kind"] in {"result_record", "execution_receipt"} and row["schema_status"] == "SUPPORTED"]
    metric_rows = [row for row in rows if row["kind"] == "metrics_observation" and row["schema_status"] == "SUPPORTED"]
    measured_cohort = prep_rows + metric_rows
    outputs = []
    prep_metric_names = (
        "preparation_command_elapsed_seconds", "preparation_wall_seconds", "active_operations",
        "approved_to_delivery_wall_seconds", "active_human_seconds", "mechanical_repairs_events",
        "first_pass", "observable_tool_calls",
    )
    for name in prep_metric_names:
        samples = []
        missing = []
        for row in measured_cohort:
            observation = row["metrics"].get(name, {"value": None, "unit": METRIC_SPECS[name]["unit"], "missing_reason": "field_not_present_in_explicit_receipt"})
            if observation["value"] is None:
                missing.append({"observation_id": row["observation_id"], "source_ref": row["source_ref"],
                                "reason": observation.get("missing_reason") or "not_measured"})
            else:
                samples.append({"observation_id": row["observation_id"], "source_ref": row["source_ref"],
                                "value_source_ref": observation["source_ref"], "value": observation["value"]})
        outputs.append(_metric_summary(name, len(measured_cohort), samples, missing,
                                       denominator_scope="preparation_receipt_and_explicit_metric_observations"))

    failure_samples, failure_missing = [], []
    stopped_samples, stopped_missing = [], []
    for row in result_rows:
        value, reason = _attempt_failure_value(row["payload"])
        payload = row["payload"]
        state_field = "run_state" if payload.get("run_state") else "status"
        state = payload.get(state_field)
        state_normalized = state.strip().upper() if isinstance(state, str) else None
        value_source_ref = row["source_ref"] + f"#/{state_field}"
        if value is None:
            failure_missing.append({"observation_id": row["observation_id"], "source_ref": row["source_ref"], "reason": reason})
        else:
            failure_samples.append({"observation_id": row["observation_id"], "source_ref": row["source_ref"],
                                    "value_source_ref": value_source_ref, "value": value})
        if state_normalized == "STOPPED":
            reason_field = next((field for field in ("stop_reason", "reason")
                                 if isinstance(payload.get(field), str) and payload[field].strip()), None)
            stopped_samples.append({
                "observation_id": row["observation_id"], "source_ref": row["source_ref"],
                "value_source_ref": value_source_ref, "value": 1,
                "stop_reason": payload.get(reason_field) if reason_field else None,
                "stop_reason_source_ref": (row["source_ref"] + f"#/{reason_field}") if reason_field else None,
                "stop_reason_status": "RECORDED" if reason_field else "NOT_RECORDED",
            })
        elif state_normalized in {"FAILED", "FAILED_OR_INCOMPLETE", "ABORTED",
                                  "COMPLETED", "COMPLETED_PENDING_REVIEW", "SUCCEEDED"}:
            stopped_samples.append({"observation_id": row["observation_id"], "source_ref": row["source_ref"],
                                    "value_source_ref": value_source_ref, "value": 0,
                                    "stop_reason": None, "stop_reason_source_ref": None,
                                    "stop_reason_status": "NOT_APPLICABLE"})
        else:
            stopped_missing.append({"observation_id": row["observation_id"],
                                    "source_ref": row["source_ref"],
                                    "reason": "attempt_status_not_terminal_or_unrecognized"})
    if not result_rows:
        failure_missing = [{"observation_id": row["observation_id"], "source_ref": row["source_ref"],
                            "reason": "no_explicit_result_or_attempt_receipt_in_observation_manifest"} for row in prep_rows]
    outputs.append(_metric_summary("failed_attempts", max(len(prep_rows), len(result_rows)), failure_samples, failure_missing,
                                   denominator_scope="preparation cohort; no attempt rate inferred"))
    outputs.append(_metric_summary("stopped_attempts", len(result_rows), stopped_samples, stopped_missing,
                                   denominator_scope="explicit result/attempt receipts; STOPPED is separate from failure"))

    for name in ("vasp_runtime_seconds", "resource_core_hours"):
        samples = []
        missing = []
        for row in measured_cohort:
            value = row["metrics"].get(name)
            if value and value.get("value") is not None:
                samples.append({"observation_id": row["observation_id"], "source_ref": row["source_ref"],
                                "value_source_ref": value["source_ref"], "value": value["value"]})
            else:
                missing.append({"observation_id": row["observation_id"], "source_ref": row["source_ref"],
                                "reason": value.get("missing_reason") if value else "no_execution_runtime_or_resource_observation_receipt_supplied"})
        outputs.append(_metric_summary(name, len(measured_cohort), samples, missing,
                                       denominator_scope="preparation cohort; planned MPI ranks excluded as cost"))
    return outputs


def _metric_summary(name, expected, samples, missing, denominator_scope):
    sample_count = max(expected, len(samples) + len(missing))
    measured = len(samples)
    missing_count = max(0, sample_count - measured)
    reasons = {}
    for item in missing:
        reason = item.get("reason", "not_measured")
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "name": name,
        "sample_count": sample_count,
        "measured_count": measured,
        "missing_count": missing_count,
        "missingness": {"fraction": (missing_count / sample_count if sample_count else None),
                        "by_reason": reasons},
        "unit": METRIC_SPECS[name]["unit"],
        "scope": METRIC_SPECS[name]["scope"],
        "improvement_direction": METRIC_SPECS[name]["improvement_direction"],
        "denominator_scope": denominator_scope,
        "source_refs": [item["value_source_ref"] for item in samples],
        "values": samples,
        "missing_observations": missing,
    }


def _comparisons(rows):
    eligible = {}
    excluded = []
    for row in rows:
        if row["schema_status"] != "SUPPORTED":
            continue
        identity = row.get("identity") or {}
        key = _comparison_key(identity)
        role = row.get("comparison_role")
        metric_names = [name for name, value in row.get("metrics", {}).items() if value.get("value") is not None]
        if key is None or role not in {"baseline", "candidate"}:
            missing = [name for name in COMPARISON_FIELDS if not (identity.get(name) or {}).get("value")]
            reason = "comparison_identity_incomplete" if key is None else "baseline_or_candidate_role_not_supplied"
            excluded.append({"observation_id": row["observation_id"], "source_ref": row["source_ref"],
                             "reason": reason, "missing_identity_fields": missing,
                             "measured_metrics": metric_names})
            continue
        for name, value in row.get("metrics", {}).items():
            if value.get("value") is None:
                continue
            group_key = (key, name, value["unit"])
            eligible.setdefault(group_key, {"identity": json.loads(key), "metric": name,
                                             "unit": value["unit"], "baseline": [], "candidate": []})
            eligible[group_key][role].append({"observation_id": row["observation_id"],
                                               "source_ref": value["source_ref"], "value": value["value"]})
    groups = []
    for group in eligible.values():
        baseline, candidate = group["baseline"], group["candidate"]
        rate = None
        status = "EVIDENCE_INSUFFICIENT"
        direction = METRIC_SPECS[group["metric"]]["improvement_direction"]
        if baseline and candidate:
            base_values = [float(item["value"]) for item in baseline]
            cand_values = [float(item["value"]) for item in candidate]
            base_mean = sum(base_values) / len(base_values)
            cand_mean = sum(cand_values) / len(cand_values)
            if direction not in {"lower_is_better", "higher_is_better"}:
                status = "IMPROVEMENT_DIRECTION_UNSPECIFIED"
            elif base_mean > 0:
                rate = (1.0 - cand_mean / base_mean if direction == "lower_is_better"
                        else cand_mean / base_mean - 1.0)
                status = "EXPLORATORY_COMPARISON_ONLY"
            else:
                status = "BASELINE_ZERO_RATE_UNDEFINED"
        groups.append({**group, "improvement_direction": direction,
                       "improvement_rate": rate, "status": status})
    comparable = [item for item in groups if item["baseline"] and item["candidate"]]
    directional = [item for item in comparable if item["improvement_rate"] is not None]
    return {
        "status": "COMPARABLE_OBSERVATIONS_AVAILABLE" if comparable else "EVIDENCE_INSUFFICIENT",
        "required_identity_fields": list(COMPARISON_FIELDS),
        "eligible_group_count": len(groups),
        "comparable_group_count": len(comparable),
        "groups": groups,
        "excluded_observations": excluded,
        "overall_improvement_rate": None,
        "directional_rate_group_count": len(directional),
        "exploratory_target_fraction": 0.30,
        "target_semantics": "Exploration target only; not a correctness gate or promised improvement.",
    }


def contract_coverage() -> list[dict]:
    return [dict(row) for row in CONTRACT_COVERAGE]


def build_report(observations_path, workspace_root=None) -> dict:
    observations_path = Path(observations_path).resolve()
    manifest = _read_manifest(observations_path)
    root = Path(workspace_root or PROJECT_ROOT).resolve()
    rows, source_errors = _load_rows(observations_path, manifest, root)
    supported = [row for row in rows if row["schema_status"] == "SUPPORTED"]
    prep_count = sum(row["kind"] == "practical_preparation" for row in supported)
    schema_counts = {}
    for row in supported:
        schema_counts[row["schema"]] = schema_counts.get(row["schema"], 0) + 1
    metrics = _metric_rows(rows, manifest)
    comparisons = _comparisons(rows)
    inventory = {
        "manifest_ref": str(observations_path),
        "declared_inventory_count": manifest.get("declared_inventory_count"),
        "manifest_entry_count": len(rows),
        "resolved_supported_receipt_count": len(supported),
        "resolved_preparation_receipt_count": prep_count,
        "source_errors": source_errors,
        "schema_counts": schema_counts,
        "result_record_refs": sum(row["kind"] == "result_record" for row in rows),
        "metrics_receipt_refs": sum(row["kind"] == "metrics_observation" for row in rows),
        "execution_receipt_refs": sum(row["kind"] == "execution_receipt" for row in rows),
        "observation_sources": [{"observation_id": row["observation_id"], "kind": row["kind"],
                                 "source_ref": row["source_ref"], "schema": row.get("schema"),
                                 "schema_status": row["schema_status"], "source_status": row["source_status"],
                                 "identity": row["identity"]} for row in rows],
    }
    return {
        "schema": "vasp-workflow-metrics-report/v1",
        "status": "EVIDENCE_INSUFFICIENT" if comparisons["status"] == "EVIDENCE_INSUFFICIENT" else "OBSERVATIONS_REPORTED_NO_RELEASE",
        "inventory": inventory,
        "metrics": metrics,
        "comparability": comparisons,
        "performance_claim": {"status": "EVIDENCE_INSUFFICIENT" if comparisons["status"] == "EVIDENCE_INSUFFICIENT" else "NOT_CLAIMED",
                              "improvement_rate": None,
                              "reason": "No paired observations with identical template, approved input identity, environment, generation/analysis scope, and metric definition." if comparisons["status"] == "EVIDENCE_INSUFFICIENT" else "No aggregate improvement claim is emitted."},
        "interpretation_limits": [
            "elapsed_seconds is preparation-command/workflow time only; it is not end-to-end or human time.",
            "preparation_wall_seconds is not approval-to-delivery or active-human time.",
            "manual_repairs=0 without RECORDED_EVENTS is unmeasured and reported null.",
            "A preparation receipt's passed flag is not evidence of first-pass success across attempts.",
            "A missing result/attempt receipt is not evidence of zero failed attempts.",
            "STOPPED is reported separately from failed_attempts; a missing stop reason is not treated as failure.",
            "Frozen/mock VASPKIT receipts are excluded from actual tool-call efficiency counts.",
            "Declared MPI ranks are planned resources, not actual runtime/resource cost.",
            "Scientific energies are not read or compared by this report.",
        ],
    }


def _paths_overlap(path: Path, protected: Path) -> bool:
    return path == protected or path.is_relative_to(protected) or protected.is_relative_to(path)


def _protected_source_directories(report: dict, workspace_root: Path) -> list[Path]:
    inventory = report.get("inventory", {})
    protected = []
    manifest_ref = inventory.get("manifest_ref")
    if isinstance(manifest_ref, str) and manifest_ref.strip():
        manifest_path = Path(manifest_ref)
        if not manifest_path.is_absolute():
            manifest_path = workspace_root / manifest_path
        protected.append(manifest_path.resolve().parent)
    for source in inventory.get("observation_sources", []):
        source_ref = source.get("source_ref") if isinstance(source, dict) else None
        if not isinstance(source_ref, str) or not source_ref.strip():
            continue
        try:
            protected.append(_workspace_path(source_ref, workspace_root).parent)
        except ValueError:
            continue
    return list(dict.fromkeys(path.resolve() for path in protected))


def _write_new_output(report: dict, output_dir, workspace_root=None):
    root = Path(workspace_root or PROJECT_ROOT).resolve()
    output_dir = Path(output_dir).resolve()
    if not output_dir.is_relative_to(root):
        raise ValueError("output directory must resolve inside the workspace")
    relative_parts = {part.casefold() for part in output_dir.relative_to(root).parts}
    if FORBIDDEN_PATH_PART.casefold() in relative_parts:
        raise ValueError("the excluded previous-results trial directory is not an allowed output")
    protected_dirs = [root / "private_runs", *_protected_source_directories(report, root)]
    if any(_paths_overlap(output_dir, protected.resolve()) for protected in protected_dirs):
        raise ValueError("output directory must not overlap calculation or explicit source directories")
    if output_dir.exists():
        raise ValueError(f"output directory must be new: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "metrics_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    audit = contract_coverage()
    (output_dir / "execution_contract_coverage.json").write_text(json.dumps({
        "schema": "vasp-execution-contract-coverage/v1", "runtime_modified": False,
        "live_runtime_observed": False, "rows": audit,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    with (output_dir / "execution_contract_coverage.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["area", "status", "fields", "code_refs", "test_refs", "not_observed"])
        writer.writeheader()
        for row in audit:
            writer.writerow({**row, "code_refs": "; ".join(row["code_refs"]),
                             "test_refs": "; ".join(row["test_refs"])})
    return output_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", required=True, help="Explicit vasp-workflow-observations/v1 JSON manifest")
    parser.add_argument("--output-dir", required=True, help="New directory for the metrics report and contract audit")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.observations)
        output = _write_new_output(report, args.output_dir)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(2, f"metrics-report stopped: {type(error).__name__}: {error}\n")
    print(json.dumps({"status": report["status"], "output_dir": str(output),
                      "preparation_receipts": report["inventory"]["resolved_preparation_receipt_count"],
                      "comparison_status": report["comparability"]["status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

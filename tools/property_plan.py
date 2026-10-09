"""Build a deterministic, read-only property dependency plan.

The planner consumes one explicit JSON request. It does not scan result
directories, infer a fresh restart, emit VASP inputs, open an execution gate,
or authorize a calculation.
"""
from __future__ import annotations

import argparse
import os
import csv
import json
from pathlib import Path, PurePosixPath, PureWindowsPath


TOOLS = Path(__file__).resolve().parent
PROJECT_ROOT = Path(os.environ.get("DFT_WORKSPACE_ROOT", str(TOOLS.parent))).resolve()
REQUEST_SCHEMA = "vasp-property-request/v1"
PLAN_SCHEMA = "vasp-property-plan/v1"
TEMPLATES = {"static", "atomref", "relax_fresh", "relax_warm", "UNVALIDATED"}
CONTRACT_NAMES = ("geometry", "environment", "paw", "restart")
SUPPORTED_PROPERTY_TEMPLATES = {
    "STATIC_ENERGY": {"static"},
    "ATOM_REFERENCE": {"atomref"},
    "RELAXED_GEOMETRY": {"relax_fresh", "relax_warm"},
    "FIXED_GEOMETRY_REFERENCE": {"static", "atomref"},
}
FULL_RESULT_SCOPES = {"COMPLETE_RESULT", "COMPLETE_GEOMETRY", "CONVERGED_GEOMETRY", "RELAXED_GEOMETRY"}
GEOMETRY_SCOPES = {"COMPLETE_GEOMETRY", "CONVERGED_GEOMETRY", "RELAXED_GEOMETRY"}
EXCLUDED_PATH_PART = "excluded_reference_trial"


def _safe_token(value, label):
    if not isinstance(value, str) or not value.strip() or value in {".", "..", "latest"}:
        raise ValueError(f"{label} must be an explicit nonempty identifier")
    if len(value) > 160 or any(ord(char) < 32 for char in value):
        raise ValueError(f"{label} is invalid")
    return value


def _safe_ref(value, label, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be an explicit workspace-relative JSON reference")
    path_part, marker, pointer = value.partition("#")
    posix = PurePosixPath(path_part.replace("\\", "/"))
    windows = PureWindowsPath(path_part)
    if (posix.is_absolute() or windows.is_absolute() or windows.drive
            or any(part in {".", ".."} for part in posix.parts)
            or posix.suffix.casefold() != ".json"
            or EXCLUDED_PATH_PART.casefold() in {part.casefold() for part in posix.parts}):
        raise ValueError(f"{label} must be a safe workspace-relative JSON reference")
    if marker and (not pointer.startswith("/") or "\x00" in pointer):
        raise ValueError(f"{label} has an invalid JSON pointer")
    return value


def _reference_file_state(value):
    """Check only whether one explicitly named JSON file exists; never open it."""
    if not value:
        return "MISSING_REFERENCE"
    path_part = value.partition("#")[0]
    relative = PurePosixPath(path_part.replace("\\", "/"))
    root = PROJECT_ROOT.resolve()
    path = root.joinpath(*relative.parts).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return "FILE_MISSING"
    return "EXISTS_CONTENT_NOT_READ"


def _string_list(value, label):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{label} must be a list of nonempty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{label} must not contain duplicates")
    return list(value)


def _identity_map(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    result = {}
    for name in CONTRACT_NAMES:
        item = value.get(name)
        if isinstance(item, str) and item.strip():
            result[name] = item
        else:
            result[name] = None
    return result


def _validate_request(request):
    if not isinstance(request, dict) or request.get("schema") != REQUEST_SCHEMA:
        raise ValueError(f"request must use {REQUEST_SCHEMA}")
    request_id = _safe_token(request.get("request_id"), "request_id")
    purpose = _safe_token(request.get("purpose"), "purpose")
    stop_condition = _safe_token(request.get("stop_condition"), "stop_condition")
    budget = request.get("case_budget")
    if type(budget) is not int or budget < 1:
        raise ValueError("case_budget must be a positive finite integer")
    stages = request.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ValueError("stages must be a nonempty explicit list")

    seen_ids = set()
    seen_nodes = set()
    normalized = []
    for index, stage in enumerate(stages):
        label = f"stages[{index}]"
        if not isinstance(stage, dict):
            raise ValueError(f"{label} must be an object")
        stage_id = _safe_token(stage.get("stage_id"), f"{label}.stage_id")
        case_id = _safe_token(stage.get("case_id"), f"{label}.case_id")
        property_name = _safe_token(stage.get("property"), f"{label}.property").upper()
        template = stage.get("template")
        if template not in TEMPLATES:
            raise ValueError(f"{label}.template must be one of {sorted(TEMPLATES)}")
        bundle_ref = _safe_ref(stage.get("bundle_ref"), f"{label}.bundle_ref", optional=True)
        depends_on = _string_list(stage.get("depends_on"), f"{label}.depends_on")
        required_artifacts = _string_list(stage.get("required_artifacts"), f"{label}.required_artifacts")
        produce = _string_list(stage.get("produce"), f"{label}.produce")
        scope = _safe_token(stage.get("required_acceptance_scope"), f"{label}.required_acceptance_scope")

        refs_input = stage.get("contract_refs")
        ids = _identity_map(stage.get("contract_identities"), f"{label}.contract_identities")
        if not isinstance(refs_input, dict):
            raise ValueError(f"{label}.contract_refs must be an object")
        contract_refs = {}
        for contract in CONTRACT_NAMES:
            ref = refs_input.get(contract)
            contract_refs[contract] = _safe_ref(ref, f"{label}.contract_refs.{contract}", optional=True)
            if contract_refs[contract] and bundle_ref and not contract_refs[contract].startswith(bundle_ref + "#/"):
                raise ValueError(f"{label}.{contract} reference must point into its explicit bundle_ref")

        parents_input = stage.get("parents")
        if not isinstance(parents_input, list):
            raise ValueError(f"{label}.parents must be an explicit list")
        parents = []
        parent_ids = set()
        for parent_index, parent in enumerate(parents_input):
            parent_label = f"{label}.parents[{parent_index}]"
            if not isinstance(parent, dict):
                raise ValueError(f"{parent_label} must be an object")
            parent_id = parent.get("parent_id")
            if parent_id is not None:
                parent_id = _safe_token(parent_id, f"{parent_label}.parent_id")
                if parent_id in parent_ids:
                    raise ValueError(f"{label} has duplicate parent_id {parent_id}")
                parent_ids.add(parent_id)
            evidence_ref = _safe_ref(parent.get("evidence_ref"), f"{parent_label}.evidence_ref", optional=True)
            execution_id = parent.get("execution_id")
            case = parent.get("case_id")
            attempt = parent.get("attempt")
            execution_id = execution_id if isinstance(execution_id, str) and execution_id.strip() else None
            case = case if isinstance(case, str) and case.strip() else None
            attempt = attempt if type(attempt) is int and attempt > 0 else None
            parent_contracts = _identity_map(parent.get("contract_identities", {}), f"{parent_label}.contract_identities")
            parents.append({
                "parent_id": parent_id,
                "evidence_ref": evidence_ref,
                "execution_id": execution_id,
                "case_id": case,
                "attempt": attempt,
                "run_state": parent.get("run_state") if isinstance(parent.get("run_state"), str) else None,
                "result_state": parent.get("result_state") if isinstance(parent.get("result_state"), str) else None,
                "geometry_relaxation_status": parent.get("geometry_relaxation_status") if isinstance(parent.get("geometry_relaxation_status"), str) else None,
                "contract_identities": parent_contracts,
                "evidence_state": _reference_file_state(evidence_ref),
            })

        if stage_id in seen_ids:
            raise ValueError(f"duplicate stage_id: {stage_id}")
        signature = json.dumps({key: value for key, value in stage.items() if key != "stage_id"},
                               sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        if signature in seen_nodes:
            raise ValueError(f"duplicate property-plan node: {stage_id}")
        seen_ids.add(stage_id)
        seen_nodes.add(signature)
        normalized.append({
            "stage_id": stage_id,
            "case_id": case_id,
            "property": property_name,
            "template": template,
            "bundle_ref": bundle_ref,
            "depends_on": depends_on,
            "parents": parents,
            "required_artifacts": required_artifacts,
            "contract_refs": contract_refs,
            "contract_identities": ids,
            "required_acceptance_scope": scope.upper(),
            "produce": produce,
        })

    case_count = len({stage["case_id"] for stage in normalized})
    if case_count > budget:
        raise ValueError(f"explicit case count {case_count} exceeds case_budget {budget}")
    id_set = {stage["stage_id"] for stage in normalized}
    for stage in normalized:
        unknown = [dep for dep in stage["depends_on"] if dep not in id_set]
        if unknown:
            raise ValueError(f"{stage['stage_id']} has unknown dependencies: {', '.join(unknown)}")
        if stage["stage_id"] in stage["depends_on"]:
            raise ValueError(f"{stage['stage_id']} cannot depend on itself")
    return request_id, purpose, stop_condition, budget, normalized


def _topological_order(stages):
    by_id = {stage["stage_id"]: stage for stage in stages}
    indegree = {stage["stage_id"]: len(stage["depends_on"]) for stage in stages}
    dependents = {stage["stage_id"]: [] for stage in stages}
    order_index = {stage["stage_id"]: index for index, stage in enumerate(stages)}
    for stage in stages:
        for dependency in stage["depends_on"]:
            dependents[dependency].append(stage["stage_id"])
    ready = [stage["stage_id"] for stage in stages if indegree[stage["stage_id"]] == 0]
    ready.sort(key=order_index.get)
    result = []
    while ready:
        stage_id = ready.pop(0)
        result.append(by_id[stage_id])
        for dependent in sorted(dependents[stage_id], key=order_index.get):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort(key=order_index.get)
    if len(result) != len(stages):
        raise ValueError("property dependency graph contains a cycle")
    return result


def _stage_blockers(stage):
    blockers = []
    supported_templates = SUPPORTED_PROPERTY_TEMPLATES.get(stage["property"])
    if supported_templates is None:
        support_state = "DATA_GATED"
        blockers.append("PROPERTY_NOT_VALIDATED_FOR_EXECUTION")
    elif stage["template"] == "UNVALIDATED":
        support_state = "DATA_GATED"
        blockers.append("TEMPLATE_UNVALIDATED")
    elif stage["template"] not in supported_templates:
        support_state = "BLOCKED"
        blockers.append("PROPERTY_TEMPLATE_MISMATCH")
    else:
        support_state = "SUPPORTED_FOR_PLANNING_ONLY"

    if not stage["bundle_ref"]:
        blockers.append("BUNDLE_REF_MISSING")
    elif _reference_file_state(stage["bundle_ref"]) != "EXISTS_CONTENT_NOT_READ":
        blockers.append("BUNDLE_FILE_MISSING")
    for contract in CONTRACT_NAMES:
        if not stage["contract_refs"][contract]:
            blockers.append(f"{contract.upper()}_CONTRACT_REF_MISSING")
        if not stage["contract_identities"][contract]:
            blockers.append(f"{contract.upper()}_CONTRACT_IDENTITY_MISSING")

    if stage["depends_on"]:
        blockers.append("PLANNED_DEPENDENCY_NOT_MATERIALIZED")
    if not stage["parents"]:
        blockers.append("PARENT_EVIDENCE_MISSING")
    for parent in stage["parents"]:
        parent_label = parent["parent_id"] or "external"
        if not parent["evidence_ref"]:
            blockers.append(f"PARENT_EVIDENCE_REF_MISSING:{parent_label}")
        elif parent["evidence_state"] != "EXISTS_CONTENT_NOT_READ":
            blockers.append(f"PARENT_EVIDENCE_FILE_MISSING:{parent_label}")
        if not parent["execution_id"] or not parent["case_id"] or parent["attempt"] is None:
            blockers.append(f"PARENT_EXECUTION_CASE_ATTEMPT_INCOMPLETE:{parent_label}")
        for contract in CONTRACT_NAMES:
            child_identity = stage["contract_identities"][contract]
            parent_identity = parent["contract_identities"][contract]
            if not parent_identity:
                blockers.append(f"PARENT_{contract.upper()}_CONTRACT_IDENTITY_MISSING:{parent_label}")
            elif child_identity and child_identity != parent_identity:
                blockers.append(f"{contract.upper()}_CONTRACT_CONFLICT:{parent_label}")
        scope = stage["required_acceptance_scope"]
        if scope in FULL_RESULT_SCOPES:
            if parent["run_state"] != "COMPLETED":
                blockers.append(f"PARENT_RUN_NOT_COMPLETED:{parent_label}")
            if parent["result_state"] != "COMPLETE_XML":
                blockers.append(f"PARENT_RESULT_NOT_COMPLETE:{parent_label}")
        if scope in GEOMETRY_SCOPES and parent["geometry_relaxation_status"] != "CONVERGED":
            blockers.append(f"PARENT_GEOMETRY_NOT_CONVERGED:{parent_label}")

    # A planning artifact never opens an execution gate. Parent references are
    # declared metadata only; this command does not resolve result records.
    return support_state, sorted(set(blockers))


def build_plan(request, *, evidence_manifest=None, evidence_receipt=None, source_root=None):
    verification = None
    if (evidence_manifest is None) != (evidence_receipt is None):
        raise ValueError('Evidence manifest and receipt must be supplied together')
    if evidence_manifest is not None:
        from evidence_verifier import consume
        verification = consume(evidence_manifest, evidence_receipt, source_root or PROJECT_ROOT, request, 'property-plan-request')
    request_id, purpose, stop_condition, budget, stages = _validate_request(request)
    ordered = _topological_order(stages)
    nodes = []
    edges = []
    all_blockers = []
    for stage in ordered:
        support_state, blockers = _stage_blockers(stage)
        node = {
            **stage,
            "geometry_contract_ref": stage["contract_refs"]["geometry"],
            "environment_contract_ref": stage["contract_refs"]["environment"],
            "paw_contract_ref": stage["contract_refs"]["paw"],
            "restart_contract_ref": stage["contract_refs"]["restart"],
            "support_state": support_state,
            "blockers": blockers,
            "plan_readiness": "BLOCKED" if blockers else "REVIEWABLE",
            "execution_ready": False,
        }
        nodes.append(node)
        all_blockers.extend({"stage_id": stage["stage_id"], "blocker": blocker} for blocker in blockers)
        for dependency in stage["depends_on"]:
            edges.append({"source_node": dependency, "target_node": stage["stage_id"],
                          "dependency_kind": "planned_stage", "source_ref": None,
                          "execution_id": None, "case_id": None, "attempt": None,
                          "status": "WAITING_FOR_EXPLICIT_RESULT"})
        for parent in stage["parents"]:
            edges.append({"source_node": parent["parent_id"] or "external_parent",
                          "target_node": stage["stage_id"], "dependency_kind": "parent_evidence",
                          "source_ref": parent["evidence_ref"], "execution_id": parent["execution_id"],
                          "case_id": parent["case_id"], "attempt": parent["attempt"],
                          "status": parent["evidence_state"]})
    result = {
        "schema": PLAN_SCHEMA,
        "request_id": request_id,
        "purpose": purpose,
        "stop_condition": stop_condition,
        "case_budget": budget,
        "explicit_case_count": len({stage["case_id"] for stage in stages}),
        "stage_count": len(nodes),
        "status": "BLOCKED" if all_blockers else "PLAN_REVIEW_REQUIRED",
        "execution_authorized": False,
        "gate_opened": False,
        "generated_vasp_inputs": False,
        "generated_scripts": False,
        "nodes": nodes,
        "dependency_edges": edges,
        "blockers": all_blockers,
        "interpretation_limits": [
            "Bundle and parent evidence references are declarations; only explicitly named path existence is checked, not JSON contents.",
            "Only exact request nodes are planned; no matrix expansion, default case, or latest-UUID selection occurs.",
            "execution_ready remains false; a plan is not scientific acceptance or execution authorization.",
            "UNVALIDATED templates and unsupported properties stay data-gated; missing restart evidence never becomes fresh.",
        ],
    }
    if verification is not None:
        result['objective_evidence_verification'] = verification
    return result


def _resolve_request(path):
    request_path = Path(path).resolve()
    root = PROJECT_ROOT.resolve()
    if not request_path.is_relative_to(root) or request_path.suffix.casefold() != ".json":
        raise ValueError("request must be a workspace-local JSON file")
    if EXCLUDED_PATH_PART.casefold() in {part.casefold() for part in request_path.parts}:
        raise ValueError("excluded trial-directory requests are not allowed")
    return request_path


def _resolve_output(path):
    output = Path(path).resolve()
    root = PROJECT_ROOT.resolve()
    if not output.is_relative_to(root):
        raise ValueError("output-dir must be inside the workspace")
    relative = output.relative_to(root)
    parts = {part.casefold() for part in relative.parts}
    if "private_runs" in parts or EXCLUDED_PATH_PART.casefold() in parts:
        raise ValueError("property plans cannot be written into calculation or excluded trial directories")
    if output.exists():
        raise ValueError(f"output directory must be new: {output}")
    return output


def _write_outputs(plan, output_dir):
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "property_plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    (output_dir / "blockers.json").write_text(json.dumps({
        "schema": "vasp-property-plan-blockers/v1", "status": plan["status"],
        "blockers": plan["blockers"],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    with (output_dir / "dependency_table.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fields = ["source_node", "target_node", "dependency_kind", "source_ref",
                  "execution_id", "case_id", "attempt", "status"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(plan["dependency_edges"])
    (output_dir / "README.md").write_text(
        "# VASP property dependency plan\n\n"
        "Planning-only output. No VASP input, execution script, gate, or new case was created.\n\n"
        f"Status: **{plan['status']}**; stages: {plan['stage_count']}; blockers: {len(plan['blockers'])}.\n",
        encoding="utf-8",
    )
    return output_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, help=f"Explicit {REQUEST_SCHEMA} JSON")
    parser.add_argument("--output-dir", required=True, help="New workspace-local plan output directory")
    parser.add_argument('--evidence-manifest', help='Explicit workspace-local verifier manifest (opt-in)')
    parser.add_argument('--evidence-receipt', help='Matching fresh verifier receipt (opt-in)')
    args = parser.parse_args(argv)
    try:
        request_path = _resolve_request(args.request)
        request = json.loads(request_path.read_text(encoding="utf-8-sig"))
        output_dir = _resolve_output(args.output_dir)
        plan = build_plan(request, evidence_manifest=args.evidence_manifest, evidence_receipt=args.evidence_receipt)
        plan["request_ref"] = request_path.relative_to(PROJECT_ROOT).as_posix()
        output = _write_outputs(plan, output_dir)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(2, f"plan-properties stopped: {type(error).__name__}: {error}\n")
    print(json.dumps({"status": plan["status"], "output_dir": str(output),
                      "stage_count": plan["stage_count"], "blocker_count": len(plan["blockers"]),
                      "execution_authorized": plan["execution_authorized"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

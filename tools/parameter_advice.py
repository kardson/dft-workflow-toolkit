"""Read-only review of explicit candidate parameter differences.

This module compares a proposal with one explicitly named approved bundle.
It writes only an advice report; it never edits or prepares VASP inputs.
"""
from __future__ import annotations

import argparse
import os
import csv
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath

from approved_bundle import validate_bundle


TOOLS = Path(__file__).resolve().parent
PROJECT_ROOT = Path(os.environ.get("DFT_WORKSPACE_ROOT", str(TOOLS.parent))).resolve()
PROPOSAL_SCHEMA = "vasp-parameter-proposal/v1"
ADVICE_SCHEMA = "vasp-parameter-advice/v1"
EXCLUDED_PATH_PART = "excluded_reference_trial"
KNOWN_KEYS = {
    "template",
    "inputs.incar.ENCUT_eV", "inputs.incar.EDIFF", "inputs.incar.EDIFFG",
    "inputs.incar.ALGO", "inputs.incar.NELM", "inputs.incar.ISMEAR", "inputs.incar.SIGMA",
    "inputs.incar.ISPIN", "inputs.incar.MAGMOM", "inputs.incar.ISYM", "inputs.incar.IBRION",
    "inputs.incar.POTIM", "inputs.incar.NSW", "inputs.incar.ISIF", "inputs.incar.LWAVE",
    "inputs.incar.LCHARG", "inputs.incar.LASPH", "inputs.incar.LREAL", "inputs.incar.ADDGRID",
    "inputs.kpoints.mesh", "inputs.kpoints.generation", "inputs.environment_id",
    "inputs.restart.fresh", "inputs.restart.source_execution_id", "inputs.restart.source_case_id",
    "inputs.restart.source_attempt", "inputs.paw_identity.paw_family",
    "inputs.paw_identity.environment_id",
}
REFERENCE_DIFFERENCES = {"atom_box", "kpoints", "spin"}
ENERGY_BASES = {"E0", "F", "TOTEN", "MP_CORRECTED_E0"}
CORRECTION_STATES = {"APPLIED", "NOT_APPLIED", "UNKNOWN"}


def _safe_json_path(value, label, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be an explicit workspace-local JSON path")
    posix = PurePosixPath(value.replace("\\", "/"))
    windows = PureWindowsPath(value)
    if (posix.is_absolute() or windows.is_absolute() or windows.drive
            or any(part in {".", ".."} for part in posix.parts)
            or posix.suffix.casefold() != ".json"
            or EXCLUDED_PATH_PART.casefold() in {part.casefold() for part in posix.parts}):
        raise ValueError(f"{label} must be a safe workspace-relative JSON path")
    return value


def _source_state(source_ref, root):
    if source_ref is None:
        return "SOURCE_REF_MISSING"
    if not isinstance(source_ref, str) or not source_ref.strip():
        return "SOURCE_REF_MISSING"
    if source_ref.startswith(("https://", "http://", "doi:")):
        return "SOURCE_NOT_VERIFIED_OFFLINE"
    path_part = source_ref.partition("#")[0]
    safe = _safe_json_path(path_part, "source_ref")
    root = Path(root).resolve()
    path = root.joinpath(*PurePosixPath(safe.replace("\\", "/")).parts).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return "SOURCE_FILE_MISSING"
    # Provenance file contents are intentionally not opened by this tool.
    return "SOURCE_PATH_EXISTS_CONTENT_NOT_REVIEWED"


def _typed_equal(left, right):
    return type(left) is type(right) and left == right


def _lookup(bundle, key):
    if key not in KNOWN_KEYS:
        raise ValueError(f"unknown proposal key: {key}")
    current = bundle
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _json_value(value):
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


def _review_comparison(contract, root):
    if contract is None:
        return None, []
    if not isinstance(contract, dict):
        raise ValueError("comparison_contract must be an object when supplied")
    output = dict(contract)
    blockers = []
    if contract.get("requested") is not True:
        output["comparison_state"] = "NOT_REQUESTED"
        return output, blockers

    basis = contract.get("energy_basis")
    if not isinstance(basis, str) or basis not in ENERGY_BASES:
        blockers.append("ENERGY_BASIS_MISSING_OR_UNSUPPORTED")
        basis = None
    source_bases = contract.get("source_energy_bases")
    if (not isinstance(source_bases, list) or not source_bases
            or any(not isinstance(item, str) or item not in ENERGY_BASES for item in source_bases)):
        blockers.append("SOURCE_ENERGY_BASIS_MISSING_OR_UNSUPPORTED")
        source_bases = []
    if len(set(source_bases)) > 1 or (basis in ENERGY_BASES and any(item != basis for item in source_bases)):
        blockers.append("ENERGY_BASIS_MISMATCH_E0_F_TOTEN_SEPARATED")

    correction_states = contract.get("mp_correction_states")
    if (not isinstance(correction_states, list) or not correction_states
            or any(not isinstance(item, str) or item not in CORRECTION_STATES for item in correction_states)):
        blockers.append("MP_CORRECTION_STATE_MISSING_OR_INVALID")
        correction_states = []
    if "UNKNOWN" in correction_states:
        blockers.append("MP_CORRECTION_STATUS_UNKNOWN")
    if len(set(correction_states)) > 1:
        blockers.append("MP_CORRECTION_MISMATCH")
    if basis == "MP_CORRECTED_E0" and correction_states and any(item != "APPLIED" for item in correction_states):
        blockers.append("MP_CORRECTED_ENTRY_NOT_UNIFORMLY_APPLIED")
    if basis in {"E0", "F", "TOTEN"} and "APPLIED" in correction_states:
        blockers.append("RAW_ENERGY_BASIS_MIXED_WITH_MP_CORRECTION")

    approved = contract.get("approved_differences")
    observed = contract.get("observed_differences")
    if not isinstance(approved, list) or any(not isinstance(item, str) for item in approved):
        blockers.append("APPROVED_DIFFERENCES_NOT_EXPLICIT")
        approved = []
    if not isinstance(observed, list) or any(not isinstance(item, str) for item in observed):
        blockers.append("OBSERVED_DIFFERENCES_NOT_EXPLICIT")
        observed = []
    if len(set(approved)) != len(approved) or len(set(observed)) != len(observed):
        blockers.append("DUPLICATE_DIFFERENCE_ENTRY")
    if contract.get("interpretation") == "FIXED_GEOMETRY_INTERACTION":
        if not REFERENCE_DIFFERENCES.issubset(set(approved)):
            blockers.append("FIXED_GEOMETRY_APPROVED_DIFFERENCES_INCOMPLETE")
        for difference in approved:
            if difference not in REFERENCE_DIFFERENCES:
                blockers.append(f"UNSUPPORTED_APPROVED_REFERENCE_DIFFERENCE:{difference}")
        for difference in observed:
            if difference not in approved:
                blockers.append(f"UNAPPROVED_REFERENCE_DIFFERENCE:{difference}")

    threshold = contract.get("threshold")
    threshold_ref = contract.get("threshold_source_ref")
    if type(threshold) not in (int, float) or not math.isfinite(float(threshold)):
        blockers.append("SCIENTIFIC_THRESHOLD_MISSING")
        output["threshold_state"] = "NOT_PROVIDED"
    else:
        source_state = _source_state(threshold_ref, root)
        output["threshold_state"] = "DECLARED_NOT_EVALUATED" if source_state == "SOURCE_PATH_EXISTS_CONTENT_NOT_REVIEWED" else "INSUFFICIENT_ADVICE_EVIDENCE"
        if source_state != "SOURCE_PATH_EXISTS_CONTENT_NOT_REVIEWED":
            blockers.append(f"THRESHOLD_SOURCE_{source_state}")
    output["comparison_state"] = "NOT_EVALUATED"
    output["scientific_conclusion"] = "NOT_EVALUATED"
    return output, blockers


def review(bundle, proposal, *, bundle_ref, proposal_ref, source_root=None):
    """Create a JSON-only review; values are not written back to the bundle."""
    if not isinstance(bundle, dict) or not isinstance(proposal, dict):
        raise ValueError("approved bundle and proposal roots must be objects")
    template = validate_bundle(bundle)
    if proposal.get("schema") != PROPOSAL_SCHEMA:
        raise ValueError(f"proposal must use {PROPOSAL_SCHEMA}")
    proposal_id = proposal.get("proposal_id")
    if not isinstance(proposal_id, str) or not proposal_id.strip():
        raise ValueError("proposal_id must be explicit")
    bundle_ref = _safe_json_path(bundle_ref, "approved_bundle")
    proposal_ref = _safe_json_path(proposal_ref, "proposal")
    changes = proposal.get("changes")
    if not isinstance(changes, list) or not changes:
        raise ValueError("changes must be a nonempty explicit list")

    root = Path(source_root or PROJECT_ROOT).resolve()
    rows = []
    all_blockers = []
    seen_keys = set()
    allowed_fields = {
        "key", "old_value", "proposed_value", "source_ref", "reason", "VASP_version_scope",
        "system_scope", "changes_method", "changes_restart", "changes_environment",
    }
    for index, item in enumerate(changes):
        if not isinstance(item, dict):
            raise ValueError(f"changes[{index}] must be an object")
        unknown_fields = set(item) - allowed_fields
        if unknown_fields:
            raise ValueError(f"changes[{index}] has unsupported fields: {', '.join(sorted(unknown_fields))}")
        key = item.get("key")
        if not isinstance(key, str) or key not in KNOWN_KEYS:
            raise ValueError(f"unknown proposal key: {key}")
        if key in seen_keys:
            raise ValueError(f"duplicate proposal key: {key}")
        seen_keys.add(key)
        if "proposed_value" not in item or item["proposed_value"] is None or not _json_value(item["proposed_value"]):
            raise ValueError(f"changes[{index}].proposed_value must be an explicit finite JSON value")
        for flag in ("changes_method", "changes_restart", "changes_environment"):
            if type(item.get(flag)) is not bool:
                raise ValueError(f"changes[{index}].{flag} must be an explicit boolean")

        old_present, old_value = _lookup(bundle, key)
        old_state = "PRESENT" if old_present else "OMITTED"
        old_display = old_value if old_present else "OMITTED"
        blockers = []
        if "old_value" in item:
            supplied_old = item["old_value"]
            old_matches = (old_present and _typed_equal(supplied_old, old_value)) or (not old_present and supplied_old == "OMITTED")
            if not old_matches:
                blockers.append("APPROVED_OLD_VALUE_MISMATCH")

        source_ref = item.get("source_ref")
        evidence_state = _source_state(source_ref, root)
        if evidence_state != "SOURCE_PATH_EXISTS_CONTENT_NOT_REVIEWED":
            blockers.append(evidence_state)
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            blockers.append("REASON_MISSING")
            reason = None
        version_scope = item.get("VASP_version_scope")
        if not isinstance(version_scope, str) or not version_scope.strip():
            blockers.append("VASP_VERSION_SCOPE_MISSING")
            version_scope = None
        system_scope = item.get("system_scope")
        if not isinstance(system_scope, str) or not system_scope.strip():
            blockers.append("SYSTEM_SCOPE_MISSING")
            system_scope = None

        same_value = old_present and _typed_equal(old_value, item["proposed_value"])
        change_state = "NO_CHANGE" if same_value else "CANDIDATE_CHANGE_NOT_ACCEPTED"
        rows.append({
            "key": key,
            "old_value": old_display,
            "old_value_state": old_state,
            "proposed_value": item["proposed_value"],
            "change_state": change_state,
            "source_ref": source_ref,
            "reason": reason,
            "VASP_version_scope": version_scope,
            "system_scope": system_scope,
            "changes_method": item["changes_method"],
            "changes_restart": item["changes_restart"],
            "changes_environment": item["changes_environment"],
            "requires_scientific_decision": not same_value,
            "evidence_state": evidence_state,
            "advice_state": "NO_CHANGE" if same_value and not blockers else (
                "INSUFFICIENT_ADVICE_EVIDENCE" if blockers else "REVIEWABLE_ONLY"),
            "blockers": sorted(set(blockers)),
        })
        all_blockers.extend({"key": key, "blocker": code} for code in sorted(set(blockers)))

    comparison, comparison_blockers = _review_comparison(proposal.get("comparison_contract"), root)
    all_blockers.extend({"scope": "comparison_contract", "blocker": code} for code in sorted(set(comparison_blockers)))
    status = "INSUFFICIENT_ADVICE_EVIDENCE" if all_blockers else "REVIEWABLE_ADVICE_ONLY"
    return {
        "schema": ADVICE_SCHEMA,
        "status": status,
        "proposal_id": proposal_id,
        "approved_bundle_ref": bundle_ref,
        "approved_template": template,
        "proposal_ref": proposal_ref,
        "changes": rows,
        "comparison_contract": comparison,
        "blockers": all_blockers,
        "execution_authorized": False,
        "input_written": False,
        "restart_changed": False,
        "scientific_acceptance": "NOT_EVALUATED",
        "interpretation_limits": [
            "The approved bundle is read as the sole old-value source; no input file is written.",
            "Source references are checked by local path existence only; their contents are not opened or verified.",
            "An unchanged value is reported as NO_CHANGE; OMITTED is distinct from numeric zero or boolean false.",
            "Candidate changes remain unaccepted and require the scientific decision identified in this report.",
            "E0, F, TOTEN and MP-corrected entries remain distinct; no energy or acceptance conclusion is computed.",
        ],
    }


def _resolve_input(path, label):
    resolved = Path(path).resolve()
    root = PROJECT_ROOT.resolve()
    if not resolved.is_relative_to(root) or resolved.suffix.casefold() != ".json" or not resolved.is_file():
        raise ValueError(f"{label} must be an existing workspace-local JSON file")
    if EXCLUDED_PATH_PART.casefold() in {part.casefold() for part in resolved.parts}:
        raise ValueError("excluded trial-directory inputs are not allowed")
    return resolved


def _resolve_output(path):
    output = Path(path).resolve()
    root = PROJECT_ROOT.resolve()
    if not output.is_relative_to(root):
        raise ValueError("output-dir must be inside the workspace")
    parts = {part.casefold() for part in output.relative_to(root).parts}
    if "private_runs" in parts or EXCLUDED_PATH_PART.casefold() in parts:
        raise ValueError("advice output cannot be written into calculation or excluded trial directories")
    if output.exists():
        raise ValueError(f"output directory must be new: {output}")
    return output


def _write_outputs(report, output):
    output.mkdir(parents=True, exist_ok=False)
    (output / "parameter_advice.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    (output / "blockers.json").write_text(json.dumps({
        "schema": "vasp-parameter-advice-blockers/v1", "status": report["status"],
        "blockers": report["blockers"],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    with (output / "source_table.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        columns = ["key", "source_ref", "reason", "VASP_version_scope", "system_scope", "evidence_state", "advice_state", "blockers"]
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in report["changes"]:
            writer.writerow({name: ";".join(row["blockers"]) if name == "blockers" else row.get(name)
                             for name in columns})
    (output / "README.md").write_text(
        "# Parameter advice review\n\nRead-only candidate review. No VASP input or restart was written.\n\n"
        f"Status: **{report['status']}**; changes: {len(report['changes'])}; blockers: {len(report['blockers'])}.\n",
        encoding="utf-8",
    )
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved-bundle", required=True, help="Explicit original vasp-approved-bundle/v1 JSON")
    parser.add_argument("--proposal", required=True, help=f"Explicit {PROPOSAL_SCHEMA} JSON")
    parser.add_argument("--output-dir", required=True, help="New workspace-local review directory")
    args = parser.parse_args(argv)
    try:
        bundle_path = _resolve_input(args.approved_bundle, "approved-bundle")
        proposal_path = _resolve_input(args.proposal, "proposal")
        output_dir = _resolve_output(args.output_dir)
        bundle = json.loads(bundle_path.read_text(encoding="utf-8-sig"))
        proposal = json.loads(proposal_path.read_text(encoding="utf-8-sig"))
        report = review(
            bundle, proposal,
            bundle_ref=bundle_path.relative_to(PROJECT_ROOT).as_posix(),
            proposal_ref=proposal_path.relative_to(PROJECT_ROOT).as_posix(),
        )
        output = _write_outputs(report, output_dir)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(2, f"parameter-advice stopped: {type(error).__name__}: {error}\n")
    print(json.dumps({"status": report["status"], "output_dir": str(output),
                      "change_count": len(report["changes"]), "blocker_count": len(report["blockers"]),
                      "execution_authorized": report["execution_authorized"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

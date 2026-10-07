"""Read-only reference/contract checks. No submission or scientific acceptance."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

STATES = ("DRAFT", "PREPARED", "USER_APPROVED", "SUBMITTED", "RETURNED", "ACCEPTED", "REJECTED")
PROJECTS = {"dft-book", "pymatgen", "ase", "py4vasp", "custodian", "atomate2", "quacc", "aiida-vasp"}
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def present(value):
    return isinstance(value, str) and bool(value.strip()) and "REPLACE_ME" not in value


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def contract_errors(data):
    """Check declared record completeness, NOT underlying input/output truth."""
    errors = []
    def need(ok, message):
        if not ok:
            errors.append(message)

    if not isinstance(data, dict):
        return ["Manifest must be a JSON object."]
    need(type(data.get("schema_version")) is int and data.get("schema_version") == 1, "Unsupported schema_version.")
    state = data.get("status")
    need(state in STATES, "Unknown status.")
    need(state != "DRAFT", "DRAFT is incomplete by definition; not ready for handoff.")
    for key in ("task_id", "purpose", "environment_id", "method_spec_ref", "runtime_record_ref"):
        need(present(data.get(key)), f"Missing {key}.")
    need(data.get("backend") == "VASP", "This contract is VASP-only.")
    blocks = data.get("species_blocks")
    if not isinstance(blocks, list) or not blocks:
        errors.append("species_blocks must be a non-empty ordered list.")
        blocks = []
    role_ids = []
    electron_sum = 0.0
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            errors.append(f"Block {index + 1} must be an object.")
            continue
        role_ids.append(block.get("role"))
        for key in ("role", "element", "poscar_label", "paw_dataset", "paw_path"):
            need(present(block.get(key)), f"Block {index + 1}: missing {key}.")
        need(type(block.get("block_index")) is int and block.get("block_index") == index + 1, "Block order must be explicit and 1-based.")
        count, zval = block.get("count"), block.get("zval")
        need(type(count) is int and count > 0, f"Block {index + 1}: invalid count.")
        need(finite(zval) and zval > 0, f"Block {index + 1}: invalid zval.")
        need(bool(HEX64.fullmatch(str(block.get("paw_sha256", "")))), f"Block {index + 1}: invalid PAW SHA-256.")
        if type(count) is int and finite(zval):
            electron_sum += count * zval
    need(all(isinstance(role, str) for role in role_ids), "Role IDs must be strings.")
    # Same element may occur in multiple independently identified PAW blocks.
    need(len({str(role) for role in role_ids}) == len(role_ids), "Duplicate role IDs; do not collapse distinct PAW roles.")
    charge, expected = data.get("net_charge_e"), data.get("expected_nelect")
    need(finite(charge) and finite(expected), "net_charge_e/expected_nelect must be finite numbers.")
    if finite(charge) and finite(expected):
        need(abs(electron_sum - charge - expected) < 1e-8, "NELECT mismatch: sum(count*ZVAL) - net_charge_e.")
    resources = data.get("resources")
    if not isinstance(resources, dict):
        resources = {}
    values = [resources.get(k) for k in ("mpi_ranks", "kpar", "ncore")]
    need(all(type(x) is int and x > 0 for x in values), "Invalid MPI/KPAR/NCORE record.")
    if all(type(x) is int and x > 0 for x in values):
        ranks, kpar, ncore = values
        need(ranks % (kpar * ncore) == 0, "MPI ranks must divide into KPAR*NCORE groups.")
        need(ranks <= 64, "This workflow version covers at most 64 physical-core MPI ranks.")
    need(type(resources.get("omp_num_threads")) is int and resources.get("omp_num_threads") == 1, "Current environment is MPI-only.")
    input_files = data.get("input_sha256")
    if not isinstance(input_files, dict):
        input_files = {}
    for name in ("POSCAR", "INCAR", "KPOINTS"):
        need(bool(HEX64.fullmatch(str(input_files.get(name, "")))), f"Missing or invalid {name} SHA-256.")
    for key in ("input_validation_ref", "paw_validation_ref", "acceptance_criteria_ref", "stop_conditions_ref"):
        need(present(data.get(key)), f"Missing {key}.")
    # Submitted/rejected states do not imply successful computation.
    if state in ("USER_APPROVED", "SUBMITTED", "RETURNED", "ACCEPTED", "REJECTED"):
        approval = data.get("approval")
        if not isinstance(approval, dict):
            approval = {}
        need(approval.get("task_id") == data.get("task_id"), "Approval must name this task.")
        need(present(approval.get("evidence_ref")), "Missing approval evidence reference.")
        need(present(approval.get("approved_at")), "Missing approval time.")
        need(bool(HEX64.fullmatch(str(approval.get("package_sha256", "")))), "Missing approved package fingerprint.")
    if state in ("SUBMITTED", "RETURNED", "ACCEPTED", "REJECTED"):
        execution = data.get("execution")
        if not isinstance(execution, dict):
            execution = {}
        for key in ("host", "workdir", "command_ref", "started_at", "submission_evidence_ref"):
            need(present(execution.get(key)), f"Missing execution.{key}.")
    if state in ("RETURNED", "ACCEPTED", "REJECTED"):
        returned = data.get("return_evidence")
        if not isinstance(returned, dict):
            returned = {}
        for key in ("output_inventory_ref", "extraction_ref"):
            need(present(returned.get(key)), f"Missing return_evidence.{key}.")
        # Missing historical exit code may be declared; never manufacture zero.
        exit_code = returned.get("exit_code")
        need(exit_code is None or type(exit_code) is int, "exit_code must be integer or null.")
        if exit_code is None:
            need(present(returned.get("exit_code_missing_reason")), "Missing exit code needs an explanation.")
        wall = returned.get("wall_seconds")
        need(wall is None or (finite(wall) and wall >= 0), "Invalid wall_seconds.")
        if wall is None:
            need(present(returned.get("wall_time_missing_reason")), "Missing wall time needs an explanation.")
    if state in ("ACCEPTED", "REJECTED"):
        decision = data.get("decision")
        if not isinstance(decision, dict):
            decision = {}
        need(decision.get("responsible_role") == "VASP Sol", "Final interpretation belongs to VASP Sol.")
        for key in ("evidence_ref", "limited_use", "decided_at"):
            need(present(decision.get(key)), f"Missing decision.{key}.")
    return errors


def git_read(repo, *args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=30, env=env)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Git read failed.")
    return result.stdout.strip()


def source_errors(lock, root):
    errors = []
    if not isinstance(lock, dict) or type(lock.get("schema_version")) is not int or lock.get("schema_version") != 1:
        return ["Unsupported source lock schema."]
    sources = lock.get("sources")
    if not isinstance(sources, list) or any(not isinstance(item, dict) for item in sources):
        return ["sources must be a list of objects."]
    if not sources or {str(item.get("name")) for item in sources} != PROJECTS or len(sources) != len(PROJECTS):
        errors.append("Expected exactly the 8 agreed source repositories.")
    for item in sources:
        name = item.get("name", "?")
        try:
            repo = (root / item["local_path"]).resolve()
            if not repo.is_relative_to((root / "vendor").resolve()):
                raise ValueError("Path must stay under vendor.")
            commit = item.get("commit")
            if not re.fullmatch(r"[0-9a-fA-F]{40}", str(commit)):
                raise ValueError("Missing valid locked commit.")
            if git_read(repo, "rev-parse", "--show-toplevel").replace("\\", "/").lower() != str(repo).replace("\\", "/").lower():
                raise ValueError("Not an independent clone root.")
            if git_read(repo, "remote", "get-url", "origin").rstrip("/") != item["url"].rstrip("/"):
                raise ValueError("Origin does not match lock.")
            if git_read(repo, "rev-parse", "HEAD") != commit:
                raise ValueError("HEAD does not match lock.")
            if git_read(repo, "status", "--porcelain", "--untracked-files=normal"):
                raise ValueError("Checkout has local changes.")
            if git_read(repo, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD":
                raise ValueError("Snapshot must have detached HEAD.")
            if git_read(repo, "rev-parse", "--is-shallow-repository") != "true":
                raise ValueError("Snapshot must remain a shallow clone.")
            entries = item.get("entrypoints", [])
            licenses = item.get("license_evidence", [])
            if not isinstance(entries, list) or not entries or not isinstance(licenses, list) or not licenses:
                raise ValueError("Entrypoints and license evidence must be non-empty lists.")
            for entry in entries + licenses:
                if not isinstance(entry, str):
                    raise ValueError("Entrypoints must be relative path strings.")
                path = (repo / entry).resolve()
                if not path.is_relative_to(repo) or not path.exists():
                    raise ValueError(f"Missing or escaping entrypoint: {entry}")
        except (KeyError, TypeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{name}: {exc}")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("sources", "contract"))
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    try:
        data = json.loads(args.file.read_text(encoding="utf-8-sig"))
        errors = source_errors(data, args.file.resolve().parent) if args.mode == "sources" else contract_errors(data)
    except (OSError, ValueError, TypeError) as exc:
        errors = [str(exc)]
    label = "SOURCE_IDENTITY_CHECK" if args.mode == "sources" else "DECLARED_CONTRACT_CHECK"
    print(json.dumps({"check": label, "passed": not errors, "errors": errors,
                      "scientific_acceptance": "NOT_EVALUATED",
                      "submission_authorized_by_this_check": False}, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

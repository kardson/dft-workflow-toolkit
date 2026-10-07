#!/usr/bin/env python3
"""Shared read-only VASP input preflight and output postcheck.

This module is an executor handoff gate, not a VASP submitter.  It reads an
approved input_manifest and local files, reports independent program/evidence/
convergence states, and never connects to SSH or starts a process.  The internal
execute_once helper is only an integration seam for an existing runner that
injects its own launcher, lock, tmux and logging policy; it is not exposed as a
CLI execution command.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import stat
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

try:
    from progress_snapshot import (
        compare_parameter_sources,
        incar_float,
        incar_int,
        incar_raw,
        parse_incar,
        parse_outcar,
        parse_poscar,
        parse_timing,
    )
except ImportError:  # pragma: no cover - package-style import fallback
    from .progress_snapshot import (  # type: ignore
        compare_parameter_sources,
        incar_float,
        incar_int,
        incar_raw,
        parse_incar,
        parse_outcar,
        parse_poscar,
        parse_timing,
    )


SCHEMA = "vasp-executor-check/v1"
SCIENTIFIC_REVIEW = {
    "status": "NOT_EVALUATED",
    "owner": "VASP Sol",
    "reason": "Execution tooling never grants scientific acceptance or release.",
}

INCAR_FIELDS: dict[str, tuple[str, str]] = {
    "PREC": ("PREC", "text"),
    "ENCUT_eV": ("ENCUT", "float"),
    "EDIFF_eV": ("EDIFF", "float"),
    "ALGO": ("ALGO", "text"),
    "NELM": ("NELM", "int"),
    "NELMIN": ("NELMIN", "int"),
    "ISMEAR": ("ISMEAR", "int"),
    "SIGMA_eV": ("SIGMA", "float"),
    "ISPIN": ("ISPIN", "int"),
    "MAGMOM": ("MAGMOM", "text"),
    "NUPDOWN": ("NUPDOWN", "int"),
    "ISYM": ("ISYM", "int"),
    "LREAL": ("LREAL", "bool"),
    "LASPH": ("LASPH", "bool"),
    "ADDGRID": ("ADDGRID", "bool"),
    "NBANDS": ("NBANDS", "int"),
    "ISTART": ("ISTART", "int"),
    "ICHARG": ("ICHARG", "int"),
    "IBRION": ("IBRION", "int"),
    "POTIM": ("POTIM", "float"),
    "NSW": ("NSW", "int"),
    "ISIF": ("ISIF", "int"),
    "EDIFFG_eV_per_A": ("EDIFFG", "float"),
    "LDIPOL": ("LDIPOL", "bool"),
    "IDIPOL": ("IDIPOL", "int"),
    "DIPOL": ("DIPOL", "vector"),
    "LWAVE": ("LWAVE", "bool"),
    "LCHARG": ("LCHARG", "bool"),
    "KPAR": ("KPAR", "int"),
    "NCORE": ("NCORE", "int"),
}

FORBIDDEN_FEATURE_TAGS = {
    "external_field": ("EFIELD", "EFIELD_PEAD"),
    "soc": ("LSORBIT",),
    "dispersion": ("IVDW",),
    "projection_output": ("LORBIT",),
}

STANDARD_RESTART_FILES = ("WAVECAR", "CHGCAR", "CHG", "TMPCAR")
WARM_RESTART_MODE = "wavefunction_and_charge_scf"
WARM_RESTART_COMPATIBILITY_KEYS = frozenset(
    {"geometry", "environment", "paw", "encut", "kpoints", "nbands", "spin"}
)
GEOMETRY_SOURCE_KEYS = frozenset({"parent_repo_path", "parent_sha256", "source_kind", "provenance"})
WARM_RESTART_SOURCE_KEYS = frozenset(
    {
        "unit_id",
        "task_id",
        "case_id",
        "case_path",
        "poscar_sha256",
        "environment_id",
        "paw_family",
        "ordered_paw_roles",
        "combined_potcar_sha256",
        "encut_eV",
        "kpoints",
        "nbands",
        "ispin",
        "magmom",
        "nupdown",
        "compatibility",
        "files",
        "copy_policy",
        "remote_preflight_state",
    }
)
WARM_RESTART_FILE_KEYS = frozenset(
    {
        "source_path",
        "source_postcheck_nonempty",
        "source_size_bytes",
        "source_observed_utc",
        "local_state",
        "target_preflight_state",
    }
)
RESTART_SPEC_KEYS = frozenset(
    {"ISTART", "ICHARG", "fresh", "restart_files_present", "potcar_present", "mode", "source"}
)
INPUT_FILES = ("POSCAR", "INCAR", "KPOINTS")
STANDARD_OUTPUT_FILES = (
    "OUTCAR", "OSZICAR", "vasprun.xml", "CONTCAR", "vasp.stdout", "vasp.stderr",
    "IBZKPT", "EIGENVAL", "XDATCAR", "DOSCAR", "PROCAR", "LOCPOT", "ELFCAR",
    "PARCHG", "vaspout.h5", "run_timing.txt", "status.json",
)
EXPLICIT_FALSE_TAGS = frozenset({"LSORBIT", "LNONCOLLINEAR", "LDAU", "LHFCALC"})
APPROVED_INCAR_TAGS = frozenset({tag for tag, _ in INCAR_FIELDS.values()} | {"SYSTEM"})


def _issue(code: str, message: str, **fields: Any) -> dict[str, Any]:
    result = {"code": code, "message": message}
    result.update(fields)
    return result


def _normalise_text(value: str) -> str:
    return " ".join(value.strip().split()).upper()


def _number(value: Any) -> float | None:
    try:
        parsed = float(str(value).replace("D", "E").replace("d", "e"))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    token = str(value).strip().split()[0].lower() if str(value).strip() else ""
    if token in {".true.", "true", "t", "1"}:
        return True
    if token in {".false.", "false", "f", "0"}:
        return False
    return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed


def _read_text(path: Path) -> tuple[str | None, dict[str, Any] | None]:
    try:
        return path.read_text(encoding="utf-8", errors="replace"), None
    except FileNotFoundError:
        return None, _issue("MISSING_FILE", f"Required file is absent: {path}", path=str(path))
    except OSError as error:
        return None, _issue(
            "UNREADABLE_FILE",
            f"File could not be read: {path}",
            path=str(path),
            error_type=type(error).__name__,
            error=str(error),
        )


def _load_manifest(path: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    text, error = _read_text(path)
    if error:
        return None, [error]
    try:
        data = json.loads(text or "")
    except json.JSONDecodeError as parse_error:
        return None, [
            _issue(
                "INVALID_MANIFEST_JSON",
                f"Manifest JSON is invalid: {parse_error.msg}",
                path=str(path),
                line=parse_error.lineno,
            )
        ]
    if not isinstance(data, dict):
        return None, [_issue("MANIFEST_NOT_OBJECT", "input_manifest must be a JSON object", path=str(path))]
    return data, []


def validate_restart_spec(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate the single shared fresh / approved warm-restart manifest contract."""

    errors: list[dict[str, Any]] = []
    incar = manifest.get("incar")
    if not isinstance(incar, Mapping):
        incar = {}
    restart = manifest.get("restart")
    if not isinstance(restart, Mapping):
        return [_issue("MISSING_RESTART_SPEC", "Manifest restart specification is missing.")]
    unknown_restart_keys = sorted(set(restart) - RESTART_SPEC_KEYS)
    if unknown_restart_keys:
        errors.append(_issue(
            "UNKNOWN_RESTART_SPEC_KEY",
            "Restart specification contains unsupported fields.",
            keys=unknown_restart_keys,
        ))

    mode = restart.get("mode")
    if mode in (None, "fresh") and restart.get("fresh") is True:
        if restart.get("ISTART") != 0 or restart.get("ICHARG") != 2:
            errors.append(_issue(
                "INCONSISTENT_FRESH_SPEC",
                "Fresh restart must declare ISTART=0 and ICHARG=2.",
            ))
        if incar.get("ISTART") != 0:
            errors.append(_issue("UNSUPPORTED_RESTART_MODE", "Fresh input requires INCAR ISTART=0."))
        if incar.get("ICHARG") != 2:
            errors.append(_issue("UNSUPPORTED_CHARGE_MODE", "Fresh input requires INCAR ICHARG=2."))
        if restart.get("source") is not None:
            errors.append(_issue("UNEXPECTED_RESTART_SOURCE", "Fresh mode must not declare a restart source."))
        for key in ("restart_files_present", "potcar_present"):
            if key in restart and restart.get(key) is not False:
                errors.append(_issue(
                    "INCONSISTENT_FRESH_SPEC",
                    f"Fresh restart declaration {key} must be false when supplied.",
                    field=key,
                ))
        return errors

    if mode != WARM_RESTART_MODE or restart.get("fresh") is not False:
        errors.append(_issue(
            "UNSUPPORTED_RESTART_MODE",
            "Only fresh mode or the approved 1/1 wavefunction-and-charge restart mode is supported.",
        ))
        return errors
    if "locked_geometry" in manifest:
        errors.append(_issue(
            "LOCKED_GEOMETRY_UNVERIFIED",
            "Warm-restart manifests may not carry locked_geometry claims; bind the accepted source POSCAR instead.",
        ))
    if restart.get("ISTART") != 1 or restart.get("ICHARG") != 1:
        errors.append(_issue("INCONSISTENT_WARM_SPEC", "Warm restart must declare ISTART=1 and ICHARG=1."))
    if incar.get("ISTART") != 1:
        errors.append(_issue("UNSUPPORTED_RESTART_MODE", "Warm input requires INCAR ISTART=1."))
    if incar.get("ICHARG") != 1:
        errors.append(_issue("UNSUPPORTED_CHARGE_MODE", "Warm input requires INCAR ICHARG=1."))
    if type(incar.get("IBRION")) is not int or incar.get("IBRION") not in (1, 2) or incar.get("ISIF") != 2 or type(incar.get("NSW")) is not int or incar.get("NSW") <= 0:
        errors.append(_issue(
            "UNSUPPORTED_WARM_RESTART_FORM",
            "Warm 1/1 restart is supported only for fixed-cell IBRION=1/2 relaxation with NSW>0 and ISIF=2.",
        ))
    for key in ("restart_files_present", "potcar_present"):
        if key in restart and restart.get(key) is not False:
            errors.append(_issue(
                "INCONSISTENT_WARM_SPEC",
                f"Local preparation must not declare {key} true.",
                field=key,
            ))

    source = restart.get("source")
    if not isinstance(source, Mapping):
        errors.append(_issue("MISSING_RESTART_SOURCE", "Warm restart requires an explicit source identity and compatibility record."))
        return errors
    unknown_source_keys = sorted(set(source) - WARM_RESTART_SOURCE_KEYS)
    if unknown_source_keys:
        errors.append(_issue(
            "UNKNOWN_RESTART_SOURCE_KEY",
            "Warm restart source contains unsupported fields.",
            keys=unknown_source_keys,
        ))
    required_text = (
        "unit_id", "task_id", "case_id", "case_path", "environment_id", "paw_family",
        "copy_policy", "remote_preflight_state",
    )
    for key in required_text:
        if not isinstance(source.get(key), str) or not source[key].strip():
            errors.append(_issue(
                "INVALID_RESTART_SOURCE",
                f"restart.source.{key} must be non-empty text.",
                field=key,
            ))

    case_path = source.get("case_path")
    case_path_valid = (
        isinstance(case_path, str)
        and case_path.startswith("/")
        and all(part not in {"", ".", ".."} for part in case_path.split("/")[1:])
    )
    if not case_path_valid:
        errors.append(_issue("INVALID_RESTART_SOURCE_PATH", "restart.source.case_path must be a normalized absolute POSIX path."))
    if source.get("copy_policy") != "REGULAR_FILE_COPY_NO_HARDLINK":
        errors.append(_issue("UNSAFE_RESTART_COPY_POLICY", "Warm restart files must be copied as independent regular files, never hard-linked."))
    if source.get("remote_preflight_state") != "REMOTE_RESTART_PENDING":
        errors.append(_issue("INVALID_RESTART_REMOTE_STATE", "Local preparation must leave remote restart verification pending."))

    poscar_sha = source.get("poscar_sha256")
    target_outputs = manifest.get("outputs")
    poscar_entry = target_outputs.get("POSCAR") if isinstance(target_outputs, Mapping) else None
    target_poscar_sha = poscar_entry.get("sha256") if isinstance(poscar_entry, Mapping) else None
    sha_pattern = r"[0-9a-fA-F]{64}"
    source_geometry_ok = isinstance(poscar_sha, str) and re.fullmatch(sha_pattern, poscar_sha) is not None
    target_geometry_ok = isinstance(target_poscar_sha, str) and re.fullmatch(sha_pattern, target_poscar_sha) is not None
    if not source_geometry_ok:
        errors.append(_issue("INVALID_RESTART_GEOMETRY_IDENTITY", "restart.source.poscar_sha256 must be a SHA-256 digest."))
    if not target_geometry_ok:
        errors.append(_issue("INVALID_TARGET_GEOMETRY_IDENTITY", "Warm restart requires a declared target outputs.POSCAR.sha256."))
    geometry_match = source_geometry_ok and target_geometry_ok and poscar_sha.lower() == target_poscar_sha.lower()
    if source_geometry_ok and target_geometry_ok and not geometry_match:
        errors.append(_issue("RESTART_GEOMETRY_MISMATCH", "Restart source POSCAR identity differs from the target input identity."))

    geometry_source = manifest.get("source")
    if not isinstance(geometry_source, Mapping):
        errors.append(_issue(
            "MISSING_GEOMETRY_SOURCE",
            "Warm-restart input requires a top-level source record for the exact approved geometry POSCAR.",
        ))
    else:
        unknown_geometry_source_keys = sorted(set(geometry_source) - GEOMETRY_SOURCE_KEYS)
        if unknown_geometry_source_keys:
            errors.append(_issue(
                "UNKNOWN_GEOMETRY_SOURCE_KEY",
                "Top-level geometry source contains unsupported fields.",
                keys=unknown_geometry_source_keys,
            ))
        source_path = geometry_source.get("parent_repo_path")
        source_kind = geometry_source.get("source_kind")
        source_hash = geometry_source.get("parent_sha256")
        normalized_source_path = (
            isinstance(source_path, str)
            and "\\" not in source_path
            and not PurePosixPath(source_path).is_absolute()
            and str(PurePosixPath(source_path)) == source_path
            and all(part not in {"", ".", ".."} for part in source_path.split("/"))
            and PurePosixPath(source_path).name in {"POSCAR", "CONTCAR"}
        )
        if not normalized_source_path:
            errors.append(_issue(
                "INVALID_GEOMETRY_SOURCE_PATH",
                "source.parent_repo_path must be a normalized repository-relative POSCAR or CONTCAR path.",
            ))
        source_task_id = source.get("task_id")
        provenance = geometry_source.get("provenance")
        if provenance is not None:
            expected_keys = {"original_repo_path", "original_sha256", "original_task_id"}
            valid = isinstance(provenance, Mapping) and set(provenance) == expected_keys
            if valid:
                original_path = provenance.get("original_repo_path")
                valid = (isinstance(original_path, str) and bool(original_path) and "\\" not in original_path
                         and not PurePosixPath(original_path).is_absolute()
                         and str(PurePosixPath(original_path)) == original_path
                         and all(part not in {"", ".", ".."} for part in original_path.split("/"))
                         and isinstance(source_task_id, str) and bool(source_task_id)
                         and provenance.get("original_task_id") == source_task_id
                         and provenance.get("original_sha256") == source_hash
                         and re.fullmatch(sha_pattern, str(source_hash)) is not None)
            if not valid:
                errors.append(_issue("GEOMETRY_PROVENANCE_MISMATCH", "Original source identity, path and hash must match the declared electronic-state source and staged geometry."))
        elif normalized_source_path and (
            not isinstance(source_task_id, str)
            or source_task_id not in PurePosixPath(source_path).parts
        ):
            errors.append(_issue(
                "GEOMETRY_SOURCE_TASK_MISMATCH",
                "The exact geometry source path must identify the task declared by restart.source.task_id.",
                source_task_id=source_task_id,
                source_path=source_path,
            ))
        if not isinstance(source_kind, str) or not source_kind.strip() or (
            normalized_source_path and source_kind.split("/")[-1] != PurePosixPath(source_path).name
        ):
            errors.append(_issue(
                "INVALID_GEOMETRY_SOURCE_KIND",
                "source.source_kind must be explicit and end with the source filename kind.",
            ))
        source_hash_ok = isinstance(source_hash, str) and re.fullmatch(sha_pattern, source_hash) is not None
        if not source_hash_ok:
            errors.append(_issue(
                "INVALID_GEOMETRY_SOURCE_HASH",
                "source.parent_sha256 must be a SHA-256 digest for the exact source geometry.",
            ))
        if source_hash_ok and target_geometry_ok and source_hash.lower() != target_poscar_sha.lower():
            errors.append(_issue(
                "GEOMETRY_SOURCE_HASH_MISMATCH",
                "Top-level source geometry SHA-256 must match outputs.POSCAR and the warm-restart source POSCAR.",
                source_sha256=source_hash,
                target_sha256=target_poscar_sha,
            ))
        if source_hash_ok and source_geometry_ok and source_hash.lower() != poscar_sha.lower():
            errors.append(_issue(
                "GEOMETRY_SOURCE_RESTART_MISMATCH",
                "Top-level source geometry SHA-256 must match restart.source.poscar_sha256.",
                source_sha256=source_hash,
                restart_source_sha256=poscar_sha,
            ))

    environment_id = manifest.get("environment_id")
    paw = manifest.get("paw_identity")
    if not isinstance(paw, Mapping):
        paw = {}
    structure = manifest.get("structure")
    if not isinstance(structure, Mapping):
        structure = {}
    kpoints = manifest.get("kpoints")
    if not isinstance(kpoints, Mapping):
        kpoints = {}
    paw_order = structure.get("paw_order")
    source_roles = source.get("ordered_paw_roles")
    target_combined = paw.get("combined_potcar_sha256")
    source_combined = source.get("combined_potcar_sha256")
    paw_match = (
        isinstance(paw_order, list)
        and source_roles == paw_order
        and paw.get("ordered_roles") == paw_order
        and source.get("paw_family") == paw.get("paw_family")
        and isinstance(target_combined, str)
        and re.fullmatch(sha_pattern, target_combined) is not None
        and source_combined == target_combined
    )
    if not isinstance(source_roles, list) or not source_roles or not all(isinstance(role, str) and role for role in source_roles):
        errors.append(_issue("INVALID_RESTART_PAW_ROLES", "Restart source ordered_paw_roles must be a non-empty string list."))
    if not isinstance(target_combined, str) or re.fullmatch(sha_pattern, target_combined) is None:
        errors.append(_issue("INVALID_TARGET_PAW_IDENTITY", "Target PAW identity must declare a combined POTCAR SHA-256."))
    if not isinstance(source_combined, str) or re.fullmatch(sha_pattern, source_combined) is None:
        errors.append(_issue("INVALID_RESTART_PAW_IDENTITY", "Restart source must declare a combined PAW SHA-256."))
    if not paw_match:
        errors.append(_issue("RESTART_PAW_MISMATCH", "Restart source and target PAW family, ordered roles and combined identity must match."))

    environment_match = (
        isinstance(environment_id, str)
        and source.get("environment_id") == environment_id
        and paw.get("environment_id") == environment_id
    )
    if not environment_match:
        errors.append(_issue("RESTART_ENVIRONMENT_MISMATCH", "Restart source, target and PAW identity environment IDs must match."))

    target_encut = incar.get("ENCUT_eV")
    source_encut = _number(source.get("encut_eV"))
    target_encut_number = _number(target_encut)
    encut_match = (
        source_encut is not None
        and target_encut_number is not None
        and math.isclose(source_encut, target_encut_number, rel_tol=0.0, abs_tol=1e-10)
    )
    if not encut_match:
        errors.append(_issue("RESTART_ENCUT_MISMATCH", "Restart source and target ENCUT must match."))

    source_kpoints = source.get("kpoints")
    source_kpoints_shape_ok = (
        isinstance(source_kpoints, Mapping)
        and set(source_kpoints) == {"generation", "mesh", "shift"}
        and isinstance(source_kpoints.get("generation"), str)
        and isinstance(source_kpoints.get("mesh"), list)
        and isinstance(source_kpoints.get("shift"), list)
    )
    target_shift = kpoints.get("shift")
    source_shift = source_kpoints.get("shift") if isinstance(source_kpoints, Mapping) else None
    shifts_match = (
        isinstance(target_shift, list)
        and isinstance(source_shift, list)
        and len(target_shift) == len(source_shift) == 3
        and all(_number(left) is not None and _number(right) is not None and math.isclose(float(_number(left)), float(_number(right)), rel_tol=0.0, abs_tol=1e-12) for left, right in zip(target_shift, source_shift))
    )
    kpoints_match = (
        source_kpoints_shape_ok
        and source_kpoints.get("generation", "").casefold() == str(kpoints.get("generation", "")).casefold()
        and source_kpoints.get("mesh") == kpoints.get("mesh")
        and shifts_match
    )
    if not kpoints_match:
        errors.append(_issue("RESTART_KPOINTS_MISMATCH", "Restart source and target KPOINTS generation, mesh and shift must match."))

    target_nbands = incar.get("NBANDS")
    nbands_match = type(target_nbands) is int and source.get("nbands") == target_nbands
    if not nbands_match:
        errors.append(_issue("RESTART_NBANDS_MISMATCH", "Restart source and target NBANDS must match."))
    spin_match = (
        type(incar.get("ISPIN")) is int
        and source.get("ispin") == incar.get("ISPIN")
        and isinstance(source.get("magmom"), str)
        and isinstance(incar.get("MAGMOM"), str)
        and _normalise_text(source["magmom"]) == _normalise_text(incar["MAGMOM"])
        and source.get("nupdown") == incar.get("NUPDOWN")
    )
    if not spin_match:
        errors.append(_issue("RESTART_SPIN_MISMATCH", "Restart source and target ISPIN, MAGMOM and NUPDOWN must match."))

    computed_compatibility = {
        "geometry": geometry_match,
        "environment": environment_match,
        "paw": paw_match,
        "encut": encut_match,
        "kpoints": kpoints_match,
        "nbands": nbands_match,
        "spin": spin_match,
    }
    compatibility = source.get("compatibility")
    if not isinstance(compatibility, Mapping) or set(compatibility) != WARM_RESTART_COMPATIBILITY_KEYS:
        errors.append(_issue(
            "RESTART_COMPATIBILITY_INCOMPLETE",
            "Warm restart compatibility must declare exactly geometry, environment, PAW, ENCUT, KPOINTS, NBANDS and spin.",
        ))
    else:
        for key, matches in computed_compatibility.items():
            expected = "MATCH" if matches else "MISMATCH"
            if compatibility.get(key) != expected:
                errors.append(_issue(
                    "RESTART_COMPATIBILITY_CLAIM_MISMATCH",
                    f"Declared restart compatibility for {key} does not match the independently checked source fields.",
                    field=key,
                    expected=expected,
                    actual=compatibility.get(key),
                ))

    files = source.get("files")
    if not isinstance(files, Mapping) or set(files) != {"WAVECAR", "CHGCAR"}:
        errors.append(_issue("RESTART_FILE_DECLARATION_INCOMPLETE", "Warm 1/1 restart requires source declarations for exactly WAVECAR and CHGCAR."))
    else:
        for name, item in files.items():
            if not isinstance(item, Mapping):
                errors.append(_issue("INVALID_RESTART_FILE_DECLARATION", f"restart.source.files.{name} must be an object.", file=name))
                continue
            unknown_file_keys = sorted(set(item) - WARM_RESTART_FILE_KEYS)
            if unknown_file_keys:
                errors.append(_issue(
                    "UNKNOWN_RESTART_FILE_KEY",
                    f"restart.source.files.{name} contains unsupported fields.",
                    file=name,
                    keys=unknown_file_keys,
                ))
            expected_path = f"{case_path.rstrip('/')}/{name}" if case_path_valid else None
            if item.get("source_path") != expected_path:
                errors.append(_issue("RESTART_FILE_SOURCE_PATH_MISMATCH", f"{name} source path must be inside the declared source case.", file=name))
            if item.get("source_postcheck_nonempty") is not True or type(item.get("source_size_bytes")) is not int or item.get("source_size_bytes", 0) <= 0:
                errors.append(_issue("RESTART_SOURCE_FILE_NOT_CONFIRMED", f"{name} must have a prior nonempty source postcheck observation.", file=name))
            observed = item.get("source_observed_utc")
            try:
                parsed_observed = datetime.fromisoformat(observed.replace("Z", "+00:00")) if isinstance(observed, str) else None
            except ValueError:
                parsed_observed = None
            if parsed_observed is None or parsed_observed.tzinfo is None:
                errors.append(_issue("RESTART_SOURCE_OBSERVATION_MISSING", f"{name} must have a timezone-qualified source postcheck timestamp.", file=name))
            if item.get("local_state") != "NOT_DOWNLOADED":
                errors.append(_issue("RESTART_LOCAL_STATE_UNSAFE", f"{name} must remain not downloaded during local preparation.", file=name))
            if item.get("target_preflight_state") != "REMOTE_RESTART_PENDING":
                errors.append(_issue("RESTART_TARGET_STATE_INVALID", f"{name} target preflight must remain pending.", file=name))

    return errors


def _manifest_support_errors(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if manifest.get("route") != "vasp" and str(manifest.get("backend", "")).upper() != "VASP":
        errors.append(_issue("UNSUPPORTED_ROUTE", "Manifest is not a VASP route manifest."))
    if not isinstance(manifest.get("unit_id"), str) or not manifest["unit_id"].strip():
        errors.append(_issue("MISSING_UNIT_ID", "Manifest must name the execution unit."))

    structure = manifest.get("structure")
    if not isinstance(structure, dict):
        errors.append(_issue("MISSING_STRUCTURE_SPEC", "Manifest structure specification is missing."))
        structure = {}
    species = structure.get("species_order")
    paw_order = structure.get("paw_order")
    counts = structure.get("counts")
    if not isinstance(species, list) or not species or not all(isinstance(item, str) and item for item in species):
        errors.append(_issue("INVALID_SPECIES_SPEC", "structure.species_order must be a non-empty string list."))
    if not isinstance(paw_order, list) or not paw_order or not all(isinstance(item, str) and item for item in paw_order):
        errors.append(_issue("INVALID_PAW_ORDER_SPEC", "structure.paw_order must be a non-empty string list."))
    if isinstance(species, list) and isinstance(paw_order, list) and len(species) != len(paw_order):
        errors.append(_issue("SPECIES_PAW_LENGTH_MISMATCH", "species_order and paw_order lengths differ."))
    if not isinstance(counts, list) or not counts or not all(type(item) is int and item > 0 for item in counts):
        errors.append(_issue("INVALID_COUNTS_SPEC", "structure.counts must be a non-empty positive integer list."))
    elif isinstance(species, list) and len(species) != len(counts):
        errors.append(_issue("SPECIES_COUNTS_LENGTH_MISMATCH", "species_order and counts lengths differ."))
    if type(structure.get("nions")) is not int or structure.get("nions", 0) <= 0:
        errors.append(_issue("INVALID_NIONS_SPEC", "structure.nions must be a positive integer."))
    elif isinstance(counts, list) and sum(counts) != structure["nions"]:
        errors.append(_issue("NIONS_SPEC_MISMATCH", "structure.nions differs from the sum of structure.counts."))
    if _number(structure.get("nelect")) is None:
        errors.append(_issue("INVALID_NELECT_SPEC", "structure.nelect must be a finite number."))
    cell = structure.get("cell_A")
    if (
        not isinstance(cell, list)
        or len(cell) != 3
        or any(
            not isinstance(row, list)
            or len(row) != 3
            or any(_number(value) is None for value in row)
            for row in cell
        )
    ):
        errors.append(_issue("INVALID_CELL_SPEC", "structure.cell_A must be a finite 3x3 cell matrix."))
    fixed = structure.get("fixed_global_indices")
    if not isinstance(fixed, list) or not all(type(item) is int for item in fixed):
        errors.append(_issue("INVALID_FIXED_MASK_SPEC", "fixed_global_indices must be an integer list."))
    else:
        nions = structure.get("nions")
        if type(nions) is int and len(set(fixed)) != len(fixed):
            errors.append(_issue("DUPLICATE_FIXED_INDEX_SPEC", "fixed_global_indices contains duplicates."))
        if type(nions) is int and any(item < 1 or item > nions for item in fixed):
            errors.append(_issue("FIXED_INDEX_OUT_OF_RANGE", "fixed_global_indices leaves the declared atom range."))
        free_count = structure.get("free_global_count")
        if type(nions) is int and type(free_count) is int and free_count != nions - len(fixed):
            errors.append(_issue("FREE_COUNT_SPEC_MISMATCH", "free_global_count differs from nions-fixed count."))
    incar = manifest.get("incar")
    if not isinstance(incar, dict):
        errors.append(_issue("MISSING_INCAR_SPEC", "Manifest INCAR specification is missing."))
        incar = {}
    required_incar = (
        "PREC", "ENCUT_eV", "EDIFF_eV", "ALGO", "NELM", "NELMIN", "ISMEAR",
        "SIGMA_eV", "ISPIN", "MAGMOM", "NUPDOWN", "ISYM", "LREAL", "LASPH",
        "ADDGRID", "NBANDS", "ISTART", "ICHARG", "IBRION", "POTIM", "NSW",
        "ISIF", "EDIFFG_eV_per_A", "LDIPOL", "IDIPOL", "DIPOL", "LWAVE", "LCHARG",
        "external_field", "soc", "dispersion", "projection_output",
    )
    static_form = incar.get("IBRION") == -1 and incar.get("NSW") == 0
    relaxation_form = type(incar.get("IBRION")) is int and incar.get("IBRION") in (1, 2) and type(incar.get("NSW")) is int and incar.get("NSW") > 0
    if static_form:
        required_incar = tuple(key for key in required_incar if key != "EDIFFG_eV_per_A")
    if incar.get("LDIPOL") is not True:
        required_incar = tuple(key for key in required_incar if key not in {"IDIPOL", "DIPOL"})
    for key in required_incar:
        if key not in incar:
            errors.append(_issue("MISSING_INCAR_SPEC_FIELD", f"Manifest INCAR field is missing: {key}", field=key))
    if type(incar.get("ISPIN")) is not int or incar.get("ISPIN") not in (1, 2):
        errors.append(_issue("UNSUPPORTED_ISPIN", "Supported task form requires ISPIN 1 or 2."))
    if incar.get("external_field") is not False:
        errors.append(_issue("UNSUPPORTED_EXTERNAL_FIELD", "Only explicit zero-field tasks are supported."))
    if incar.get("soc") is not False:
        errors.append(_issue("UNSUPPORTED_SOC", "SOC tasks are outside this executor gate."))
    if incar.get("dispersion") is not False:
        errors.append(_issue("UNSUPPORTED_DISPERSION", "Dispersion-enabled tasks are outside this executor gate."))
    if incar.get("projection_output") is not False:
        errors.append(_issue("UNSUPPORTED_PROJECTION", "Projection-output tasks are outside this executor gate."))
    if not static_form and not relaxation_form:
        errors.append(_issue(
            "UNSUPPORTED_RUN_KIND",
            "Supported task forms are fixed-cell IBRION=1/2 relaxation or IBRION=-1 static.",
        ))
    if type(incar.get("IBRION")) is not int:
        errors.append(_issue("UNSUPPORTED_IBRION", "IBRION must be an integer in a supported task form."))
    if type(incar.get("NSW")) is not int:
        errors.append(_issue("UNSUPPORTED_NSW", "NSW must be an integer in a supported task form."))
    if incar.get("ISIF") != 2:
        errors.append(_issue("UNSUPPORTED_CELL_MODE", "Supported task form requires fixed-cell ISIF=2."))
    errors.extend(validate_restart_spec(manifest))
    if type(incar.get("LDIPOL")) is not bool:
        errors.append(_issue("INVALID_LDIPOL_SPEC", "LDIPOL must be an explicit boolean."))
    dipole_active = incar.get("LDIPOL") is True
    if (dipole_active or incar.get("IDIPOL") is not None) and (
        type(incar.get("IDIPOL")) is not int or incar.get("IDIPOL") not in (1, 2, 3)
    ):
        errors.append(_issue("INVALID_DIPOL_DIRECTION", "IDIPOL must be one of the supported Cartesian directions."))
    if (dipole_active or incar.get("DIPOL") is not None) and (
        not isinstance(incar.get("DIPOL"), list)
        or len(incar.get("DIPOL", [])) != 3
        or any(_number(item) is None for item in incar.get("DIPOL", []))
    ):
        errors.append(_issue("INVALID_DIPOL_SPEC", "DIPOL must be a three-number list."))
    if not isinstance(incar.get("NUPDOWN"), (int, type(None))):
        errors.append(_issue("INVALID_SPIN_CONSTRAINT_SPEC", "NUPDOWN must be an integer or null."))

    kpoints = manifest.get("kpoints")
    if not isinstance(kpoints, dict):
        errors.append(_issue("MISSING_KPOINTS_SPEC", "Manifest KPOINTS specification is missing."))
        kpoints = {}
    if str(kpoints.get("generation", "")).lower() not in {"gamma", "monkhorst-pack", "monkhorst"}:
        errors.append(_issue("UNSUPPORTED_KPOINT_GENERATION", "KPOINTS generation must explicitly be Gamma or Monkhorst."))
    mesh = kpoints.get("mesh")
    shift = kpoints.get("shift")
    if not isinstance(mesh, list) or len(mesh) != 3 or not all(type(item) is int and item > 0 for item in mesh):
        errors.append(_issue("INVALID_KPOINT_MESH_SPEC", "KPOINTS mesh must be three positive integers."))
    if not isinstance(shift, list) or len(shift) != 3 or any(_number(item) is None for item in shift):
        errors.append(_issue("INVALID_KPOINT_SHIFT_SPEC", "KPOINTS shift must be three finite numbers."))
    parallel = manifest.get("parallel")
    if not isinstance(parallel, dict):
        errors.append(_issue("MISSING_PARALLEL_SPEC", "Manifest parallel specification is missing."))
    else:
        for field in ("mpi_ranks", "kpar", "ncore", "omp_num_threads"):
            value = parallel.get(field)
            if type(value) is not int or value <= 0:
                errors.append(_issue(
                    "INVALID_PARALLEL_SPEC",
                    f"parallel.{field} must be a positive integer.",
                    field=field,
                ))
        if all(
            type(parallel.get(field)) is int and parallel.get(field) > 0
            for field in ("mpi_ranks", "kpar", "ncore")
        ):
            ranks = parallel["mpi_ranks"]
            kpar = parallel["kpar"]
            ncore = parallel["ncore"]
            group = kpar * ncore
            if ranks % group != 0:
                errors.append(_issue(
                    "PARALLEL_GROUP_NOT_DIVISIBLE",
                    "MPI ranks must divide into KPAR*NCORE groups.",
                    mpi_ranks=ranks,
                    kpar=kpar,
                    ncore=ncore,
                ))
        if parallel.get("omp_num_threads") != 1:
            errors.append(_issue("UNSUPPORTED_OMP_MODE", "Supported task form is MPI-only with omp_num_threads=1."))
    input_policy = manifest.get("input_policy")
    if input_policy is not None:
        if not isinstance(input_policy, dict):
            errors.append(_issue("INVALID_INPUT_POLICY", "input_policy must be an object when present."))
        elif "allow_poscar_trailing_blank_lines" in input_policy and not isinstance(
            input_policy["allow_poscar_trailing_blank_lines"], bool
        ):
            errors.append(_issue("INVALID_POSCAR_TAIL_POLICY", "POSCAR tail policy must be boolean."))
    return errors


def _poscar_tail(lines: list[str], nions: int) -> tuple[list[int], int]:
    cursor = 5
    if cursor < len(lines):
        tokens = lines[cursor].split()
        if not tokens or not all(re.fullmatch(r"\d+", token) for token in tokens):
            cursor += 1
    cursor += 1
    if cursor < len(lines) and lines[cursor].strip().lower().startswith("selective"):
        cursor += 1
    cursor += 1
    end = cursor + nions
    nonblank = [index + 1 for index, line in enumerate(lines[end:], end) if line.strip()]
    return nonblank, end


def _validate_poscar_cell(
    manifest: Mapping[str, Any],
    parsed: Mapping[str, Any],
    text: str | None,
    path: Path,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "errors": [],
        "scale": None,
        "scaled_cell": None,
        "comparison": "NOT_EVALUATED",
    }
    structure = manifest.get("structure", {}) if isinstance(manifest, Mapping) else {}
    expected = structure.get("cell_A") if isinstance(structure, Mapping) else None
    if text is None:
        result["comparison"] = "NOT_AVAILABLE"
        return result
    lines = text.splitlines()
    if len(lines) < 2:
        result["comparison"] = "NOT_AVAILABLE"
        return result
    scale_tokens = lines[1].split()
    if len(scale_tokens) != 1:
        result["errors"].append(_issue(
            "POSCAR_SCALE_INVALID",
            "POSCAR scale line must contain one finite scalar; volume-style forms are unsupported.",
            path=str(path),
        ))
        result["comparison"] = "UNSUPPORTED"
        return result
    scale = _number(scale_tokens[0])
    result["scale"] = scale
    if scale is None or scale == 0:
        result["errors"].append(_issue(
            "POSCAR_SCALE_INVALID",
            "POSCAR scale must be a nonzero finite scalar.",
            path=str(path),
        ))
        result["comparison"] = "UNSUPPORTED"
        return result
    if scale < 0:
        result["errors"].append(_issue(
            "POSCAR_SCALE_UNSUPPORTED",
            "Negative volume-style POSCAR scaling is outside this gate.",
            path=str(path),
            scale=scale,
        ))
        result["comparison"] = "UNSUPPORTED"
        return result
    lattice = parsed.get("lattice")
    if (
        not isinstance(lattice, list)
        or len(lattice) != 3
        or any(not isinstance(row, list) or len(row) != 3 for row in lattice)
    ):
        result["comparison"] = "NOT_AVAILABLE"
        return result
    scaled = [[float(scale) * float(value) for value in row] for row in lattice]
    result["scaled_cell"] = scaled
    if (
        not isinstance(expected, list)
        or len(expected) != 3
        or any(
            not isinstance(row, list)
            or len(row) != 3
            or any(_number(value) is None for value in row)
            for row in expected
        )
    ):
        result["errors"].append(_issue(
            "POSCAR_CELL_SPEC_MISSING",
            "Manifest structure.cell_A is required for scaled-cell validation.",
            path=str(path),
        ))
        result["comparison"] = "SPEC_INVALID"
        return result
    matches = all(
        math.isclose(float(actual), float(reference), rel_tol=0.0, abs_tol=1e-8)
        for actual_row, reference_row in zip(scaled, expected)
        for actual, reference in zip(actual_row, reference_row)
    )
    if not matches:
        result["errors"].append(_issue(
            "POSCAR_CELL_MISMATCH",
            "Actual scaled POSCAR lattice differs from manifest structure.cell_A.",
            expected=expected,
            actual=scaled,
        ))
        result["comparison"] = "MISMATCH"
    else:
        result["comparison"] = "MATCH"
    return result


def validate_poscar(
    manifest: Mapping[str, Any],
    text: str | None,
    path: Path,
) -> dict[str, Any]:
    structure = manifest.get("structure", {}) if isinstance(manifest, Mapping) else {}
    expected_species = structure.get("species_order")
    expected_counts = structure.get("counts")
    expected_nions = structure.get("nions")
    expected_fixed = structure.get("fixed_global_indices")
    expected_free = (
        [index for index in range(1, expected_nions + 1) if index not in set(expected_fixed)]
        if type(expected_nions) is int and isinstance(expected_fixed, list)
        else []
    )
    errors: list[dict[str, Any]] = []
    parsed = parse_poscar(text, path)
    errors.extend(_issue("POSCAR_PARSE_ERROR", message, path=str(path)) for message in parsed.get("errors", []))
    cell_report = _validate_poscar_cell(manifest, parsed, text, path)
    errors.extend(cell_report["errors"])
    if parsed.get("species") != expected_species:
        errors.append(_issue("POSCAR_SPECIES_MISMATCH", "POSCAR species order differs from manifest."))
    if parsed.get("counts") != expected_counts:
        errors.append(_issue("POSCAR_COUNTS_MISMATCH", "POSCAR species counts differ from manifest."))
    if parsed.get("nions") != expected_nions:
        errors.append(_issue("POSCAR_NIONS_MISMATCH", "POSCAR atom count differs from manifest."))
    if expected_fixed and not parsed.get("selective_dynamics"):
        errors.append(_issue("POSCAR_SELECTIVE_MISSING", "Fixed atoms require Selective Dynamics."))
    if parsed.get("mask_status") != "OK":
        errors.append(_issue("POSCAR_MASK_INVALID", f"Selective mask state is {parsed.get('mask_status')}."))
    if parsed.get("fixed_indices_1based") != expected_fixed:
        errors.append(_issue("POSCAR_FIXED_MASK_MISMATCH", "POSCAR fixed indices differ from manifest."))
    if parsed.get("free_indices_1based") != expected_free:
        errors.append(_issue("POSCAR_FREE_MASK_MISMATCH", "POSCAR free indices differ from manifest."))
    tail_nonblank: list[int] = []
    tail_end = None
    if text is not None and type(expected_nions) is int:
        lines = text.splitlines()
        tail_nonblank, tail_end = _poscar_tail(lines, expected_nions)
        if tail_nonblank:
            errors.append(_issue(
                "POSCAR_NONBLANK_TAIL",
                "Only blank lines may follow the approved POSCAR coordinate rows.",
                line_numbers=tail_nonblank,
            ))
        policy = manifest.get("input_policy")
        if isinstance(policy, dict) and policy.get("allow_poscar_trailing_blank_lines") is False and tail_end < len(lines):
            errors.append(_issue("POSCAR_BLANK_TAIL_DISALLOWED", "Manifest explicitly disallows POSCAR trailing blank lines."))
    return {
        "passed": not errors,
        "errors": errors,
        "parsed": {
            "species": parsed.get("species"),
            "counts": parsed.get("counts"),
            "nions": parsed.get("nions"),
            "scale": cell_report["scale"],
            "scaled_cell": cell_report["scaled_cell"],
            "cell_comparison": cell_report["comparison"],
            "coordinate_mode": parsed.get("coordinate_mode"),
            "selective_dynamics": parsed.get("selective_dynamics"),
            "mask_status": parsed.get("mask_status"),
            "fixed_indices_1based": parsed.get("fixed_indices_1based", []),
            "free_indices_1based": parsed.get("free_indices_1based", []),
            "partial_indices_1based": parsed.get("partial_indices_1based", []),
            "trailing_nonblank_lines": tail_nonblank,
            "coordinate_identity": {
                "status": "NOT_COMPARED",
                "reason": "Coordinate values are covered by frozen input hash/geometric review, not this semantic mask gate.",
            },
        },
    }


def _incar_occurrences(text: str) -> dict[str, list[str]]:
    occurrences: dict[str, list[str]] = {}
    for raw in text.splitlines():
        code = raw.split("#", 1)[0].split("!", 1)[0].strip()
        if "=" not in code:
            continue
        key, value = code.split("=", 1)
        key = key.strip().upper()
        if re.fullmatch(r"[A-Z][A-Z0-9_+-]*", key):
            occurrences.setdefault(key, []).append(value.strip())
    return occurrences


def _is_explicit_false(raw: str) -> bool:
    tokens = raw.strip().split()
    return len(tokens) == 1 and tokens[0].lower() in {".false.", "false", "f", "0"}


def _actual_incar_value(raw: str | None, kind: str) -> Any:
    if raw is None:
        return None
    if kind == "text":
        return _normalise_text(raw)
    if kind == "float":
        return _number(raw.split()[0]) if raw.split() else None
    if kind == "int":
        return _int(raw.split()[0]) if raw.split() else None
    if kind == "bool":
        return _bool(raw)
    if kind == "vector":
        values = [_number(item) for item in raw.split()]
        return values if len(values) == 3 and all(item is not None for item in values) else None
    return None


def validate_incar(manifest: Mapping[str, Any], text: str | None, path: Path) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if text is None:
        return {"passed": False, "errors": [_issue("INCAR_MISSING", "INCAR is missing.", path=str(path))], "warnings": []}
    parsed = parse_incar(text, path)
    for message in parsed.get("errors", []):
        errors.append(_issue("INCAR_PARSE_ERROR", message, path=str(path)))
    occurrences = _incar_occurrences(text)
    for key, values in occurrences.items():
        distinct = {_normalise_text(value) for value in values}
        if len(distinct) > 1:
            errors.append(_issue(
                "INCAR_CONFLICTING_DUPLICATE",
                f"INCAR tag {key} is repeated with conflicting values.",
                tag=key,
            ))
        elif len(values) > 1:
            warnings.append(_issue(
                "INCAR_DUPLICATE_SAME_VALUE",
                f"INCAR tag {key} is repeated with the same value.",
                tag=key,
            ))
    for tag, values in occurrences.items():
        if tag in APPROVED_INCAR_TAGS:
            continue
        if tag in EXPLICIT_FALSE_TAGS and values and all(_is_explicit_false(value) for value in values):
            warnings.append(_issue(
                "INCAR_EXPLICIT_FALSE_IGNORED",
                f"INCAR tag {tag} is outside the approved set but is explicitly false.",
                tag=tag,
            ))
            continue
        if tag == "NELECT":
            errors.append(_issue(
                "INCAR_UNAPPROVED_NELECT",
                "An explicit INCAR NELECT override changes the charge model and is not approved for this gate.",
                tag=tag,
            ))
        else:
            errors.append(_issue(
                "INCAR_UNKNOWN_TAG_REVIEW",
                f"INCAR tag {tag} is outside the approved set and requires Sol review.",
                tag=tag,
            ))
    incar_spec = manifest.get("incar", {}) if isinstance(manifest, Mapping) else {}
    for manifest_key, tag in (("IDIPOL", "IDIPOL"), ("DIPOL", "DIPOL")):
        if manifest_key not in incar_spec and incar_raw(parsed, tag) is not None:
            errors.append(_issue(
                "INCAR_UNAPPROVED_DIPOL_TAG",
                f"INCAR tag {tag} is present but the manifest does not approve its value.",
                tag=tag,
            ))
    for manifest_key, (tag, kind) in INCAR_FIELDS.items():
        if manifest_key not in incar_spec:
            continue
        expected = incar_spec.get(manifest_key)
        actual_raw = incar_raw(parsed, tag)
        if expected is None:
            if actual_raw is not None:
                errors.append(_issue(
                    "INCAR_UNEXPECTED_TAG",
                    f"INCAR tag {tag} is present although the manifest requires it to be absent.",
                    tag=tag,
                ))
            continue
        actual = _actual_incar_value(actual_raw, kind)
        if actual_raw is None:
            errors.append(_issue("INCAR_MISSING_APPROVED_TAG", f"INCAR tag {tag} is missing.", tag=tag))
            continue
        if actual is None:
            errors.append(_issue("INCAR_INVALID_VALUE", f"INCAR tag {tag} cannot be parsed.", tag=tag))
            continue
        if kind == "float":
            matches = math.isclose(float(actual), float(expected), rel_tol=0.0, abs_tol=1e-10)
        elif kind == "vector":
            matches = len(actual) == len(expected) and all(
                math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-10)
                for left, right in zip(actual, expected)
            )
        elif kind == "bool":
            matches = actual is expected
        elif kind == "int":
            matches = actual == expected
        else:
            matches = actual == _normalise_text(str(expected))
        if not matches:
            errors.append(_issue(
                "INCAR_VALUE_MISMATCH",
                f"INCAR tag {tag} differs from the manifest-approved value.",
                tag=tag,
                expected=expected,
                actual=actual,
            ))
    parallel_spec = manifest.get("parallel", {}) if isinstance(manifest, Mapping) else {}
    if isinstance(parallel_spec, Mapping):
        for field, tag in (("kpar", "KPAR"), ("ncore", "NCORE")):
            if field not in parallel_spec:
                continue
            expected = parallel_spec[field]
            actual_raw = incar_raw(parsed, tag)
            if actual_raw is None:
                errors.append(_issue(
                    "INCAR_MISSING_PARALLEL_TAG",
                    f"INCAR tag {tag} is missing although parallel.{field} is declared.",
                    tag=tag,
                    field=field,
                ))
                continue
            actual = _actual_incar_value(actual_raw, "int")
            if actual is None:
                errors.append(_issue(
                    "INCAR_INVALID_PARALLEL_TAG",
                    f"INCAR tag {tag} cannot be parsed as an integer.",
                    tag=tag,
                    field=field,
                ))
            elif actual != expected:
                errors.append(_issue(
                    "INCAR_PARALLEL_VALUE_MISMATCH",
                    f"INCAR tag {tag} differs from parallel.{field}.",
                    tag=tag,
                    field=field,
                    expected=expected,
                    actual=actual,
                ))
    for feature, tags in FORBIDDEN_FEATURE_TAGS.items():
        if manifest.get("incar", {}).get(feature) is False:
            for tag in tags:
                raw = incar_raw(parsed, tag)
                if raw is not None and not (tag in EXPLICIT_FALSE_TAGS and _is_explicit_false(raw)):
                    errors.append(_issue(
                        "INCAR_FORBIDDEN_FEATURE_TAG",
                        f"INCAR tag {tag} contradicts manifest {feature}=false.",
                        tag=tag,
                    ))
    return {
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "parsed_tags": sorted(parsed.get("parameters", {}).keys()),
    }


def validate_kpoints(manifest: Mapping[str, Any], text: str | None, path: Path) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    parsed: dict[str, Any] = {}
    if text is None:
        return {"passed": False, "errors": [_issue("KPOINTS_MISSING", "KPOINTS is missing.", path=str(path))], "parsed": parsed}
    lines = text.splitlines()
    if len(lines) < 5:
        return {"passed": False, "errors": [_issue("KPOINTS_TRUNCATED", "KPOINTS has fewer than five required lines.")], "parsed": parsed}
    try:
        count = int(lines[1].strip())
    except ValueError:
        count = None
    mode = lines[2].strip().lower()
    mesh = lines[3].split()
    shift = lines[4].split()
    try:
        mesh_values = [int(value) for value in mesh]
    except ValueError:
        mesh_values = []
    shift_values = [_number(value) for value in shift]
    parsed = {"count": count, "mode": mode, "mesh": mesh_values, "shift": shift_values}
    spec = manifest.get("kpoints", {}) if isinstance(manifest, Mapping) else {}
    expected_generation = str(spec.get("generation", "")).lower()
    if expected_generation == "gamma" and not mode.startswith("g"):
        errors.append(_issue("KPOINTS_MODE_MISMATCH", "KPOINTS mode is not Gamma as declared."))
    if expected_generation in {"monkhorst", "monkhorst-pack"} and not mode.startswith("m"):
        errors.append(_issue("KPOINTS_MODE_MISMATCH", "KPOINTS mode is not Monkhorst as declared."))
    if mesh_values != spec.get("mesh"):
        errors.append(_issue("KPOINTS_MESH_MISMATCH", "KPOINTS mesh differs from manifest."))
    expected_shift = spec.get("shift")
    if len(shift_values) != 3 or any(value is None for value in shift_values):
        errors.append(_issue("KPOINTS_SHIFT_INVALID", "KPOINTS shift is not three finite numbers."))
    elif not isinstance(expected_shift, list) or len(expected_shift) != 3:
        errors.append(_issue("KPOINTS_SHIFT_SPEC_INVALID", "Manifest KPOINTS shift is not a three-number list."))
    elif not all(math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-10) for left, right in zip(shift_values, expected_shift)):
        errors.append(_issue("KPOINTS_SHIFT_MISMATCH", "KPOINTS shift differs from manifest."))
    return {"passed": not errors, "errors": errors, "parsed": parsed}


def _nelect_validation(manifest: Mapping[str, Any]) -> dict[str, Any]:
    structure = manifest.get("structure", {})
    expected = _number(structure.get("nelect"))
    paw = manifest.get("paw_identity")
    roles = structure.get("paw_order") if isinstance(structure, dict) else None
    counts = structure.get("counts") if isinstance(structure, dict) else None
    zvals: list[float] = []
    complete = isinstance(paw, dict) and isinstance(roles, list) and isinstance(counts, list)
    if complete:
        for role in roles:
            item = paw.get(role)
            if not isinstance(item, dict) or _number(item.get("ZVAL")) is None:
                complete = False
                break
            zvals.append(float(item["ZVAL"]))
    charge = manifest.get("net_charge_e")
    if complete and len(zvals) == len(counts) and _number(charge) is not None:
        calculated = sum(float(count) * zval for count, zval in zip(counts, zvals)) - float(charge)
        return {
            "status": "INDEPENDENTLY_VERIFIED",
            "declared_nelect": expected,
            "calculated_nelect": calculated,
            "matches": expected is not None and math.isclose(expected, calculated, rel_tol=0.0, abs_tol=1e-8),
            "source": "complete manifest PAW ZVAL metadata",
        }
    return {
        "status": "DECLARED_ONLY",
        "declared_nelect": expected,
        "independently_verified": False,
        "reason": "Complete ordered PAW ZVAL metadata and explicit charge were not supplied in input_manifest.",
    }


def validate_inputs(manifest_path: Path | str, input_dir: Path | str | None = None) -> dict[str, Any]:
    manifest_file = Path(manifest_path).resolve()
    root = Path(input_dir).resolve() if input_dir is not None else manifest_file.parent
    manifest, manifest_errors = _load_manifest(manifest_file)
    base: dict[str, Any] = {
        "schema": SCHEMA,
        "mode": "input",
        "manifest_path": str(manifest_file),
        "input_dir": str(root),
        "passed": False,
        "supported_task_form": False,
        "errors": list(manifest_errors),
        "warnings": [],
        "observations": {},
        "scientific_acceptance": dict(SCIENTIFIC_REVIEW),
    }
    if manifest is None:
        return base
    support_errors = _manifest_support_errors(manifest)
    base["errors"].extend(support_errors)
    base["supported_task_form"] = not support_errors
    poscar_text, poscar_error = _read_text(root / "POSCAR")
    incar_text, incar_error = _read_text(root / "INCAR")
    kpoints_text, kpoints_error = _read_text(root / "KPOINTS")
    for error in (poscar_error, incar_error, kpoints_error):
        if error:
            base["errors"].append(error)
    poscar_report = validate_poscar(manifest, poscar_text, root / "POSCAR")
    incar_report = validate_incar(manifest, incar_text, root / "INCAR")
    kpoints_report = validate_kpoints(manifest, kpoints_text, root / "KPOINTS")
    base["errors"].extend(poscar_report["errors"])
    base["errors"].extend(incar_report["errors"])
    base["errors"].extend(kpoints_report["errors"])
    base["warnings"].extend(incar_report.get("warnings", []))
    base["observations"].update({
        "unit_id": manifest.get("unit_id"),
        "poscar": poscar_report["parsed"],
        "incar": {"parsed_tags": incar_report.get("parsed_tags", [])},
        "kpoints": kpoints_report["parsed"],
        "nelect": _nelect_validation(manifest),
    })
    restart = manifest.get("restart")
    restart = restart if isinstance(restart, Mapping) else {}
    warm_restart = restart.get("mode") == WARM_RESTART_MODE
    restart_source = restart.get("source") if isinstance(restart.get("source"), Mapping) else {}
    restart_observation: dict[str, Any] = {
        "mode": restart.get("mode", "fresh" if restart.get("fresh") is True else "UNKNOWN"),
        "target_preflight_state": restart_source.get("remote_preflight_state"),
        "local_restart_files": "NOT_DOWNLOADED" if warm_restart else "NOT_REQUIRED",
        "required_restart_files": sorted(restart_source.get("files", {})) if warm_restart and isinstance(restart_source.get("files"), Mapping) else [],
        "source_file_contents_read": False,
    }
    if warm_restart:
        expected_geometry_hash = _manifest_input_hash(manifest, "POSCAR")
        try:
            actual_geometry_hash = hashlib.sha256((root / "POSCAR").read_bytes()).hexdigest()
        except OSError as error:
            actual_geometry_hash = None
            base["errors"].append(_issue(
                "WARM_RESTART_TARGET_GEOMETRY_UNREADABLE",
                "Could not hash the local POSCAR required for warm-restart geometry identity.",
                error_type=type(error).__name__,
                error=str(error),
            ))
        restart_observation["target_poscar_sha256"] = actual_geometry_hash
        restart_observation["declared_target_poscar_sha256"] = expected_geometry_hash
        if expected_geometry_hash is not None and actual_geometry_hash is not None and actual_geometry_hash.lower() != expected_geometry_hash.lower():
            base["errors"].append(_issue(
                "WARM_RESTART_TARGET_GEOMETRY_MISMATCH",
                "Local POSCAR bytes do not match the target geometry hash bound to the restart source.",
                expected=expected_geometry_hash,
                actual=actual_geometry_hash,
            ))
        geometry_source = manifest.get("source")
        declared_source_hash = geometry_source.get("parent_sha256") if isinstance(geometry_source, Mapping) else None
        if (
            isinstance(declared_source_hash, str)
            and actual_geometry_hash is not None
            and declared_source_hash.lower() != actual_geometry_hash.lower()
        ):
            base["errors"].append(_issue(
                "GEOMETRY_SOURCE_ACTUAL_POSCAR_MISMATCH",
                "Top-level source geometry SHA-256 does not match the actual local POSCAR bytes.",
                declared=declared_source_hash,
                actual=actual_geometry_hash,
            ))
    base["observations"]["restart"] = restart_observation
    base["passed"] = not base["errors"]
    return base


def _load_requirements(
    manifest: Mapping[str, Any],
    requirements: Mapping[str, Any] | Path | str | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if requirements is None:
        candidate = manifest.get("output_requirements")
        if candidate is None and isinstance(manifest.get("outputs"), dict):
            possible = manifest["outputs"].get("requirements")
            if isinstance(possible, dict):
                candidate = possible
        requirements = candidate
    if isinstance(requirements, (str, Path)):
        path = Path(requirements).resolve()
        data, errors = _load_manifest(path)
        if data is None:
            return None, errors
        requirements = data
    if not isinstance(requirements, Mapping):
        return None, [_issue(
            "OUTPUT_REQUIREMENTS_UNDECLARED",
            "Postcheck requires explicit task output requirements; no defaults are inferred.",
        )]
    result = dict(requirements)
    errors: list[dict[str, Any]] = []
    kind = result.get("kind")
    if kind not in {"static", "relaxation"}:
        errors.append(_issue("OUTPUT_KIND_UNDECLARED", "Output requirements kind must be static or relaxation."))
    required = result.get("required_files")
    nonempty = result.get("nonempty_files")
    optional = result.get("optional_files", [])
    for field, values in (("required_files", required), ("nonempty_files", nonempty), ("optional_files", optional)):
        if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
            errors.append(_issue("OUTPUT_FILE_SPEC_INVALID", f"{field} must be a string list.", field=field))
            continue
        for value in values:
            if Path(value).name != value or value in {".", ".."}:
                errors.append(_issue("OUTPUT_PATH_UNSAFE", f"{field} contains a non-basename path.", field=field, value=value))
    if isinstance(required, list) and isinstance(nonempty, list) and not set(nonempty).issubset(set(required)):
        errors.append(_issue("OUTPUT_NONEMPTY_NOT_REQUIRED", "nonempty_files must be a subset of required_files."))
    markers = result.get("markers")
    if not isinstance(markers, list) or not markers:
        errors.append(_issue("OUTPUT_MARKERS_UNDECLARED", "Postcheck requires explicit marker patterns."))
        markers = []
    marker_categories = set()
    for marker in markers:
        if not isinstance(marker, dict):
            errors.append(_issue("OUTPUT_MARKER_INVALID", "Each output marker must be an object."))
            continue
        if not all(isinstance(marker.get(key), str) and marker.get(key) for key in ("file", "name", "pattern", "category")):
            errors.append(_issue("OUTPUT_MARKER_INVALID", "Each marker requires file/name/pattern/category."))
            continue
        try:
            re.compile(marker["pattern"])
        except re.error as error:
            errors.append(_issue(
                "OUTPUT_MARKER_REGEX_INVALID",
                "Output marker pattern is not a valid regular expression.",
                name=marker["name"],
                error=str(error),
            ))
        if marker["category"] not in {"program", "electronic", "ionic", "diagnostic"}:
            errors.append(_issue("OUTPUT_MARKER_CATEGORY_INVALID", "Marker category is unsupported.", category=marker["category"]))
        if "required" in marker and type(marker["required"]) is not bool:
            errors.append(_issue("OUTPUT_MARKER_REQUIRED_INVALID", "Marker required must be an explicit boolean."))
        if marker.get("required") is False and marker["category"] not in {"ionic", "diagnostic"}:
            errors.append(_issue("OUTPUT_REQUIRED_MARKER_WEAKENED", "Program/electronic completion markers cannot be optional."))
        marker_categories.add(marker["category"])
        if Path(marker["file"]).name != marker["file"] or marker["file"] in {".", ".."}:
            errors.append(_issue(
                "OUTPUT_PATH_UNSAFE",
                "markers contains a non-basename file path.",
                field="markers",
                value=marker["file"],
            ))
        if isinstance(required, list) and marker["file"] not in required:
            errors.append(_issue("OUTPUT_MARKER_FILE_NOT_REQUIRED", "Marker file must be in required_files.", file=marker["file"]))
    if kind == "relaxation" and "ionic" not in marker_categories:
        errors.append(_issue("OUTPUT_IONIC_MARKER_UNDECLARED", "Relaxation requirements must declare an ionic marker."))
    identity_fields = result.get("identity_fields", [])
    if not isinstance(identity_fields, list) or not all(isinstance(value, str) for value in identity_fields):
        errors.append(_issue("OUTPUT_IDENTITY_SPEC_INVALID", "identity_fields must be a string list."))
    result["markers"] = markers
    return result, errors


def _safe_stat(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"present": False, "bytes": None}
    except OSError as error:
        return {"present": None, "bytes": None, "error": f"{type(error).__name__}: {error}"}
    return {"present": True, "bytes": stat.st_size}


def _restart_file_stat(path: Path) -> dict[str, Any]:
    """Return filesystem metadata only; never open a restart file."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"present": False, "path": str(path), "content_read": False}
    except OSError as error:
        return {
            "present": None,
            "path": str(path),
            "content_read": False,
            "error": f"{type(error).__name__}: {error}",
        }
    return {
        "present": True,
        "path": str(path),
        "content_read": False,
        "bytes": info.st_size,
        "regular_file": stat.S_ISREG(info.st_mode),
        "symlink": stat.S_ISLNK(info.st_mode),
        "hardlink_count": getattr(info, "st_nlink", None),
    }


def _manifest_input_hash(manifest: Mapping[str, Any], name: str) -> str | None:
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping):
        return None
    item = outputs.get(name)
    if not isinstance(item, Mapping):
        return None
    digest = item.get("sha256")
    return digest.lower() if isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{64}", digest) else None


def _input_hashes(
    manifest: Mapping[str, Any],
    root: Path,
    role: str,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    hashes: dict[str, str] = {}
    errors: list[dict[str, Any]] = []
    for name in INPUT_FILES:
        path = root / name
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except FileNotFoundError:
            continue
        except OSError as error:
            errors.append(_issue(
                "INPUT_HASH_UNAVAILABLE",
                f"Could not hash {role} input {name}.",
                role=role,
                file=name,
                error_type=type(error).__name__,
                error=str(error),
            ))
            continue
        hashes[name] = digest
        expected = _manifest_input_hash(manifest, name)
        if expected is not None and digest.lower() != expected:
            errors.append(_issue(
                "INPUT_MANIFEST_HASH_MISMATCH",
                f"{role} input {name} differs from the frozen manifest hash.",
                role=role,
                file=name,
                expected=expected,
                actual=digest,
            ))
    return hashes, errors


def preflight_inputs(
    manifest_path: Path | str,
    input_dir: Path | str | None = None,
    case_dir: Path | str | None = None,
    requirements: Mapping[str, Any] | Path | str | None = None,
) -> dict[str, Any]:
    input_report = validate_inputs(manifest_path, input_dir)
    manifest, manifest_errors = _load_manifest(Path(manifest_path).resolve())
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "mode": "preflight",
        "passed": input_report["passed"],
        "input": input_report,
        "case_input": None,
        "case_guard": {
            "checked": case_dir is not None,
            "errors": [],
            "restart_files": [],
            "restart_file_observations": [],
        },
        "program_exit": {"status": "NOT_RUN", "raw": None},
        "evidence": {"status": "NOT_EVALUATED", "reason": "No VASP process was run by preflight."},
        "convergence": {"status": "NOT_EVALUATED", "reason": "No VASP output was evaluated by preflight."},
        "scientific_acceptance": dict(SCIENTIFIC_REVIEW),
    }
    if manifest is None:
        result["passed"] = False
        result["case_guard"]["errors"].extend(manifest_errors)
        return result
    restart = manifest.get("restart")
    restart = restart if isinstance(restart, Mapping) else {}
    restart_source = restart.get("source") if isinstance(restart.get("source"), Mapping) else {}
    warm_restart = restart.get("mode") == WARM_RESTART_MODE
    result["restart"] = {
        "mode": restart.get("mode", "fresh" if restart.get("fresh") is True else "UNKNOWN"),
        "target_preflight_state": restart_source.get("remote_preflight_state"),
        "local_restart_files": "NOT_DOWNLOADED" if warm_restart else "NOT_REQUIRED",
        "case_files_stat_validated": False,
        "content_read": False,
        "scientific_acceptance": "NOT_EVALUATED",
    }
    if case_dir is not None:
        case = Path(case_dir).resolve()
        if not case.is_dir():
            result["case_guard"]["errors"].append(_issue("CASE_MISSING", "Runner case directory is missing.", path=str(case)))
        else:
            case_input = validate_inputs(manifest_path, case)
            result["case_input"] = case_input
            if not case_input["passed"]:
                result["case_guard"]["errors"].append(_issue(
                    "CASE_INPUT_INVALID",
                    "Runner case POSCAR/INCAR/KPOINTS do not pass the same input gate.",
                    path=str(case),
                ))
            source_root = Path(input_report["input_dir"])
            source_hashes, source_hash_errors = _input_hashes(manifest, source_root, "source")
            case_hashes, case_hash_errors = _input_hashes(manifest, case, "case")
            result["case_guard"]["source_input_hashes"] = source_hashes
            result["case_guard"]["case_input_hashes"] = case_hashes
            result["case_guard"]["errors"].extend(source_hash_errors)
            result["case_guard"]["errors"].extend(case_hash_errors)
            for name in INPUT_FILES:
                source_digest = source_hashes.get(name)
                case_digest = case_hashes.get(name)
                if source_digest is not None and case_digest is not None and source_digest != case_digest:
                    result["case_guard"]["errors"].append(_issue(
                        "CASE_INPUT_IDENTITY_MISMATCH",
                        f"Case input {name} differs from the source input by frozen-file identity.",
                        file=name,
                        source_sha256=source_digest,
                        case_sha256=case_digest,
                    ))
            if (case / ".run_once").exists():
                result["case_guard"]["errors"].append(_issue("RUN_LOCK_PRESENT", "Single-run lock already exists.", path=str(case / ".run_once")))
            if restart.get("fresh") is True:
                for name in STANDARD_RESTART_FILES:
                    if (case / name).exists():
                        result["case_guard"]["restart_files"].append(name)
                        result["case_guard"]["errors"].append(_issue(
                            "FRESH_RESTART_PRESENT",
                            f"Fresh preflight found restart file {name}.",
                            file=name,
                        ))
            elif restart.get("mode") == WARM_RESTART_MODE:
                required_restart_files = ("WAVECAR", "CHGCAR")
                result["restart"]["required_case_files"] = list(required_restart_files)
                for name in required_restart_files:
                    observation = _restart_file_stat(case / name)
                    observation["file"] = name
                    result["case_guard"]["restart_file_observations"].append(observation)
                    if observation.get("present") is True:
                        result["case_guard"]["restart_files"].append(name)
                    if observation.get("present") is not True:
                        result["case_guard"]["errors"].append(_issue(
                            "WARM_RESTART_FILE_MISSING",
                            f"Warm restart preflight requires a readable directory entry for {name}.",
                            file=name,
                            observation=observation,
                        ))
                    elif observation.get("symlink") is True or observation.get("regular_file") is not True:
                        result["case_guard"]["errors"].append(_issue(
                            "WARM_RESTART_FILE_NOT_REGULAR",
                            f"Warm restart file {name} must be a regular non-symlink file.",
                            file=name,
                            observation=observation,
                        ))
                    elif type(observation.get("bytes")) is not int or observation.get("bytes", 0) <= 0:
                        result["case_guard"]["errors"].append(_issue(
                            "WARM_RESTART_FILE_EMPTY",
                            f"Warm restart file {name} must be non-empty.",
                            file=name,
                            observation=observation,
                        ))
                    if observation.get("present") is True and observation.get("hardlink_count") != 1:
                        result["case_guard"]["errors"].append(_issue(
                            "WARM_RESTART_HARDLINK_UNSAFE",
                            f"Warm restart file {name} must be an independent file with link count 1.",
                            file=name,
                            observation=observation,
                        ))
                undeclared_restart_files = []
                for name in (*STANDARD_RESTART_FILES, "WAVECAR.tmp", "CHGCAR.tmp"):
                    if name not in required_restart_files and (case / name).exists():
                        undeclared_restart_files.append(name)
                if undeclared_restart_files:
                    result["case_guard"]["errors"].append(_issue(
                        "UNDECLARED_RESTART_FILE_PRESENT",
                        "Warm restart case contains undeclared restart artifacts.",
                        files=undeclared_restart_files,
                    ))
                result["restart"]["case_files_stat_validated"] = not any(
                    item.get("code", "").startswith("WARM_RESTART_") or item.get("code") == "UNDECLARED_RESTART_FILE_PRESENT"
                    for item in result["case_guard"]["errors"]
                )
            existing_outputs = [name for name in STANDARD_OUTPUT_FILES if (case / name).exists()]
            if existing_outputs:
                result["case_guard"]["errors"].append(_issue(
                    "OUTPUT_OVERWRITE_GUARD",
                    "Runner case already contains output files; an execution cannot overwrite them.",
                    files=existing_outputs,
                ))
    requirements_data, requirement_errors = _load_requirements(manifest, requirements)
    if requirements is not None or manifest.get("output_requirements") is not None:
        result["output_requirements"] = requirements_data
        result["case_guard"]["errors"].extend(requirement_errors)
        if requirement_errors:
            result["passed"] = False
            return result
    result["passed"] = result["passed"] and not result["case_guard"]["errors"]
    return result


def _manifest_expected_identity(manifest: Mapping[str, Any], requirements: Mapping[str, Any]) -> dict[str, Any]:
    structure = manifest.get("structure", {})
    incar = manifest.get("incar", {})
    parallel = manifest.get("parallel", {})
    expected: dict[str, Any] = {}
    if isinstance(structure, dict):
        for key in ("nions", "nelect", "counts"):
            if key in structure:
                expected[{"nions": "NIONS", "nelect": "NELECT", "counts": "IONS_PER_TYPE"}[key]] = structure[key]
    if isinstance(incar, dict):
        for key in ("NBANDS", "ISPIN", "KPAR", "NCORE"):
            if key in incar:
                expected[key] = incar[key]
    if isinstance(parallel, dict) and "mpi_ranks" in parallel:
        expected["MPI_RANKS"] = parallel["mpi_ranks"]
    if isinstance(parallel, dict):
        if "kpar" in parallel:
            expected["KPAR"] = parallel["kpar"]
        if "ncore" in parallel:
            expected["NCORE"] = parallel["ncore"]
    explicit = requirements.get("expected_identity")
    if isinstance(explicit, dict):
        expected.update(explicit)
    return expected


def _outcar_identity(outcar_data: Mapping[str, Any], text: str) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    for key, item in outcar_data.get("identity", {}).items():
        if isinstance(item, dict) and "value" in item:
            identity[key] = item["value"]
    # The startup banner owns the executable version. GGA_COMPAT explanatory
    # text also mentions old VASP versions and must never replace this value.
    version = re.search(r"^\s*vasp\.(\d+\.\d+(?:\.\d+)?)(?:\s|$)", text, re.IGNORECASE | re.MULTILINE)
    identity["VASP_VERSION"] = version.group(1) if version else None
    match = re.search(r"\bISPIN\s*=\s*(\d+)", text, re.IGNORECASE)
    identity["ISPIN"] = int(match.group(1)) if match else None
    parameters = outcar_data.get("parameters", {})
    fields = parameters.get("fields", {}) if isinstance(parameters, Mapping) else {}
    field = fields.get("NUPDOWN", {}) if isinstance(fields, Mapping) else {}
    observations = field.get("observations", []) if isinstance(field, Mapping) else []
    effective = [
        item.get("value")
        for item in observations
        if isinstance(item, Mapping) and item.get("section") == "effective_parameter"
    ]
    identity["NUPDOWN"] = (
        effective[0]
        if effective and type(effective[0]) is int and all(value == effective[0] for value in effective)
        else None
    )
    return identity


def _marker_result(
    requirements: Mapping[str, Any],
    case: Path,
    texts: Mapping[str, str],
    evidence_complete: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    categories: dict[str, list[bool]] = {}
    for spec in requirements.get("markers", []):
        file_name = spec["file"]
        text = texts.get(file_name, "")
        try:
            count = len(re.findall(spec["pattern"], text, flags=re.IGNORECASE | re.MULTILINE))
        except re.error as error:
            markers.append({
                "name": spec["name"],
                "file": file_name,
                "category": spec["category"],
                "observed": False,
                "count": 0,
                "error": f"invalid regex: {error}",
            })
            categories.setdefault(spec["category"], []).append(False)
            continue
        observed = count > 0
        markers.append({
            "name": spec["name"],
            "file": file_name,
            "category": spec["category"],
            "observed": observed,
            "count": count,
            "required": spec.get("required", True),
        })
        categories.setdefault(spec["category"], []).append(observed)
    history: dict[str, Any] = {}
    for category, label in (("electronic", "electronic_scf"), ("ionic", "ionic_convergence"), ("program", "program_end")):
        if category == "ionic" and requirements.get("kind") == "static":
            history[label] = "NOT_APPLICABLE"
            continue
        states = categories.get(category, [])
        if evidence_complete:
            history[label] = "OBSERVED" if states and all(states) else "NOT_OBSERVED"
        else:
            history[label] = "NOT_EVALUATED"
    return markers, history


def postcheck(
    manifest_path: Path | str,
    input_dir: Path | str | None,
    case_dir: Path | str,
    requirements: Mapping[str, Any] | Path | str | None = None,
) -> dict[str, Any]:
    manifest_file = Path(manifest_path).resolve()
    case = Path(case_dir).resolve()
    input_report = validate_inputs(manifest_file, input_dir)
    manifest, manifest_errors = _load_manifest(manifest_file)
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "mode": "postcheck",
        "passed": False,
        "input": input_report,
        "output_requirements": None,
        "program_exit": {"status": "UNKNOWN", "raw": None, "reason": "run_timing.txt is unavailable."},
        "evidence": {
            "status": "INCOMPLETE",
            "required_files": [],
            "missing_files": [],
            "empty_files": [],
            "parser_status": "NOT_RUN",
            "check_errors": [],
        },
        "convergence": {
            "electronic_scf": "NOT_EVALUATED",
            "ionic_convergence": "NOT_EVALUATED",
            "program_end": "NOT_EVALUATED",
            "reason": "Final-step SCF/ionic convergence is not inferred from historical markers.",
        },
        "scientific_acceptance": dict(SCIENTIFIC_REVIEW),
        "parameter_comparison": {
            "schema": "vasp-parameter-compare/v1",
            "status": "UNKNOWN",
            "reason": "approved manifest, actual INCAR, or effective OUTCAR evidence is unavailable",
        },
        "check_errors": list(manifest_errors),
    }
    if manifest is None:
        return result
    requirements_data, requirement_errors = _load_requirements(manifest, requirements)
    result["output_requirements"] = requirements_data
    result["check_errors"].extend(requirement_errors)
    if requirement_errors or requirements_data is None:
        return result
    required = list(requirements_data.get("required_files", []))
    nonempty = set(requirements_data.get("nonempty_files", []))
    result["evidence"]["required_files"] = required
    texts: dict[str, str] = {}
    for name in required:
        path = case / name
        info = _safe_stat(path)
        if not info.get("present"):
            result["evidence"]["missing_files"].append(name)
            continue
        if name in nonempty and info.get("bytes") == 0:
            result["evidence"]["empty_files"].append(name)
            continue
        text, error = _read_text(path)
        if error:
            result["evidence"]["check_errors"].append(error)
        elif text is not None:
            texts[name] = text
    timing_path = case / "run_timing.txt"
    timing_text, timing_error = _read_text(timing_path)
    if timing_text is not None:
        timing = parse_timing(timing_text, timing_path)
        raw = timing.get("exit_code", {}).get("value")
        if isinstance(raw, int):
            result["program_exit"] = {
                "status": "ZERO_EXIT" if raw == 0 else "NONZERO_EXIT",
                "raw": raw,
                "source": str(timing_path),
            }
        else:
            result["program_exit"] = {
                "status": "UNKNOWN",
                "raw": None,
                "reason": "run_timing.txt has no parseable exit_code.",
                "source": str(timing_path),
            }
            result["check_errors"].append(_issue("EXIT_CODE_UNAVAILABLE", "run_timing.txt has no parseable exit_code."))
    elif timing_error:
        result["evidence"]["check_errors"].append(timing_error)
        result["check_errors"].append(timing_error)

    missing_or_empty = bool(result["evidence"]["missing_files"] or result["evidence"]["empty_files"])
    result["evidence"]["status"] = "COMPLETE" if not missing_or_empty and not result["evidence"]["check_errors"] else "INCOMPLETE"
    outcar_text = texts.get("OUTCAR")
    outcar_data: dict[str, Any] = {}
    if outcar_text is not None:
        try:
            outcar_data = parse_outcar(outcar_text, case / "OUTCAR", input_report.get("observations", {}).get("poscar", {}).get("nions"))
            result["evidence"]["parser_status"] = "OK"
            for message in outcar_data.get("errors", []):
                parser_issue = _issue(
                    "OUTCAR_PARSE_ERROR",
                    str(message),
                    path=str(case / "OUTCAR"),
                )
                result["evidence"]["check_errors"].append(parser_issue)
                result["check_errors"].append(parser_issue)
            if outcar_data.get("errors"):
                result["evidence"]["status"] = "INCOMPLETE"
        except Exception as error:
            result["evidence"]["parser_status"] = "ERROR"
            result["check_errors"].append(_issue(
                "OUTCAR_PARSER_EXCEPTION",
                "OUTCAR parser raised an exception; this is a checker error, not a VASP failure.",
                error_type=type(error).__name__,
                error=str(error),
            ))
    else:
        result["evidence"]["parser_status"] = "NOT_RUN"
    actual_incar = parse_incar(texts.get("INCAR"), case / "INCAR")
    result["parameter_comparison"] = compare_parameter_sources(
        manifest,
        actual_incar,
        outcar_data.get("parameters") if isinstance(outcar_data, dict) else None,
        manifest_source=str(manifest_file),
    )
    evidence_complete = result["evidence"]["status"] == "COMPLETE" and result["evidence"]["parser_status"] != "ERROR"
    marker_results, marker_history = _marker_result(requirements_data, case, texts, evidence_complete)
    result["markers"] = marker_results
    result["marker_observation"] = {
        "status": "HISTORICAL_ONLY" if evidence_complete else "INCOMPLETE",
        "history": marker_history,
        "reason": "Marker matches are observations anywhere in the captured files; they are not final-step convergence claims.",
    }
    identity = _outcar_identity(outcar_data, outcar_text or "")
    expected_identity = _manifest_expected_identity(manifest, requirements_data)
    identity_checks: list[dict[str, Any]] = []
    for field in requirements_data.get("identity_fields", []):
        if field not in expected_identity:
            result["check_errors"].append(_issue(
                "IDENTITY_EXPECTATION_UNDECLARED",
                f"No expected value is declared for output identity field {field}; no guess is made.",
                field=field,
            ))
            identity_checks.append({"field": field, "status": "UNDECLARED"})
            continue
        actual = identity.get(field)
        expected = expected_identity[field]
        matches = actual == expected
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            matches = math.isclose(float(actual), float(expected), rel_tol=0.0, abs_tol=1e-8)
        identity_checks.append({"field": field, "expected": expected, "actual": actual, "matches": matches})
        if not matches:
            result["check_errors"].append(_issue(
                "OUTPUT_IDENTITY_MISMATCH",
                f"Output identity field {field} differs from the manifest/requirements.",
                field=field,
                expected=expected,
                actual=actual,
            ))
    result["identity"] = {"expected": expected_identity, "actual": identity, "checks": identity_checks}
    stderr_info = _safe_stat(case / "vasp.stderr")
    result["stderr"] = {
        "present": stderr_info.get("present"),
        "bytes": stderr_info.get("bytes"),
        "empty_allowed": True,
        "affects_pass": False,
    }
    marker_ok = evidence_complete and bool(marker_results) and all(
        item.get("required", True) is False or item.get("observed") for item in marker_results
    )
    identity_ok = all(item.get("matches") is True for item in identity_checks)
    no_checker_errors = not result["check_errors"] and not result["evidence"]["check_errors"]
    result["passed"] = (
        input_report["passed"]
        and result["program_exit"].get("raw") == 0
        and evidence_complete
        and marker_ok
        and identity_ok
        and no_checker_errors
    )
    result["pass_semantics"] = "MECHANICAL_EVIDENCE_ONLY"
    return result


def execute_once(
    manifest_path: Path | str,
    input_dir: Path | str | None,
    case_dir: Path | str,
    launcher_argv: Sequence[str],
    requirements: Mapping[str, Any] | Path | str | None = None,
    launcher: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Existing-runner integration seam: gate first, invoke an injected launcher once.

    The caller owns the single-run lock, tmux target, environment and timing log.
    This helper never supplies a default launcher and never creates a second
    production execution path.
    """
    if not launcher_argv or not all(isinstance(value, str) and value for value in launcher_argv):
        raise ValueError("launcher_argv must be a non-empty string sequence")
    if launcher is None:
        raise ValueError("launcher must be explicitly injected by the existing runner")
    preflight = preflight_inputs(manifest_path, input_dir, case_dir, requirements)
    if not preflight["passed"]:
        return {
            "schema": SCHEMA,
            "mode": "execute",
            "passed": False,
            "launcher_called": False,
            "preflight": preflight,
            "program_exit": {"status": "NOT_RUN", "raw": None},
            "evidence": {"status": "NOT_EVALUATED"},
            "convergence": {"status": "NOT_EVALUATED"},
            "scientific_acceptance": dict(SCIENTIFIC_REVIEW),
        }
    try:
        completed = launcher(
            list(launcher_argv),
            cwd=str(Path(case_dir).resolve()),
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception as error:
        return {
            "schema": SCHEMA,
            "mode": "execute",
            "passed": False,
            "launcher_called": True,
            "preflight": preflight,
            "program_exit": {
                "status": "LAUNCHER_EXCEPTION",
                "raw": None,
                "error_type": type(error).__name__,
                "error": str(error),
            },
            "evidence": {"status": "NOT_EVALUATED"},
            "convergence": {"status": "NOT_EVALUATED"},
            "check_errors": [_issue(
                "LAUNCHER_EXCEPTION",
                "Launcher raised an exception; this is not converted to a fabricated VASP exit code.",
                error_type=type(error).__name__,
                error=str(error),
            )],
            "scientific_acceptance": dict(SCIENTIFIC_REVIEW),
        }
    raw_exit = getattr(completed, "returncode", None)
    post = postcheck(manifest_path, input_dir, case_dir, requirements)
    timing_exit = post["program_exit"].get("raw")
    if timing_exit is not None and raw_exit != timing_exit:
        post.setdefault("check_errors", []).append(_issue(
            "EXIT_CODE_MISMATCH",
            "Launcher return code differs from run_timing.txt; raw launcher code is preserved.",
            launcher_returncode=raw_exit,
            timing_returncode=timing_exit,
        ))
        post["passed"] = False
    if isinstance(raw_exit, int):
        post["program_exit"]["launcher_raw"] = raw_exit
        post["program_exit"]["raw"] = raw_exit
        post["program_exit"]["status"] = "ZERO_EXIT" if raw_exit == 0 else "NONZERO_EXIT"
    else:
        post.setdefault("check_errors", []).append(_issue(
            "LAUNCHER_EXIT_UNAVAILABLE",
            "Launcher returned no integer returncode.",
        ))
        post["passed"] = False
    return {
        "schema": SCHEMA,
        "mode": "execute",
        "passed": bool(post["passed"] and isinstance(raw_exit, int) and raw_exit == 0),
        "launcher_called": True,
        "launcher_argv": list(launcher_argv),
        "launcher_stdout_bytes": len(getattr(completed, "stdout", "") or ""),
        "launcher_stderr_bytes": len(getattr(completed, "stderr", "") or ""),
        "preflight": preflight,
        "postcheck": post,
        "program_exit": post["program_exit"],
        "evidence": post["evidence"],
        "convergence": post["convergence"],
        "scientific_acceptance": dict(SCIENTIFIC_REVIEW),
    }


def _cli_exit(result: Mapping[str, Any]) -> int:
    program = result.get("program_exit", {})
    raw = program.get("raw") if isinstance(program, Mapping) else None
    if isinstance(raw, int) and raw != 0:
        return raw if 1 <= raw <= 125 else 1
    if result.get("passed"):
        return 0
    errors = result.get("check_errors", [])
    if isinstance(errors, list) and any(
        isinstance(item, Mapping) and "PARSER_EXCEPTION" in str(item.get("code"))
        for item in errors
    ):
        return 4
    return 2 if result.get("mode") == "preflight" or result.get("mode") == "input" else 3


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("preflight", "postcheck"):
        sub = subparsers.add_parser(mode)
        sub.add_argument("--manifest", required=True, type=Path)
        sub.add_argument("--input-dir", type=Path)
        sub.add_argument("--case-dir", required=(mode == "postcheck"), type=Path)
        sub.add_argument("--requirements", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.mode == "preflight":
            result = preflight_inputs(args.manifest, args.input_dir, args.case_dir, args.requirements)
        elif args.mode == "postcheck":
            result = postcheck(args.manifest, args.input_dir, args.case_dir, args.requirements)
    except Exception as error:
        result = {
            "schema": SCHEMA,
            "mode": args.mode,
            "passed": False,
            "check_errors": [_issue(
                "CHECKER_EXCEPTION",
                "Checker raised an exception; no VASP failure is inferred.",
                error_type=type(error).__name__,
                error=str(error),
            )],
            "scientific_acceptance": dict(SCIENTIFIC_REVIEW),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return _cli_exit(result)


if __name__ == "__main__":
    raise SystemExit(main())

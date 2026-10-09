#!/usr/bin/env python3
"""Deterministically render a narrow, local VASP input candidate.

The generator consumes an approved input_manifest as its only source of
scientific values.  It never connects to SSH, reads or creates POTCAR, starts
VASP, invokes a launcher, or overwrites a non-empty destination.  Independent
validation is performed by vasp_executor.py in staging before publication.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import shutil
import sys
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

try:
    from vasp_executor import (
        WARM_RESTART_MODE,
        performance_diagnostic_spec_errors,
        validate_inputs,
        validate_restart_spec,
    )
except ImportError:
    from .vasp_executor import (
        WARM_RESTART_MODE,
        performance_diagnostic_spec_errors,
        validate_inputs,
        validate_restart_spec,
    )


SCHEMA = "vasp-input-generator/v1"
INPUT_FILES = ("POSCAR", "INCAR", "KPOINTS")
OPTIONAL_COPIED_FILES = ("atom_mapping.csv", "paw_identity.json")
FEATURE_KEYS = ("external_field", "soc", "dispersion", "projection_output")

# Renderer and executor use one field contract; values still come from the manifest.
try:
    from vasp_contracts import INCAR_RENDER_FIELDS as INCAR_FIELDS
except ImportError:
    if not __package__: raise
    from .vasp_contracts import INCAR_RENDER_FIELDS as INCAR_FIELDS
INCAR_KEYS = frozenset(name for name, _, _ in INCAR_FIELDS) | {"SYSTEM"}
REQUIRED_INCAR_KEYS = frozenset(name for name, _, _ in INCAR_FIELDS)
PARALLEL_KEYS = frozenset({"mpi_ranks", "kpar", "ncore", "omp_num_threads"})
STRUCTURE_KEYS = frozenset(
    {
        "species_order",
        "paw_order",
        "counts",
        "nions",
        "nelect",
        "cell_A",
        "fixed_global_indices",
        "free_global_count",
    }
)
KPOINT_KEYS = frozenset({"generation", "mesh", "shift"})
INPUT_POLICY_KEYS = frozenset({"allow_poscar_trailing_blank_lines"})


class GeneratorError(RuntimeError):
    """A deterministic, user-actionable generator failure."""

    def __init__(self, code: str, message: str, **fields: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = fields

    def as_dict(self) -> dict[str, Any]:
        result = {"code": self.code, "message": self.message}
        result.update(self.fields)
        return result


def _issue(code: str, message: str, **fields: Any) -> dict[str, Any]:
    result = {"code": code, "message": message}
    result.update(fields)
    return result


def _number(value: Any) -> float | None:
    try:
        parsed = float(str(value).replace("D", "E").replace("d", "e"))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _normalise_text(value: Any) -> str:
    return " ".join(str(value).strip().split()).upper()


def _format_number(value: Any) -> str:
    number = _number(value)
    if number is None:
        raise GeneratorError("INVALID_NUMBER", "A finite number was required.", value=value)
    if number == 0:
        return "0"
    rendered = format(number, ".15g")
    if "e" in rendered:
        mantissa, exponent = rendered.split("e", 1)
        rendered = mantissa + "E" + exponent.lstrip("+0") if exponent.startswith("+") else mantissa + "E" + exponent.lstrip("0") if exponent.startswith("-") else mantissa + "E" + exponent.lstrip("0")
        if rendered.endswith("E-"):
            rendered += "0"
    return rendered


def _format_value(value: Any, kind: str) -> str:
    if kind == "text":
        rendered = str(value).strip()
        if not rendered or "\n" in rendered or "\r" in rendered:
            raise GeneratorError("INVALID_TEXT_VALUE", "Text value must be a single non-empty line.", value=value)
        return rendered
    if kind == "float":
        return _format_number(value)
    if kind == "int":
        if type(value) is not int:
            raise GeneratorError("INVALID_INTEGER_VALUE", "An integer value was required.", value=value)
        return str(value)
    if kind == "bool":
        if type(value) is not bool:
            raise GeneratorError("INVALID_BOOLEAN_VALUE", "A boolean value was required.", value=value)
        return ".TRUE." if value else ".FALSE."
    if kind == "lreal":
        if type(value) is bool:
            return ".TRUE." if value else ".FALSE."
        if value == "Auto":
            return "Auto"
        raise GeneratorError("INVALID_LREAL_VALUE", "LREAL must be a boolean or the exact token Auto.", value=value)
    if kind == "vector":
        if (
            not isinstance(value, list)
            or len(value) != 3
            or any(_number(item) is None for item in value)
        ):
            raise GeneratorError("INVALID_VECTOR_VALUE", "A finite three-number vector was required.", value=value)
        return " ".join(_format_number(item) for item in value)
    raise GeneratorError("UNSUPPORTED_RENDER_KIND", "The renderer kind is unsupported.", kind=kind)


def _load_json_object(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as error:
        raise GeneratorError("SPEC_MISSING", "The approved manifest is missing.", path=str(path)) from error
    except OSError as error:
        raise GeneratorError(
            "SPEC_UNREADABLE",
            "The approved manifest could not be read.",
            path=str(path),
            error_type=type(error).__name__,
            error=str(error),
        ) from error
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GeneratorError("SPEC_INVALID_JSON", "The approved manifest is not valid UTF-8 JSON.", path=str(path)) from error
    if not isinstance(value, dict):
        raise GeneratorError("SPEC_NOT_OBJECT", "The approved manifest must be a JSON object.", path=str(path))
    return value, raw


def _hash_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hash_path(path: Path) -> str:
    try:
        return _hash_bytes(path.read_bytes())
    except FileNotFoundError as error:
        raise GeneratorError("SOURCE_FILE_MISSING", "A required source file is missing.", path=str(path)) from error
    except OSError as error:
        raise GeneratorError(
            "SOURCE_FILE_UNREADABLE",
            "A required source file could not be read.",
            path=str(path),
            error_type=type(error).__name__,
            error=str(error),
        ) from error


def _ensure_known_keys(
    value: Mapping[str, Any],
    allowed: frozenset[str],
    scope: str,
    errors: list[dict[str, Any]],
) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        errors.append(
            _issue(
                "UNKNOWN_SPEC_KEY",
                f"{scope} contains keys outside the supported manifest schema.",
                scope=scope,
                keys=unknown,
            )
        )


def _finite_vector(value: Any, length: int) -> bool:
    return (
        isinstance(value, list)
        and len(value) == length
        and all(_number(item) is not None for item in value)
    )


def validate_spec(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate generator-supported manifest semantics without reading files."""

    errors: list[dict[str, Any]] = []
    if spec.get("route") != "vasp":
        errors.append(_issue("UNSUPPORTED_ROUTE", "Only a VASP manifest is supported."))
    if type(spec.get("schema_version")) is not int:
        errors.append(_issue("INVALID_SCHEMA_VERSION", "schema_version must be an integer."))
    if not isinstance(spec.get("unit_id"), str) or not spec["unit_id"].strip():
        errors.append(_issue("MISSING_UNIT_ID", "unit_id must be a non-empty string."))
    errors.extend(performance_diagnostic_spec_errors(spec))

    structure = spec.get("structure")
    if not isinstance(structure, dict):
        errors.append(_issue("INVALID_STRUCTURE_SPEC", "structure must be an object."))
        structure = {}
    else:
        _ensure_known_keys(structure, STRUCTURE_KEYS, "structure", errors)
    species = structure.get("species_order")
    paw_order = structure.get("paw_order")
    counts = structure.get("counts")
    if (
        not isinstance(species, list)
        or not species
        or not all(isinstance(item, str) and item.strip() for item in species)
        or len(set(species)) != len(species)
    ):
        errors.append(_issue("INVALID_SPECIES_SPEC", "structure.species_order must be a unique non-empty string list."))
    if (
        not isinstance(paw_order, list)
        or not paw_order
        or not all(isinstance(item, str) and item.strip() for item in paw_order)
    ):
        errors.append(_issue("INVALID_PAW_ORDER_SPEC", "structure.paw_order must be a non-empty string list."))
    if isinstance(species, list) and isinstance(paw_order, list) and len(species) != len(paw_order):
        errors.append(_issue("SPECIES_PAW_LENGTH_MISMATCH", "species_order and paw_order lengths differ."))
    if (
        not isinstance(counts, list)
        or not counts
        or not all(type(item) is int and item > 0 for item in counts)
    ):
        errors.append(_issue("INVALID_COUNTS_SPEC", "structure.counts must be a positive integer list."))
    elif isinstance(species, list) and len(species) != len(counts):
        errors.append(_issue("SPECIES_COUNTS_LENGTH_MISMATCH", "species_order and counts lengths differ."))
    nions = structure.get("nions")
    if type(nions) is not int or nions <= 0:
        errors.append(_issue("INVALID_NIONS_SPEC", "structure.nions must be a positive integer."))
    elif isinstance(counts, list) and sum(counts) != nions:
        errors.append(_issue("NIONS_SPEC_MISMATCH", "structure.nions differs from the sum of counts."))
    if _number(structure.get("nelect")) is None:
        errors.append(_issue("INVALID_NELECT_SPEC", "structure.nelect must be finite."))
    cell = structure.get("cell_A")
    if (
        not isinstance(cell, list)
        or len(cell) != 3
        or any(not _finite_vector(row, 3) for row in cell)
    ):
        errors.append(_issue("INVALID_CELL_SPEC", "structure.cell_A must be a finite 3x3 matrix."))
    fixed = structure.get("fixed_global_indices")
    if not isinstance(fixed, list) or not all(type(item) is int for item in fixed):
        errors.append(_issue("INVALID_FIXED_MASK_SPEC", "fixed_global_indices must be an integer list."))
    else:
        if fixed != sorted(fixed) or len(set(fixed)) != len(fixed):
            errors.append(_issue("INVALID_FIXED_MASK_SPEC", "fixed_global_indices must be sorted and unique."))
        if type(nions) is int and any(item < 1 or item > nions for item in fixed):
            errors.append(_issue("FIXED_INDEX_OUT_OF_RANGE", "fixed_global_indices exceeds the declared atom range."))
        free_count = structure.get("free_global_count")
        if type(nions) is int and type(free_count) is int and free_count != nions - len(fixed):
            errors.append(_issue("FREE_COUNT_SPEC_MISMATCH", "free_global_count differs from nions minus fixed count."))

    incar = spec.get("incar")
    if not isinstance(incar, dict):
        errors.append(_issue("INVALID_INCAR_SPEC", "incar must be an object."))
        incar = {}
    else:
        _ensure_known_keys(incar, INCAR_KEYS | frozenset(FEATURE_KEYS), "incar", errors)
        if "KPAR" in incar or "NCORE" in incar:
            errors.append(
                _issue(
                    "CONFLICTING_PARALLEL_SPEC",
                    "KPAR/NCORE belong only in parallel.kpar/parallel.ncore, not incar.",
                )
            )
    static_form = incar.get("IBRION") == -1 and incar.get("NSW") == 0
    relaxation_form = type(incar.get("IBRION")) is int and incar.get("IBRION") in (1, 2) and type(incar.get("NSW")) is int and incar.get("NSW") > 0
    required_incar_keys = (
        REQUIRED_INCAR_KEYS - {"EDIFFG_eV_per_A", "POTIM"}
        if static_form
        else REQUIRED_INCAR_KEYS
    )
    if incar.get("LDIPOL") is not True:
        required_incar_keys = required_incar_keys - {"IDIPOL", "DIPOL"}
    for key in sorted(required_incar_keys - set(incar)):
        errors.append(_issue("MISSING_INCAR_SPEC_KEY", f"Approved INCAR key is missing: {key}.", key=key))
    for name, _, kind in INCAR_FIELDS:
        if name not in incar:
            continue
        value = incar[name]
        if name in {"NUPDOWN", "EDIFFG_eV_per_A"} and value is None:
            continue
        if name == "POTIM" and static_form and value is None:
            continue
        if name in {"IDIPOL", "DIPOL"} and value is None and incar.get("LDIPOL") is not True:
            continue
        if kind == "text" and (not isinstance(value, str) or not value.strip()):
            errors.append(_issue("INVALID_INCAR_SPEC_VALUE", f"INCAR field {name} must be non-empty text.", key=name))
        elif kind == "float" and _number(value) is None:
            errors.append(_issue("INVALID_INCAR_SPEC_VALUE", f"INCAR field {name} must be finite.", key=name))
        elif kind == "int" and type(value) is not int:
            errors.append(_issue("INVALID_INCAR_SPEC_VALUE", f"INCAR field {name} must be an integer or null only for NUPDOWN.", key=name))
        elif kind == "bool" and type(value) is not bool:
            errors.append(_issue("INVALID_INCAR_SPEC_VALUE", f"INCAR field {name} must be boolean.", key=name))
        elif kind == "lreal" and type(value) is not bool and value != "Auto":
            errors.append(_issue("INVALID_INCAR_SPEC_VALUE", f"INCAR field {name} must be boolean or exact text Auto.", key=name))
        elif kind == "vector" and not _finite_vector(value, 3):
            errors.append(_issue("INVALID_INCAR_SPEC_VALUE", f"INCAR field {name} must be a finite three-number vector.", key=name))
    if "SYSTEM" in incar and (not isinstance(incar["SYSTEM"], str) or not incar["SYSTEM"].strip()):
        errors.append(_issue("INVALID_INCAR_SPEC_VALUE", "INCAR SYSTEM must be non-empty text.", key="SYSTEM"))
    for key in FEATURE_KEYS:
        if key not in incar:
            errors.append(_issue("MISSING_FEATURE_SPEC", f"Feature flag is missing: {key}.", key=key))
        elif incar[key] is not False:
            errors.append(_issue("UNSUPPORTED_FEATURE_SPEC", f"Only explicit false is supported for {key}.", key=key))
    if not static_form and not relaxation_form:
        errors.append(_issue(
            "UNSUPPORTED_RUN_KIND",
            "The generator supports fixed-cell IBRION=1/2 relaxation or IBRION=-1 static forms.",
        ))
    if incar.get("ISIF") != 2:
        errors.append(_issue("UNSUPPORTED_CELL_MODE", "The current generator supports only fixed-cell ISIF=2."))
    if type(incar.get("IBRION")) is not int:
        errors.append(_issue("UNSUPPORTED_IBRION", "IBRION must be an integer in a supported task form."))
    if type(incar.get("NSW")) is not int:
        errors.append(_issue("UNSUPPORTED_RUN_KIND", "NSW must be an integer in a supported task form."))
    if not static_form and "EDIFFG_eV_per_A" in incar and incar["EDIFFG_eV_per_A"] is None:
        errors.append(_issue("MISSING_EDIFFG_SPEC", "EDIFFG_eV_per_A must be explicit and finite."))
    if type(incar.get("ISPIN")) is not int or incar.get("ISPIN") not in (1, 2):
        errors.append(_issue("UNSUPPORTED_ISPIN", "ISPIN must be 1 or 2."))

    kpoints = spec.get("kpoints")
    if not isinstance(kpoints, dict):
        errors.append(_issue("INVALID_KPOINTS_SPEC", "kpoints must be an object."))
        kpoints = {}
    else:
        _ensure_known_keys(kpoints, KPOINT_KEYS, "kpoints", errors)
    generation = str(kpoints.get("generation", "")).lower()
    if generation not in {"gamma", "monkhorst", "monkhorst-pack"}:
        errors.append(_issue("UNSUPPORTED_KPOINT_GENERATION", "KPOINTS generation must be Gamma or Monkhorst."))
    mesh = kpoints.get("mesh")
    if not isinstance(mesh, list) or len(mesh) != 3 or not all(type(item) is int and item > 0 for item in mesh):
        errors.append(_issue("INVALID_KPOINT_MESH_SPEC", "KPOINTS mesh must be three positive integers."))
    shift = kpoints.get("shift")
    if not _finite_vector(shift, 3):
        errors.append(_issue("INVALID_KPOINT_SHIFT_SPEC", "KPOINTS shift must be a finite three-number vector."))

    errors.extend(validate_restart_spec(spec))

    parallel = spec.get("parallel")
    if not isinstance(parallel, dict):
        errors.append(_issue("INVALID_PARALLEL_SPEC", "parallel must be an object."))
        parallel = {}
    else:
        _ensure_known_keys(parallel, PARALLEL_KEYS, "parallel", errors)
    if not all(type(parallel.get(key)) is int and parallel.get(key) > 0 for key in PARALLEL_KEYS):
        errors.append(_issue("INVALID_PARALLEL_SPEC", "parallel fields must be positive integers."))
    else:
        group = parallel["kpar"] * parallel["ncore"]
        if parallel["mpi_ranks"] % group != 0:
            errors.append(_issue("PARALLEL_GROUP_NOT_DIVISIBLE", "mpi_ranks must divide into KPAR*NCORE groups."))
    if parallel.get("omp_num_threads") != 1:
        errors.append(_issue("UNSUPPORTED_OMP_MODE", "Only omp_num_threads=1 is supported."))

    policy = spec.get("input_policy")
    if policy is not None:
        if not isinstance(policy, dict):
            errors.append(_issue("INVALID_INPUT_POLICY", "input_policy must be an object."))
        else:
            _ensure_known_keys(policy, INPUT_POLICY_KEYS, "input_policy", errors)
            if (
                "allow_poscar_trailing_blank_lines" in policy
                and type(policy["allow_poscar_trailing_blank_lines"]) is not bool
            ):
                errors.append(_issue("INVALID_POSCAR_TAIL_POLICY", "POSCAR tail policy must be boolean."))
    return errors


def _manifest_hash(spec: Mapping[str, Any], name: str) -> str | None:
    outputs = spec.get("outputs")
    if not isinstance(outputs, Mapping):
        return None
    entry = outputs.get(name)
    if not isinstance(entry, Mapping):
        return None
    value = entry.get("sha256")
    return value.lower() if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value.lower()) else None


def _read_source_text(path: Path) -> tuple[bytes, str]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as error:
        raise GeneratorError("SOURCE_FILE_MISSING", "A required source file is missing.", path=str(path)) from error
    except OSError as error:
        raise GeneratorError(
            "SOURCE_FILE_UNREADABLE",
            "A required source file could not be read.",
            path=str(path),
            error_type=type(error).__name__,
            error=str(error),
        ) from error
    try:
        return raw, raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GeneratorError("SOURCE_FILE_NOT_UTF8", "A source input is not valid UTF-8.", path=str(path)) from error


def _parse_poscar(text: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    lines = text.splitlines()
    structure = spec["structure"]
    if len(lines) < 8:
        raise GeneratorError("SOURCE_POSCAR_TRUNCATED", "Source POSCAR is too short.")
    scale_tokens = lines[1].split()
    if len(scale_tokens) != 1:
        raise GeneratorError("SOURCE_POSCAR_SCALE_INVALID", "Source POSCAR must use one positive scalar scale.")
    scale = _number(scale_tokens[0])
    if scale is None or scale <= 0:
        raise GeneratorError("SOURCE_POSCAR_SCALE_UNSUPPORTED", "Only positive scalar POSCAR scaling is supported.", scale=scale)
    raw_cell: list[list[float]] = []
    for line in lines[2:5]:
        values = [_number(item) for item in line.split()]
        if len(values) < 3 or any(item is None for item in values[:3]):
            raise GeneratorError("SOURCE_POSCAR_CELL_INVALID", "Source POSCAR lattice vectors are invalid.")
        raw_cell.append([float(item) for item in values[:3] if item is not None])
    scaled_cell = [[scale * value for value in row] for row in raw_cell]
    expected_cell = structure["cell_A"]
    if not all(
        math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-8)
        for actual_row, expected_row in zip(scaled_cell, expected_cell)
        for actual, expected in zip(actual_row, expected_row)
    ):
        raise GeneratorError("SOURCE_POSCAR_CELL_MISMATCH", "Source POSCAR cell differs from the approved manifest.")
    species = lines[5].split()
    try:
        counts = [int(item) for item in lines[6].split()]
    except ValueError as error:
        raise GeneratorError("SOURCE_POSCAR_COUNTS_INVALID", "Source POSCAR counts are invalid.") from error
    if species != structure["species_order"] or counts != structure["counts"]:
        raise GeneratorError("SOURCE_POSCAR_IDENTITY_MISMATCH", "Source POSCAR species/counts differ from the approved manifest.")
    cursor = 7
    selective = False
    if cursor < len(lines) and lines[cursor].strip().lower().startswith("selective"):
        selective = True
        cursor += 1
    if cursor >= len(lines) or not lines[cursor].strip().lower().startswith(("direct", "cart")):
        raise GeneratorError("SOURCE_POSCAR_COORDINATE_MODE_INVALID", "Source POSCAR coordinate mode is not Direct or Cartesian.")
    coordinate_mode = lines[cursor].strip()
    cursor += 1
    nions = sum(counts)
    fixed: list[int] = []
    free: list[int] = []
    partial: list[int] = []
    for global_index in range(1, nions + 1):
        if cursor >= len(lines):
            raise GeneratorError("SOURCE_POSCAR_COORDINATES_MISSING", "Source POSCAR has fewer coordinate rows than nions.", global_index=global_index)
        parts = lines[cursor].split()
        cursor += 1
        values = [_number(item) for item in parts[:3]]
        if len(values) != 3 or any(item is None for item in values):
            raise GeneratorError("SOURCE_POSCAR_COORDINATE_INVALID", "Source POSCAR contains an invalid coordinate row.", global_index=global_index)
        if selective:
            if len(parts) < 6 or any(item.upper() not in {"T", "F"} for item in parts[3:6]):
                raise GeneratorError("SOURCE_POSCAR_MASK_INVALID", "Source POSCAR contains an invalid T/F mask.", global_index=global_index)
            flags = [item.upper() for item in parts[3:6]]
            if flags == ["F", "F", "F"]:
                fixed.append(global_index)
            elif flags == ["T", "T", "T"]:
                free.append(global_index)
            else:
                partial.append(global_index)
    nonblank_tail = [index + 1 for index, line in enumerate(lines[cursor:], cursor) if line.strip()]
    if nonblank_tail:
        raise GeneratorError("SOURCE_POSCAR_NONBLANK_TAIL", "Only blank lines may follow source POSCAR coordinates.", line_numbers=nonblank_tail)
    expected_fixed = structure["fixed_global_indices"]
    expected_free = [index for index in range(1, nions + 1) if index not in set(expected_fixed)]
    if not selective and expected_fixed:
        raise GeneratorError("SOURCE_POSCAR_SELECTIVE_MISSING", "Fixed atoms require Selective Dynamics.")
    if partial or fixed != expected_fixed or free != expected_free:
        raise GeneratorError(
            "SOURCE_POSCAR_MASK_MISMATCH",
            "Source POSCAR selective mask differs from the approved manifest.",
            fixed=fixed,
            free=free,
            partial=partial,
        )
    return {
        "species": species,
        "counts": counts,
        "nions": nions,
        "scale": scale,
        "scaled_cell": scaled_cell,
        "coordinate_mode": coordinate_mode,
        "fixed_global_indices": fixed,
        "free_global_indices": free,
        "trailing_nonblank_lines": nonblank_tail,
    }


def _validate_warm_geometry_source_path(spec: Mapping[str, Any], source_root: Path) -> None:
    restart = spec.get("restart")
    if not isinstance(restart, Mapping) or restart.get("mode") != WARM_RESTART_MODE:
        return
    source = spec.get("source")
    if not isinstance(source, Mapping):
        raise GeneratorError(
            "MISSING_GEOMETRY_SOURCE",
            "Warm-restart generation requires the exact approved source POSCAR path.",
        )
    relative = PurePosixPath(str(source.get("parent_repo_path", "")))
    project_root = Path(__file__).resolve().parents[2]
    declared_source = project_root.joinpath(*relative.parts).resolve()
    actual_source = (source_root / "POSCAR").resolve()
    if declared_source != actual_source:
        raise GeneratorError(
            "GEOMETRY_SOURCE_PATH_MISMATCH",
            "The approved top-level geometry source path must resolve to the POSCAR used for generation.",
            declared=str(declared_source),
            actual=str(actual_source),
        )
    provenance = source.get("provenance")
    if provenance is not None:
        relative_original = PurePosixPath(str(provenance.get("original_repo_path", "")))
        original_declared = project_root.joinpath(*relative_original.parts)
        original = original_declared.resolve()
        if not original.is_relative_to(project_root) or original_declared.is_symlink() or not original.is_file():
            raise GeneratorError("GEOMETRY_PROVENANCE_PATH_INVALID", "Original geometry must be an existing regular project file.")
        original_bytes = original.read_bytes()
        if original_bytes != actual_source.read_bytes() or _hash_bytes(original_bytes) != provenance.get("original_sha256"):
            raise GeneratorError("GEOMETRY_PROVENANCE_BYTES_MISMATCH", "Staged geometry must match the original bytes and declared identity.")


def _optional_source_observation(
    name: str,
    source_raw: bytes | None,
    generated_raw: bytes,
) -> dict[str, Any]:
    """Record old optional-file differences without treating them as approval."""

    if source_raw is None:
        return {
            "present": False,
            "note": "Optional source file was not supplied; generation uses the manifest.",
        }
    observation: dict[str, Any] = {
        "present": True,
        "sha256": _hash_bytes(source_raw),
        "size_bytes": len(source_raw),
        "byte_identical_to_generated": source_raw == generated_raw,
    }
    if name == "INCAR":
        text = source_raw.decode("utf-8", errors="replace")
        has_ediffg = bool(re.search(r"(?im)^\s*EDIFFG\s*=", text))
        observation["source_has_ediffg"] = has_ediffg
        if not has_ediffg:
            observation["note"] = "Source INCAR lacks EDIFFG; generated EDIFFG comes from the manifest."
        else:
            observation["note"] = "Source INCAR is informational; approved values come from the manifest."
    else:
        observation["note"] = "Source KPOINTS is informational; approved mesh and shift come from the manifest."
    return observation


def render_incar(spec: Mapping[str, Any]) -> str:
    """Render all supported manifest INCAR fields and derived parallel tags."""

    incar = spec["incar"]
    lines: list[str] = []
    static_form = incar.get("IBRION") == -1 and incar.get("NSW") == 0
    if "SYSTEM" in incar:
        lines.append(f"SYSTEM = {_format_value(incar['SYSTEM'], 'text')}")
    for name, tag, kind in INCAR_FIELDS:
        if name not in incar:
            continue
        value = incar[name]
        if name in {"NUPDOWN", "EDIFFG_eV_per_A"} and value is None:
            continue
        if name == "POTIM" and static_form and value is None:
            continue
        if name in {"IDIPOL", "DIPOL"} and value is None and incar.get("LDIPOL") is not True:
            continue
        lines.append(f"{tag} = {_format_value(value, kind)}")
    parallel = spec["parallel"]
    lines.append(f"KPAR = {_format_value(parallel['kpar'], 'int')}")
    lines.append(f"NCORE = {_format_value(parallel['ncore'], 'int')}")
    return "\n".join(lines) + "\n"


def render_kpoints(spec: Mapping[str, Any]) -> str:
    """Render only the approved explicit generation, mesh and shift."""

    kpoints = spec["kpoints"]
    generation = str(kpoints["generation"]).lower()
    mode = "Gamma" if generation == "gamma" else "Monkhorst-Pack"
    mesh = " ".join(str(item) for item in kpoints["mesh"])
    shift = " ".join(_format_number(item) for item in kpoints["shift"])
    return "\n".join(
        [
            f"Generated from approved manifest: {mode} mesh {mesh}",
            "0",
            mode,
            mesh,
            shift,
            "",
        ]
    )


def _validation_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    observations = report.get("observations", {})
    poscar = observations.get("poscar", {}) if isinstance(observations, Mapping) else {}
    nelect = observations.get("nelect", {}) if isinstance(observations, Mapping) else {}
    errors = report.get("errors", [])
    warnings = report.get("warnings", [])
    return {
        "status": "PASSED" if report.get("passed") else "FAILED",
        "passed": bool(report.get("passed")),
        "supported_task_form": bool(report.get("supported_task_form")),
        "error_codes": [
            item.get("code")
            for item in errors
            if isinstance(item, Mapping) and item.get("code")
        ],
        "warning_codes": [
            item.get("code")
            for item in warnings
            if isinstance(item, Mapping) and item.get("code")
        ],
        "nions": poscar.get("nions") if isinstance(poscar, Mapping) else None,
        "cell_comparison": poscar.get("cell_comparison") if isinstance(poscar, Mapping) else None,
        "nelect_status": nelect.get("status") if isinstance(nelect, Mapping) else None,
        "scientific_acceptance": "NOT_EVALUATED",
    }


def _render_manifest(
    spec: Mapping[str, Any],
    source_spec_sha256: str,
    generated_files: Mapping[str, bytes],
    validation_summary: Mapping[str, Any] | None = None,
    source_spec_path: str | None = None,
) -> bytes:
    rendered = copy.deepcopy(dict(spec))
    warm_restart = spec.get("restart", {}).get("mode") == WARM_RESTART_MODE
    package_kind = "PREPARED" if warm_restart else "LOCAL_TOOL_DEMONSTRATION"
    rendered["status"] = package_kind
    rendered["scientific_release"] = "NOT_AUTHORIZED"
    if warm_restart:
        rendered["candidate_state"] = "PREPARED_LOCAL_INPUTS"
        rendered["remote_restart_state"] = "REMOTE_RESTART_PENDING"
    verifier: dict[str, Any] = {
        "tool": "tools/vasp_executor.py",
        "status": "PENDING_STAGING_CHECK" if validation_summary is None else "PASSED",
    }
    if validation_summary is not None:
        verifier["summary"] = dict(validation_summary)
    rendered["generation"] = {
        "kind": package_kind,
        "generator_schema": SCHEMA,
        "source_spec_sha256": source_spec_sha256,
        "source_spec_path": source_spec_path,
        "approved_values_source": "approved_manifest.json" if warm_restart else "input_manifest.json",
        "independent_verifier": verifier,
    }
    if warm_restart:
        rendered["generation"]["approved_manifest_file"] = "approved_manifest.json"
        approved_manifest = generated_files.get("approved_manifest.json")
        if approved_manifest is not None:
            rendered["generation"]["approved_manifest_sha256"] = _hash_bytes(approved_manifest)
        geometry_source = spec["source"]
        rendered["generation"]["geometry_source"] = {
            "source_kind": geometry_source["source_kind"],
            "parent_repo_path": geometry_source["parent_repo_path"],
            "parent_sha256": geometry_source["parent_sha256"],
            "matches_output_poscar_sha256": True,
            "locked_geometry": "OMITTED_UNVERIFIED_ANNOTATIONS_REJECTED",
        }
    rendered["outputs"] = {
        name: {
            "sha256": _hash_bytes(raw),
            "size_bytes": len(raw),
        }
        for name, raw in generated_files.items()
        if name in INPUT_FILES or name in OPTIONAL_COPIED_FILES
    }
    return (
        json.dumps(rendered, ensure_ascii=False, indent=2, sort_keys=False)
        + "\n"
    ).encode("utf-8")


def _render_readme(
    spec: Mapping[str, Any],
    changes: Mapping[str, Sequence[str]],
    included_files: Sequence[str],
    validation_summary: Mapping[str, Any],
) -> bytes:
    structure = spec["structure"]
    incar = spec["incar"]
    kpoints = spec["kpoints"]
    warm_restart = spec.get("restart", {}).get("mode") == WARM_RESTART_MODE
    package_kind = "PREPARED" if warm_restart else "LOCAL_TOOL_DEMONSTRATION"
    fixed = ", ".join(str(item) for item in structure["fixed_global_indices"])
    ediffg = (
        "not an ionic stopping criterion"
        if incar.get("EDIFFG_eV_per_A") is None and incar.get("IBRION") == -1 and incar.get("NSW") == 0
        else f"{_format_number(incar['EDIFFG_eV_per_A'])} eV/A"
    )
    lines = [
        f"# {package_kind}",
        "",
        (
            "Local VASP input candidate prepared from the Sol-approved warm-restart specification."
            if warm_restart
            else "This directory is a local generated-input demonstration."
        ),
        (
            "No POTCAR or execution runner is included. WAVECAR/CHGCAR are not downloaded; remote restart preflight is pending."
            if warm_restart
            else "It is not a submission package and contains no POTCAR or execution runner."
        ),
        "",
        f"unit_id: {spec['unit_id']}",
        f"candidate state: {package_kind}",
        f"approved scope: {spec.get('scope', '')}",
        f"structure: {', '.join(structure['species_order'])} / counts {structure['counts']} / NIONS {structure['nions']}",
        f"fixed global indices: {fixed}",
        (
            f"task: {'warm' if warm_restart else 'fresh'} "
            f"ISTART={incar['ISTART']} ICHARG={incar['ICHARG']}; "
            f"IBRION={incar['IBRION']} ISIF={incar['ISIF']} NSW={incar['NSW']}; "
            f"EDIFFG={ediffg}"
        ),
        (
            f"KPOINTS: {kpoints['generation']} mesh "
            f"{kpoints['mesh']} shift {kpoints['shift']}"
        ),
        "",
        "Source-to-output changes (all values are taken from the approved manifest):",
    ]
    if warm_restart:
        geometry_source = spec["source"]
        source = spec["restart"]["source"]
        lines.extend(
            [
                f"geometry source: `{geometry_source['source_kind']}` at `{geometry_source['parent_repo_path']}` (SHA-256 `{geometry_source['parent_sha256']}`)",
                f"restart source: {source['task_id']} at {source['case_path']}",
                f"restart files: WAVECAR and CHGCAR; local state NOT_DOWNLOADED; remote state {source['remote_preflight_state']}",
                "No locked_geometry annotation is carried; the source hash is bound to the actual input POSCAR.",
                "Use boundary: do not submit from this local package. A separately authorized remote preflight must verify matching regular-file copies with no hard links before any run.",
            ]
        )
    for name in ("POSCAR", "INCAR", "KPOINTS"):
        for change in changes.get(name, []):
            lines.append(f"- {name}: {change}")
    lines.extend(
        [
            "",
            f"Included files: {', '.join(included_files)}",
            (
                "Independent check in staging: "
                f"{validation_summary.get('status', 'UNKNOWN')} "
                f"(supported_task_form={validation_summary.get('supported_task_form')})"
            ),
            (
                "python -B tools/vasp_executor.py preflight --manifest <candidate>/input_manifest.json --input-dir <candidate>"
                if warm_restart
                else "python -B tools/vasp_executor.py preflight --manifest <demo>/input_manifest.json --input-dir <demo>"
            ),
            "The independent checker remains the authority for actual rendered-file validation.",
        ]
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _render_validation_note(
    spec: Mapping[str, Any],
    validation_summary: Mapping[str, Any],
) -> bytes:
    restart = spec["restart"]
    source = restart.get("source", {}) if isinstance(restart, Mapping) else {}
    warm_restart = restart.get("mode") == WARM_RESTART_MODE
    lines = [
        "# Local input validation",
        "",
        f"- Unit: `{spec['unit_id']}`",
        f"- Candidate state: `{'PREPARED' if warm_restart else 'LOCAL_TOOL_DEMONSTRATION'}`",
        f"- Actual-input validator: `{validation_summary.get('status', 'UNKNOWN')}`; supported task form: `{validation_summary.get('supported_task_form')}`.",
        f"- POSCAR: NIONS `{validation_summary.get('nions')}`; scaled cell `{validation_summary.get('cell_comparison')}`; complete selective-dynamics mask and source-byte identity are checked by the generator and executor.",
        f"- Electron count: `{validation_summary.get('nelect_status')}` from declared PAW ZVAL metadata.",
        f"- Scientific acceptance: `{validation_summary.get('scientific_acceptance', 'NOT_EVALUATED')}` (not evaluated by input tooling).",
        "- POTCAR contents were not read or created.",
    ]
    if warm_restart:
        geometry_source = spec.get("source", {})
        files = source.get("files", {}) if isinstance(source, Mapping) else {}
        lines.extend(
            [
                f"- Restart contract: `ISTART=1`, `ICHARG=1`, fixed-cell `IBRION={spec['incar']['IBRION']}`, `ISIF=2`, `NSW>0`.",
                f"- Exact geometry source: `{geometry_source.get('source_kind')}` at `{geometry_source.get('parent_repo_path')}`; source SHA-256 `{geometry_source.get('parent_sha256')}` must match the actual target POSCAR.",
                "- `locked_geometry` is intentionally omitted; the warm-restart gate rejects unverified geometry annotations.",
                f"- Source geometry SHA-256: `{source.get('poscar_sha256')}`; generator and formal executor independently bind it to actual target POSCAR bytes.",
                "- Compatibility fields (geometry, environment, PAW, ENCUT, KPOINTS, NBANDS, spin) must all be independently recomputed as `MATCH` by the formal executor.",
                "- Source restart observations are metadata-only; file contents were not read and restart data were not downloaded.",
                f"- WAVECAR source stat: {files.get('WAVECAR', {})}",
                f"- CHGCAR source stat: {files.get('CHGCAR', {})}",
                f"- Local restart state: `NOT_DOWNLOADED`; remote restart preflight: `{source.get('remote_preflight_state')}`.",
                "- This is a local prepared-input candidate only. It does not authorize upload, remote POTCAR assembly, tmux, VASP execution, or watcher activity.",
            ]
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _destination_check(
    source_dir: Path,
    output_dir: Path,
    allow_empty_destination: bool,
) -> None:
    if output_dir == source_dir:
        raise GeneratorError("DESTINATION_IS_SOURCE", "Output directory cannot be the source directory.")
    try:
        output_dir.relative_to(source_dir)
    except ValueError:
        pass
    else:
        raise GeneratorError("DESTINATION_INSIDE_SOURCE", "Output directory cannot be inside the source directory.")
    if not output_dir.exists():
        return
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise GeneratorError("DESTINATION_UNSAFE", "Output path exists but is not a normal directory.", path=str(output_dir))
    try:
        children = list(output_dir.iterdir())
    except OSError as error:
        raise GeneratorError("DESTINATION_UNREADABLE", "Output directory cannot be inspected.", path=str(output_dir)) from error
    if children:
        raise GeneratorError(
            "DESTINATION_NOT_EMPTY",
            "Refusing to overwrite an existing non-empty output directory.",
            path=str(output_dir),
        )
    if not allow_empty_destination:
        raise GeneratorError(
            "DESTINATION_EXISTS",
            "An existing empty output directory requires --allow-empty-destination.",
            path=str(output_dir),
        )


def generate_candidate(
    spec_path: Path | str | None,
    source_dir: Path | str,
    output_dir: Path | str,
    *,
    allow_empty_destination: bool = False,
    approved_spec_raw: bytes | None = None,
    incar_renderer=None,
    kpoints_renderer=None,
) -> dict[str, Any]:
    """Generate a local candidate atomically from one approved manifest."""

    spec_file = Path(spec_path).resolve() if spec_path is not None else None
    source_root = Path(source_dir).resolve()
    destination = Path(output_dir).resolve()
    if approved_spec_raw is not None:
        if spec_file is not None:
            raise GeneratorError("SPEC_SOURCE_AMBIGUOUS", "Provide either a manifest path or approved manifest bytes, not both.")
        try:
            spec = json.loads(approved_spec_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GeneratorError(
                "SPEC_JSON_INVALID",
                "Approved manifest bytes must be valid UTF-8 JSON.",
                error=str(error),
            ) from error
        if not isinstance(spec, dict):
            raise GeneratorError("SPEC_NOT_OBJECT", "Approved manifest must be a JSON object.")
        spec_raw = approved_spec_raw
        source_spec_path = "<stdin>"
    else:
        if spec_file is None:
            raise GeneratorError("SPEC_SOURCE_MISSING", "A manifest path or approved manifest bytes are required.")
        spec, spec_raw = _load_json_object(spec_file)
        source_spec_path = str(spec_file)
    spec_errors = validate_spec(spec)
    if spec_errors:
        first = spec_errors[0]
        raise GeneratorError(first["code"], first["message"], errors=spec_errors)
    if not source_root.is_dir():
        raise GeneratorError("SOURCE_DIR_MISSING", "Source input directory is missing.", path=str(source_root))
    _destination_check(source_root, destination, allow_empty_destination)

    source_poscar_raw, source_poscar_text = _read_source_text(source_root / "POSCAR")
    expected_poscar_hash = _manifest_hash(spec, "POSCAR")
    if expected_poscar_hash is not None and _hash_bytes(source_poscar_raw) != expected_poscar_hash:
        raise GeneratorError(
            "SOURCE_POSCAR_HASH_MISMATCH",
            "Source POSCAR differs from the manifest's recorded source identity.",
            expected=expected_poscar_hash,
            actual=_hash_bytes(source_poscar_raw),
        )
    _parse_poscar(source_poscar_text, spec)
    _validate_warm_geometry_source_path(spec, source_root)
    optional_source_raw: dict[str, bytes] = {}
    for name in ("INCAR", "KPOINTS"):
        path = source_root / name
        if path.exists():
            optional_source_raw[name] = path.read_bytes()
    changes: dict[str, list[str]] = {
        "POSCAR": ["byte-identical copy of the approved source POSCAR"],
        "INCAR": ["canonical rendering of all approved INCAR fields"],
        "KPOINTS": ["canonical rendering of approved generation/mesh/shift"],
    }
    warm_restart = spec.get("restart", {}).get("mode") == WARM_RESTART_MODE
    package_kind = "PREPARED" if warm_restart else "LOCAL_TOOL_DEMONSTRATION"
    included_files = ["POSCAR", "INCAR", "KPOINTS", "input_manifest.json", "generation_receipt.json", "README.md"]
    if warm_restart:
        included_files.extend(["approved_manifest.json", "validation.md"])
    copied: dict[str, bytes] = {}
    for name in OPTIONAL_COPIED_FILES:
        path = source_root / name
        if path.exists():
            copied[name] = path.read_bytes()
            included_files.append(name)

    generated_files: dict[str, bytes] = {
        "POSCAR": source_poscar_raw,
        "INCAR": (incar_renderer(spec, source_poscar_text) if incar_renderer else render_incar(spec)).encode("utf-8"),
        "KPOINTS": (kpoints_renderer(spec) if kpoints_renderer else render_kpoints(spec)).encode("utf-8"),
    }
    generated_files.update(copied)
    if warm_restart:
        generated_files["approved_manifest.json"] = spec_raw
    optional_source_observations = {
        name: _optional_source_observation(name, optional_source_raw.get(name), generated_files[name])
        for name in ("INCAR", "KPOINTS")
    }
    if optional_source_observations["INCAR"].get("present") and not optional_source_observations["INCAR"].get("source_has_ediffg"):
        changes["INCAR"].insert(0, "added missing approved EDIFFG from manifest")
    source_inputs_raw = {"POSCAR": source_poscar_raw}
    source_inputs_raw.update(optional_source_raw)
    source_inputs_raw.update(copied)
    changes_for_receipt = {
        name: list(values)
        for name, values in changes.items()
    }
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    # Create the exact staging directory with the same permissive mode used by
    # the project's sandbox-compatible test fixtures.  mkdtemp() creates a
    # 0700 ACL here that can block child writes in the managed Windows host.
    stage = parent / (".vasp_input_generator_" + uuid.uuid4().hex)
    stage.mkdir(mode=0o777)
    try:
        rendered_manifest = _render_manifest(
            spec,
            _hash_bytes(spec_raw),
            generated_files,
            source_spec_path=source_spec_path,
        )
        generated_files["input_manifest.json"] = rendered_manifest
        for name, raw in generated_files.items():
            (stage / name).write_bytes(raw)

        try:
            validation = validate_inputs(stage / "input_manifest.json", stage)
        except Exception as error:
            raise GeneratorError(
                "INDEPENDENT_VALIDATOR_EXCEPTION",
                "The independent input validator raised an exception; candidate was not published.",
                error_type=type(error).__name__,
                error=str(error),
            ) from error
        validation_summary = _validation_summary(validation)
        if not validation.get("passed"):
            raise GeneratorError(
                "GENERATED_INPUT_VALIDATION_FAILED",
                "The independent input validator rejected the generated candidate.",
                validation=validation_summary,
            )

        rendered_manifest = _render_manifest(
            spec,
            _hash_bytes(spec_raw),
            generated_files,
            validation_summary,
            source_spec_path,
        )
        generated_files["input_manifest.json"] = rendered_manifest
        source_inputs = {
            name: {
                "path": str(source_root / name),
                "sha256": _hash_bytes(raw),
                "size_bytes": len(raw),
            }
            for name, raw in source_inputs_raw.items()
        }
        receipt = {
            "schema": SCHEMA,
            "kind": package_kind,
            "unit_id": spec["unit_id"],
            "source_spec": {
                "path": source_spec_path,
                "sha256": _hash_bytes(spec_raw),
                "embedded_file": "approved_manifest.json" if warm_restart else None,
            },
            "source_inputs": source_inputs,
            "geometry_source": (
                {
                    "source_kind": spec["source"]["source_kind"],
                    "parent_repo_path": spec["source"]["parent_repo_path"],
                    "parent_sha256": spec["source"]["parent_sha256"],
                    "provenance": spec["source"].get("provenance"),
                    "path_matches_generation_source": True,
                    "matches_output_poscar_sha256": True,
                    "locked_geometry": "OMITTED_UNVERIFIED_ANNOTATIONS_REJECTED",
                }
                if warm_restart
                else None
            ),
            "optional_source_observations": optional_source_observations,
            "generated_inputs": {
                name: {
                    "sha256": _hash_bytes(raw),
                    "size_bytes": len(raw),
                }
                for name, raw in generated_files.items()
                if name in INPUT_FILES or name in OPTIONAL_COPIED_FILES or name == "input_manifest.json"
            },
            "changes": changes_for_receipt,
            "invariants": {
                "poscar_byte_identical": generated_files["POSCAR"] == source_poscar_raw,
                "species_order": spec["structure"]["species_order"],
                "counts": spec["structure"]["counts"],
                "nions": spec["structure"]["nions"],
                "fixed_global_indices": spec["structure"]["fixed_global_indices"],
                "approved_ediffg_eV_per_A": spec["incar"]["EDIFFG_eV_per_A"],
                "potcar_created": False,
                "restart_mode": spec["restart"].get("mode", "fresh"),
                "restart_local_state": "NOT_DOWNLOADED" if warm_restart else "NOT_REQUIRED",
                "restart_remote_preflight_state": "REMOTE_RESTART_PENDING" if warm_restart else "NOT_APPLICABLE",
                "geometry_source_matches_poscar": True if warm_restart else None,
                "external_actions": [],
                "independent_validation": validation_summary,
            },
            "independent_verifier": {
                "tool": "tools/vasp_executor.py",
                "status": "PASSED",
                "summary": validation_summary,
                "command": "python -B tools/vasp_executor.py preflight --manifest <candidate>/input_manifest.json --input-dir <candidate>",
            },
        }
        generated_files["README.md"] = _render_readme(
            spec,
            changes_for_receipt,
            included_files,
            validation_summary,
        )
        if warm_restart:
            generated_files["validation.md"] = _render_validation_note(spec, validation_summary)
        # The receipt's generated hash list intentionally excludes itself; it is
        # the provenance record, not an approved scientific input.
        generated_files["generation_receipt.json"] = (
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
        ).encode("utf-8")
        for name, raw in generated_files.items():
            (stage / name).write_bytes(raw)
        if destination.exists():
            destination.rmdir()
        stage.replace(destination)
    except Exception:
        try:
            if stage.exists():
                shutil.rmtree(stage)
        except OSError:
            pass
        raise
    return {
        "schema": SCHEMA,
        "passed": True,
        "kind": package_kind,
        "output_dir": str(destination),
        "unit_id": spec["unit_id"],
        "files": sorted(generated_files),
        "changes": changes_for_receipt,
        "source_spec_sha256": _hash_bytes(spec_raw),
        "generated_input_hashes": {
            name: _hash_bytes(raw)
            for name, raw in generated_files.items()
            if name in INPUT_FILES
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    generate = subparsers.add_parser("generate")
    spec_input = generate.add_mutually_exclusive_group(required=True)
    spec_input.add_argument("--spec", type=Path)
    spec_input.add_argument("--spec-stdin", action="store_true")
    generate.add_argument("--source-dir", required=True, type=Path)
    generate.add_argument("--output-dir", required=True, type=Path)
    generate.add_argument("--allow-empty-destination", action="store_true")
    args = parser.parse_args(argv)
    try:
        approved_spec_raw = sys.stdin.buffer.read() if args.spec_stdin else None
        result = generate_candidate(
            args.spec,
            args.source_dir,
            args.output_dir,
            allow_empty_destination=args.allow_empty_destination,
            approved_spec_raw=approved_spec_raw,
        )
    except GeneratorError as error:
        result = {
            "schema": SCHEMA,
            "passed": False,
            "errors": [error.as_dict()],
        }
        if "errors" in error.fields:
            result["errors"] = error.fields["errors"]
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    except Exception as error:
        result = {
            "schema": SCHEMA,
            "passed": False,
            "errors": [
                {
                    "code": "GENERATOR_EXCEPTION",
                    "message": "Generator failed without touching a completed candidate.",
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            ],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 3
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

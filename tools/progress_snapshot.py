#!/usr/bin/env python3
"""Read-only VASP progress snapshot extractor.

The command reads one local snapshot directory and writes JSON to stdout.
It never connects to SSH, sends notifications, changes inputs, starts or
stops a process, or loops waiting for new data.  Remote collection is kept
outside this module.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from vasp_execution_state import UNKNOWN, normalize_state


FLOAT_RE = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?"
PRIMARY_FILES = ("POSCAR", "INCAR", "OUTCAR", "OSZICAR")
OPTIONAL_FILES = (
    "CONTCAR",
    "vasprun.xml",
    "run_timing.txt",
    "vasp.stdout",
    "vasp.stderr",
    "status.json",
    "process_snapshot.json",
    "watcher_snapshot.json",
    "POTCAR",
    "CHGCAR",
    "WAVECAR",
)
SENSITIVE_FILES = ("POTCAR", "CHGCAR", "WAVECAR")

# These are the parameters whose approved value, actual INCAR value, and
# effective OUTCAR evidence can be compared without reading licensed PAW
# payloads.  The manifest keys intentionally retain the project's unit-aware
# names while OUTCAR/INCAR use the VASP tag.
PARAMETER_FIELDS: dict[str, tuple[str, str]] = {
    "ENCUT_eV": ("ENCUT", "float"),
    "EDIFF_eV": ("EDIFF", "float"),
    "EDIFFG_eV_per_A": ("EDIFFG", "float"),
    "PREC": ("PREC", "prec"),
    "ISPIN": ("ISPIN", "int"),
    "NUPDOWN": ("NUPDOWN", "int"),
    "ISTART": ("ISTART", "int"),
    "ICHARG": ("ICHARG", "int"),
    "ISMEAR": ("ISMEAR", "int"),
    "SIGMA_eV": ("SIGMA", "float"),
    "NELM": ("NELM", "int"),
    "NELMIN": ("NELMIN", "int"),
    "NSW": ("NSW", "int"),
    "IBRION": ("IBRION", "int"),
    "ISIF": ("ISIF", "int"),
    "LDIPOL": ("LDIPOL", "bool"),
    "IDIPOL": ("IDIPOL", "int"),
    "DIPOL": ("DIPOL", "vector"),
}
OUTCAR_PARAMETER_FIELDS: dict[str, str] = {
    tag: kind for tag, kind in PARAMETER_FIELDS.values()
}
PREC_ALIASES = {
    "LOW": "LOW",
    "NORMAL": "NORMAL",
    "ACCURATE": "ACCURATE",
    "SINGLE": "SINGLE",
    # VASP 6.5.1 writes the legacy/truncated effective value in OUTCAR.
    "ACCURA": "ACCURATE",
}
PREC_ENUMS = frozenset(PREC_ALIASES)
STRICT_NUMERIC_REL_TOL = 1e-12
STRICT_NUMERIC_ABS_TOL = 1e-12
OUTPUT_ROUNDING_MAX_ABS_TOL = 1e-3


def source(path: Path, line: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path)}
    if line is not None:
        result["line"] = line
        result["line_space"] = "local_snapshot"
    return result


def optional(value: Any, reason: str | None = None) -> dict[str, Any]:
    result = {"value": value}
    if value is None and reason:
        result["reason"] = reason
    return result


def number(value: str) -> float:
    return float(value.replace("D", "E").replace("d", "e"))


def finite_number(value: str) -> float | None:
    try:
        parsed = number(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def file_info(path: Path, read_content: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "read_content": read_content,
    }
    try:
        stat = path.stat()
    except FileNotFoundError:
        result.update({"present": False, "bytes": None, "mtime_utc": None})
        result["reason"] = "file is absent from this local snapshot"
    except OSError as error:
        result.update({"present": None, "bytes": None, "mtime_utc": None})
        result["reason"] = f"stat failed: {type(error).__name__}: {error}"
    else:
        result.update({
            "present": True,
            "bytes": stat.st_size,
            "mtime_utc": iso_utc(stat.st_mtime),
        })
    return result


def read_text_once(path: Path) -> tuple[str | None, dict[str, Any] | None]:
    try:
        return path.read_text(encoding="utf-8", errors="replace"), None
    except FileNotFoundError:
        return None, {"path": str(path), "reason": "file is absent from this local snapshot"}
    except OSError as error:
        return None, {
            "path": str(path),
            "reason": f"read failed: {type(error).__name__}: {error}",
        }


def parse_bool_token(raw: str) -> bool | None:
    token = raw.strip().split()[0].lower() if raw.strip() else ""
    if token in {".true.", "true", "t", "1"}:
        return True
    if token in {".false.", "false", "f", "0"}:
        return False
    return None


def _first_numeric_token(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    match = re.search(FLOAT_RE, raw)
    return match.group(0) if match else None


def normalise_parameter_value(raw: Any, kind: str) -> Any:
    """Parse one INCAR/OUTCAR parameter without applying VASP defaults."""

    if raw is None:
        return None
    if kind == "prec":
        token = str(raw).strip().split()[0].upper() if str(raw).strip() else ""
        return PREC_ALIASES.get(token)
    if kind == "text":
        return " ".join(str(raw).strip().split()).upper()
    if kind == "bool":
        return parse_bool_token(str(raw))
    if kind == "int":
        match = re.match(r"^\s*([+-]?\d+)(?=\s|$)", str(raw))
        return int(match.group(1)) if match else None
    if kind == "float":
        token = _first_numeric_token(str(raw))
        return finite_number(token) if token is not None else None
    if kind == "vector":
        values = [finite_number(item) for item in re.findall(FLOAT_RE, str(raw))]
        if len(values) < 3 or any(item is None for item in values[:3]):
            return None
        return [float(item) for item in values[:3]]
    return None


def _numeric_resolution_token(token: str) -> float:
    mantissa = token
    exponent = 0
    exponent_match = re.search(r"[EeDd]([+-]?\d+)$", token)
    if exponent_match:
        exponent = int(exponent_match.group(1))
        mantissa = token[: exponent_match.start()]
    decimals = len(mantissa.split(".", 1)[1]) if "." in mantissa else 0
    try:
        value = 10.0 ** (exponent - decimals)
    except OverflowError:
        return 1e-12
    return value if math.isfinite(value) and value > 0 else 1e-12


def _numeric_resolutions(raw: Any) -> list[float]:
    if raw is None:
        return []
    return [_numeric_resolution_token(token) for token in re.findall(FLOAT_RE, str(raw))]


def _numeric_resolution(raw: Any) -> float:
    """Return the representational resolution of the first numeric token."""

    resolutions = _numeric_resolutions(raw)
    return resolutions[0] if resolutions else 1e-12


def _parameter_values_comparison(
    left: Any,
    right: Any,
    kind: str,
    *,
    left_raw: Any = None,
    right_raw: Any = None,
    mode: str = "strict",
) -> bool | None:
    if left is None or right is None:
        return left is None and right is None
    if kind in {"prec", "text", "int", "bool"}:
        return left == right
    if kind == "float":
        if mode == "strict":
            tolerance = STRICT_NUMERIC_ABS_TOL + STRICT_NUMERIC_REL_TOL * max(
                1.0, abs(float(left)), abs(float(right))
            )
        else:
            raw_sources = [right_raw] if mode == "effective" else [left_raw, right_raw]
            resolutions = [
                resolution
                for raw in raw_sources
                for resolution in _numeric_resolutions(raw)
            ]
            if len(resolutions) != len(raw_sources) or any(
                resolution > OUTPUT_ROUNDING_MAX_ABS_TOL
                for resolution in resolutions
            ):
                return None
            tolerance = max(resolutions) * 0.51 + STRICT_NUMERIC_ABS_TOL
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=tolerance)
    if kind == "vector":
        if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
            return False
        if mode == "strict":
            return all(
                math.isclose(
                    float(a),
                    float(b),
                    rel_tol=STRICT_NUMERIC_REL_TOL,
                    abs_tol=STRICT_NUMERIC_ABS_TOL,
                )
                for a, b in zip(left, right)
            )
        raw_sources = [right_raw] if mode == "effective" else [left_raw, right_raw]
        resolution_lists = [_numeric_resolutions(raw) for raw in raw_sources]
        if any(len(values) < len(left) for values in resolution_lists):
            return None
        if any(
            resolution > OUTPUT_ROUNDING_MAX_ABS_TOL
            for values in resolution_lists
            for resolution in values[:len(left)]
        ):
            return None
        for index, (a, b) in enumerate(zip(left, right)):
            tolerance = max(
                values[index]
                for values in resolution_lists
            ) * 0.51 + STRICT_NUMERIC_ABS_TOL
            if not math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=tolerance):
                return False
        return True
    return False


def parameter_values_equal(
    left: Any,
    right: Any,
    kind: str,
    *,
    left_raw: Any = None,
    right_raw: Any = None,
) -> bool:
    """Compare approved/input values with strict semantic numeric tolerance."""

    return _parameter_values_comparison(
        left,
        right,
        kind,
        left_raw=left_raw,
        right_raw=right_raw,
        mode="strict",
    ) is True


def _parameter_section_marker(line: str) -> str | None:
    lowered = line.lower()
    if re.search(
        r"parameters?\s+from\s+incar|incar\s*:\s*$|"
        r"(?:input|incar)\s+(?:file|echo)|input\s+parameters?",
        lowered,
    ):
        return "input_echo"
    if re.search(
        r"(?:effective|actual|applied|used)\s+(?:incar|parameters?)|"
        r"(?:incar|parameters?).*(?:effective|actual|applied|used)|"
        r"startparameter\s+for\s+this\s+run",
        lowered,
    ):
        return "effective_parameter"
    return None


def _parameter_section(
    lines: list[str],
    line_index: int,
    *,
    default: str = "unknown",
) -> str:
    """Classify evidence conservatively; an input echo is never effective."""

    line = lines[line_index]
    prior = lines[max(0, line_index - 3):line_index]
    for candidate in reversed(prior):
        marker = _parameter_section_marker(candidate)
        if marker is not None:
            return marker
    marker = _parameter_section_marker(line)
    if marker is not None:
        return marker
    return default if default in {"input_echo", "effective_parameter", "unknown"} else "unknown"


def _parameter_pattern(tag: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(tag)}\s*=\s*(?P<value>.*)$",
        re.IGNORECASE,
    )


def parse_outcar_parameters(
    text: str | None,
    path: Path,
    *,
    default_section: str = "unknown",
    original_start_line: int = 1,
) -> dict[str, Any]:
    """Extract boundedly selected parameter assignments from OUTCAR text.

    The result deliberately labels the evidence section.  A line copied from
    an INCAR/input echo is retained as evidence but cannot satisfy an
    effective-parameter comparison.
    """

    fields = {
        tag: {"tag": tag, "kind": kind, "observations": []}
        for tag, kind in OUTCAR_PARAMETER_FIELDS.items()
    }
    result: dict[str, Any] = {
        "available": text is not None,
        "source": source(path),
        "coverage": "full" if text is not None else "unknown",
        "section_markers": [],
        "fields": fields,
        "errors": [],
    }
    if text is None:
        result["errors"].append("OUTCAR parameter evidence is unavailable")
        return result
    lines = text.splitlines()
    line_offset = original_start_line if isinstance(original_start_line, int) and original_start_line >= 1 else 1
    patterns = {tag: _parameter_pattern(tag) for tag in OUTCAR_PARAMETER_FIELDS}
    current_section = default_section if default_section in {"input_echo", "effective_parameter", "unknown"} else "unknown"
    for line_index, line in enumerate(lines):
        marker = _parameter_section_marker(line)
        if marker is not None:
            current_section = marker
            result["section_markers"].append({
                "section": marker,
                "source": source(path, line_offset + line_index),
                "raw": line.strip(),
            })
        for tag, kind in OUTCAR_PARAMETER_FIELDS.items():
            match = patterns[tag].search(line)
            if not match:
                continue
            raw_value = match.group("value").strip()
            section = current_section
            if section == "unknown":
                section = _parameter_section(lines, line_index, default=default_section)
            fields[tag]["observations"].append({
                "tag": tag,
                "kind": kind,
                "raw": line.strip(),
                "raw_value": raw_value,
                "value": normalise_parameter_value(raw_value, kind),
                "section": section,
                "source": source(path, line_offset + line_index),
            })
    return result


def _manifest_parameter_source(
    manifest: Mapping[str, Any] | None,
) -> tuple[Mapping[str, Any] | None, str | None]:
    if not isinstance(manifest, Mapping):
        return None, "approved input manifest was not supplied"
    incar = manifest.get("incar")
    if not isinstance(incar, Mapping):
        return None, "approved input manifest lacks an incar object"
    return incar, None


def _observation_summary(observations: list[dict[str, Any]]) -> dict[str, Any]:
    selected = observations if len(observations) <= 4 else observations[:2] + observations[-2:]
    return {
        "count": len(observations),
        "observations": selected,
    }


def compare_parameter_sources(
    manifest: Mapping[str, Any] | None,
    incar: Mapping[str, Any] | None,
    outcar_parameters: Mapping[str, Any] | None,
    *,
    manifest_source: str | None = None,
) -> dict[str, Any]:
    """Compare approved manifest -> actual INCAR -> effective OUTCAR evidence."""

    approved, manifest_reason = _manifest_parameter_source(manifest)
    actual_parameters = incar.get("parameters", {}) if isinstance(incar, Mapping) else {}
    outcar_fields = (
        outcar_parameters.get("fields", {})
        if isinstance(outcar_parameters, Mapping)
        else {}
    )
    fields: dict[str, Any] = {}
    counts = {"MATCH": 0, "MISMATCH": 0, "REVIEW": 0, "UNKNOWN": 0}
    for manifest_key, (tag, kind) in PARAMETER_FIELDS.items():
        item: dict[str, Any] = {
            "field": manifest_key,
            "tag": tag,
            "kind": kind,
            "approved": {
                "status": "UNKNOWN",
                "value": None,
                "source": manifest_source,
            },
            "actual_incar": {
                "status": "UNKNOWN",
                "value": None,
                "raw": None,
                "source": None,
            },
            "outcar": {
                "status": "UNKNOWN",
                "value": None,
                "raw": None,
                "section": None,
                "source": None,
                "observations": [],
            },
            "comparison": "UNKNOWN",
            "reasons": [],
            "numeric_tolerance": (
                "approved-to-INCAR strict fixed tolerance; OUTCAR output-only per-component rounding with a conservative upper bound"
                if kind in {"float", "vector"}
                else "exact normalized comparison"
            ),
        }
        if approved is None:
            item["reasons"].append(manifest_reason)
        elif manifest_key not in approved:
            item["approved"]["status"] = "MISSING_APPROVAL_LABEL"
            item["comparison"] = "REVIEW"
            item["reasons"].append(
                f"approved manifest does not label {manifest_key}; output values are not classified as legal or illegal"
            )
        else:
            approved_raw = approved.get(manifest_key)
            item["approved"] = {
                "status": "EXPLICIT_ABSENCE" if approved_raw is None else "PRESENT",
                "value": normalise_parameter_value(approved_raw, kind),
                "raw": approved_raw,
                "source": manifest_source,
            }
            if approved_raw is not None and item["approved"]["value"] is None:
                item["comparison"] = "REVIEW"
                item["reasons"].append("approved manifest value cannot be normalized for this parameter kind")

        actual_item = actual_parameters.get(tag) if isinstance(actual_parameters, Mapping) else None
        if isinstance(actual_item, Mapping):
            actual_raw = actual_item.get("raw")
            actual_value = normalise_parameter_value(actual_raw, kind)
            item["actual_incar"] = {
                "status": "PRESENT" if actual_value is not None else "INVALID",
                "value": actual_value,
                "raw": actual_raw,
                "source": actual_item.get("source"),
            }
        else:
            item["actual_incar"]["status"] = "MISSING"

        field_data = outcar_fields.get(tag) if isinstance(outcar_fields, Mapping) else None
        observations = (
            field_data.get("observations", [])
            if isinstance(field_data, Mapping)
            else []
        )
        observations = [item for item in observations if isinstance(item, Mapping)]
        effective = [item for item in observations if item.get("section") == "effective_parameter"]
        input_echo = [item for item in observations if item.get("section") == "input_echo"]
        usable_effective = [item for item in effective if item.get("value") is not None]
        if usable_effective:
            first = usable_effective[0]
            conflicting = False
            ambiguous = False
            for other in usable_effective[1:]:
                consistency = _parameter_values_comparison(
                    first.get("value"),
                    other.get("value"),
                    kind,
                    left_raw=first.get("raw_value"),
                    right_raw=other.get("raw_value"),
                    mode="outcar",
                )
                if consistency is False:
                    conflicting = True
                elif consistency is None:
                    ambiguous = True
            if conflicting:
                item["outcar"]["status"] = "CONFLICT"
                item["comparison"] = "REVIEW"
                item["reasons"].append("multiple effective OUTCAR values conflict")
            elif ambiguous:
                item["outcar"]["status"] = "AMBIGUOUS"
                item["comparison"] = "REVIEW"
                item["reasons"].append(
                    "effective OUTCAR values cannot be distinguished at their printed precision"
                )
            else:
                recent = usable_effective[-1]
                item["outcar"] = {
                    "status": "EFFECTIVE",
                    "value": recent.get("value"),
                    "raw": recent.get("raw"),
                    "section": recent.get("section"),
                    "source": recent.get("source"),
                    "observations": effective,
                }
        elif input_echo:
            item["outcar"] = {
                "status": "INPUT_ECHO_ONLY",
                "value": input_echo[-1].get("value"),
                "raw": input_echo[-1].get("raw"),
                "section": "input_echo",
                "source": input_echo[-1].get("source"),
                "observations": input_echo,
            }
            item["reasons"].append(
                "OUTCAR contains only input-echo evidence; effective parameter use is unknown"
            )
        elif observations:
            item["outcar"]["status"] = "UNKNOWN"
            item["outcar"]["observations"] = observations
            item["reasons"].append("OUTCAR parameter occurrence has no trusted section classification")
        else:
            item["reasons"].append("effective OUTCAR parameter evidence is absent")

        if item["comparison"] not in {"REVIEW", "MISMATCH"}:
            approved_value = item["approved"].get("value")
            actual_value = item["actual_incar"].get("value")
            approved_present = item["approved"]["status"] == "PRESENT"
            actual_present = item["actual_incar"]["status"] == "PRESENT"
            if item["approved"]["status"] == "MISSING_APPROVAL_LABEL":
                item["comparison"] = "REVIEW"
            elif not approved_present:
                item["comparison"] = "UNKNOWN"
                item["reasons"].append("approved value is absent; no default is supplied")
            elif not actual_present:
                item["comparison"] = "MISMATCH"
                item["reasons"].append("actual INCAR value is missing or invalid")
            else:
                approved_actual = _parameter_values_comparison(
                    approved_value,
                    actual_value,
                    kind,
                    left_raw=approved.get(manifest_key),
                    right_raw=item["actual_incar"].get("raw"),
                    mode="strict",
                )
                if approved_actual is None:
                    item["comparison"] = "REVIEW"
                    item["reasons"].append(
                        "approved manifest and actual INCAR values cannot be compared strictly"
                    )
                elif approved_actual is False:
                    item["comparison"] = "MISMATCH"
                    item["reasons"].append("approved manifest and actual INCAR values differ")
                elif item["outcar"]["status"] == "EFFECTIVE":
                    effective_match = _parameter_values_comparison(
                        actual_value,
                        item["outcar"].get("value"),
                        kind,
                        left_raw=item["actual_incar"].get("raw"),
                        right_raw=item["outcar"].get("raw"),
                        mode="effective",
                    )
                    if effective_match is None:
                        item["comparison"] = "REVIEW"
                        item["reasons"].append(
                            "effective OUTCAR precision is too coarse or incomplete for a safe MATCH"
                        )
                    elif effective_match is False:
                        item["comparison"] = "MISMATCH"
                        item["reasons"].append("effective OUTCAR value differs from actual INCAR")
                    else:
                        item["comparison"] = "MATCH"
                elif item["outcar"]["status"] in {"CONFLICT", "AMBIGUOUS", "INPUT_ECHO_ONLY"}:
                    item["comparison"] = (
                        "REVIEW"
                        if item["outcar"]["status"] in {"CONFLICT", "AMBIGUOUS"}
                        else "UNKNOWN"
                    )
                else:
                    item["comparison"] = "UNKNOWN"

        counts[item["comparison"]] = counts.get(item["comparison"], 0) + 1
        fields[manifest_key] = item

    overall = "MATCH"
    for state in ("MISMATCH", "REVIEW", "UNKNOWN"):
        if counts.get(state):
            overall = state
            break
    return {
        "schema": "vasp-parameter-compare/v1",
        "status": overall,
        "fields": fields,
        "summary": counts,
        "rules": {
            "defaults": "NOT_INFERRED",
            "method_bias": "NOT_HIDDEN",
            "approved_to_incar_numeric": "STRICT_FIXED_TOLERANCE",
            "incar_to_outcar_numeric": "OUTPUT_ONLY_PER_COMPONENT_BOUNDED",
            "uncovered_derived_rules": "NOT_EVALUATED",
            "input_echo_satisfies_effective": False,
            "scientific_release": "NOT_AUTHORIZED",
        },
    }


def parse_int_token(raw: str) -> int | None:
    match = re.search(r"[-+]?\d+", raw)
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def parse_poscar(text: str | None, path: Path) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source": source(path),
        "available": text is not None,
        "valid": False,
        "nions": None,
        "species": [],
        "counts": [],
        "coordinate_mode": None,
        "selective_dynamics": False,
        "mask_status": "UNAVAILABLE",
        "fixed_indices_1based": [],
        "free_indices_1based": [],
        "partial_indices_1based": [],
        "atoms": [],
        "source_lines": {},
        "errors": [],
    }
    if text is None:
        base["errors"].append("POSCAR is missing or unreadable")
        return base
    lines = text.splitlines()
    if len(lines) < 7:
        base["errors"].append("POSCAR is truncated before species/counts")
        return base
    base["source_lines"]["comment"] = source(path, 1)
    scale = finite_number(lines[1].strip())
    if scale is None or scale == 0:
        base["errors"].append("POSCAR scale line is missing or invalid")
    lattice: list[list[float]] = []
    for index in range(2, 5):
        values = lines[index].split() if index < len(lines) else []
        vector = [finite_number(value) for value in values[:3]]
        if len(vector) != 3 or any(value is None for value in vector):
            base["errors"].append(f"POSCAR lattice line {index + 1} is invalid")
        else:
            lattice.append([float(value) for value in vector])
    base["lattice"] = lattice
    symbol_line_index = 5
    symbol_tokens = lines[symbol_line_index].split()
    counts_index = symbol_line_index + 1
    if symbol_tokens and all(re.fullmatch(r"\d+", token) for token in symbol_tokens):
        symbols: list[str] = []
        counts_index = symbol_line_index
    else:
        symbols = symbol_tokens
    count_tokens = lines[counts_index].split() if counts_index < len(lines) else []
    if not count_tokens or not all(re.fullmatch(r"\d+", token) for token in count_tokens):
        base["errors"].append("POSCAR atom-count line is missing or invalid")
        return base
    counts = [int(token) for token in count_tokens]
    if symbols and len(symbols) != len(counts):
        base["errors"].append("POSCAR species/count lengths differ")
    if not symbols:
        symbols = [None] * len(counts)
        base["species_unresolved"] = True
    else:
        base["species_unresolved"] = False
    nions = sum(counts)
    base.update({
        "species": symbols,
        "counts": counts,
        "nions": nions,
        "source_lines": {
            **base["source_lines"],
            "species": source(path, symbol_line_index + 1),
            "counts": source(path, counts_index + 1),
        },
    })
    cursor = counts_index + 1
    selective = False
    if cursor < len(lines) and lines[cursor].strip().lower().startswith("selective"):
        selective = True
        base["source_lines"]["selective_dynamics"] = source(path, cursor + 1)
        cursor += 1
    if cursor >= len(lines):
        base["errors"].append("POSCAR coordinate-mode line is missing")
        return base
    coordinate_mode = lines[cursor].strip().lower()
    if coordinate_mode.startswith("d"):
        coordinate_mode = "direct"
    elif coordinate_mode.startswith("c") or coordinate_mode.startswith("k"):
        coordinate_mode = "cartesian"
    else:
        base["errors"].append(f"POSCAR coordinate mode is not recognized: {lines[cursor].strip()}")
        coordinate_mode = None
    base["coordinate_mode"] = coordinate_mode
    base["source_lines"]["coordinate_mode"] = source(path, cursor + 1)
    cursor += 1
    if cursor < len(lines) and not selective:
        # A VASP POSCAR may contain an optional velocity section only after
        # positions; no pre-position line is valid here.  Keep the row offset.
        pass
    species_by_atom: list[str | None] = []
    for symbol, count in zip(symbols, counts):
        species_by_atom.extend([symbol] * count)
    atoms: list[dict[str, Any]] = []
    for atom_index in range(nions):
        line_number = cursor + atom_index + 1
        if cursor + atom_index >= len(lines):
            base["errors"].append(f"POSCAR is truncated at atom {atom_index + 1}")
            break
        parts = lines[cursor + atom_index].split()
        if len(parts) < 3:
            base["errors"].append(f"POSCAR atom {atom_index + 1} has fewer than 3 coordinates")
            continue
        coordinates = [finite_number(value) for value in parts[:3]]
        if any(value is None for value in coordinates):
            base["errors"].append(f"POSCAR atom {atom_index + 1} has invalid coordinates")
            continue
        flags: list[bool] | None = None
        flag_state = "NO_SELECTIVE_DYNAMICS"
        if selective:
            if len(parts) < 6:
                base["errors"].append(f"POSCAR atom {atom_index + 1} lacks T/F flags")
                flag_state = "INVALID"
            else:
                flags = []
                for token in parts[3:6]:
                    if token[:1].upper() == "T":
                        flags.append(True)
                    elif token[:1].upper() == "F":
                        flags.append(False)
                    else:
                        flags = None
                        break
                if flags is None:
                    base["errors"].append(f"POSCAR atom {atom_index + 1} has invalid T/F flags")
                    flag_state = "INVALID"
                elif all(flags):
                    flag_state = "FREE_TTT"
                elif not any(flags):
                    flag_state = "FIXED_FFF"
                else:
                    flag_state = "PARTIAL"
        else:
            flags = [True, True, True]
            flag_state = "FREE_BY_ABSENCE_OF_SELECTIVE_DYNAMICS"
        atoms.append({
            "index_1based": atom_index + 1,
            "species": species_by_atom[atom_index] if atom_index < len(species_by_atom) else None,
            "coordinates": [float(value) for value in coordinates],
            "flags": flags,
            "flag_state": flag_state,
            "source": source(path, line_number),
        })
    base["atoms"] = atoms
    base["selective_dynamics"] = selective
    if len(atoms) != nions:
        base["errors"].append(f"POSCAR atom rows={len(atoms)} but expected nions={nions}")
    fixed = [atom["index_1based"] for atom in atoms if atom["flag_state"] == "FIXED_FFF"]
    free = [
        atom["index_1based"]
        for atom in atoms
        if atom["flag_state"] in {"FREE_TTT", "FREE_BY_ABSENCE_OF_SELECTIVE_DYNAMICS"}
    ]
    partial = [atom["index_1based"] for atom in atoms if atom["flag_state"] == "PARTIAL"]
    invalid_flags = [atom["index_1based"] for atom in atoms if atom["flag_state"] == "INVALID"]
    if invalid_flags:
        mask_status = "INVALID"
    elif partial:
        mask_status = "UNSUPPORTED_PARTIAL_CONSTRAINTS"
    elif len(atoms) == nions:
        mask_status = "OK"
    else:
        mask_status = "UNAVAILABLE"
    base.update({
        "fixed_indices_1based": fixed,
        "free_indices_1based": free,
        "partial_indices_1based": partial,
        "mask_status": mask_status,
        "valid": not base["errors"] and len(atoms) == nions and coordinate_mode is not None,
    })
    return base


def parse_incar(text: str | None, path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source": source(path),
        "available": text is not None,
        "parameters": {},
        "errors": [],
    }
    if text is None:
        result["errors"].append("INCAR is missing or unreadable")
        return result
    for line_number, raw in enumerate(text.splitlines(), 1):
        code = raw.split("#", 1)[0].split("!", 1)[0].strip()
        if "=" not in code:
            continue
        key, value = code.split("=", 1)
        key = key.strip().upper()
        if not key or not re.fullmatch(r"[A-Z][A-Z0-9_+-]*", key):
            continue
        result["parameters"][key] = {
            "raw": value.strip(),
            "source": source(path, line_number),
        }
    return result


def incar_raw(incar: dict[str, Any], key: str) -> str | None:
    item = incar.get("parameters", {}).get(key.upper())
    return item.get("raw") if item else None


def incar_int(incar: dict[str, Any], key: str) -> int | None:
    raw = incar_raw(incar, key)
    return parse_int_token(raw) if raw is not None else None


def incar_float(incar: dict[str, Any], key: str) -> float | None:
    raw = incar_raw(incar, key)
    if raw is None:
        return None
    match = re.search(FLOAT_RE, raw)
    return finite_number(match.group(0)) if match else None


def run_kind(incar: dict[str, Any]) -> dict[str, Any]:
    ibrion = incar_int(incar, "IBRION")
    nsw = incar_int(incar, "NSW")
    if nsw == 0 or ibrion == -1:
        kind = "static"
        applicable = False
    elif nsw is not None and nsw > 0 and ibrion is not None and ibrion >= 0:
        kind = "relaxation"
        applicable = True
    else:
        kind = "unknown"
        applicable = None
    return {
        "kind": kind,
        "ionic_convergence_applicable": applicable,
        "IBRION": optional(ibrion, "INCAR tag absent or not an integer"),
        "NSW": optional(nsw, "INCAR tag absent or not an integer"),
    }


def parse_oszicar(text: str | None, path: Path, incar: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source": source(path),
        "available": text is not None,
        "completed_ionic_steps": [],
        "current_scf": optional(None, "OSZICAR is missing or unreadable"),
        "electronic_record_count": 0,
        "errors": [],
    }
    if text is None:
        result["errors"].append("OSZICAR is missing or unreadable")
        return result
    float_pattern = re.compile(FLOAT_RE)
    electronic_pattern = re.compile(r"^\s*(DAV|RMM):\s*(\d+)(.*)$", re.IGNORECASE)
    ionic_pattern = re.compile(
        rf"^\s*(\d+)\s+F\s*=\s*({FLOAT_RE})\s+E0\s*=\s*({FLOAT_RE})(.*)$",
        re.IGNORECASE,
    )
    pending: list[dict[str, Any]] = []
    for line_number, raw in enumerate(text.splitlines(), 1):
        electronic_match = electronic_pattern.match(raw)
        if electronic_match:
            rest = electronic_match.group(3)
            first_energy = float_pattern.search(rest)
            record = {
                "algorithm": electronic_match.group(1).upper(),
                "iteration": int(electronic_match.group(2)),
                "energy_like_eV": (
                    finite_number(first_energy.group(0)) if first_energy else None
                ),
                "source": source(path, line_number),
                "raw": raw.strip(),
            }
            pending.append(record)
            result["electronic_record_count"] += 1
            continue
        ionic_match = ionic_pattern.match(raw)
        if ionic_match:
            tail = ionic_match.group(4)
            de_match = re.search(rf"d\s*E\s*=\s*({FLOAT_RE})", tail, re.IGNORECASE)
            step = {
                "ionic_step": int(ionic_match.group(1)),
                "F_eV": number(ionic_match.group(2)),
                "E0_eV": number(ionic_match.group(3)),
                "dE_eV": finite_number(de_match.group(1)) if de_match else None,
                "source": source(path, line_number),
                "raw": raw.strip(),
                "electronic_steps": pending,
                "electronic_step_count": len(pending),
                "last_electronic_step": pending[-1] if pending else None,
            }
            result["completed_ionic_steps"].append(step)
            pending = []
    nelm = incar_int(incar, "NELM")
    for step in result["completed_ionic_steps"]:
        iterations = [item["iteration"] for item in step["electronic_steps"]]
        step["NELM"] = optional(nelm, "INCAR NELM is absent or not an integer")
        step["max_electronic_iteration"] = optional(
            max(iterations) if iterations else None,
            "no DAV/RMM record associated with this completed ionic row",
        )
        step["NELM_reached"] = nelm_reached_evidence(iterations, nelm)
        step["oscillation_hint"] = oscillation_hint(step["electronic_steps"])
    previous_f: float | None = None
    previous_e0: float | None = None
    for step in result["completed_ionic_steps"]:
        step["delta_from_previous_F_eV"] = optional(
            step["F_eV"] - previous_f if previous_f is not None else None,
            "no previous completed ionic F row",
        )
        step["delta_from_previous_E0_eV"] = optional(
            step["E0_eV"] - previous_e0 if previous_e0 is not None else None,
            "no previous completed ionic E0 row",
        )
        previous_f = step["F_eV"]
        previous_e0 = step["E0_eV"]
    if pending:
        iterations = [item["iteration"] for item in pending]
        result["current_scf"] = {"value": {
            "state": "IN_PROGRESS",
            "ionic_step": None,
            "step_alignment": "uncertain",
            "reason": (
                "DAV/RMM records occur after the last completed F/E0 row; "
                "the extractor does not guess the unfinished ionic-step number"
            ),
            "electronic_steps": pending,
            "electronic_step_count": len(pending),
            "last_electronic_step": pending[-1],
            "NELM": optional(nelm, "INCAR NELM is absent or not an integer"),
            "NELM_reached": nelm_reached_evidence(iterations, nelm),
            "oscillation_hint": oscillation_hint(pending),
        }}
    elif result["completed_ionic_steps"]:
        result["current_scf"] = {"value": {
            "state": "NO_UNASSOCIATED_ELECTRONIC_ROWS",
            "ionic_step": None,
            "step_alignment": "not_applicable",
            "reason": "snapshot ends after a completed F/E0 row",
            "electronic_steps": [],
            "electronic_step_count": 0,
            "last_electronic_step": None,
            "NELM": optional(nelm, "INCAR NELM is absent or not an integer"),
            "NELM_reached": optional(None, "no current electronic rows"),
            "oscillation_hint": optional(None, "no current electronic rows"),
        }}
    return result


def oscillation_hint(records: list[dict[str, Any]]) -> dict[str, Any]:
    energies = [
        item["energy_like_eV"]
        for item in records
        if item.get("energy_like_eV") is not None
    ]
    if len(energies) < 4:
        return {
            "suspected": None,
            "reason": "fewer than four parseable energy-like DAV/RMM values",
            "method": "heuristic sign alternation only; not a failure criterion",
        }
    deltas = [right - left for left, right in zip(energies, energies[1:])]
    signs = [1 if delta > 0 else -1 if delta < 0 else 0 for delta in deltas]
    tail = signs[-3:]
    alternating = all(
        left != 0 and right != 0 and left != right
        for left, right in zip(tail, tail[1:])
    )
    return {
        "suspected": alternating,
        "reason": (
            "last three energy-like deltas alternate sign"
            if alternating
            else "last three energy-like deltas do not alternate sign"
        ),
        "method": "heuristic sign alternation only; not a failure criterion",
    }


def nelm_reached_evidence(
    iterations: list[int],
    nelm: int | None,
) -> dict[str, Any]:
    if nelm is None:
        return {
            "value": None,
            "reason": "INCAR NELM is absent or not an integer",
        }
    if not iterations:
        return {
            "value": None,
            "reason": "no DAV/RMM electronic iteration is available",
        }
    return {"value": max(iterations) >= nelm}


def _field_from_line(
    lines: list[str],
    pattern: str,
    integer: bool = False,
) -> dict[str, Any]:
    compiled = re.compile(pattern, re.IGNORECASE)
    found: dict[str, Any] | None = None
    for line_number, line in enumerate(lines, 1):
        match = compiled.search(line)
        if match:
            raw_value = match.group(1)
            value: Any
            if integer:
                try:
                    value = int(raw_value)
                except ValueError:
                    value = None
            else:
                value = finite_number(raw_value)
            found = {
                "value": value,
                "raw": raw_value,
                "source": {"line": line_number},
            }
    return found or {"value": None, "source": None}


def _attach_path(item: dict[str, Any], path: Path) -> dict[str, Any]:
    result = dict(item)
    if result.get("source"):
        result["source"] = source(path, result["source"]["line"])
    return result


def parse_loops(lines: list[str], path: Path) -> dict[str, Any]:
    pattern = re.compile(
        rf"\b(LOOP\+?):\s+cpu time\s+({FLOAT_RE}):\s+real time\s+({FLOAT_RE})",
        re.IGNORECASE,
    )
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        match = pattern.search(line)
        if not match:
            continue
        cpu = finite_number(match.group(2))
        real = finite_number(match.group(3))
        records.append({
            "kind": match.group(1).upper(),
            "cpu_seconds": cpu,
            "real_seconds": real,
            "source": source(path, line_number),
            "raw": line.strip(),
        })
    def bucket(kind: str) -> dict[str, Any]:
        selected = [item for item in records if item["kind"] == kind]
        cpu_values = [
            item["cpu_seconds"] for item in selected
            if item["cpu_seconds"] is not None
        ]
        real_values = [
            item["real_seconds"] for item in selected
            if item["real_seconds"] is not None
        ]
        return {
            "records": selected,
            "count": len(selected),
            "cpu_seconds": sum(cpu_values) if cpu_values else None,
            "real_seconds": sum(real_values) if real_values else None,
            "reason": None if selected else f"no {kind} timing records in OUTCAR",
        }
    return {
        "LOOP_electronic": bucket("LOOP"),
        "LOOP_plus_ionic": bucket("LOOP+"),
        "all_records": records,
        "separation_note": (
            "LOOP and LOOP+ are retained as separate source-labelled buckets; "
            "no CPU utilization or idle/busy inference is made"
        ),
    }


def parse_errors_warnings(lines: list[str], path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        lowered = line.lower()
        if re.search(r"\bwarning\b|warning:", lowered):
            records.append({
                "kind": "warning",
                "source": source(path, line_number),
                "raw": line.rstrip(),
            })
        if re.search(
            r"fatal error|segmentation fault|mpi_abort|internal error|"
            r"error while|brmix\s*:\s*very serious|zbrent|very serious",
            lowered,
        ):
            records.append({
                "kind": "error_or_suspicion",
                "source": source(path, line_number),
                "raw": line.rstrip(),
            })
    return records


def parse_force_blocks(
    lines: list[str],
    path: Path,
    nions: int | None,
) -> dict[str, Any]:
    marker_re = re.compile(r"TOTAL-FORCE\s*\(eV/Angst\)", re.IGNORECASE)
    marker_indices = [index for index, line in enumerate(lines) if marker_re.search(line)]
    all_blocks: list[dict[str, Any]] = []
    complete_blocks: list[dict[str, Any]] = []
    incomplete_blocks: list[dict[str, Any]] = []
    if nions is None or nions <= 0:
        return {
            "marker_count": len(marker_indices),
            "complete_blocks": [],
            "incomplete_blocks": [],
            "last_complete": optional(
                None,
                "NIONS is unavailable; force rows cannot be classified as complete",
            ),
        }
    for marker_number, marker_index in enumerate(marker_indices, 1):
        next_marker = (
            marker_indices[marker_number]
            if marker_number < len(marker_indices)
            else len(lines)
        )
        rows: list[dict[str, Any]] = []
        for row_index in range(marker_index + 1, next_marker):
            raw = lines[row_index]
            parts = raw.split()
            if len(parts) < 6:
                if rows and raw.strip() and not set(raw.strip()) <= {"-", "="}:
                    # The force table ended before NIONS rows.
                    break
                continue
            values = [finite_number(value) for value in parts[:6]]
            if any(value is None for value in values):
                if rows and raw.strip():
                    break
                continue
            rows.append({
                "position_cart_A": [float(value) for value in values[:3]],
                "force_eV_A": [float(value) for value in values[3:6]],
                "source": source(path, row_index + 1),
                "raw": raw.strip(),
            })
            if len(rows) == nions:
                break
        block = {
            "block_number": marker_number,
            "marker": source(path, marker_index + 1),
            "row_count": len(rows),
            "rows": rows,
            "complete": len(rows) == nions,
        }
        all_blocks.append(block)
        if block["complete"]:
            complete_blocks.append(block)
        else:
            incomplete_blocks.append(block)
    return {
        "marker_count": len(marker_indices),
        "complete_blocks": complete_blocks,
        "incomplete_blocks": incomplete_blocks,
        "last_complete": (
            optional(complete_blocks[-1])
            if complete_blocks
            else optional(None, "no complete NIONS force block in this snapshot")
        ),
        "all_blocks": all_blocks,
    }


def vector_norm(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def max_force_entry(
    block: dict[str, Any],
    atoms: list[dict[str, Any]],
    allowed_indices: set[int] | None,
    kind: str,
    ionic_step: int | None,
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for atom, row in zip(atoms, block["rows"]):
        index = atom["index_1based"]
        if allowed_indices is not None and index not in allowed_indices:
            continue
        force = row["force_eV_A"]
        candidates.append({
            "index_1based": index,
            "species": atom.get("species"),
            "force_eV_A": force,
            "norm_eV_A": vector_norm(force),
            "force_row_source": row["source"],
            "ionic_step": ionic_step,
            "block_number": block["block_number"],
            "selection": kind,
        })
    return max(candidates, key=lambda item: item["norm_eV_A"]) if candidates else None


def parse_forces(
    outcar: dict[str, Any],
    poscar: dict[str, Any],
    ionic_steps: list[dict[str, Any]],
) -> dict[str, Any]:
    parsed = outcar["force_blocks"]
    complete = parsed["complete_blocks"]
    expected_nions = poscar.get("nions")
    actual_nions = outcar["identity"].get("NIONS", {}).get("value")
    alignment = "uncertain"
    alignment_reason = (
        "no explicit OUTCAR ionic-step marker or full-range alignment evidence "
        "was parsed; equal force-block and OSZICAR counts are insufficient"
    )
    if not poscar.get("valid"):
        status = "UNAVAILABLE"
        status_reason = "POSCAR is missing, truncated, or invalid"
    elif poscar.get("mask_status") == "UNSUPPORTED_PARTIAL_CONSTRAINTS":
        status = "UNSUPPORTED_PARTIAL_CONSTRAINTS"
        status_reason = "partial T/F flags are not interpreted as a free-force mask"
    elif poscar.get("mask_status") != "OK":
        status = "UNAVAILABLE"
        status_reason = f"POSCAR mask status is {poscar.get('mask_status')}"
    elif actual_nions is not None and expected_nions != actual_nions:
        status = "MISMATCH"
        status_reason = f"POSCAR nions={expected_nions} differs from OUTCAR NIONS={actual_nions}"
    else:
        status = "OK" if complete else "INCOMPLETE"
        status_reason = None if complete else "no complete force block is available"
    history: list[dict[str, Any]] = []
    for block_index, block in enumerate(complete):
        step_number = None
        history.append({
            "block_number": block["block_number"],
            "marker": block["marker"],
            "row_count": block["row_count"],
            "ionic_step": step_number,
            "step_alignment": alignment,
            "max_all": max_force_entry(block, poscar["atoms"], None, "all", step_number),
            "max_free": (
                max_force_entry(
                    block,
                    poscar["atoms"],
                    set(poscar["free_indices_1based"]),
                    "free",
                    step_number,
                )
                if status == "OK"
                else None
            ),
            "max_fixed": max_force_entry(
                block,
                poscar["atoms"],
                set(poscar["fixed_indices_1based"]),
                "fixed",
                step_number,
            ),
        })
    last = history[-1] if history else None
    if status == "OK" and last is not None:
        last_free = optional(last["max_free"], "no free atoms in the submitted mask")
    elif status == "UNSUPPORTED_PARTIAL_CONSTRAINTS":
        last_free = optional(None, status_reason)
    elif status == "MISMATCH":
        last_free = optional(None, status_reason)
    else:
        last_free = optional(None, status_reason or "last complete force block is unavailable")
    last_record: dict[str, Any] = {
        "value": None,
        "reason": "last complete force block is unavailable",
    }
    if last is not None:
        last_record = {
            "value": {
                "block_number": last["block_number"],
                "marker": last["marker"],
                "row_count": last["row_count"],
                "ionic_step": last["ionic_step"],
                "step_alignment": last["step_alignment"],
                "max_all": last["max_all"],
                "max_free": last_free,
                "max_fixed": optional(last["max_fixed"], "no fixed atoms in the submitted mask"),
            }
        }
    if parsed["incomplete_blocks"]:
        trailing = parsed["incomplete_blocks"][-1]["block_number"] == parsed["all_blocks"][-1]["block_number"]
        trailing_note = (
            "incomplete trailing force block retained as incomplete; it does not replace the preceding complete block"
            if trailing
            else "one or more incomplete force blocks were observed"
        )
    else:
        trailing_note = None
    return {
        "status": status,
        "reason": status_reason,
        "expected_nions_from_POSCAR": optional(expected_nions, "POSCAR nions unavailable"),
        "actual_NIONS_from_OUTCAR": optional(actual_nions, "OUTCAR NIONS unavailable"),
        "step_alignment": alignment,
        "step_alignment_reason": alignment_reason,
        "complete_block_count": len(complete),
        "incomplete_block_count": len(parsed["incomplete_blocks"]),
        "trailing_incomplete_note": trailing_note,
        "history": history,
        "last_complete": last_record,
    }


def parse_outcar(text: str | None, path: Path, poscar_nions: int | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source": source(path),
        "available": text is not None,
        "identity": {},
        "markers": {
            "electronic_ediff": [],
            "ionic_convergence": [],
            "normal_end": [],
        },
        "toten_records": [],
        "sigma0_records": [],
        "loops": None,
        "errors_warnings": [],
        "parameters": parse_outcar_parameters(None, path),
        "force_blocks": {
            "marker_count": 0,
            "complete_blocks": [],
            "incomplete_blocks": [],
            "last_complete": optional(None, "OUTCAR is missing or unreadable"),
        },
        "errors": [],
    }
    if text is None:
        result["errors"].append("OUTCAR is missing or unreadable")
        return result
    lines = text.splitlines()
    field_patterns: dict[str, tuple[str, bool]] = {
        "NIONS": (r"\bNIONS\s*=\s*(\d+)", True),
        "NELECT": (rf"\bNELECT\s*=\s*({FLOAT_RE})", False),
        "NKPTS": (r"\bNKPTS\s*=\s*(\d+)", True),
        "NBANDS": (r"\bNBANDS\s*=\s*(\d+)", True),
        "KPAR": (r"\bKPAR\s*=\s*(\d+)", True),
        "NCORE": (r"\bNCORE\s*=\s*(\d+)", True),
        "MPI_RANKS": (r"running\s+(\d+)\s+mpi-ranks", True),
    }
    for key, (pattern, is_integer) in field_patterns.items():
        result["identity"][key] = _attach_path(
            _field_from_line(lines, pattern, is_integer), path
        )
    ions_per_type: dict[str, Any] = {"value": None, "source": None}
    for line_number, line in enumerate(lines, 1):
        match = re.search(r"ions per type\s*=\s*((?:\d+\s+)+\d+)", line, re.IGNORECASE)
        if match:
            ions_per_type = {
                "value": [int(item) for item in match.group(1).split()],
                "source": source(path, line_number),
            }
    result["identity"]["IONS_PER_TYPE"] = ions_per_type
    version: dict[str, Any] = {"value": None, "source": None}
    for line_number, line in enumerate(lines, 1):
        if re.search(r"\bvasp\.\d", line, re.IGNORECASE):
            version = {"value": line.strip(), "source": source(path, line_number)}
    result["identity"]["VASP_VERSION"] = version
    for line_number, line in enumerate(lines, 1):
        lowered = line.lower()
        if "aborting loop because ediff is reached" in lowered:
            result["markers"]["electronic_ediff"].append({
                "source": source(path, line_number),
                "raw": line.rstrip(),
            })
        if "reached required accuracy" in lowered:
            result["markers"]["ionic_convergence"].append({
                "source": source(path, line_number),
                "raw": line.rstrip(),
            })
        if "general timing and accounting informations" in lowered:
            result["markers"]["normal_end"].append({
                "source": source(path, line_number),
                "raw": line.rstrip(),
            })
        toten = re.search(rf"free\s+energy\s+TOTEN\s*=\s*({FLOAT_RE})", line, re.IGNORECASE)
        if toten:
            result["toten_records"].append({
                "value_eV": number(toten.group(1)),
                "source": source(path, line_number),
                "raw": line.strip(),
                "role": "latest electronic/OUTCAR TOTEN record; not an OSZICAR ionic F/E0 value",
            })
        sigma = re.search(
            rf"energy\s+without\s+entropy\s*=\s*({FLOAT_RE}).*?"
            rf"energy\s*\(\s*sigma->0\s*\)\s*=\s*({FLOAT_RE})",
            line,
            re.IGNORECASE,
        )
        if sigma:
            result["sigma0_records"].append({
                "energy_without_entropy_eV": number(sigma.group(1)),
                "sigma0_eV": number(sigma.group(2)),
                "source": source(path, line_number),
                "raw": line.strip(),
                "role": "OUTCAR energy record; not an OSZICAR ionic F/E0 value",
            })
    result["loops"] = parse_loops(lines, path)
    result["errors_warnings"] = parse_errors_warnings(lines, path)
    result["parameters"] = parse_outcar_parameters(text, path)
    result["force_blocks"] = parse_force_blocks(lines, path, poscar_nions)
    result["last_electronic_toten"] = (
        optional(result["toten_records"][-1])
        if result["toten_records"]
        else optional(None, "no OUTCAR free-energy TOTEN record")
    )
    result["last_sigma0"] = (
        optional(result["sigma0_records"][-1])
        if result["sigma0_records"]
        else optional(None, "no OUTCAR sigma->0 record")
    )
    return result


def parse_key_value_text(text: str, path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            result[key] = {"value": value.strip(), "source": source(path, line_number)}
    return result


def parse_timing(text: str | None, path: Path) -> dict[str, Any]:
    if text is None:
        return {
            "available": False,
            "values": {},
            "wall_clock_seconds": optional(None, "run_timing.txt is absent"),
            "exit_code": optional(None, "run_timing.txt is absent"),
        }
    values = parse_key_value_text(text, path)
    wall_value = values.get("wall_seconds", {}).get("value")
    wall = finite_number(wall_value) if wall_value is not None else None
    if wall is not None:
        wall_result = {"value": wall, "source": values["wall_seconds"]["source"]}
    else:
        start_raw = values.get("start_utc", {}).get("value")
        end_raw = values.get("end_utc", {}).get("value")
        wall_result = {"value": None, "reason": "explicit wall_seconds or both start_utc/end_utc unavailable"}
        if start_raw and end_raw:
            try:
                start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                delta = (end - start).total_seconds()
                if math.isfinite(delta) and delta >= 0:
                    wall_result = {
                        "value": delta,
                        "source": {
                            "start": values["start_utc"]["source"],
                            "end": values["end_utc"]["source"],
                        },
                    }
            except ValueError:
                wall_result = {"value": None, "reason": "start_utc/end_utc are not parseable"}
    exit_raw = values.get("exit_code", {}).get("value")
    exit_code = parse_int_token(exit_raw) if exit_raw is not None else None
    return {
        "available": True,
        "values": values,
        "wall_clock_seconds": (
            {"value": wall_result["value"], "source": wall_result["source"]}
            if wall_result.get("value") is not None
            else wall_result
        ),
        "exit_code": (
            {"value": exit_code, "source": values["exit_code"]["source"]}
            if exit_code is not None
            else {"value": None, "reason": "exit_code is absent or not an integer"}
        ),
    }


def load_json_once(path: Path) -> tuple[Any, dict[str, Any] | None]:
    text, error = read_text_once(path)
    if error:
        return None, error
    try:
        return json.loads(text or ""), None
    except json.JSONDecodeError as parse_error:
        return None, {
            "path": str(path),
            "reason": f"invalid JSON at line {parse_error.lineno}: {parse_error.msg}",
        }


def load_approved_manifest(
    manifest_path: Path | str | None,
) -> tuple[Mapping[str, Any] | None, dict[str, Any] | None]:
    if manifest_path is None:
        return None, {
            "reason": "approved input manifest was not supplied; parameter comparison is UNKNOWN"
        }
    path = Path(manifest_path).resolve()
    data, error = load_json_once(path)
    if error is not None:
        return None, error
    if not isinstance(data, Mapping):
        return None, {
            "path": str(path),
            "reason": "approved manifest is not a JSON object",
        }
    return data, None


def load_metadata(root: Path, metadata_path: Path | None) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    status_data: Any = None
    process_data: Any = None
    watcher_data: Any = None
    timing_text: str | None = None
    timing_path = root / "run_timing.txt"
    timing_text, timing_error = read_text_once(timing_path)
    if timing_error and timing_path.exists():
        errors.append(timing_error)
    if timing_text is not None:
        sources.append(source(timing_path))
    status_path = root / "status.json"
    status_data, status_error = load_json_once(status_path)
    if status_error and status_path.exists():
        errors.append(status_error)
    if status_data is not None:
        sources.append(source(status_path))
    process_path = root / "process_snapshot.json"
    process_data, process_error = load_json_once(process_path)
    if process_error and process_path.exists():
        errors.append(process_error)
    if process_data is not None:
        sources.append(source(process_path))
    watcher_path = root / "watcher_snapshot.json"
    watcher_data, watcher_error = load_json_once(watcher_path)
    if watcher_error and watcher_path.exists():
        errors.append(watcher_error)
    if watcher_data is not None:
        sources.append(source(watcher_path))
    explicit_data: Any = None
    explicit_path: Path | None = None
    if metadata_path is not None:
        resolved = metadata_path.resolve()
        explicit_path = resolved
        if resolved == status_path.resolve():
            explicit_data = status_data
        elif resolved == process_path.resolve():
            explicit_data = process_data
        elif resolved == watcher_path.resolve():
            explicit_data = watcher_data
        else:
            explicit_data, explicit_error = load_json_once(resolved)
            if explicit_error:
                errors.append(explicit_error)
            else:
                sources.append(source(resolved))
    if isinstance(explicit_data, dict):
        if isinstance(explicit_data.get("status"), dict) and status_data is None:
            status_data = explicit_data["status"]
            status_path = resolved
        if isinstance(explicit_data.get("process"), (dict, list)) and process_data is None:
            process_data = explicit_data["process"]
            process_path = resolved
        if isinstance(explicit_data.get("watcher"), (dict, list)) and watcher_data is None:
            watcher_data = explicit_data["watcher"]
            watcher_path = resolved
        if isinstance(explicit_data.get("run_timing"), dict) and timing_text is None:
            timing_text = "\n".join(
                f"{key}={value}"
                for key, value in explicit_data["run_timing"].items()
            )
            timing_path = resolved
        if isinstance(explicit_data.get("status"), str) and status_data is None:
            status_data = explicit_data
            status_path = resolved
    return {
        "timing": parse_timing(timing_text, timing_path),
        "status": status_data,
        "process": process_data,
        "watcher": watcher_data,
        "explicit": explicit_data,
        "explicit_path": str(explicit_path) if explicit_path is not None else None,
        "file_metadata": (
            explicit_data.get("files")
            if isinstance(explicit_data, dict) and isinstance(explicit_data.get("files"), dict)
            else explicit_data.get("coverage")
            if isinstance(explicit_data, dict) and isinstance(explicit_data.get("coverage"), dict)
            else {}
        ),
        "status_path": str(status_path),
        "process_path": str(process_path),
        "watcher_path": str(watcher_path),
        "sources": sources,
        "errors": errors,
    }


def remote_observed_evidence(metadata: dict[str, Any]) -> dict[str, Any]:
    candidates: list[tuple[Any, Path]] = []
    explicit = metadata.get("explicit")
    explicit_path = metadata.get("explicit_path")
    if isinstance(explicit, dict) and explicit_path:
        candidates.append((explicit, Path(explicit_path)))
    status = metadata.get("status")
    status_path = metadata.get("status_path")
    if isinstance(status, dict) and status_path:
        candidates.append((status, Path(status_path)))
    for data, path in candidates:
        for key in ("remote_observed_utc", "observed_utc"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return {"value": value.strip(), "source": source(path)}
    return {
        "value": None,
        "reason": "remote observed timestamp was not supplied by collection metadata",
    }


def execution_summary(metadata: dict[str, Any]) -> dict[str, Any]:
    status_data = metadata.get("status")
    timing = metadata.get("timing", {})
    status_source = source(Path(metadata.get("status_path", "status.json")))
    process_source = source(Path(metadata.get("process_path", "process_snapshot.json")))
    watcher_source = source(Path(metadata.get("watcher_path", "watcher_snapshot.json")))
    remote_status = None
    canonical_status: dict[str, Any]
    supervisor = None
    runner = None
    if isinstance(status_data, dict):
        status_value = status_data.get("status")
        if isinstance(status_value, str):
            remote_status = {"value": status_value, "source": status_source}
            try:
                canonical, alias = normalize_state(status_value)
                canonical_status = {
                    "value": canonical,
                    "raw_value": status_value,
                    "legacy_alias": alias,
                    "source": status_source,
                }
            except ValueError as error:
                canonical_status = {
                    "value": UNKNOWN,
                    "raw_value": status_value,
                    "reason": str(error),
                    "source": status_source,
                }
        supervisor = status_data.get("supervisor_pid")
        runner = status_data.get("runner_pid")
    if remote_status is None:
        canonical_status = {
            "value": UNKNOWN,
            "reason": "status.json or equivalent status metadata is absent",
        }
    process_data = metadata.get("process")
    process_evidence = process_data if process_data is not None else None
    watcher_data = metadata.get("watcher")
    watcher_evidence = watcher_data if watcher_data is not None else None
    process_result = optional(
        process_evidence,
        "process_snapshot.json was not supplied; no process or tmux inference is made",
    )
    if process_evidence is not None:
        process_result["source"] = process_source
    watcher_result = optional(
        watcher_evidence,
        "watcher_snapshot.json was not supplied; watcher liveness is a separate observation",
    )
    if watcher_evidence is not None:
        watcher_result["source"] = watcher_source
    exit_evidence = timing.get("exit_code")
    if exit_evidence.get("value") is None and isinstance(status_data, dict):
        case_values = status_data.get("cases")
        if isinstance(case_values, list) and case_values:
            case_exit = case_values[0].get("exit_code") if isinstance(case_values[0], dict) else None
            if isinstance(case_exit, int):
                exit_evidence = {"value": case_exit, "source": status_source}
    return {
        "remote_status": (
            remote_status
            if remote_status is not None
            else {"value": None, "reason": "status.json or equivalent status metadata is absent"}
        ),
        "canonical_execution_state": canonical_status,
        "supervisor_pid": optional(supervisor, "supervisor PID is absent from status metadata"),
        "runner_pid": optional(runner, "runner PID is absent from status metadata"),
        "process_tmux_evidence": process_result,
        "watcher_evidence": watcher_result,
        "exit_code": exit_evidence,
        "wall_clock_seconds": metadata["timing"]["wall_clock_seconds"],
        "remote_observed_utc": remote_observed_evidence(metadata),
        "metadata_sources": metadata.get("sources", []),
    }


def status_summary(
    incar: dict[str, Any],
    oszicar: dict[str, Any],
    outcar: dict[str, Any],
) -> dict[str, Any]:
    kind = run_kind(incar)
    ediff_markers = outcar["markers"]["electronic_ediff"]
    ionic_markers = outcar["markers"]["ionic_convergence"]
    normal_markers = outcar["markers"]["normal_end"]
    current_scf = oszicar["current_scf"]["value"]
    current_state = current_scf.get("state") if isinstance(current_scf, dict) else None
    if current_state == "IN_PROGRESS":
        electronic_state = "CURRENT_SCF_IN_PROGRESS"
    elif oszicar["completed_ionic_steps"]:
        electronic_state = "COMPLETED_ROWS_PRESENT"
    else:
        electronic_state = "UNKNOWN"
    if kind["kind"] == "relaxation":
        ionic_state = "OBSERVED" if ionic_markers else "NOT_OBSERVED"
    elif kind["kind"] == "static":
        ionic_state = "NOT_APPLICABLE"
    else:
        ionic_state = "UNKNOWN"
    return {
        "execution_state": "NOT_DERIVED_FROM_OUTPUTS",
        "program_normal_end": {
            "observed": bool(normal_markers),
            "source": normal_markers[-1]["source"] if normal_markers else None,
            "reason": None if normal_markers else "normal-end marker not observed in this snapshot",
        },
        "electronic_scf": {
            "state": electronic_state,
            "completed_ionic_rows": len(oszicar["completed_ionic_steps"]),
            "EDIFF_marker_count": len(ediff_markers),
            "EDIFF_markers": ediff_markers,
            "current_scf": current_scf,
            "NELM": optional(incar_int(incar, "NELM"), "INCAR NELM is absent or not an integer"),
        },
        "ionic_convergence": {
            "state": ionic_state,
            "marker_count": len(ionic_markers),
            "markers": ionic_markers,
        },
        "scientific_acceptance": {
            "state": "NOT_EVALUATED",
            "reason": "snapshot extraction separates execution/convergence observations from Sol scientific acceptance",
        },
    }


def text_coverage(
    text: str | None,
    path: Path,
    metadata: dict[str, Any],
    name: str,
) -> dict[str, Any]:
    file_meta = metadata.get("file_metadata", {})
    item = file_meta.get(name) if isinstance(file_meta, dict) else None
    if not isinstance(item, dict):
        item = {}
    coverage = item.get("coverage")
    if coverage not in {"full", "head", "tail", "unknown"}:
        coverage = "unknown"
    coverage_reason = item.get("reason")
    if coverage == "unknown" and not coverage_reason:
        coverage_reason = "per-file coverage metadata is absent or unknown"
    original_start = item.get("original_start_line")
    if not isinstance(original_start, int) or original_start < 1:
        original_start_evidence = {
            "value": None,
            "reason": "original_start_line was not supplied by collection metadata",
        }
    else:
        original_start_evidence = {"value": original_start}
    collected_utc = item.get("collected_utc")
    if not isinstance(collected_utc, str) or not collected_utc.strip():
        collected_evidence = {
            "value": None,
            "reason": "collected_utc was not supplied by collection metadata",
        }
    else:
        collected_evidence = {"value": collected_utc.strip()}
    if text is None:
        return {
            "source": source(path),
            "available": False,
            "line_count": None,
            "ends_with_newline": None,
            "reason": "file is absent or unreadable",
            "coverage": coverage,
            "coverage_reason": coverage_reason,
            "original_start_line": original_start_evidence,
            "collected_utc": collected_evidence,
            "line_number_space": "local_snapshot",
        }
    return {
        "source": source(path),
        "available": True,
        "line_count": len(text.splitlines()),
        "ends_with_newline": text.endswith("\n"),
        "coverage": coverage,
        "coverage_reason": coverage_reason,
        "original_start_line": original_start_evidence,
        "collected_utc": collected_evidence,
        "line_number_space": "local_snapshot",
    }


def collect_conflicts(
    poscar: dict[str, Any],
    incar: dict[str, Any],
    outcar: dict[str, Any],
    oszicar: dict[str, Any],
    forces: dict[str, Any],
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    expected_nions = poscar.get("nions")
    actual_nions = outcar["identity"].get("NIONS", {}).get("value")
    if expected_nions is not None and actual_nions is not None and expected_nions != actual_nions:
        conflicts.append({
            "kind": "NIONS_MISMATCH",
            "detail": f"POSCAR nions={expected_nions} differs from OUTCAR NIONS={actual_nions}",
            "sources": [
                poscar.get("source_lines", {}).get("counts"),
                outcar["identity"].get("NIONS", {}).get("source"),
            ],
        })
    ions_per_type = outcar["identity"].get("IONS_PER_TYPE", {}).get("value")
    if ions_per_type and expected_nions is not None and sum(ions_per_type) != expected_nions:
        conflicts.append({
            "kind": "IONS_PER_TYPE_MISMATCH",
            "detail": f"sum(OUTCAR ions per type)={sum(ions_per_type)} differs from POSCAR nions={expected_nions}",
            "sources": [
                poscar.get("source_lines", {}).get("counts"),
                outcar["identity"].get("IONS_PER_TYPE", {}).get("source"),
            ],
        })
    for tag, field in (("NBANDS", "NBANDS"), ("KPAR", "KPAR"), ("NCORE", "NCORE")):
        input_value = incar_int(incar, tag)
        actual_value = outcar["identity"].get(field, {}).get("value")
        if input_value is not None and actual_value is not None and input_value != actual_value:
            conflicts.append({
                "kind": f"{tag}_MISMATCH",
                "detail": f"INCAR {tag}={input_value} differs from OUTCAR {field}={actual_value}",
                "sources": [
                    incar.get("parameters", {}).get(tag, {}).get("source"),
                    outcar["identity"].get(field, {}).get("source"),
                ],
            })
    complete_count = forces.get("complete_block_count", 0)
    ionic_count = len(oszicar.get("completed_ionic_steps", []))
    if complete_count and ionic_count and complete_count != ionic_count:
        conflicts.append({
            "kind": "FORCE_IONIC_STEP_ALIGNMENT",
            "detail": f"complete force blocks={complete_count} differs from completed OSZICAR ionic rows={ionic_count}",
            "sources": [
                outcar["source"],
                oszicar["source"],
            ],
        })
    return conflicts


def collect_snapshot(
    snapshot_dir: Path | str,
    metadata_path: Path | str | None = None,
    manifest_path: Path | str | None = None,
) -> dict[str, Any]:
    root = Path(snapshot_dir).resolve()
    if not root.is_dir():
        raise ValueError(f"snapshot directory does not exist: {root}")
    text_by_name: dict[str, str | None] = {}
    read_errors: list[dict[str, Any]] = []
    for name in PRIMARY_FILES:
        text, error = read_text_once(root / name)
        text_by_name[name] = text
        if error and (root / name).exists():
            read_errors.append(error)
    poscar = parse_poscar(text_by_name["POSCAR"], root / "POSCAR")
    incar = parse_incar(text_by_name["INCAR"], root / "INCAR")
    oszicar = parse_oszicar(text_by_name["OSZICAR"], root / "OSZICAR", incar)
    outcar = parse_outcar(text_by_name["OUTCAR"], root / "OUTCAR", poscar.get("nions"))
    metadata = load_metadata(
        root,
        Path(metadata_path) if metadata_path is not None else None,
    )
    forces = parse_forces(
        outcar,
        poscar,
        oszicar["completed_ionic_steps"],
    )
    approved_manifest, parameter_manifest_error = load_approved_manifest(manifest_path)
    parameter_comparison = compare_parameter_sources(
        approved_manifest,
        incar,
        outcar.get("parameters"),
        manifest_source=str(Path(manifest_path).resolve()) if manifest_path is not None else None,
    )
    inventory = {
        name: file_info(root / name, read_content=name not in SENSITIVE_FILES)
        for name in PRIMARY_FILES + OPTIONAL_FILES
    }
    missing = []
    for name in PRIMARY_FILES:
        if not inventory[name]["present"]:
            missing.append({
                "field": name,
                "source": source(root / name),
                "reason": "required primary evidence is absent from this snapshot",
            })
    limitations = [
        "This result is a single local snapshot; it does not imply current CPU activity or future progress.",
        "The extractor does not read or require POTCAR, CHGCAR, or WAVECAR contents.",
        "No force threshold, energy decrease, or single marker is used as a termination or scientific-acceptance decision.",
        "Scientific acceptance remains with the VASP Sol controller.",
    ]
    if outcar["force_blocks"]["incomplete_blocks"]:
        limitations.append(
            "At least one incomplete force block is present; the last complete block is retained and step alignment is not guessed."
        )
    if oszicar["current_scf"]["value"] and oszicar["current_scf"]["value"].get("state") == "IN_PROGRESS":
        limitations.append(
            "Unassociated DAV/RMM rows are treated as an in-progress SCF with unknown unfinished ionic-step number."
        )
    if parameter_manifest_error is not None:
        limitations.append(
            "Approved-manifest parameter comparison is UNKNOWN until an explicit input_manifest.json is supplied; no VASP defaults are inferred."
        )
    if poscar.get("mask_status") == "UNSUPPORTED_PARTIAL_CONSTRAINTS":
        limitations.append(
            "Partial T/F constraints are reported but the free-force conclusion is explicitly refused."
        )
    conflicts = collect_conflicts(poscar, incar, outcar, oszicar, forces)
    return {
        "schema": "vasp-progress-snapshot/v1",
        "parsed_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "observed_utc": remote_observed_evidence(metadata),
        "snapshot": {
            "directory": str(root),
            "primary_files_read_once": list(PRIMARY_FILES),
            "file_inventory": inventory,
            "sensitive_files_not_read": list(SENSITIVE_FILES),
        },
        "inputs": {
            "POSCAR": poscar,
            "INCAR": {
                **incar,
                "run_kind": run_kind(incar),
            },
        },
        "oszicar": oszicar,
        "outcar": {
            **outcar,
            "identity": {
                key: value
                for key, value in outcar["identity"].items()
            },
        },
        "parameter_comparison": parameter_comparison,
        "forces": forces,
        "execution": execution_summary(metadata),
        "status": status_summary(incar, oszicar, outcar),
        "coverage": {
            name: text_coverage(text_by_name[name], root / name, metadata, name)
            for name in PRIMARY_FILES
        },
        "conflicts": conflicts,
        "warnings": outcar["errors_warnings"],
        "errors": read_errors + poscar["errors"] + incar["errors"] + oszicar["errors"] + outcar["errors"] + metadata["errors"],
        "missing_evidence": missing,
        "limitations": limitations,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read one local VASP snapshot directory and emit traceable JSON."
    )
    parser.add_argument("snapshot_dir", help="local directory containing the snapshot")
    parser.add_argument(
        "--metadata",
        help="optional JSON metadata file; process/timing metadata is never collected over SSH here",
    )
    parser.add_argument(
        "--manifest",
        help="optional approved input_manifest.json for manifest -> INCAR -> OUTCAR parameter comparison",
    )
    parser.add_argument("--indent", type=int, default=2, help="JSON indentation (default: 2)")
    args = parser.parse_args(argv)
    try:
        result = collect_snapshot(args.snapshot_dir, args.metadata, args.manifest)
    except (OSError, ValueError) as error:
        print(json.dumps({
            "schema": "vasp-progress-snapshot/v1",
            "error": f"{type(error).__name__}: {error}",
        }, ensure_ascii=False, indent=args.indent))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=args.indent, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Build a closed, local VASP fresh-static delivery package from one manifest.

The approved input manifest owns all task identity, source identity, remote
identity and scientific values. This builder reads only the approved source
vasprun.xml frame and source POSCAR, never reads POTCAR, never executes a
generated script, and never connects to a remote host.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import shutil
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

try:
    from progress_snapshot import parse_poscar
    from static_delivery_templates import (
        postcheck_text as trusted_postcheck_text,
        preparer_text as trusted_preparer_text,
        runner_text as trusted_runner_text,
        task_env_text as trusted_task_env_text,
    )
    from static_delivery_check import (
        STATIC_PACKAGE_TEMPLATE,
        check_delivery_manifest,
        load_delivery_identity,
    )
    from static_runtime_guard import DEPENDENCY_SCHEMA, validate_dependency_descriptor, validate_paw_metadata
    from vasp_executor import validate_inputs
    from vasp_input_generator import render_incar, render_kpoints, validate_spec
except ImportError:  # pragma: no cover - package-style import fallback
    from .progress_snapshot import parse_poscar  # type: ignore
    from .static_delivery_templates import (  # type: ignore
        postcheck_text as trusted_postcheck_text,
        preparer_text as trusted_preparer_text,
        runner_text as trusted_runner_text,
        task_env_text as trusted_task_env_text,
    )
    from .static_delivery_check import (  # type: ignore
        STATIC_PACKAGE_TEMPLATE,
        check_delivery_manifest,
        load_delivery_identity,
    )
    from .static_runtime_guard import DEPENDENCY_SCHEMA, validate_dependency_descriptor, validate_paw_metadata  # type: ignore
    from .vasp_executor import validate_inputs  # type: ignore
    from .vasp_input_generator import render_incar, render_kpoints, validate_spec  # type: ignore


SCHEMA = "vasp-static-delivery-builder/v1"
INPUT_FILES = ("POSCAR", "INCAR", "KPOINTS")
MUTABLE_FILES = ("sol_review_gate.json",)
LOCAL_ONLY_FILES = ("execution_manifest.json", "generation_receipt.json", "README.md")


class BuilderError(RuntimeError):
    """A deterministic static-package build failure."""

    def __init__(self, code: str, message: str, **fields: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = fields

    def as_dict(self) -> dict[str, Any]:
        result = {"code": self.code, "message": self.message}
        result.update(self.fields)
        return result


def _fail(code: str, message: str, **fields: Any) -> None:
    raise BuilderError(code, message, **fields)


def _load_identity_or_fail(manifest: Mapping[str, Any]) -> Any:
    try:
        return load_delivery_identity(manifest)
    except Exception as error:
        code = getattr(error, "code", None)
        message = getattr(error, "message", None)
        if isinstance(code, str) and isinstance(message, str):
            _fail(code, message)
        raise


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as error:
        _fail("SPEC_MISSING", f"{label} is missing.", path=str(path))
        raise AssertionError from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail("SPEC_INVALID", f"{label} is not readable UTF-8 JSON.", path=str(path), error=str(error))
        raise AssertionError from error
    if not isinstance(value, dict):
        _fail("SPEC_NOT_OBJECT", f"{label} must be a JSON object.", path=str(path))
    return value, raw


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_path(path: Path, label: str) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        _fail("SOURCE_UNREADABLE", f"{label} is not readable.", path=str(path), error=str(error))
        raise AssertionError from error


def _resolve_source_file(root: Path, relative: str, label: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        _fail("SOURCE_PATH_INVALID", f"{label} must be a relative POSIX path.", value=relative)
    raw_parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts) or PurePosixPath(relative).is_absolute():
        _fail("SOURCE_PATH_INVALID", f"{label} must not be absolute or escape source-dir.", value=relative)
    unresolved = root / Path(*raw_parts)
    cursor = root.resolve()
    for part in raw_parts:
        cursor = cursor / part
        if cursor.is_symlink():
            _fail("SOURCE_PATH_SYMLINK", f"{label} must not traverse a symbolic link.", value=relative)
    candidate = unresolved.resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        _fail("SOURCE_PATH_ESCAPE", f"{label} escapes source-dir.", value=relative)
        raise AssertionError from error
    if not candidate.is_file():
        _fail("SOURCE_FILE_MISSING", f"{label} does not resolve to a file.", path=str(candidate))
    return candidate


def _finite(value: str, label: str) -> float:
    try:
        result = float(value.replace("D", "E").replace("d", "e"))
    except ValueError as error:
        _fail("SOURCE_NUMBER_INVALID", f"{label} contains a non-numeric token.", value=value)
        raise AssertionError from error
    if not math.isfinite(result):
        _fail("SOURCE_NUMBER_INVALID", f"{label} contains a non-finite number.", value=value)
    return result


def _rows(parent: ET.Element | None, name: str, label: str) -> tuple[list[list[str]], list[list[float]]]:
    if parent is None:
        _fail("SOURCE_XML_STRUCTURE_MISSING", f"Missing XML structure while reading {label}.")
    varray = parent.find(f"./varray[@name='{name}']")
    if varray is None:
        _fail("SOURCE_XML_VARRAY_MISSING", f"vasprun.xml is missing {label}.")
    tokens: list[list[str]] = []
    values: list[list[float]] = []
    for index, row in enumerate(varray.findall("./v"), start=1):
        row_tokens = (row.text or "").split()
        if len(row_tokens) != 3:
            _fail("SOURCE_XML_ROW_INVALID", f"{label} row {index} is not a three-number row.")
        tokens.append(row_tokens)
        values.append([_finite(token, f"{label} row {index}") for token in row_tokens])
    return tokens, values


def _read_xml_frame(path: Path, index_1_based: int) -> dict[str, Any]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as error:
        _fail("SOURCE_XML_INVALID", "The approved vasprun.xml cannot be parsed.", path=str(path), error=str(error))
        raise AssertionError from error
    calculations = root.findall("./calculation")
    if index_1_based < 1 or index_1_based > len(calculations):
        _fail(
            "SOURCE_FRAME_MISSING",
            "The approved calculation index does not exist in vasprun.xml.",
            calculation_index_1_based=index_1_based,
            calculation_count=len(calculations),
        )
    calculation = calculations[index_1_based - 1]
    structure = calculation.find("./structure")
    crystal = structure.find("./crystal") if structure is not None else None
    basis_tokens, basis = _rows(crystal, "basis", "structure.crystal.basis")
    position_tokens, positions = _rows(structure, "positions", "structure.positions")
    if len(basis) != 3 or len(position_tokens) == 0:
        _fail("SOURCE_XML_STRUCTURE_INVALID", "The approved XML frame has no complete cell or positions.")
    result = {
        "calculation_index_1_based": index_1_based,
        "basis_tokens": basis_tokens,
        "cell_A": basis,
        "position_tokens": position_tokens,
        "positions": positions,
        "calculation_count": len(calculations),
    }
    return result


def _matrix_close(left: Any, right: Any, tolerance: float = 1e-8) -> bool:
    return (
        isinstance(left, list)
        and isinstance(right, list)
        and len(left) == len(right) == 3
        and all(
            isinstance(left_row, list)
            and isinstance(right_row, list)
            and len(left_row) == len(right_row) == 3
            and all(math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=tolerance) for a, b in zip(left_row, right_row))
            for left_row, right_row in zip(left, right)
        )
    )


def _read_source_poscar(path: Path, *, require_constraints: bool = True) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        _fail("SOURCE_POSCAR_UNREADABLE", "The approved source POSCAR cannot be read.", path=str(path), error=str(error))
        raise AssertionError from error
    parsed = parse_poscar(text, path)
    if not parsed.get("valid") or parsed.get("errors"):
        _fail("SOURCE_POSCAR_INVALID", "The approved source POSCAR is not structurally valid.", errors=parsed.get("errors", []))
    if parsed.get("species_unresolved") or any(item is None for item in parsed.get("species", [])):
        _fail("SOURCE_POSCAR_SPECIES_UNRESOLVED", "The approved source POSCAR must declare species labels.")
    if parsed.get("mask_status") != "OK" or (require_constraints and parsed.get("selective_dynamics") is not True):
        _fail(
            "SOURCE_POSCAR_CONSTRAINTS_UNSUPPORTED",
            "The approved source POSCAR must have complete Selective Dynamics flags.",
            selective_dynamics=parsed.get("selective_dynamics"),
            mask_status=parsed.get("mask_status"),
        )
    lines = text.splitlines()
    if len(lines) < 5:
        _fail("SOURCE_POSCAR_INVALID", "The approved source POSCAR is truncated.")
    scale = _finite(lines[1].strip(), "POSCAR scale")
    if scale <= 0:
        _fail("SOURCE_POSCAR_SCALE_UNSUPPORTED", "Only positive scalar POSCAR scaling is supported.", scale=scale)
    raw_lattice = parsed.get("lattice")
    if not isinstance(raw_lattice, list) or len(raw_lattice) != 3:
        _fail("SOURCE_POSCAR_CELL_INVALID", "The approved source POSCAR has no complete lattice.")
    scaled_lattice = [[scale * float(value) for value in row] for row in raw_lattice]
    cursor = 7
    if cursor < len(lines) and lines[cursor].strip().lower().startswith("selective"):
        cursor += 1
    coordinate_mode = parsed.get("coordinate_mode")
    position_start = cursor + 1
    position_tokens = [
        lines[position_start + index].split()[:3]
        for index in range(parsed["nions"])
    ]
    basis_tokens = [line.split()[:3] for line in lines[2:5]]
    if any(len(row) != 3 for row in position_tokens) or any(len(row) != 3 for row in basis_tokens):
        _fail("SOURCE_POSCAR_INVALID", "The approved POSCAR coordinate/cell tokens are incomplete.", path=str(path))
    return {
        "text": text,
        "parsed": parsed,
        "scale": scale,
        "scale_token": lines[1].strip(),
        "scaled_cell": scaled_lattice,
        "basis_tokens": basis_tokens,
        "position_tokens": position_tokens,
        "coordinate_mode": coordinate_mode,
    }


def _check_source_constraints(spec: Mapping[str, Any], poscar: Mapping[str, Any], frame: Mapping[str, Any]) -> None:
    structure = spec.get("structure")
    if not isinstance(structure, dict):
        _fail("APPROVED_STRUCTURE_MISSING", "The approved manifest must declare structure constraints.")
    parsed = poscar["parsed"]
    for field, actual in (
        ("species_order", parsed.get("species")),
        ("counts", parsed.get("counts")),
        ("nions", parsed.get("nions")),
        ("fixed_global_indices", parsed.get("fixed_indices_1based")),
        ("free_global_count", len(parsed.get("free_indices_1based", []))),
    ):
        if field not in structure:
            _fail("APPROVED_STRUCTURE_FIELD_MISSING", f"Approved structure.{field} is required.", field=field)
        if structure[field] != actual:
            _fail(
                "SOURCE_APPROVED_CONSTRAINT_CONFLICT",
                f"Source POSCAR conflicts with approved structure.{field}.",
                field=field,
                expected=structure[field],
                actual=actual,
            )
    if not _matrix_close(poscar["scaled_cell"], frame["cell_A"]):
        _fail(
            "SOURCE_CELL_FRAME_CONFLICT",
            "Source POSCAR cell differs from the approved XML frame cell.",
            poscar_cell=poscar["scaled_cell"],
            xml_cell=frame["cell_A"],
        )
    if "cell_A" in structure and not _matrix_close(structure["cell_A"], frame["cell_A"]):
        _fail(
            "SOURCE_APPROVED_CELL_CONFLICT",
            "Approved structure.cell_A differs from the selected XML frame cell.",
            expected=structure["cell_A"],
            actual=frame["cell_A"],
        )


def _render_poscar(identity: Any, poscar: Mapping[str, Any], frame: Mapping[str, Any]) -> bytes:
    parsed = poscar["parsed"]
    atoms = parsed["atoms"]
    if len(atoms) != len(frame["position_tokens"]):
        _fail(
            "SOURCE_XML_NIONS_MISMATCH",
            "Selected XML frame positions do not match source POSCAR NIONS.",
            xml_positions=len(frame["position_tokens"]),
            nions=len(atoms),
        )
    if identity.source_kind == "xml_frame":
        comment = f"{identity.task_id} static frame {identity.source_calculation_index_1_based}"
    else:
        comment = f"{identity.task_id} static geometry {identity.source_kind}"
    lines = [
        comment,
        str(frame.get("scale_token", "1.0")),
        *(" ".join(row) for row in frame["basis_tokens"]),
        " ".join(parsed["species"]),
        " ".join(str(value) for value in parsed["counts"]),
        "Selective Dynamics",
        "Direct",
    ]
    for tokens, atom in zip(frame["position_tokens"], atoms):
        flags = atom.get("flags")
        if not isinstance(flags, list) or len(flags) != 3:
            _fail("SOURCE_POSCAR_CONSTRAINTS_UNSUPPORTED", "A source atom lacks a complete T/F mask.")
        rendered_flags = ["T" if flag else "F" for flag in flags]
        lines.append(" ".join(tokens + rendered_flags))
    return (chr(10).join(lines) + chr(10)).encode("utf-8")


def _require_static_approval(spec: Mapping[str, Any]) -> None:
    authorization = spec.get("authorization")
    if isinstance(authorization, Mapping):
        for key in ("upload", "launch", "remote_prepare", "automatic_retry", "follow_on_task"):
            if authorization.get(key) is True:
                _fail(
                    "APPROVED_AUTHORIZATION_ALREADY_OPEN",
                    "The builder refuses to silently convert an already-authorized spec into a new package.",
                    field=key,
                )
    incar = spec.get("incar")
    if not isinstance(incar, Mapping) or incar.get("IBRION") != -1 or incar.get("NSW") != 0 or incar.get("ISIF") != 2:
        _fail("UNSUPPORTED_STATIC_FORM", "The builder supports only IBRION=-1, NSW=0, ISIF=2.")
    if incar.get("ISTART") != 0 or incar.get("ICHARG") != 2:
        _fail("UNSUPPORTED_RESTART_MODE", "The static builder requires fresh ISTART=0 and ICHARG=2.")
    if any(incar.get(feature) is not False for feature in ("external_field", "soc", "dispersion", "projection_output")):
        _fail("UNSUPPORTED_FEATURE", "Static builder requires explicit false external_field, soc, dispersion and projection_output.")


def _compatibility_views(spec: dict[str, Any], identity: Any) -> None:
    spec["delivery_identity"] = identity.as_dict()
    spec["task_id"] = identity.task_id
    spec["unit_id"] = identity.unit_id
    spec["host"] = dict(identity.host)
    spec["remote_batch_dir"] = identity.remote_batch_dir
    spec["case"] = identity.case
    spec["runtime_input_dir"] = identity.runtime_input_dir
    spec["case_dir"] = identity.runtime_input_dir
    spec["tmux_session"] = identity.tmux_session
    if identity.source_kind == "xml_frame":
        spec["source"] = {
            "path": identity.source_path,
            "sha256": identity.source_sha256,
            "calculation_index_1_based": identity.source_calculation_index_1_based,
            "poscar_path": identity.source_poscar_path,
            "poscar_sha256": identity.source_poscar_sha256,
        }
    else:
        spec["source"] = copy.deepcopy(identity.source_descriptor)
    spec["remote_target"] = {
        "host": dict(identity.host),
        "port": identity.host["port"],
        "batch_dir": identity.remote_batch_dir,
        "case_dir": identity.runtime_input_dir,
        "tmux_session": identity.tmux_session,
    }
    spec["progress_evidence"] = {
        "host": dict(identity.host),
        "remote_batch_dir": identity.remote_batch_dir,
        "case": identity.case,
        "runtime_input_dir": identity.runtime_input_dir,
    }


def _output_requirements(spec: Mapping[str, Any]) -> dict[str, Any]:
    structure = spec["structure"]
    incar = spec["incar"]
    parallel = spec["parallel"]
    identity_fields = ["NIONS", "NELECT", "NBANDS", "ISPIN", "KPAR", "NCORE", "MPI_RANKS"]
    expected_identity = {
        "NIONS": structure["nions"],
        "NELECT": structure["nelect"],
        "NBANDS": incar["NBANDS"],
        "ISPIN": incar["ISPIN"],
        "KPAR": parallel["kpar"],
        "NCORE": parallel["ncore"],
        "MPI_RANKS": parallel["mpi_ranks"],
    }
    if type(incar.get("NUPDOWN")) is int:
        identity_fields.append("NUPDOWN")
        expected_identity["NUPDOWN"] = incar["NUPDOWN"]
    contract = spec.get("output_contract", {})
    if not isinstance(contract, Mapping):
        _fail("OUTPUT_CONTRACT_INVALID", "output_contract must be an object when supplied.")
    explicit_identity = contract.get("expected_identity", {})
    if not isinstance(explicit_identity, Mapping):
        _fail("OUTPUT_CONTRACT_INVALID", "output_contract.expected_identity must be an object.")
    if set(explicit_identity) - {"NKPTS"}:
        _fail("OUTPUT_CONTRACT_INVALID", "Only explicitly approved NKPTS is supported in output_contract.expected_identity.")
    if "NKPTS" in explicit_identity:
        nkpts = explicit_identity["NKPTS"]
        if type(nkpts) is not int or nkpts <= 0:
            _fail("OUTPUT_CONTRACT_NKPTS_INVALID", "Explicit output_contract.expected_identity.NKPTS must be a positive integer.")
        identity_fields.append("NKPTS")
        expected_identity["NKPTS"] = nkpts
    return {
        "schema": "vasp-executor-output-requirements/v1",
        "kind": "static",
        "required_files": ["OUTCAR", "OSZICAR", "vasprun.xml", "CONTCAR", "vasp.stdout", "vasp.stderr", "run_timing.txt"],
        "nonempty_files": ["OUTCAR", "OSZICAR", "vasprun.xml", "CONTCAR", "vasp.stdout", "run_timing.txt"],
        "optional_files": ["IBZKPT", "EIGENVAL", "XDATCAR", "CHGCAR", "WAVECAR"],
        "allow_empty_files": ["vasp.stderr"],
        "identity_fields": identity_fields,
        "expected_identity": expected_identity,
        "markers": [
            {"file": "OUTCAR", "name": "program_end", "pattern": "General timing and accounting informations", "category": "program"},
            {"file": "OUTCAR", "name": "electronic_ediff", "pattern": "aborting loop because EDIFF is reached", "category": "electronic"},
        ],
        "static_policy": {
            "ionic_convergence_required": False,
            "geometry_change_allowed_A": 1e-5,
            "scientific_acceptance": "Sol review; mechanical postcheck is not scientific acceptance",
        },
    }


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")


def _runtime_guard_bytes() -> bytes:
    path = Path(__file__).resolve().with_name("static_runtime_guard.py")
    try:
        return path.read_bytes()
    except OSError as error:
        _fail("RUNTIME_GUARD_MISSING", "The trusted static runtime guard is not readable.", path=str(path), error=str(error))
        raise AssertionError from error


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_bytes(_json_bytes(value))


def _task_env_text() -> bytes:
    return (
        "#!/usr/bin/env bash\n"
        "set -u\n"
        "TASK_ENV_DIR=\"$(cd -- \"$(dirname -- \"$0\")\" && pwd)\"\n"
        "export VASP_STATIC_MANIFEST=\"$TASK_ENV_DIR/input_manifest.json\"\n"
        "export VASP_STATIC_TEMPLATE=\"vasp-static-package/v1\"\n"
    ).encode("utf-8")


def _runner_text() -> bytes:
    return r"""#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
MANIFEST_PATH="$SCRIPT_DIR/input_manifest.json"
. "$SCRIPT_DIR/task_env.sh"

IFS='|' read -r BATCH_DIR CASE_ID RUNTIME_DIR TMUX_SESSION VASP_BIN MPI_LAUNCHER MPI_RANKS < <(
python3 - "$MANIFEST_PATH" <<'PY'
import json
import sys

manifest = json.loads(open(sys.argv[1], encoding="utf-8").read())
identity = manifest["delivery_identity"]
plan = manifest["execution_plan"]
environment = manifest["environment"]
parallel = manifest["parallel"]
if manifest.get("delivery_template") != "vasp-static-package/v1":
    raise SystemExit("unsupported static package template")
if plan.get("kind") != "static" or plan.get("ibrion") != -1 or plan.get("nsw") != 0:
    raise SystemExit("manifest is not the approved fresh static form")
if plan.get("run_count") != 1 or plan.get("fresh") is not True:
    raise SystemExit("manifest is not a single fresh run")
print("|".join(
    str(value)
    for value in (
        identity["remote_batch_dir"],
        identity["case"],
        identity["runtime_input_dir"],
        identity["tmux_session"],
        environment["vasp_bin"],
        environment["mpi_launcher"],
        parallel["mpi_ranks"],
    )
))
PY
)

if [ "$RUNTIME_DIR" != "$BATCH_DIR/$CASE_ID" ]; then
    echo "derived runtime directory does not match delivery identity" >&2
    exit 2
fi
if [ -z "$VASP_BIN" ] || [ -z "$MPI_LAUNCHER" ] || [ -z "$MPI_RANKS" ]; then
    echo "manifest execution environment is incomplete" >&2
    exit 2
fi
CASE_DIR="$RUNTIME_DIR"
if [ ! -d "$CASE_DIR" ]; then
    echo "prepared case directory is missing: $CASE_DIR" >&2
    exit 2
fi

python3 "$SCRIPT_DIR/static_postcheck.py" preflight \
    --manifest "$MANIFEST_PATH" \
    --input-dir "$SCRIPT_DIR" \
    --case-dir "$CASE_DIR"

cd -- "$CASE_DIR"
if [ -e .run_once ]; then
    echo "single-run lock already exists" >&2
    exit 2
fi
( set -o noclobber; : > .run_once ) 2>/dev/null || {
    echo "could not acquire single-run lock" >&2
    exit 2
}
printf 'tmux_session=%s\n' "$TMUX_SESSION"
START_EPOCH="$(date +%s)"
set +e
OMP_NUM_THREADS=1 "$MPI_LAUNCHER" -np "$MPI_RANKS" "$VASP_BIN" > vasp.stdout 2> vasp.stderr
VASP_STATUS=$?
set -e
END_EPOCH="$(date +%s)"
{
    printf 'exit_code=%s\n' "$VASP_STATUS"
    printf 'start_epoch=%s\n' "$START_EPOCH"
    printf 'end_epoch=%s\n' "$END_EPOCH"
} > run_timing.txt

POSTCHECK_STATUS=0
python3 "$SCRIPT_DIR/static_postcheck.py" postcheck \
    --manifest "$MANIFEST_PATH" \
    --input-dir "$SCRIPT_DIR" \
    --case-dir "$CASE_DIR" \
    --requirements "$SCRIPT_DIR/output_requirements.json" || POSTCHECK_STATUS=$?
if [ "$VASP_STATUS" -ne 0 ]; then
    exit "$VASP_STATUS"
fi
exit "$POSTCHECK_STATUS"
""".encode("utf-8")


def _preparer_text() -> bytes:
    return r"""#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
MANIFEST_PATH="$SCRIPT_DIR/input_manifest.json"
. "$SCRIPT_DIR/task_env.sh"

IFS='|' read -r BATCH_DIR CASE_ID RUNTIME_DIR PAW_ROOT COMPONENTS < <(
python3 - "$MANIFEST_PATH" "$SCRIPT_DIR/paw_identity.json" <<'PY'
import json
import sys

manifest = json.loads(open(sys.argv[1], encoding="utf-8").read())
paw = json.loads(open(sys.argv[2], encoding="utf-8").read())
identity = manifest["delivery_identity"]
if manifest.get("delivery_template") != "vasp-static-package/v1":
    raise SystemExit("unsupported static package template")
components = paw.get("ordered_components")
if not isinstance(components, list) or not components:
    raise SystemExit("ordered PAW component identity is missing")
environment = manifest["environment"]
print("|".join(
    str(value)
    for value in (
        identity["remote_batch_dir"],
        identity["case"],
        identity["runtime_input_dir"],
        environment["paw_root"],
        " ".join(str(item) for item in components),
    )
))
PY
)

if [ "$RUNTIME_DIR" != "$BATCH_DIR/$CASE_ID" ]; then
    echo "derived runtime directory does not match delivery identity" >&2
    exit 2
fi
if [ -z "$PAW_ROOT" ] || [ -z "$COMPONENTS" ]; then
    echo "manifest PAW identity is incomplete" >&2
    exit 2
fi
CASE_DIR="$RUNTIME_DIR"
mkdir -p -- "$CASE_DIR"
for name in POSCAR INCAR KPOINTS POTCAR; do
    if [ -e "$CASE_DIR/$name" ]; then
        echo "refusing to overwrite existing case file: $name" >&2
        exit 2
    fi
done
for name in POSCAR INCAR KPOINTS; do
    cp -- "$SCRIPT_DIR/$name" "$CASE_DIR/$name"
done
: > "$CASE_DIR/POTCAR"
for component in $COMPONENTS; do
    component_file="$PAW_ROOT/$component/POTCAR"
    if [ ! -f "$component_file" ]; then
        echo "locked PAW component is missing: $component_file" >&2
        exit 2
    fi
    cat -- "$component_file" >> "$CASE_DIR/POTCAR"
done
python3 "$SCRIPT_DIR/static_postcheck.py" preflight \
    --manifest "$MANIFEST_PATH" \
    --input-dir "$SCRIPT_DIR" \
    --case-dir "$CASE_DIR"
""".encode("utf-8")


def _static_postcheck_text() -> bytes:
    return r"""#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

try:
    from vasp_executor import postcheck, preflight_inputs
except ImportError as error:
    raise SystemExit("vasp_executor.py must be available in the execution environment") from error

parser = argparse.ArgumentParser(description="Static-package mechanical preflight/postcheck.")
subparsers = parser.add_subparsers(dest="mode", required=True)
for mode in ("preflight", "postcheck"):
    subparser = subparsers.add_parser(mode)
    subparser.add_argument("--manifest", required=True, type=Path)
    subparser.add_argument("--input-dir", type=Path)
    subparser.add_argument("--case-dir", required=(mode == "postcheck"), type=Path)
    subparser.add_argument("--requirements", type=Path)
args = parser.parse_args()
if args.mode == "preflight":
    result = preflight_inputs(args.manifest, args.input_dir, args.case_dir, args.requirements)
else:
    result = postcheck(args.manifest, args.input_dir, args.case_dir, args.requirements)
print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
raise SystemExit(0 if result.get("passed") else 2)
""".encode("utf-8")


def _task_env_text() -> bytes:
    return trusted_task_env_text()


def _runner_text() -> bytes:
    return trusted_runner_text()


def _preparer_text() -> bytes:
    return trusted_preparer_text()


def _static_postcheck_text() -> bytes:
    return trusted_postcheck_text()


def _destination_check(source_dir: Path | None, output_dir: Path, allow_empty_destination: bool) -> None:
    if source_dir is not None:
        if output_dir == source_dir:
            _fail("DESTINATION_IS_SOURCE", "Output directory cannot be the source directory.")
        try:
            output_dir.relative_to(source_dir)
        except ValueError:
            pass
        else:
            _fail("DESTINATION_INSIDE_SOURCE", "Output directory cannot be inside source-dir.")
    if not output_dir.exists():
        return
    if output_dir.is_symlink() or not output_dir.is_dir():
        _fail("DESTINATION_UNSAFE", "Output path exists but is not a normal directory.", path=str(output_dir))
    try:
        children = list(output_dir.iterdir())
    except OSError as error:
        _fail("DESTINATION_UNREADABLE", "Output directory cannot be inspected.", path=str(output_dir), error=str(error))
    if children:
        _fail("DESTINATION_NOT_EMPTY", "Refusing to overwrite a non-empty output directory.", path=str(output_dir))
    if not allow_empty_destination:
        _fail("DESTINATION_EXISTS", "An existing empty output directory requires --allow-empty-destination.", path=str(output_dir))


def _paw_metadata(spec: Mapping[str, Any]) -> dict[str, Any]:
    source = spec.get("paw_identity")
    if not isinstance(source, Mapping):
        source = spec.get("paw")
    if not isinstance(source, Mapping):
        _fail("PAW_IDENTITY_MISSING", "The approved manifest must declare non-sensitive ordered PAW metadata.")
    components = source.get("ordered_components")
    if (
        not isinstance(components, list)
        or not components
        or not all(isinstance(item, str) and item.strip() for item in components)
    ):
        _fail("PAW_IDENTITY_INVALID", "paw_identity.ordered_components must be a non-empty string list.")
    approved_components = source.get("components")
    if not isinstance(approved_components, list) or len(approved_components) != len(components):
        _fail("PAW_COMPONENTS_MISSING", "paw_identity.components must lock every ordered PAW component.")
    normalized_components: list[dict[str, str]] = []
    for index, item in enumerate(approved_components):
        if not isinstance(item, Mapping):
            _fail("PAW_COMPONENT_INVALID", "Each PAW component identity must be an object.", index=index)
        name = item.get("name")
        relative_path = item.get("relative_path")
        digest = item.get("sha256")
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(relative_path, str)
            or not relative_path
            or "\\" in relative_path
            or any(part in {"", ".", ".."} for part in PurePosixPath(relative_path).parts)
            or PurePosixPath(relative_path).is_absolute()
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in digest)
        ):
            _fail("PAW_COMPONENT_INVALID", "Each PAW component must have a safe relative path and SHA-256.", index=index)
        if name != components[index]:
            _fail("PAW_ORDER_CONFLICT", "PAW component names must match ordered_components.", index=index)
        normalized_components.append(
            {"name": name, "relative_path": PurePosixPath(relative_path).as_posix(), "sha256": digest.lower()}
        )
    combined_sha256 = source.get(
        "combined_sha256",
        source.get("combined_potcar_sha256", source.get("potcar_sha256")),
    )
    if not isinstance(combined_sha256, str) or len(combined_sha256) != 64 or any(
        character not in "0123456789abcdefABCDEF" for character in combined_sha256
    ):
        _fail("PAW_COMBINED_HASH_MISSING", "paw_identity must lock the combined POTCAR SHA-256.")
    result: dict[str, Any] = {
        "schema": "vasp-paw-identity/v1",
        "environment_id": spec.get("environment", {}).get("environment_id"),
        "family": source.get("family", source.get("paw_family")),
        "ordered_components": list(components),
        "components": normalized_components,
        "combined_sha256": combined_sha256.lower(),
        "content_local": False,
        "potcar_content_present": False,
        "source": "approved metadata only; POTCAR is never read or stored by the builder",
    }
    for key in ("component_sha256", "zval", "expected_nelect"):
        if key in source:
            result[key] = copy.deepcopy(source[key])
    return result


def _runtime_dependencies(spec: Mapping[str, Any]) -> dict[str, Any]:
    environment = spec.get("environment")
    if not isinstance(environment, Mapping):
        _fail("RUNTIME_ENVIRONMENT_MISSING", "The approved manifest must declare a complete runtime environment.")
    approved = spec.get("runtime_dependencies")
    if not isinstance(approved, Mapping):
        _fail("RUNTIME_DEPENDENCIES_MISSING", "The approved manifest must lock remote runtime dependencies.")
    required_environment = (
        "python_bin",
        "toolchain_root",
        "pythonpath",
        "ld_library_path",
        "mpi_launcher",
        "mpi_args",
        "tmux_bin",
        "tmux_required",
        "cpu_binding",
        "vasp_bin",
    )
    missing = [key for key in required_environment if key not in environment]
    if missing:
        _fail("RUNTIME_ENVIRONMENT_FIELD_MISSING", "Approved environment fields are missing.", fields=missing)
    modules = approved.get("modules")
    if not isinstance(modules, list) or not modules:
        _fail("RUNTIME_MODULES_MISSING", "runtime_dependencies.modules must be provided by the approved environment.")
    result = {
        "schema": DEPENDENCY_SCHEMA,
        "environment_id": environment.get("environment_id"),
        "python_bin": environment["python_bin"],
        "toolchain_root": environment["toolchain_root"],
        "pythonpath": copy.deepcopy(environment["pythonpath"]),
        "ld_library_path": copy.deepcopy(environment["ld_library_path"]),
        "mpi_launcher": environment["mpi_launcher"],
        "mpi_args": copy.deepcopy(environment["mpi_args"]),
        "tmux_bin": environment["tmux_bin"],
        "tmux_required": environment["tmux_required"],
        "vasp_bin": environment["vasp_bin"],
        "omp_num_threads": spec["parallel"]["omp_num_threads"],
        "cpu_binding": copy.deepcopy(environment["cpu_binding"]),
        "modules": copy.deepcopy(modules),
    }
    try:
        validate_dependency_descriptor(spec, result)
    except Exception as error:
        code = getattr(error, "code", None)
        message = getattr(error, "message", None)
        fields = getattr(error, "fields", {})
        if isinstance(code, str) and isinstance(message, str):
            _fail(code, message, **fields)
        raise
    return result


def _static_plan(spec: Mapping[str, Any]) -> dict[str, Any]:
    existing = spec.get("execution_plan")
    plan = dict(existing) if isinstance(existing, Mapping) else {}
    plan.update(
        {
            "kind": "static",
            "static": True,
            "ibrion": -1,
            "nsw": 0,
            "isif": 2,
            "fresh": True,
            "restart_mode": "fresh",
            "istart": 0,
            "icharg": 2,
            "run_count": 1,
            "ionic_convergence_gate": False,
            "automatic_retry": False,
            "follow_on_task": False,
        }
    )
    return plan


def _closed_authorization(spec: Mapping[str, Any]) -> dict[str, Any]:
    previous = spec.get("authorization")
    inherited = previous.get("user_authorization_inherited") if isinstance(previous, Mapping) else False
    return {
        "user_authorization_inherited": bool(inherited),
        "execution_gate": "PENDING_SOL_REVIEW",
        "mutable_gate_file": "sol_review_gate.json",
        "approval_scope": "One fresh fixed-geometry static package after explicit Sol acceptance; no retry, follow-on task or configuration change.",
        "upload": False,
        "launch": False,
        "automatic_retry": False,
        "follow_on_task": False,
    }


def _closed_flags() -> dict[str, bool]:
    return {
        "local_candidate_prepared": False,
        "upload": False,
        "remote_prepare": False,
        "launch": False,
        "local_watcher": False,
        "automatic_retry": False,
        "follow_on": False,
        "stop_before_remote": False,
    }


def _source_geometry(identity: Any, frame: Mapping[str, Any], poscar: Mapping[str, Any]) -> dict[str, Any]:
    parsed = poscar["parsed"]
    return {
        "source_kind": "xml_frame",
        "file": identity.source_path,
        "sha256": identity.source_sha256,
        "poscar_file": identity.source_poscar_path,
        "poscar_sha256": identity.source_poscar_sha256,
        "calculation_index_1_based": identity.source_calculation_index_1_based,
        "calculation_count_in_source_xml": frame["calculation_count"],
        "coordinate_mode": "direct",
        "cell_A": copy.deepcopy(frame["cell_A"]),
        "cell_source_decimal_tokens": copy.deepcopy(frame["basis_tokens"]),
        "position_source_decimal_tokens": copy.deepcopy(frame["position_tokens"]),
        "species_order": copy.deepcopy(parsed["species"]),
        "counts": copy.deepcopy(parsed["counts"]),
        "nions": parsed["nions"],
        "fixed_global_indices_1based": copy.deepcopy(parsed["fixed_indices_1based"]),
        "free_global_count": len(parsed["free_indices_1based"]),
        "geometry_transform": "No translation, reordering, symmetry operation or relaxation; XML frame values are retained.",
        "mask_source": "approved source POSCAR Selective Dynamics rows",
    }


def _validate_target_structure(
    spec: Mapping[str, Any],
    parsed: Mapping[str, Any],
    cell: list[list[float]],
    *,
    source_label: str,
) -> None:
    structure = spec.get("structure")
    if not isinstance(structure, Mapping):
        _fail("APPROVED_STRUCTURE_MISSING", "The approved manifest must declare target structure constraints.")
    expected_values = {
        "species_order": parsed.get("species"),
        "counts": parsed.get("counts"),
        "nions": parsed.get("nions"),
        "fixed_global_indices": parsed.get("fixed_indices_1based"),
        "free_global_count": len(parsed.get("free_indices_1based", [])),
    }
    for key, actual in expected_values.items():
        if key not in structure:
            _fail("APPROVED_STRUCTURE_FIELD_MISSING", f"Approved structure.{key} is required.", field=key)
        if structure[key] != actual:
            _fail(
                "SOURCE_APPROVED_CONSTRAINT_CONFLICT",
                f"{source_label} transform conflicts with approved structure.{key}.",
                field=key,
                expected=structure[key],
                actual=actual,
            )
    if not _matrix_close(structure.get("cell_A"), cell):
        _fail(
            "SOURCE_APPROVED_CELL_CONFLICT",
            f"{source_label} cell differs from approved structure.cell_A.",
            expected=structure.get("cell_A"),
            actual=cell,
        )


def _file_geometry_source(
    spec: Mapping[str, Any],
    identity: Any,
    source_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, str]]:
    source = identity.source_descriptor
    coordinate_path = _resolve_source_file(source_root, source["coordinate_file"], "delivery_identity.source.coordinate_file")
    mask_path = _resolve_source_file(source_root, source["mask_poscar_file"], "delivery_identity.source.mask_poscar_file")
    coordinate_sha = _sha256_path(coordinate_path, "source coordinate file")
    mask_sha = coordinate_sha if mask_path == coordinate_path else _sha256_path(mask_path, "source mask POSCAR")
    if coordinate_sha != source["coordinate_sha256"]:
        _fail("SOURCE_GEOMETRY_HASH_MISMATCH", "Source coordinate file differs from delivery_identity.", expected=source["coordinate_sha256"], actual=coordinate_sha)
    if mask_sha != source["mask_poscar_sha256"]:
        _fail("SOURCE_MASK_POSCAR_HASH_MISMATCH", "Source mask POSCAR differs from delivery_identity.", expected=source["mask_poscar_sha256"], actual=mask_sha)

    coordinates = _read_source_poscar(coordinate_path, require_constraints=False)
    masks = coordinates if mask_path == coordinate_path else _read_source_poscar(mask_path, require_constraints=False)
    coordinate_parsed = coordinates["parsed"]
    mask_parsed = masks["parsed"]
    if coordinates["coordinate_mode"] != "direct":
        _fail("SOURCE_COORDINATE_MODE_UNSUPPORTED", "File-geometry sources currently require Direct coordinates.", mode=coordinates["coordinate_mode"])
    for field in ("species", "counts", "nions"):
        if coordinate_parsed.get(field) != mask_parsed.get(field):
            _fail(
                "SOURCE_GEOMETRY_PAIR_MISMATCH",
                f"Coordinate file and mask POSCAR disagree on {field}.",
                field=field,
                coordinate=coordinate_parsed.get(field),
                mask=mask_parsed.get(field),
            )
    if not _matrix_close(coordinates["scaled_cell"], masks["scaled_cell"]):
        _fail("SOURCE_GEOMETRY_PAIR_MISMATCH", "Coordinate file and mask POSCAR cells differ.")

    source_atoms = coordinate_parsed["atoms"]
    mask_atoms = mask_parsed["atoms"]
    removals = source["remove_global_indices_1based"]
    removed_species = source["removed_species"]
    if any(index > len(source_atoms) for index in removals):
        _fail("SOURCE_ATOM_MAP_OUT_OF_RANGE", "A removal index exceeds source NIONS.", source_nions=len(source_atoms), indices=removals)
    for index, expected_species in zip(removals, removed_species):
        actual_species = source_atoms[index - 1].get("species")
        if actual_species != expected_species:
            _fail("SOURCE_REMOVED_SPECIES_MISMATCH", "A deleted atom is not the Sol-approved species at its source index.", source_index_1based=index, expected=expected_species, actual=actual_species)

    removal_set = set(removals)
    source_to_target: list[int | None] = [None] * len(source_atoms)
    target_atoms: list[dict[str, Any]] = []
    target_fixed: list[int] = []
    target_free: list[int] = []
    for source_index, (atom, mask_atom) in enumerate(zip(source_atoms, mask_atoms), start=1):
        if source_index in removal_set:
            continue
        flags = mask_atom.get("flags")
        if not isinstance(flags, list) or len(flags) != 3 or any(type(flag) is not bool for flag in flags):
            _fail("SOURCE_POSCAR_CONSTRAINTS_UNSUPPORTED", "Source mask POSCAR has an incomplete Selective Dynamics row.", source_index_1based=source_index)
        target_index = len(target_atoms) + 1
        source_to_target[source_index - 1] = target_index
        target_atom = copy.deepcopy(atom)
        target_atom["index_1based"] = target_index
        target_atom["flags"] = list(flags)
        target_atoms.append(target_atom)
        if flags == [False, False, False]:
            target_fixed.append(target_index)
        elif flags == [True, True, True]:
            target_free.append(target_index)
        else:
            _fail("SOURCE_POSCAR_CONSTRAINTS_UNSUPPORTED", "Partial Selective Dynamics masks are outside this static builder.", source_index_1based=source_index)

    source_species = coordinate_parsed["species"]
    source_counts = coordinate_parsed["counts"]
    target_species: list[str] = []
    target_counts: list[int] = []
    for symbol, count in zip(source_species, source_counts):
        removed_here = sum(1 for index in removals if source_atoms[index - 1].get("species") == symbol)
        retained = count - removed_here
        if retained > 0:
            target_species.append(symbol)
            target_counts.append(retained)
    target_parsed = {
        "species": target_species,
        "counts": target_counts,
        "nions": len(target_atoms),
        "atoms": target_atoms,
        "fixed_indices_1based": target_fixed,
        "free_indices_1based": target_free,
    }
    cell = coordinates["scaled_cell"]
    _validate_target_structure(spec, target_parsed, cell, source_label="File-geometry")
    frame = {
        "basis_tokens": coordinates["basis_tokens"],
        "scale_token": coordinates["scale_token"],
        "cell_A": copy.deepcopy(cell),
        "position_tokens": [coordinates["position_tokens"][index - 1] for index, target in enumerate(source_to_target, start=1) if target is not None],
        "positions": [atom["coordinates"] for atom in target_atoms],
    }
    target_poscar = {"parsed": target_parsed}
    removed_atoms = [
        {"source_index_1based": index, "species": species}
        for index, species in zip(removals, removed_species)
    ]
    source_geometry = {
        "source_kind": "file_geometry",
        "source_identity": copy.deepcopy(source),
        "source_coordinate_file": source["coordinate_file"],
        "source_coordinate_sha256": coordinate_sha,
        "mask_poscar_file": source["mask_poscar_file"],
        "mask_poscar_sha256": mask_sha,
        "coordinate_mode": "direct",
        "cell_A": copy.deepcopy(cell),
        "cell_source_decimal_tokens": copy.deepcopy(coordinates["basis_tokens"]),
        "source_position_decimal_tokens": copy.deepcopy(coordinates["position_tokens"]),
        "source_species_order": copy.deepcopy(source_species),
        "source_counts": copy.deepcopy(source_counts),
        "source_nions": len(source_atoms),
        "source_fixed_global_indices_1based": copy.deepcopy(mask_parsed["fixed_indices_1based"]),
        "removed_atoms": removed_atoms,
        "source_to_target_index_1based": source_to_target,
        "species_order": copy.deepcopy(target_species),
        "counts": copy.deepcopy(target_counts),
        "nions": len(target_atoms),
        "fixed_global_indices_1based": copy.deepcopy(target_fixed),
        "free_global_count": len(target_free),
        "geometry_transform": "Delete only explicitly listed source rows; preserve every retained Direct coordinate, cell, species order and mask without reordering; omit CONTCAR tail data.",
        "mask_source": "Explicit source POSCAR mask rows bound by SHA-256.",
    }
    return frame, target_poscar, source_geometry, {"coordinate_sha256": coordinate_sha, "mask_poscar_sha256": mask_sha}


def _format_number(value: Any) -> str:
    return format(float(value), ".16g")


def _approved_isolated_atom_source(
    spec: Mapping[str, Any],
    identity: Any,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    source = identity.source_descriptor
    geometry = source["geometry"]
    positions = geometry["fractional_positions"]
    flags = geometry["selective_dynamics_flags"]
    if flags[0] not in ([True, True, True], [False, False, False]):
        _fail("SOURCE_POSCAR_CONSTRAINTS_UNSUPPORTED", "Isolated-atom partial masks are outside this static builder.")
    fixed = [1] if flags[0] == [False, False, False] else []
    free = [] if fixed else [1]
    parsed = {
        "species": copy.deepcopy(geometry["species_order"]),
        "counts": copy.deepcopy(geometry["counts"]),
        "nions": 1,
        "atoms": [{
            "index_1based": 1,
            "species": geometry["species_order"][0],
            "coordinates": copy.deepcopy(positions[0]),
            "flags": copy.deepcopy(flags[0]),
        }],
        "fixed_indices_1based": fixed,
        "free_indices_1based": free,
    }
    cell = copy.deepcopy(geometry["cell_A"])
    _validate_target_structure(spec, parsed, cell, source_label="Approved isolated-atom")
    frame = {
        "basis_tokens": [[_format_number(value) for value in row] for row in cell],
        "scale_token": "1.0",
        "cell_A": cell,
        "position_tokens": [[_format_number(value) for value in positions[0]]],
        "positions": copy.deepcopy(positions),
    }
    source_geometry = {
        "source_kind": "approved_isolated_atom_spec",
        "source_identity": copy.deepcopy(source),
        "geometry_sha256": source["geometry_sha256"],
        "coordinate_mode": "direct",
        "cell_A": cell,
        "cell_source_decimal_tokens": frame["basis_tokens"],
        "position_source_decimal_tokens": frame["position_tokens"],
        "species_order": copy.deepcopy(parsed["species"]),
        "counts": copy.deepcopy(parsed["counts"]),
        "nions": 1,
        "fixed_global_indices_1based": fixed,
        "free_global_count": len(free),
        "geometry_transform": "Use the explicit approved isolated-atom geometry verbatim; no XML frame, external source file, or inferred default.",
        "mask_source": "Explicit approved isolated-atom specification.",
    }
    return frame, {"parsed": parsed}, source_geometry


def _remote_target(identity: Any) -> dict[str, Any]:
    return {
        "host": dict(identity.host),
        "port": identity.host["port"],
        "batch_dir": identity.remote_batch_dir,
        "case_dir": identity.runtime_input_dir,
        "tmux_session": identity.tmux_session,
    }


def _execution_manifest(
    spec: Mapping[str, Any],
    identity: Any,
    source_geometry: Mapping[str, Any],
    input_hashes: Mapping[str, str],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": 1,
        "route": "vasp",
        "delivery_template": STATIC_PACKAGE_TEMPLATE,
        "status": "LOCAL_CANDIDATE_PENDING_SOL_REVIEW",
        "scientific_release": "NOT_AUTHORIZED",
    }
    _compatibility_views(result, identity)
    result.update(
        {
            "source_geometry": copy.deepcopy(dict(source_geometry)),
            "remote_actions": {
                "ssh": False,
                "upload": False,
                "remote_directory_query_or_create": False,
                "remote_prepare": False,
                "potcar_assembly": False,
                "tmux_launch": False,
                "vasp_launch": False,
                "watcher_start": False,
            },
            "package": {
                "input_manifest": "input_manifest.json",
                "upload_whitelist": "remote_upload_whitelist.json",
                "immutable_checksums": "input_checksums.sha256",
                "runner": "run_static.sh",
                "remote_prepare": "remote_prepare_static.sh",
                "postcheck": "static_postcheck.py",
                "runtime_dependencies": "runtime_dependencies.json",
                "runtime_guard": "static_runtime_guard.py",
                "execution_manifest_is_local_only": True,
            },
            "authorization": _closed_authorization(spec),
            "execution_flags": _closed_flags(),
            "input_sha256": dict(input_hashes),
            "output_requirements": "output_requirements.json",
            "remote_actions_note": "No remote action was performed by the local builder.",
        }
    )
    return result


def _upload_whitelist() -> dict[str, Any]:
    files = [
        "POSCAR",
        "INCAR",
        "KPOINTS",
        "input_manifest.json",
        "paw_identity.json",
        "output_requirements.json",
        "remote_upload_whitelist.json",
        "task_env.sh",
        "static_postcheck.py",
        "run_static.sh",
        "remote_prepare_static.sh",
        "runtime_dependencies.json",
        "static_runtime_guard.py",
        "input_checksums.sha256",
    ]
    return {
        "schema": "vasp-static-upload-whitelist/v1",
        "files": files,
        "mutable_files": ["sol_review_gate.json"],
        "local_only": ["execution_manifest.json", "generation_receipt.json", "README.md"],
        "forbidden_uploads": [
            "POTCAR",
            "CHGCAR",
            "WAVECAR",
            "TMPCAR",
            "CONTCAR",
            "OUTCAR",
            "OSZICAR",
            "vasprun.xml",
            "STOPCAR",
            "execution_manifest.json",
            "generation_receipt.json",
            "README.md",
        ],
        "mutable_control_policy": "sol_review_gate.json remains PENDING_SOL_REVIEW until VASP Sol explicitly accepts this candidate.",
        "potcar_policy": "POTCAR is assembled only on the approved remote environment from locked component identities; POTCAR content is never uploaded or stored locally.",
    }


def _closed_gate(identity: Any) -> dict[str, Any]:
    return {
        "schema": "vasp-sol-review-gate/v1",
        "task_id": identity.task_id,
        "unit_id": identity.unit_id,
        "state": "PENDING_SOL_REVIEW",
        "reviewer_role": "VASP Sol",
        "decision_id": None,
        "execution_authorized": False,
        "scope": "One fresh fixed-geometry static package only after explicit Sol acceptance; no retry, follow-on task or configuration change.",
    }


def _readme_text(spec: Mapping[str, Any], identity: Any, source_geometry: Mapping[str, Any]) -> bytes:
    structure = spec["structure"]
    incar = spec["incar"]
    if identity.source_kind == "xml_frame":
        source_lines = [
            f"source XML: {identity.source_path} (calculation {identity.source_calculation_index_1_based})",
            f"source POSCAR: {identity.source_poscar_path}",
        ]
    elif identity.source_kind == "file_geometry":
        source_lines = [
            f"source coordinates: {identity.source_path} ({identity.source_sha256})",
            f"source mask POSCAR: {identity.source_poscar_path} ({identity.source_poscar_sha256})",
            f"removed source atoms: {source_geometry['removed_atoms']}",
        ]
    else:
        source_lines = [
            f"source: explicit approved isolated-atom geometry ({identity.source_descriptor['geometry_sha256']})",
        ]
    lines = [
        "# LOCAL STATIC DELIVERY CANDIDATE",
        "",
        "This directory is a locally built, closed static-delivery candidate.",
        "It has not uploaded files, assembled POTCAR, started tmux, started VASP, or started a watcher.",
        "Scientific and PAW acceptance remain with VASP Sol.",
        "",
        f"task_id: {identity.task_id}",
        f"unit_id: {identity.unit_id}",
        *source_lines,
        f"structure: {structure['species_order']} counts={structure['counts']} nions={structure['nions']}",
        f"fixed_global_indices: {structure['fixed_global_indices']}",
        f"static form: ISTART={incar['ISTART']} ICHARG={incar['ICHARG']} IBRION={incar['IBRION']} NSW={incar['NSW']} ISIF={incar['ISIF']}",
        f"source frame cell rows: {source_geometry['cell_A']}",
        "",
        "The mutable Sol gate is closed in sol_review_gate.json.",
        "POTCAR content is not present; remote_prepare_static.sh is a generic runtime template only.",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_static_delivery(
    spec_path: Path | str,
    source_dir: Path | str | None,
    output_dir: Path | str,
    *,
    allow_empty_destination: bool = False,
    incar_renderer=None,
    kpoints_renderer=None,
) -> dict[str, Any]:
    """Build and mechanically check one closed fresh-static candidate."""

    spec_file = Path(spec_path).resolve()
    source_root = Path(source_dir).resolve() if source_dir is not None else None
    destination = Path(output_dir).resolve()
    spec, spec_raw = _read_json(spec_file, "approved input manifest")
    identity = _load_identity_or_fail(spec)
    _require_static_approval(spec)
    if identity.source_kind in {"xml_frame", "file_geometry"}:
        if source_root is None or not source_root.is_dir():
            _fail("SOURCE_DIR_MISSING", "This source kind requires an existing source directory.", path=str(source_root) if source_root else None)
    elif source_root is not None:
        _fail("SOURCE_DIR_UNEXPECTED", "An approved isolated-atom specification does not accept an external source directory.")
    _destination_check(source_root, destination, allow_empty_destination)

    source_provenance: dict[str, Any] = {"source_kind": identity.source_kind}
    if identity.source_kind == "xml_frame":
        assert source_root is not None
        xml_path = _resolve_source_file(source_root, identity.source_path, "delivery_identity.source.path")
        poscar_path = _resolve_source_file(source_root, identity.source_poscar_path, "delivery_identity.source.poscar_path")
        xml_hash = _sha256_path(xml_path, "source vasprun.xml")
        poscar_hash = _sha256_path(poscar_path, "source POSCAR")
        if xml_hash != identity.source_sha256:
            _fail("SOURCE_XML_HASH_MISMATCH", "The accepted source vasprun.xml hash differs from delivery_identity.", expected=identity.source_sha256, actual=xml_hash)
        if poscar_hash != identity.source_poscar_sha256:
            _fail("SOURCE_POSCAR_HASH_MISMATCH", "The accepted source POSCAR hash differs from delivery_identity.", expected=identity.source_poscar_sha256, actual=poscar_hash)
        frame = _read_xml_frame(xml_path, identity.source_calculation_index_1_based)
        poscar = _read_source_poscar(poscar_path)
        _check_source_constraints(spec, poscar, frame)
        source_geometry = _source_geometry(identity, frame, poscar)
        source_provenance.update({
            "source_xml_sha256": xml_hash,
            "source_poscar_sha256": poscar_hash,
            "source_frame_selected_by": "delivery_identity.source.calculation_index_1_based",
        })
    elif identity.source_kind == "file_geometry":
        assert source_root is not None
        frame, poscar, source_geometry, file_provenance = _file_geometry_source(spec, identity, source_root)
        source_provenance.update(file_provenance)
        source_provenance["source_frame_selected_by"] = "delivery_identity.source.remove_global_indices_1based"
    else:
        frame, poscar, source_geometry = _approved_isolated_atom_source(spec, identity)
        source_provenance["geometry_sha256"] = identity.source_descriptor["geometry_sha256"]
        source_provenance["source_frame_selected_by"] = "delivery_identity.source.geometry"
    paw_identity = _paw_metadata(spec)
    runtime_dependencies = _runtime_dependencies(spec)

    effective = copy.deepcopy(spec)
    structure = effective.setdefault("structure", {})
    structure["cell_A"] = copy.deepcopy(frame["cell_A"])
    effective["delivery_template"] = STATIC_PACKAGE_TEMPLATE
    effective["status"] = "LOCAL_CANDIDATE_PENDING_SOL_REVIEW"
    effective["scientific_release"] = "NOT_AUTHORIZED"
    effective["authorization"] = _closed_authorization(spec)
    effective["execution_flags"] = _closed_flags()
    effective["execution_plan"] = _static_plan(spec)
    effective["paw_identity"] = copy.deepcopy(paw_identity)
    effective["runtime_dependencies"] = copy.deepcopy(runtime_dependencies)
    effective["output_requirements"] = _output_requirements(effective)
    _compatibility_views(effective, identity)
    geometry = dict(effective.get("geometry")) if isinstance(effective.get("geometry"), Mapping) else {}
    if identity.source_kind != "xml_frame" and "calculation_index_1_based" in geometry:
        _fail("XML_SOURCE_INDEX_FORBIDDEN", "Non-XML geometry sources must not carry a calculation_index_1_based field.")
    geometry.update(source_geometry)
    effective["geometry"] = geometry

    spec_errors = validate_spec(effective)
    if spec_errors:
        _fail("SPEC_INVALID", "The effective static manifest failed generator validation.", errors=spec_errors)
    try:
        generated_inputs = {
            "POSCAR": _render_poscar(identity, poscar, frame),
            "INCAR": (incar_renderer(effective, _render_poscar(identity, poscar, frame).decode("utf-8")) if incar_renderer else render_incar(effective)).encode("utf-8"),
            "KPOINTS": (kpoints_renderer(effective) if kpoints_renderer else render_kpoints(effective)).encode("utf-8"),
        }
    except Exception as error:
        _fail("INPUT_RENDER_FAILED", "The approved static inputs could not be rendered.", error=f"{type(error).__name__}: {error}")

    input_hashes = {name: _sha256(raw) for name, raw in generated_inputs.items()}
    effective["outputs"] = {
        name: {"sha256": digest, "size_bytes": len(generated_inputs[name])}
        for name, digest in input_hashes.items()
    }
    effective["input_sha256"] = dict(input_hashes)
    effective["generation"] = {
        "schema": SCHEMA,
        "approved_values_source": "input_manifest.json",
        "source_spec_sha256": _sha256(spec_raw),
        **source_provenance,
        "scripts_executed": False,
        "potcar_read": False,
    }
    execution_manifest = _execution_manifest(
        effective,
        identity,
        geometry,
        input_hashes,
    )
    whitelist = _upload_whitelist()
    gate = _closed_gate(identity)
    requirements = effective["output_requirements"]

    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = parent / (".vasp_static_builder_" + uuid.uuid4().hex)
    stage.mkdir(mode=0o777)
    try:
        initial_files: dict[str, bytes] = {
            **generated_inputs,
            "output_requirements.json": _json_bytes(requirements),
            "paw_identity.json": _json_bytes(paw_identity),
            "remote_upload_whitelist.json": _json_bytes(whitelist),
            "task_env.sh": _task_env_text(),
            "run_static.sh": _runner_text(),
            "remote_prepare_static.sh": _preparer_text(),
            "static_postcheck.py": _static_postcheck_text(),
            "runtime_dependencies.json": _json_bytes(runtime_dependencies),
            "static_runtime_guard.py": _runtime_guard_bytes(),
            "input_manifest.json": _json_bytes(effective),
            "execution_manifest.json": _json_bytes(execution_manifest),
            "sol_review_gate.json": _json_bytes(gate),
        }
        for name, raw in initial_files.items():
            (stage / name).write_bytes(raw)
        immutable_names = list(whitelist["files"])
        checksum_lines = [
            f"{_sha256_path(stage / name, name)}  {name}"
            for name in immutable_names
            if name != "input_checksums.sha256"
        ]
        (stage / "input_checksums.sha256").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")

        input_report = validate_inputs(stage / "input_manifest.json", stage)
        if not input_report.get("passed"):
            _fail("REAL_INPUT_VALIDATION_FAILED", "The generated static inputs failed the independent executor gate.", validation=input_report)
        try:
            delivery_report = check_delivery_manifest(stage)
        except Exception as error:
            code = getattr(error, "code", None)
            message = getattr(error, "message", None)
            if isinstance(code, str) and isinstance(message, str):
                _fail(code, message)
            raise

        receipt = {
            "schema": SCHEMA,
            "kind": "LOCAL_STATIC_DELIVERY_CANDIDATE",
            "task_id": identity.task_id,
            "unit_id": identity.unit_id,
            "source_spec": {"path": str(spec_file), "sha256": _sha256(spec_raw)},
            "source_geometry": copy.deepcopy(geometry),
            "input_sha256": dict(input_hashes),
            "checksums_cover": immutable_names,
            "validation": {
                "executor_input_gate": "PASS",
                "static_delivery_check": "PASS",
                "scientific_acceptance": "NOT_EVALUATED",
            },
            "actions": {
                "generated_script_execution": False,
                "potcar_content_read": False,
                "ssh": False,
                "upload": False,
                "remote_prepare": False,
                "launch": False,
                "watcher_start": False,
            },
        }
        (stage / "generation_receipt.json").write_bytes(_json_bytes(receipt))
        (stage / "README.md").write_bytes(_readme_text(effective, identity, geometry))
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
    result = {
        "schema": SCHEMA,
        "passed": True,
        "kind": "LOCAL_STATIC_DELIVERY_CANDIDATE",
        "output_dir": str(destination),
        "task_id": identity.task_id,
        "unit_id": identity.unit_id,
        "files": sorted(path.name for path in destination.iterdir()),
        "input_sha256": input_hashes,
        "delivery_check": delivery_report,
        "scripts_executed": False,
        "potcar_content_read": False,
        "remote_actions": False,
    }
    if identity.source_kind == "xml_frame":
        result["source_calculation_index_1_based"] = identity.source_calculation_index_1_based
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--spec", required=True, type=Path)
    build.add_argument("--source-dir", type=Path)
    build.add_argument("--output-dir", required=True, type=Path)
    build.add_argument("--allow-empty-destination", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = build_static_delivery(
            args.spec,
            args.source_dir,
            args.output_dir,
            allow_empty_destination=args.allow_empty_destination,
        )
    except BuilderError as error:
        result = {"schema": SCHEMA, "passed": False, "errors": [error.as_dict()]}
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
                    "code": "BUILDER_EXCEPTION",
                    "message": "Static delivery builder failed without publishing a completed candidate.",
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

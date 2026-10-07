#!/usr/bin/env python3
"""Check a narrow VASP static-delivery identity package without executing it.

The approved identity specification is the only source of expected task,
source-frame, host and remote-path values.  This checker parses the candidate
generator and shell declarations; it never imports or executes the generator,
runner, launcher, VASP, SSH, or a remote exporter.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from pathlib import PurePosixPath
import re
import sys
from typing import Any, Mapping

try:
    import progress_evidence
    import static_delivery_templates as trusted_templates
    import static_runtime_guard as runtime_guard
except ImportError as error:  # pragma: no cover - only affects direct misuse
    raise RuntimeError("progress_evidence.py, static_delivery_templates.py and static_runtime_guard.py must be importable beside this checker") from error


SCHEMA = "vasp-static-delivery-check/v1"
DELIVERY_IDENTITY_SCHEMA = "vasp-delivery-identity/v1"
STATIC_PACKAGE_TEMPLATE = "vasp-static-package/v1"
STATIC_PACKAGE_REQUIRED_FILES = (
    "POSCAR",
    "INCAR",
    "KPOINTS",
    "input_manifest.json",
    "execution_manifest.json",
    "output_requirements.json",
    "remote_upload_whitelist.json",
    "sol_review_gate.json",
    "task_env.sh",
    "run_static.sh",
    "remote_prepare_static.sh",
    "static_postcheck.py",
    "paw_identity.json",
    "runtime_dependencies.json",
    "static_runtime_guard.py",
    "input_checksums.sha256",
)
INPUT_FILES = ("POSCAR", "INCAR", "KPOINTS")
REQUIRED_STATIC_FILES = (
    "task_env.sh",
    "input_manifest.json",
    "execution_manifest.json",
)
REMOTE_BATCH_RE = re.compile(r"^/srv/dft/calculations/[A-Za-z0-9_./-]+$")
CASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
RUNNER_HEREDOC_RE = re.compile(
    r"""^\s*(?:python3\s+-|[A-Za-z_][A-Za-z0-9_]*\s*=\s*["']?\$\(\s*python3\s+-).*<<\s*(?P<quote>["']?)(?P<delimiter>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)\s*(?:\|\|.*)?$"""
)
INDEX_IDENTITY_NAMES = frozenset({"XML_CALCULATION_1_BASED", "calculation_index_1_based"})


class DeliveryError(ValueError):
    """A deterministic candidate or identity incompatibility."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@dataclass(frozen=True)
class Identity:
    task_id: str
    unit_id: str
    source_calculation_index_1_based: int
    host: dict[str, Any]
    remote_batch_dir: str
    case: str
    runtime_input_dir: str

    @property
    def remote_case_dir(self) -> str:
        return f"{self.remote_batch_dir}/{self.case}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "unit_id": self.unit_id,
            "source_calculation_index_1_based": self.source_calculation_index_1_based,
            "host": dict(self.host),
            "remote_batch_dir": self.remote_batch_dir,
            "case": self.case,
            "runtime_input_dir": self.runtime_input_dir,
            "remote_case_dir": self.remote_case_dir,
        }


@dataclass(frozen=True)
class DeliveryIdentity:
    task_id: str
    unit_id: str
    source_kind: str
    source_descriptor: dict[str, Any]
    host: dict[str, Any]
    remote_batch_dir: str
    case: str
    tmux_session: str

    @property
    def source_path(self) -> str | None:
        if self.source_kind == "xml_frame":
            return self.source_descriptor["path"]
        if self.source_kind == "file_geometry":
            return self.source_descriptor["coordinate_file"]
        return None

    @property
    def source_sha256(self) -> str | None:
        if self.source_kind == "xml_frame":
            return self.source_descriptor["sha256"]
        if self.source_kind == "file_geometry":
            return self.source_descriptor["coordinate_sha256"]
        return None

    @property
    def source_calculation_index_1_based(self) -> int | None:
        return self.source_descriptor.get("calculation_index_1_based")

    @property
    def source_poscar_path(self) -> str | None:
        if self.source_kind == "xml_frame":
            return self.source_descriptor["poscar_path"]
        if self.source_kind == "file_geometry":
            return self.source_descriptor["mask_poscar_file"]
        return None

    @property
    def source_poscar_sha256(self) -> str | None:
        if self.source_kind == "xml_frame":
            return self.source_descriptor["poscar_sha256"]
        if self.source_kind == "file_geometry":
            return self.source_descriptor["mask_poscar_sha256"]
        return None

    @property
    def runtime_input_dir(self) -> str:
        return f"{self.remote_batch_dir}/{self.case}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": DELIVERY_IDENTITY_SCHEMA,
            "task_id": self.task_id,
            "unit_id": self.unit_id,
            "source": dict(self.source_descriptor),
            "host": dict(self.host),
            "remote_batch_dir": self.remote_batch_dir,
            "case": self.case,
            "runtime_input_dir": self.runtime_input_dir,
            "case_dir": self.runtime_input_dir,
            "tmux_session": self.tmux_session,
        }


def _error(code: str, message: str, **details: Any) -> None:
    raise DeliveryError(code, message, **details)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        _error("MISSING_FILE", f"{label} is missing.", path=str(path))
        raise AssertionError from error
    except json.JSONDecodeError as error:
        _error(
            "INVALID_JSON",
            f"{label} is not valid JSON.",
            path=str(path),
            line=error.lineno,
            column=error.colno,
        )
        raise AssertionError from error
    if not isinstance(value, dict):
        _error("JSON_OBJECT_REQUIRED", f"{label} must be a JSON object.", path=str(path))
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _error("OBJECT_REQUIRED", f"{label} must be an object.")
    return value


def _required(mapping: Mapping[str, Any], key: str, label: str) -> Any:
    if key not in mapping:
        _error("MISSING_FIELD", f"{label}.{key} is required.", field=f"{label}.{key}")
    return mapping[key]


def _host(value: Any, port: Any, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        user = value.get("user")
        address = value.get("address")
        actual_port = value.get("port", port)
    elif isinstance(value, str) and "@" in value:
        user, address = value.split("@", 1)
        actual_port = port
    else:
        _error("HOST_INVALID", f"{label} must be user@address or a host object.")
    if (
        not isinstance(user, str)
        or not user
        or not isinstance(address, str)
        or not address
        or type(actual_port) is not int
        or not 1 <= actual_port <= 65535
    ):
        _error("HOST_INVALID", f"{label} has invalid user, address or port.")
    return {"user": user, "address": address, "port": actual_port}


def _remote_batch(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not REMOTE_BATCH_RE.fullmatch(value)
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        _error("REMOTE_BATCH_INVALID", f"{label} is not an allowed remote batch path.")
    return value


def _case(value: Any, label: str) -> str:
    if not isinstance(value, str) or not CASE_RE.fullmatch(value) or value in {".", ".."}:
        _error("CASE_INVALID", f"{label} is not an allowed case identifier.")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("TEXT_REQUIRED", f"{label} must be non-empty text.")
    return value


def _index(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        _error("SOURCE_INDEX_INVALID", f"{label} must be a positive integer.")
    return value


def _relative_identity_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        _error("IDENTITY_SOURCE_PATH_INVALID", f"{label} must be a relative POSIX path.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        _error("IDENTITY_SOURCE_PATH_INVALID", f"{label} must not be absolute or escape source-dir.")
    return path.as_posix()


def _sha256_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        _error("IDENTITY_SOURCE_HASH_INVALID", f"{label} must be a SHA-256 hex digest.")
    return value.lower()


def _tmux_session(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or re.fullmatch(r"[A-Za-z0-9_.:-]+", value) is None
    ):
        _error("TMUX_SESSION_INVALID", f"{label} is not a safe tmux session name.")
    return value


def _compat_same(label: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        _error(
            "DELIVERY_IDENTITY_CONFLICT",
            f"{label} conflicts with delivery_identity.",
            field=label,
            expected=expected,
            actual=actual,
        )


def _finite_geometry_number(value: Any, label: str) -> None:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        _error("IDENTITY_GEOMETRY_INVALID", f"{label} must be a finite JSON number.")


def _source_descriptor(value: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    kind = value.get("kind", "xml_frame")
    if kind == "xml_frame":
        allowed = {"kind", "path", "sha256", "calculation_index_1_based", "poscar_path", "poscar_sha256"}
        if set(value) - allowed:
            _error("IDENTITY_SOURCE_KIND_INVALID", "XML-frame identity has unsupported source fields.", fields=sorted(set(value) - allowed))
        descriptor: dict[str, Any] = {
            "path": _relative_identity_path(
                _required(value, "path", "delivery_identity.source"),
                "delivery_identity.source.path",
            ),
            "sha256": _sha256_text(
                _required(value, "sha256", "delivery_identity.source"),
                "delivery_identity.source.sha256",
            ),
            "calculation_index_1_based": _index(
                _required(value, "calculation_index_1_based", "delivery_identity.source"),
                "delivery_identity.source.calculation_index_1_based",
            ),
            "poscar_path": _relative_identity_path(
                _required(value, "poscar_path", "delivery_identity.source"),
                "delivery_identity.source.poscar_path",
            ),
            "poscar_sha256": _sha256_text(
                _required(value, "poscar_sha256", "delivery_identity.source"),
                "delivery_identity.source.poscar_sha256",
            ),
        }
        if "kind" in value:
            descriptor["kind"] = "xml_frame"
        return "xml_frame", descriptor

    if kind == "file_geometry":
        allowed = {
            "kind", "coordinate_file", "coordinate_sha256", "mask_poscar_file",
            "mask_poscar_sha256", "remove_global_indices_1based", "removed_species",
        }
        if set(value) != allowed:
            _error("IDENTITY_SOURCE_KIND_INVALID", "File-geometry identity must provide exactly its coordinate, mask and atom-map fields.", fields=sorted(set(value)))
        indices = _required(value, "remove_global_indices_1based", "delivery_identity.source")
        species = _required(value, "removed_species", "delivery_identity.source")
        if (
            not isinstance(indices, list)
            or any(type(index) is not int or index <= 0 for index in indices)
            or len(indices) != len(set(indices))
        ):
            _error("IDENTITY_ATOM_MAP_INVALID", "remove_global_indices_1based must contain unique positive integers.")
        if (
            not isinstance(species, list)
            or len(species) != len(indices)
            or any(not isinstance(item, str) or not item.strip() for item in species)
        ):
            _error("IDENTITY_ATOM_MAP_INVALID", "removed_species must name each deleted atom in the same order as its source index.")
        return "file_geometry", {
            "kind": "file_geometry",
            "coordinate_file": _relative_identity_path(
                _required(value, "coordinate_file", "delivery_identity.source"),
                "delivery_identity.source.coordinate_file",
            ),
            "coordinate_sha256": _sha256_text(
                _required(value, "coordinate_sha256", "delivery_identity.source"),
                "delivery_identity.source.coordinate_sha256",
            ),
            "mask_poscar_file": _relative_identity_path(
                _required(value, "mask_poscar_file", "delivery_identity.source"),
                "delivery_identity.source.mask_poscar_file",
            ),
            "mask_poscar_sha256": _sha256_text(
                _required(value, "mask_poscar_sha256", "delivery_identity.source"),
                "delivery_identity.source.mask_poscar_sha256",
            ),
            "remove_global_indices_1based": list(indices),
            "removed_species": list(species),
        }

    if kind == "approved_isolated_atom_spec":
        if set(value) != {"kind", "geometry", "geometry_sha256"}:
            _error("IDENTITY_SOURCE_KIND_INVALID", "Isolated-atom identity must contain only the explicit geometry and its fingerprint.")
        geometry = _mapping(_required(value, "geometry", "delivery_identity.source"), "delivery_identity.source.geometry")
        expected_geometry_keys = {
            "species_order", "counts", "cell_A", "coordinate_mode",
            "fractional_positions", "selective_dynamics_flags",
        }
        if set(geometry) != expected_geometry_keys:
            _error("IDENTITY_GEOMETRY_INVALID", "Isolated-atom geometry must explicitly declare species, counts, cell, Direct coordinates and masks.")
        species = geometry.get("species_order")
        counts = geometry.get("counts")
        cell = geometry.get("cell_A")
        positions = geometry.get("fractional_positions")
        flags = geometry.get("selective_dynamics_flags")
        if (
            not isinstance(species, list) or len(species) != 1
            or not isinstance(species[0], str) or not species[0].strip()
            or counts != [1]
            or geometry.get("coordinate_mode") != "direct"
            or not isinstance(cell, list) or len(cell) != 3
            or any(not isinstance(row, list) or len(row) != 3 for row in cell)
            or not isinstance(positions, list) or len(positions) != 1
            or not isinstance(positions[0], list) or len(positions[0]) != 3
            or not isinstance(flags, list) or len(flags) != 1
            or not isinstance(flags[0], list) or len(flags[0]) != 3
            or any(type(flag) is not bool for flag in flags[0])
        ):
            _error("IDENTITY_GEOMETRY_INVALID", "Isolated-atom geometry must describe exactly one atom with a finite 3x3 cell, one fractional position and three Boolean masks.")
        for row_index, row in enumerate(cell, start=1):
            for column_index, item in enumerate(row, start=1):
                _finite_geometry_number(item, f"cell_A[{row_index}][{column_index}]")
        for axis, item in enumerate(positions[0], start=1):
            _finite_geometry_number(item, f"fractional_positions[1][{axis}]")
        canonical = json.dumps(geometry, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        expected_hash = hashlib.sha256(canonical).hexdigest()
        actual_hash = _sha256_text(
            _required(value, "geometry_sha256", "delivery_identity.source"),
            "delivery_identity.source.geometry_sha256",
        )
        _same("delivery_identity.source.geometry_sha256", actual_hash, expected_hash)
        return "approved_isolated_atom_spec", {
            "kind": "approved_isolated_atom_spec",
            "geometry": json.loads(canonical.decode("utf-8")),
            "geometry_sha256": actual_hash,
        }

    _error("IDENTITY_SOURCE_KIND_INVALID", "delivery_identity.source.kind is unsupported.", kind=kind)
    raise AssertionError


def load_delivery_identity(manifest: Mapping[str, Any]) -> DeliveryIdentity:
    """Load the canonical delivery identity and reject legacy-view conflicts."""

    data = _mapping(_required(manifest, "delivery_identity", "input_manifest"), "input_manifest.delivery_identity")
    _same("delivery_identity.schema", _required(data, "schema", "delivery_identity"), DELIVERY_IDENTITY_SCHEMA)
    task_id = _text(_required(data, "task_id", "delivery_identity"), "delivery_identity.task_id")
    unit_id = _text(_required(data, "unit_id", "delivery_identity"), "delivery_identity.unit_id")
    source = _mapping(_required(data, "source", "delivery_identity"), "delivery_identity.source")
    source_kind, source_descriptor = _source_descriptor(source)
    host = _host(_required(data, "host", "delivery_identity"), None, "delivery_identity.host")
    batch = _remote_batch(
        _required(data, "remote_batch_dir", "delivery_identity"),
        "delivery_identity.remote_batch_dir",
    )
    case = _case(_required(data, "case", "delivery_identity"), "delivery_identity.case")
    tmux = _tmux_session(
        _required(data, "tmux_session", "delivery_identity"),
        "delivery_identity.tmux_session",
    )
    identity = DeliveryIdentity(
        task_id=task_id,
        unit_id=unit_id,
        source_kind=source_kind,
        source_descriptor=source_descriptor,
        host=host,
        remote_batch_dir=batch,
        case=case,
        tmux_session=tmux,
    )

    for key, expected in (
        ("task_id", identity.task_id),
        ("unit_id", identity.unit_id),
        ("remote_batch_dir", identity.remote_batch_dir),
        ("case", identity.case),
        ("runtime_input_dir", identity.runtime_input_dir),
        ("case_dir", identity.runtime_input_dir),
        ("tmux_session", identity.tmux_session),
    ):
        if key in manifest:
            _compat_same(f"input_manifest.{key}", manifest[key], expected)
    if "host" in manifest:
        _compat_same("input_manifest.host", _host(manifest["host"], None, "input_manifest.host"), identity.host)

    legacy_source = manifest.get("source")
    if legacy_source is not None:
        source_view = _mapping(legacy_source, "input_manifest.source")
        if identity.source_kind == "xml_frame":
            _compat_same("input_manifest.source.path", _required(source_view, "path", "input_manifest.source"), identity.source_path)
            _compat_same("input_manifest.source.sha256", _required(source_view, "sha256", "input_manifest.source"), identity.source_sha256)
            _compat_same(
                "input_manifest.source.calculation_index_1_based",
                _required(source_view, "calculation_index_1_based", "input_manifest.source"),
                identity.source_calculation_index_1_based,
            )
        else:
            _compat_same("input_manifest.source", dict(source_view), identity.source_descriptor)

    for view_name in ("remote_target", "progress_evidence"):
        view = manifest.get(view_name)
        if view is None:
            continue
        view_map = _mapping(view, f"input_manifest.{view_name}")
        if view_name == "remote_target":
            actual = _remote_target_identity(view_map, f"input_manifest.{view_name}")
            if "tmux_session" in view_map:
                _compat_same(
                    f"input_manifest.{view_name}.tmux_session",
                    _tmux_session(view_map["tmux_session"], f"input_manifest.{view_name}.tmux_session"),
                    identity.tmux_session,
                )
        else:
            actual = _remote_target_identity(
                {
                    "host": _required(view_map, "host", f"input_manifest.{view_name}"),
                    "batch_dir": _required(view_map, "remote_batch_dir", f"input_manifest.{view_name}"),
                    "case_dir": _required(view_map, "runtime_input_dir", f"input_manifest.{view_name}"),
                },
                f"input_manifest.{view_name}",
            )
        _compat_same(f"input_manifest.{view_name}.host", actual["host"], identity.host)
        _compat_same(
            f"input_manifest.{view_name}.remote_batch_dir",
            actual["remote_batch_dir"],
            identity.remote_batch_dir,
        )
        _compat_same(f"input_manifest.{view_name}.case", actual["case"], identity.case)
        _compat_same(
            f"input_manifest.{view_name}.runtime_input_dir",
            actual["runtime_input_dir"],
            identity.runtime_input_dir,
        )
    return identity


def _identity_from_spec(data: Mapping[str, Any]) -> Identity:
    task_id = _text(_required(data, "task_id", "spec"), "spec.task_id")
    unit_id = _text(_required(data, "unit_id", "spec"), "spec.unit_id")
    source_index = _index(
        _required(data, "source_calculation_index_1_based", "spec"),
        "spec.source_calculation_index_1_based",
    )
    host = _host(_required(data, "host", "spec"), None, "spec.host")
    batch = _remote_batch(_required(data, "remote_batch_dir", "spec"), "spec.remote_batch_dir")
    case = _case(_required(data, "case", "spec"), "spec.case")
    runtime = _text(_required(data, "runtime_input_dir", "spec"), "spec.runtime_input_dir")
    expected_runtime = f"{batch}/{case}"
    if runtime != expected_runtime:
        _error(
            "RUNTIME_PATH_MISMATCH",
            "The narrow checker requires runtime_input_dir to be the exact case directory.",
            expected=expected_runtime,
            actual=runtime,
        )
    return Identity(task_id, unit_id, source_index, host, batch, case, runtime)


def _same(label: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        _error(
            "IDENTITY_MISMATCH",
            f"{label} differs from the approved identity specification.",
            field=label,
            expected=expected,
            actual=actual,
        )


def _remote_target_identity(target: Mapping[str, Any], label: str) -> dict[str, Any]:
    host = _host(_required(target, "host", label), target.get("port"), f"{label}.host")
    batch = _remote_batch(_required(target, "batch_dir", label), f"{label}.batch_dir")
    case_dir = _text(_required(target, "case_dir", label), f"{label}.case_dir")
    case = case_dir.rsplit("/", 1)[-1]
    _case(case, f"{label}.case")
    if case_dir != f"{batch}/{case}":
        _error("REMOTE_PATH_CONFLICT", f"{label}.case_dir is not derived from batch_dir.")
    return {"host": host, "remote_batch_dir": batch, "case": case, "runtime_input_dir": case_dir}


def _check_input_manifest(manifest: Mapping[str, Any], identity: Identity) -> None:
    _same("input_manifest.task_id", _required(manifest, "task_id", "input_manifest"), identity.task_id)
    _same("input_manifest.unit_id", _required(manifest, "unit_id", "input_manifest"), identity.unit_id)
    geometry = _mapping(_required(manifest, "geometry", "input_manifest"), "input_manifest.geometry")
    _same(
        "input_manifest.geometry.calculation_index_1_based",
        _required(geometry, "calculation_index_1_based", "input_manifest.geometry"),
        identity.source_calculation_index_1_based,
    )
    target = _mapping(_required(manifest, "remote_target", "input_manifest"), "input_manifest.remote_target")
    actual = _remote_target_identity(target, "input_manifest.remote_target")
    _same("input_manifest.remote_target.host", actual["host"], identity.host)
    _same("input_manifest.remote_target.batch_dir", actual["remote_batch_dir"], identity.remote_batch_dir)
    _same("input_manifest.remote_target.case_dir", actual["runtime_input_dir"], identity.runtime_input_dir)
    _same("input_manifest.remote_target.case", actual["case"], identity.case)


def _check_static_form(manifest: Mapping[str, Any]) -> None:
    plan = _mapping(_required(manifest, "execution_plan", "input_manifest"), "input_manifest.execution_plan")
    expected = {
        "kind": "static",
        "fresh": True,
        "istart": 0,
        "icharg": 2,
        "ibrion": -1,
        "nsw": 0,
        "run_count": 1,
    }
    observed = {key: plan.get(key) for key in expected}
    if observed != expected:
        _error(
            "STATIC_FORM_UNSUPPORTED",
            "This checker supports only the explicit fresh fixed-geometry static form.",
            expected=expected,
            actual=observed,
        )


def _check_output_identity_contract(
    manifest: Mapping[str, Any],
    requirements: Mapping[str, Any],
) -> None:
    structure = _mapping(_required(manifest, "structure", "input_manifest"), "input_manifest.structure")
    incar = _mapping(_required(manifest, "incar", "input_manifest"), "input_manifest.incar")
    parallel = _mapping(_required(manifest, "parallel", "input_manifest"), "input_manifest.parallel")
    expected = {
        "NIONS": _required(structure, "nions", "input_manifest.structure"),
        "NELECT": _required(structure, "nelect", "input_manifest.structure"),
        "NBANDS": _required(incar, "NBANDS", "input_manifest.incar"),
        "ISPIN": _required(incar, "ISPIN", "input_manifest.incar"),
        "KPAR": _required(parallel, "kpar", "input_manifest.parallel"),
        "NCORE": _required(parallel, "ncore", "input_manifest.parallel"),
        "MPI_RANKS": _required(parallel, "mpi_ranks", "input_manifest.parallel"),
    }
    fields = ["NIONS", "NELECT", "NBANDS", "ISPIN", "KPAR", "NCORE", "MPI_RANKS"]
    nupdown = incar.get("NUPDOWN")
    if nupdown is not None:
        if type(nupdown) is not int:
            _error("OUTPUT_IDENTITY_CONTRACT_INVALID", "INCAR NUPDOWN must be an integer or explicit null.")
        fields.append("NUPDOWN")
        expected["NUPDOWN"] = nupdown
    contract = manifest.get("output_contract", {})
    if not isinstance(contract, Mapping):
        _error("OUTPUT_IDENTITY_CONTRACT_INVALID", "output_contract must be an object when supplied.")
    explicit = contract.get("expected_identity", {})
    if not isinstance(explicit, Mapping) or set(explicit) - {"NKPTS"}:
        _error("OUTPUT_IDENTITY_CONTRACT_INVALID", "Only explicit NKPTS may be supplied in output_contract.expected_identity.")
    if "NKPTS" in explicit:
        nkpts = explicit["NKPTS"]
        if type(nkpts) is not int or nkpts <= 0:
            _error("OUTPUT_IDENTITY_CONTRACT_INVALID", "Explicit output_contract.expected_identity.NKPTS must be a positive integer.")
        fields.append("NKPTS")
        expected["NKPTS"] = nkpts

    actual_fields = _required(requirements, "identity_fields", "output_requirements")
    actual_expected = _mapping(
        _required(requirements, "expected_identity", "output_requirements"),
        "output_requirements.expected_identity",
    )
    if actual_fields != fields or dict(actual_expected) != expected:
        _error(
            "OUTPUT_IDENTITY_CONTRACT_MISMATCH",
            "Generated output identity fields must match explicit approved NUPDOWN/NKPTS contracts; NKPTS is never inferred from the mesh.",
            expected_fields=fields,
            actual_fields=actual_fields,
            expected_identity=expected,
            actual_identity=dict(actual_expected),
        )


def _check_canonical_source_geometry_binding(
    input_manifest: Mapping[str, Any],
    execution_manifest: Mapping[str, Any],
    identity: DeliveryIdentity,
) -> None:
    geometry = _mapping(
        _required(input_manifest, "geometry", "input_manifest"),
        "input_manifest.geometry",
    )
    source_geometry = _mapping(
        _required(execution_manifest, "source_geometry", "execution_manifest"),
        "execution_manifest.source_geometry",
    )
    _same(
        "input_manifest.geometry.source_kind",
        geometry.get("source_kind", "xml_frame") if identity.source_kind == "xml_frame" else geometry.get("source_kind"),
        identity.source_kind,
    )

    if identity.source_kind == "xml_frame":
        _same("execution_manifest.source_geometry.file", _required(source_geometry, "file", "execution_manifest.source_geometry"), identity.source_path)
        _same("execution_manifest.source_geometry.sha256", _required(source_geometry, "sha256", "execution_manifest.source_geometry"), identity.source_sha256)
        _same(
            "execution_manifest.source_geometry.calculation_index_1_based",
            _required(source_geometry, "calculation_index_1_based", "execution_manifest.source_geometry"),
            identity.source_calculation_index_1_based,
        )
        _same(
            "input_manifest.geometry.calculation_index_1_based",
            _required(geometry, "calculation_index_1_based", "input_manifest.geometry"),
            identity.source_calculation_index_1_based,
        )
        return

    for label, mapping in (("input_manifest.geometry", geometry), ("execution_manifest.source_geometry", source_geometry)):
        if "calculation_index_1_based" in mapping or "source_calculation_index_1_based" in mapping:
            _error(
                "XML_SOURCE_INDEX_FORBIDDEN",
                "Non-XML source packages must not carry a calculation index.",
                field=label,
            )
    _same("execution_manifest.source_geometry.source_kind", source_geometry.get("source_kind"), identity.source_kind)
    _same("execution_manifest.source_geometry.source_identity", _required(source_geometry, "source_identity", "execution_manifest.source_geometry"), identity.source_descriptor)
    if identity.source_kind == "file_geometry":
        descriptor = identity.source_descriptor
        expected = {
            "source_coordinate_file": descriptor["coordinate_file"],
            "source_coordinate_sha256": descriptor["coordinate_sha256"],
            "mask_poscar_file": descriptor["mask_poscar_file"],
            "mask_poscar_sha256": descriptor["mask_poscar_sha256"],
        }
    else:
        expected = {"geometry_sha256": identity.source_descriptor["geometry_sha256"]}
    for key, value in expected.items():
        _same(f"execution_manifest.source_geometry.{key}", _required(source_geometry, key, "execution_manifest.source_geometry"), value)
    _same("execution_manifest.source_geometry.input_manifest.geometry", dict(source_geometry), dict(geometry))
    structure = _mapping(_required(input_manifest, "structure", "input_manifest"), "input_manifest.structure")
    for key in ("species_order", "counts", "nions", "fixed_global_indices", "free_global_count"):
        geometry_key = "fixed_global_indices_1based" if key == "fixed_global_indices" else key
        _same(f"input_manifest.geometry.{geometry_key}", _required(geometry, geometry_key, "input_manifest.geometry"), _required(structure, key, "input_manifest.structure"))


def _check_execution_manifest(
    manifest: Mapping[str, Any],
    path: Path,
    identity: Identity,
) -> dict[str, Any]:
    _same("execution_manifest.task_id", _required(manifest, "task_id", "execution_manifest"), identity.task_id)
    _same("execution_manifest.unit_id", _required(manifest, "unit_id", "execution_manifest"), identity.unit_id)
    source = _mapping(
        _required(manifest, "source_geometry", "execution_manifest"),
        "execution_manifest.source_geometry",
    )
    _same(
        "execution_manifest.source_geometry.calculation_index_1_based",
        _required(source, "calculation_index_1_based", "execution_manifest.source_geometry"),
        identity.source_calculation_index_1_based,
    )

    top_host = _host(_required(manifest, "host", "execution_manifest"), None, "execution_manifest.host")
    top_batch = _remote_batch(
        _required(manifest, "remote_batch_dir", "execution_manifest"),
        "execution_manifest.remote_batch_dir",
    )
    top_case = _case(_required(manifest, "case", "execution_manifest"), "execution_manifest.case")
    top_runtime = _text(
        _required(manifest, "runtime_input_dir", "execution_manifest"),
        "execution_manifest.runtime_input_dir",
    )
    _same("execution_manifest.host", top_host, identity.host)
    _same("execution_manifest.remote_batch_dir", top_batch, identity.remote_batch_dir)
    _same("execution_manifest.case", top_case, identity.case)
    _same("execution_manifest.runtime_input_dir", top_runtime, identity.runtime_input_dir)

    target = _mapping(_required(manifest, "remote_target", "execution_manifest"), "execution_manifest.remote_target")
    target_identity = _remote_target_identity(target, "execution_manifest.remote_target")
    _same("execution_manifest.remote_target.host", target_identity["host"], identity.host)
    _same(
        "execution_manifest.remote_target.batch_dir",
        target_identity["remote_batch_dir"],
        identity.remote_batch_dir,
    )
    _same("execution_manifest.remote_target.case", target_identity["case"], identity.case)
    _same(
        "execution_manifest.remote_target.case_dir",
        target_identity["runtime_input_dir"],
        identity.runtime_input_dir,
    )

    evidence = _mapping(
        _required(manifest, "progress_evidence", "execution_manifest"),
        "execution_manifest.progress_evidence",
    )
    evidence_host = _host(_required(evidence, "host", "execution_manifest.progress_evidence"), None, "execution_manifest.progress_evidence.host")
    evidence_batch = _remote_batch(
        _required(evidence, "remote_batch_dir", "execution_manifest.progress_evidence"),
        "execution_manifest.progress_evidence.remote_batch_dir",
    )
    evidence_case = _case(
        _required(evidence, "case", "execution_manifest.progress_evidence"),
        "execution_manifest.progress_evidence.case",
    )
    evidence_runtime = _text(
        _required(evidence, "runtime_input_dir", "execution_manifest.progress_evidence"),
        "execution_manifest.progress_evidence.runtime_input_dir",
    )
    _same("execution_manifest.progress_evidence.host", evidence_host, identity.host)
    _same("execution_manifest.progress_evidence.remote_batch_dir", evidence_batch, identity.remote_batch_dir)
    _same("execution_manifest.progress_evidence.case", evidence_case, identity.case)
    _same("execution_manifest.progress_evidence.runtime_input_dir", evidence_runtime, identity.runtime_input_dir)
    _same("execution_manifest.progress_evidence.case_dir", evidence_runtime, f"{evidence_batch}/{evidence_case}")

    try:
        progress_identity = progress_evidence.load_execution_identity(path)
        rendered = progress_evidence.build_remote_export_script(path)
    except Exception as error:
        _error(
            "PROGRESS_EVIDENCE_RENDER_FAILED",
            "The formal execution manifest cannot be passed directly to progress_evidence.py.",
            error=f"{type(error).__name__}: {error}",
            path=str(path),
        )
        raise AssertionError from error
    _same("progress_evidence.identity.host", progress_identity["host"], identity.host)
    _same(
        "progress_evidence.identity.remote_batch_dir",
        progress_identity["remote_batch_dir"],
        identity.remote_batch_dir,
    )
    _same("progress_evidence.identity.case", progress_identity["case"], identity.case)
    _same(
        "progress_evidence.identity.remote_input_dir",
        progress_identity["remote_input_dir"],
        identity.runtime_input_dir,
    )
    for field in (identity.remote_batch_dir, identity.case, identity.runtime_input_dir):
        if field not in rendered:
            _error(
                "PROGRESS_EVIDENCE_IDENTITY_MISSING",
                "The directly rendered exporter does not contain the approved identity.",
                field=field,
            )
    return {
        "status": "PASS",
        "rendered_script_length": len(rendered),
        "formal_manifest_path": str(path),
        "temporary_adapter_used": False,
    }


def _top_level_assignments(tree: ast.Module) -> dict[str, list[ast.expr]]:
    result: dict[str, list[ast.expr]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    result.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            result.setdefault(node.target.id, []).append(node.value)
    return result


def _static_value(
    node: ast.expr,
    assignments: Mapping[str, list[ast.expr]],
    cache: dict[str, Any],
    stack: tuple[str, ...] = (),
) -> Any:
    if isinstance(node, ast.Constant) and (type(node.value) in {str, int, bool}):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in cache:
            return cache[node.id]
        if node.id in stack:
            _error("GENERATOR_DECLARATION_CYCLE", "Generator identity declarations form a cycle.", name=node.id)
        values = assignments.get(node.id)
        if not values or len(values) != 1:
            _error(
                "GENERATOR_DECLARATION_AMBIGUOUS",
                "A generator identity name is missing or assigned more than once.",
                name=node.id,
            )
        value = _static_value(values[0], assignments, cache, stack + (node.id,))
        cache[node.id] = value
        return value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_value(node.left, assignments, cache, stack)
        right = _static_value(node.right, assignments, cache, stack)
        if type(left) is not type(right) or type(left) not in {str, int}:
            _error("GENERATOR_DECLARATION_UNSUPPORTED", "Only same-type literal additions are supported.")
        return left + right
    _error(
        "GENERATOR_DECLARATION_UNSUPPORTED",
        "The checker cannot prove a required generator identity declaration statically.",
        expression=ast.dump(node, include_attributes=False),
    )
    raise AssertionError


def _contains_name(node: ast.AST, names: frozenset[str]) -> bool:
    return any(isinstance(item, ast.Name) and item.id in names for item in ast.walk(node))


def _is_len_calculations(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return False
    return (
        node.func.id == "len"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "calculations"
    )


def _check_generator_comparison(
    node: ast.Compare,
    assignments: Mapping[str, list[ast.expr]],
    cache: dict[str, Any],
    identity: Identity,
) -> bool:
    """Check the only supported source-frame guard, or a direct static equality.

    The actual generator uses 'len(calculations) < XML_CALCULATION_1_BASED'.
    All other comparisons involving the source index must either be a direct
    statically resolvable equality or be rejected.  In particular, an unknown
    variable, a reversed length guard, a chained comparison, or an expression
    prefix such as '14 + 1' is never silently ignored.
    """

    if len(node.ops) != 1 or len(node.comparators) != 1:
        _error(
            "GENERATOR_COMPARISON_UNSUPPORTED",
            "Generator source-index comparisons must have exactly one operator and comparator.",
            expression=ast.dump(node, include_attributes=False),
        )
    left = node.left
    right = node.comparators[0]
    left_is_length_guard = _is_len_calculations(left)
    right_is_length_guard = _is_len_calculations(right)
    if left_is_length_guard or right_is_length_guard:
        if not (
            left_is_length_guard
            and not right_is_length_guard
            and isinstance(node.ops[0], ast.Lt)
            and isinstance(right, ast.Name)
            and right.id == "XML_CALCULATION_1_BASED"
        ):
            _error(
                "GENERATOR_COMPARISON_UNSUPPORTED",
                "Generator length guards must use the exact forward source-index form.",
                expression=ast.dump(node, include_attributes=False),
            )
        value = _static_value(right, assignments, cache)
        _same(
            "generator.calculation_index_1_based_comparison",
            value,
            identity.source_calculation_index_1_based,
        )
        return True

    left_is_index = isinstance(left, ast.Name) and left.id in INDEX_IDENTITY_NAMES
    right_is_index = isinstance(right, ast.Name) and right.id in INDEX_IDENTITY_NAMES
    if not (left_is_index or right_is_index):
        if _contains_name(node, INDEX_IDENTITY_NAMES):
            _error(
                "GENERATOR_COMPARISON_UNSUPPORTED",
                "Generator source-index comparisons must use a direct restricted expression.",
                expression=ast.dump(node, include_attributes=False),
            )
        return False
    if not (left_is_index ^ right_is_index) or not isinstance(node.ops[0], ast.Eq):
        _error(
            "GENERATOR_COMPARISON_UNSUPPORTED",
            "Generator source-index equality must compare the declared index directly.",
            expression=ast.dump(node, include_attributes=False),
        )
    other = right if left_is_index else left
    value = _static_value(other, assignments, cache)
    _same(
        "generator.calculation_index_1_based_comparison",
        value,
        identity.source_calculation_index_1_based,
    )
    return True


def _check_generator(path: Path, identity: Identity) -> None:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as error:
        _error("GENERATOR_SYNTAX_INVALID", "The generator cannot be parsed.", error=str(error))
        raise AssertionError from error
    assignments = _top_level_assignments(tree)
    cache: dict[str, Any] = {}
    names = ("TASK_ID", "UNIT_ID", "CASE_ID", "REMOTE_BATCH", "REMOTE_CASE", "XML_CALCULATION_1_BASED")
    for name in names:
        if name not in assignments or len(assignments[name]) != 1:
            _error(
                "GENERATOR_IDENTITY_UNDECLARED",
                f"Generator declaration {name} must occur exactly once.",
                name=name,
            )
    values = {name: _static_value(assignments[name][0], assignments, cache) for name in names}
    expected = {
        "TASK_ID": identity.task_id,
        "UNIT_ID": identity.unit_id,
        "CASE_ID": identity.case,
        "REMOTE_BATCH": identity.remote_batch_dir,
        "REMOTE_CASE": identity.runtime_input_dir,
        "XML_CALCULATION_1_BASED": identity.source_calculation_index_1_based,
    }
    for name, expected_value in expected.items():
        _same(f"generator.{name}", values[name], expected_value)

    guard_count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            guard_count += int(_check_generator_comparison(node, assignments, cache, identity))
    if guard_count < 1:
        _error(
            "GENERATOR_INDEX_GUARD_UNDECLARED",
            "Generator must contain at least one statically supported source-index guard.",
            count=guard_count,
        )


def _shell_assignments(source: str) -> dict[str, list[tuple[bool, str | None, int]]]:
    """Parse only simple declaration lines; do not model shell execution."""

    result: dict[str, list[tuple[bool, str | None, int]]] = {}
    assignment_re = re.compile(r"^\s*(?:(export)\s+)?([A-Z][A-Z0-9_]*)=(.*?)\s*$")
    export_only_re = re.compile(r"^\s*export\s+([A-Z][A-Z0-9_]*)\s*$")
    for line_number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = assignment_re.match(line)
        if match:
            exported = match.group(1) is not None
            name = match.group(2)
            result.setdefault(name, []).append((exported, match.group(3).strip(), line_number))
            continue
        match = export_only_re.match(line)
        if match:
            result.setdefault(match.group(1), []).append((True, None, line_number))
    return result


def _shell_literal(value: str | None, label: str) -> str:
    if value is None:
        _error("TASK_ENV_IDENTITY_UNDECLARED", f"{label} has no assigned literal value.")
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1]
    if not text or any(token in text for token in ("$", chr(96), ";", "&&", "||", chr(10))):
        _error("TASK_ENV_IDENTITY_UNSUPPORTED", f"{label} is not a static shell literal.")
    return text


def _check_task_env(path: Path, identity: Identity) -> None:
    assignments = _shell_assignments(path.read_text(encoding="utf-8"))
    expected = {
        "WORKFLOW_UNIT_ID": identity.unit_id,
        "TASK_ID": identity.task_id,
        "REMOTE_BATCH_DIR": identity.remote_batch_dir,
        "CASE_ID": identity.case,
    }
    for name, expected_value in expected.items():
        entries = assignments.get(name, [])
        if not entries:
            _error("TASK_ENV_IDENTITY_UNDECLARED", f"task_env.sh has no declaration for {name}.")
        if len(entries) != 1:
            _error(
                "TASK_ENV_IDENTITY_DUPLICATE",
                f"task_env.sh assigns identity {name} more than once.",
                lines=[entry[2] for entry in entries],
            )
        exported, value, line_number = entries[0]
        if not exported:
            _error(
                "TASK_ENV_IDENTITY_NOT_EXPORTED",
                f"task_env.sh identity {name} must be declared with export.",
                line=line_number,
            )
        actual = _shell_literal(value, f"task_env.{name}")
        _same(f"task_env.{name}", actual, expected_value)


def _extract_runner_python_heredocs(source: str) -> list[tuple[int, str]]:
    lines = source.splitlines()
    blocks: list[tuple[int, str]] = []
    line_number = 0
    while line_number < len(lines):
        line = lines[line_number]
        if line.lstrip().startswith("#"):
            line_number += 1
            continue
        match = RUNNER_HEREDOC_RE.match(line)
        if not match:
            line_number += 1
            continue
        delimiter = match.group("delimiter")
        body_start = line_number + 1
        body_end = body_start
        while body_end < len(lines) and lines[body_end] != delimiter:
            body_end += 1
        if body_end >= len(lines):
            _error(
                "RUNNER_HEREDOC_UNTERMINATED",
                "Runner contains an unterminated Python heredoc.",
                line=line_number + 1,
                delimiter=delimiter,
            )
        blocks.append((line_number + 1, chr(10).join(lines[body_start:body_end])))
        line_number = body_end + 1
    return blocks


def _runner_slice(node: ast.AST) -> ast.AST:
    if isinstance(node, ast.Index):  # pragma: no cover - Python < 3.9 compatibility
        return node.value
    return node


def _runner_string(node: ast.AST) -> str | None:
    node = _runner_slice(node)
    if isinstance(node, ast.Constant) and type(node.value) is str:
        return node.value
    return None


def _runner_path(node: ast.AST) -> tuple[str, ...] | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Subscript):
        key = _runner_string(current.slice)
        if key is None:
            return None
        parts.append(key)
        current = current.value
    if not isinstance(current, ast.Name) or current.id != "m":
        return None
    return tuple(reversed(parts))


def _runner_literal(node: ast.AST) -> Any:
    node = _runner_slice(node)
    if isinstance(node, ast.Constant) and type(node.value) in {str, int, bool}:
        return node.value
    if isinstance(node, ast.List):
        values: list[int] = []
        for item in node.elts:
            item = _runner_slice(item)
            if not isinstance(item, ast.Constant) or type(item.value) is not int:
                _error(
                    "RUNNER_EXPRESSION_UNSUPPORTED",
                    "Runner list guards must contain only integer literals.",
                    expression=ast.dump(node, include_attributes=False),
                )
            values.append(item.value)
        return values
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and type(node.operand.value) is int
    ):
        return -node.operand.value
    _error(
        "RUNNER_EXPRESSION_UNSUPPORTED",
        "Runner identity assertions must compare against a simple literal.",
        expression=ast.dump(node, include_attributes=False),
    )
    raise AssertionError


def _runner_manifest_loader(node: ast.AST) -> bool:
    if not isinstance(node, ast.With) or len(node.items) != 1 or len(node.body) != 1:
        return False
    item = node.items[0]
    if not isinstance(item.optional_vars, ast.Name) or item.optional_vars.id != "stream":
        return False
    context = item.context_expr
    if not isinstance(context, ast.Call) or not isinstance(context.func, ast.Name) or context.func.id != "open":
        return False
    if len(context.args) != 1 or len(context.keywords) != 1:
        return False
    argument = _runner_slice(context.args[0])
    argument_slice = _runner_slice(argument.slice) if isinstance(argument, ast.Subscript) else None
    if not (
        isinstance(argument, ast.Subscript)
        and isinstance(argument.value, ast.Attribute)
        and isinstance(argument.value.value, ast.Name)
        and argument.value.value.id == "sys"
        and argument.value.attr == "argv"
        and isinstance(argument_slice, ast.Constant)
        and type(argument_slice.value) is int
        and argument_slice.value == 1
    ):
        return False
    keyword = context.keywords[0]
    if keyword.arg != "encoding" or not isinstance(keyword.value, ast.Constant) or keyword.value.value != "utf-8":
        return False
    statement = node.body[0]
    if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
        return False
    target = statement.targets[0]
    value = statement.value
    return (
        isinstance(target, ast.Name)
        and target.id == "m"
        and isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and isinstance(value.func.value, ast.Name)
        and value.func.value.id == "json"
        and value.func.attr == "load"
        and len(value.args) == 1
        and isinstance(value.args[0], ast.Name)
        and value.args[0].id == "stream"
        and not value.keywords
    )


def _runner_manifest_tree(path: Path, source: str) -> ast.Module:
    blocks = _extract_runner_python_heredocs(source)
    candidates: list[tuple[int, ast.Module]] = []
    for line_number, body in blocks:
        try:
            tree = ast.parse(body, filename=f"{path}:{line_number}")
        except SyntaxError as error:
            _error(
                "RUNNER_HEREDOC_SYNTAX_INVALID",
                "A Python heredoc in the runner cannot be parsed.",
                line=line_number,
                error=str(error),
            )
            raise AssertionError from error
        if any(_runner_manifest_loader(node) for node in tree.body):
            candidates.append((line_number, tree))
    if len(candidates) != 1:
        _error(
            "RUNNER_MANIFEST_CHECK_UNDECLARED",
            "Runner must contain exactly one top-level Python manifest-check heredoc.",
            candidate_lines=[line for line, _ in candidates],
        )
    return candidates[0][1]


def _runner_assert_leaves(test: ast.expr) -> list[ast.expr]:
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        leaves: list[ast.expr] = []
        for value in test.values:
            leaves.extend(_runner_assert_leaves(value))
        return leaves
    return [test]


def _check_runner(path: Path, identity: Identity) -> None:
    source = path.read_text(encoding="utf-8")
    tree = _runner_manifest_tree(path, source)
    top_level_asserts = [node for node in tree.body if isinstance(node, ast.Assert)]
    top_level_assert_ids = {id(node) for node in top_level_asserts}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert) and id(node) not in top_level_assert_ids:
            _error(
                "RUNNER_ASSERT_NON_TOP_LEVEL",
                "Runner manifest assertions must be actual top-level asserts.",
                expression=ast.dump(node, include_attributes=False),
            )
    loader_count = 0
    for node in tree.body:
        if isinstance(node, ast.Import):
            if any(alias.name not in {"json", "sys"} for alias in node.names):
                _error(
                    "RUNNER_HEREDOC_UNSUPPORTED",
                    "Runner manifest heredoc contains an unsupported import.",
                    expression=ast.dump(node, include_attributes=False),
                )
        elif isinstance(node, ast.With) and _runner_manifest_loader(node):
            loader_count += 1
        elif isinstance(node, ast.Assert):
            continue
        else:
            _error(
                "RUNNER_HEREDOC_UNSUPPORTED",
                "Runner manifest heredoc contains an unsupported or conditional statement.",
                expression=ast.dump(node, include_attributes=False),
            )
    if loader_count != 1:
        _error(
            "RUNNER_MANIFEST_CHECK_UNDECLARED",
            "Runner manifest heredoc must have one supported manifest loader.",
            count=loader_count,
        )

    expected = {
        (("task_id",), "Eq"): ("runner.task_id", identity.task_id),
        (("geometry", "calculation_index_1_based"), "Eq"): (
            "runner.source_calculation_index_1_based",
            identity.source_calculation_index_1_based,
        ),
        (("execution_plan", "kind"), "Eq"): ("runner.execution_plan.kind", "static"),
        (("execution_plan", "fresh"), "Is"): ("runner.execution_plan.fresh", True),
        (("execution_plan", "istart"), "Eq"): ("runner.execution_plan.istart", 0),
        (("execution_plan", "icharg"), "Eq"): ("runner.execution_plan.icharg", 2),
        (("execution_plan", "ibrion"), "Eq"): ("runner.execution_plan.ibrion", -1),
        (("execution_plan", "nsw"), "Eq"): ("runner.execution_plan.nsw", 0),
        (("execution_plan", "run_count"), "Eq"): ("runner.execution_plan.run_count", 1),
    }
    auxiliary = {
        (("environment", "environment_id"), "Eq"),
        (("geometry", "nions"), "Eq"),
        (("geometry", "counts"), "Eq"),
    }
    seen: set[tuple[tuple[str, ...], str]] = set()
    for statement in top_level_asserts:
        for leaf in _runner_assert_leaves(statement.test):
            if not isinstance(leaf, ast.Compare) or len(leaf.ops) != 1 or len(leaf.comparators) != 1:
                _error(
                    "RUNNER_ASSERT_UNSUPPORTED",
                    "Runner manifest assertions must be simple restricted comparisons.",
                    expression=ast.dump(leaf, include_attributes=False),
                )
            path_value = _runner_path(leaf.left)
            if path_value is None or not isinstance(leaf.ops[0], (ast.Eq, ast.Is)):
                _error(
                    "RUNNER_ASSERT_UNSUPPORTED",
                    "Runner manifest assertions must use a direct manifest path on the left.",
                    expression=ast.dump(leaf, include_attributes=False),
                )
            key = (path_value, type(leaf.ops[0]).__name__)
            if key not in expected and key not in auxiliary:
                _error(
                    "RUNNER_ASSERT_UNSUPPORTED",
                    "Runner manifest assertion is outside the supported static identity guard set.",
                    expression=ast.dump(leaf, include_attributes=False),
                )
            if key in seen:
                _error(
                    "RUNNER_ASSERT_DUPLICATE",
                    "Runner manifest identity/static guard is asserted more than once.",
                    path=list(path_value),
                )
            seen.add(key)
            value = _runner_literal(leaf.comparators[0])
            if key in auxiliary:
                if key == (("environment", "environment_id"), "Eq"):
                    valid_auxiliary = type(value) is str and bool(value)
                elif key == (("geometry", "nions"), "Eq"):
                    valid_auxiliary = type(value) is int and value > 0
                else:
                    valid_auxiliary = (
                        isinstance(value, list)
                        and bool(value)
                        and all(type(item) is int and item > 0 for item in value)
                    )
                if not valid_auxiliary:
                    _error(
                        "RUNNER_AUXILIARY_GUARD_UNSUPPORTED",
                        "Runner auxiliary manifest guards have an unsupported literal shape.",
                        path=list(path_value),
                    )
                continue
            label, expected_value = expected[key]
            if value != expected_value:
                if key in {
                    (("task_id",), "Eq"),
                    (("geometry", "calculation_index_1_based"), "Eq"),
                }:
                    _same(label, value, expected_value)
                _error(
                    "RUNNER_STATIC_GUARD_MISMATCH",
                    "Runner static-form guard differs from the supported fresh static form.",
                    field=label,
                    expected=expected_value,
                    actual=value,
                )
    missing = sorted(set(expected) - seen)
    if missing:
        _error(
            "RUNNER_IDENTITY_UNDECLARED",
            "Runner is missing one or more required top-level static identity guards.",
            missing=[list(path) + [operator] for path, operator in missing],
        )


def check_delivery(candidate_dir: Path | str, spec_path: Path | str) -> dict[str, Any]:
    candidate = Path(candidate_dir).resolve()
    spec_file = Path(spec_path).resolve()
    if not candidate.is_dir():
        _error("CANDIDATE_DIR_MISSING", "Candidate directory does not exist.", path=str(candidate))
    identity = _identity_from_spec(_read_json(spec_file, "identity specification"))
    paths = {name: candidate / name for name in REQUIRED_STATIC_FILES}
    for name, path in paths.items():
        if not path.is_file():
            _error("CANDIDATE_FILE_MISSING", f"Required candidate file is missing: {name}.", path=str(path))
    generator_candidates = sorted(candidate.glob("prepare_*.py"))
    runner_candidates = sorted(candidate.glob("run_*.sh"))
    if len(generator_candidates) != 1:
        _error(
            "GENERATOR_FILE_AMBIGUOUS",
            "Candidate must contain exactly one prepare_*.py generator.",
            candidates=[path.name for path in generator_candidates],
        )
    if len(runner_candidates) != 1:
        _error(
            "RUNNER_FILE_AMBIGUOUS",
            "Candidate must contain exactly one run_*.sh runner.",
            candidates=[path.name for path in runner_candidates],
        )
    paths["generator"] = generator_candidates[0]
    paths["runner"] = runner_candidates[0]

    input_manifest = _read_json(paths["input_manifest.json"], "input_manifest")
    execution_manifest = _read_json(paths["execution_manifest.json"], "execution_manifest")
    _check_static_form(input_manifest)
    _check_input_manifest(input_manifest, identity)
    _check_execution_manifest(execution_manifest, paths["execution_manifest.json"], identity)
    _check_generator(paths["generator"], identity)
    _check_runner(paths["runner"], identity)
    _check_task_env(paths["task_env.sh"], identity)
    return {
        "schema": SCHEMA,
        "passed": True,
        "identity": identity.as_dict(),
        "checks": {
            "static_form": "PASS",
            "input_manifest_identity": "PASS",
            "execution_manifest_identity": "PASS",
            "generator_identity": "PASS",
            "runner_identity": "PASS",
            "task_env_identity": "PASS",
            "progress_evidence_direct_render": "PASS",
        },
        "scope": {
            "identity_and_local_delivery_compatibility_only": True,
            "scientific_or_paw_validation": "NOT_PERFORMED",
            "geometry_validation": "NOT_PERFORMED",
            "remote_actions": "NOT_PERFORMED",
            "process_execution": "NONE",
            "temporary_adapter_used": False,
            "relaxation_preflight_called": False,
        },
    }


def _hash_file(path: Path) -> str:
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        _error("PACKAGE_FILE_UNREADABLE", "A package file could not be hashed.", path=str(path), error=str(error))
        raise AssertionError from error


def _check_delivery_identity_equal(
    actual: DeliveryIdentity,
    expected: DeliveryIdentity,
    label: str,
) -> None:
    if actual != expected:
        _error(
            "DELIVERY_IDENTITY_CONFLICT",
            f"{label} does not exactly match input_manifest.delivery_identity.",
            expected=expected.as_dict(),
            actual=actual.as_dict(),
        )


def _check_static_script_template(path: Path, identity: DeliveryIdentity, *, requires_manifest_read: bool) -> None:
    try:
        raw = path.read_bytes()
        source = raw.decode("utf-8")
    except OSError as error:
        _error("PACKAGE_FILE_UNREADABLE", "A static package script could not be read.", path=str(path), error=str(error))
        raise AssertionError from error
    except UnicodeDecodeError as error:
        _error("PACKAGE_FILE_NOT_UTF8", "A static package script is not UTF-8.", path=str(path), error=str(error))
        raise AssertionError from error
    try:
        expected = trusted_templates.template_bytes(path.name)
    except KeyError:
        expected = None
    if expected is not None and raw != expected:
        _error(
            "STATIC_TEMPLATE_VERSION_MISMATCH",
            "A static package script differs from the trusted versioned template.",
            path=str(path),
            template_version=trusted_templates.TEMPLATE_VERSION,
        )
    forbidden_literals = (
        identity.task_id,
        identity.unit_id,
        identity.source_path,
        identity.remote_batch_dir,
        identity.case,
        identity.tmux_session,
    )
    for literal in forbidden_literals:
        if literal and literal in source:
            _error(
                "STATIC_TEMPLATE_IDENTITY_HARDCODED",
                "Runner/preparer templates must read identity from input_manifest at runtime.",
                path=str(path),
                literal=literal,
            )
    for line in source.splitlines():
        if "calculation_index_1_based" in line and str(identity.source_calculation_index_1_based) in line:
            _error(
                "STATIC_TEMPLATE_SOURCE_INDEX_HARDCODED",
                "Runner/preparer templates must not contain a source-frame literal.",
                path=str(path),
            )
    if requires_manifest_read and (
        "input_manifest.json" not in source or "delivery_identity" not in source
    ):
        _error(
            "STATIC_TEMPLATE_MANIFEST_READ_MISSING",
            "Runner/preparer must read delivery_identity from input_manifest.json.",
            path=str(path),
        )


def check_delivery_manifest(candidate_dir: Path | str) -> dict[str, Any]:
    """Check a new static package whose manifest is its sole identity source."""

    candidate = Path(candidate_dir).resolve()
    if not candidate.is_dir():
        _error("CANDIDATE_DIR_MISSING", "Candidate directory does not exist.", path=str(candidate))
    paths = {name: candidate / name for name in STATIC_PACKAGE_REQUIRED_FILES}
    for name, path in paths.items():
        if not path.is_file():
            _error("CANDIDATE_FILE_MISSING", f"Static package file is missing: {name}.", path=str(path))
    input_manifest = _read_json(paths["input_manifest.json"], "input_manifest")
    identity = load_delivery_identity(input_manifest)
    _same(
        "input_manifest.delivery_template",
        _required(input_manifest, "delivery_template", "input_manifest"),
        STATIC_PACKAGE_TEMPLATE,
    )
    dependencies = _read_json(paths["runtime_dependencies.json"], "runtime_dependencies")
    try:
        runtime_guard.validate_dependency_descriptor(input_manifest, dependencies)
    except runtime_guard.RuntimeGuardError as error:
        _error(error.code, error.message, **error.fields)
    if input_manifest.get("runtime_dependencies") != dependencies:
        _error(
            "RUNTIME_DEPENDENCY_PACKAGE_CONFLICT",
            "runtime_dependencies.json must exactly match input_manifest.runtime_dependencies.",
        )
    paw_metadata = _read_json(paths["paw_identity.json"], "paw_identity")
    try:
        runtime_guard.validate_paw_metadata(input_manifest, paw_metadata)
    except runtime_guard.RuntimeGuardError as error:
        _error(error.code, error.message, **error.fields)
    if input_manifest.get("paw_identity") != paw_metadata:
        _error(
            "PAW_IDENTITY_PACKAGE_CONFLICT",
            "paw_identity.json must exactly match input_manifest.paw_identity.",
        )
    trusted_guard = Path(__file__).resolve().with_name("static_runtime_guard.py")
    try:
        if paths["static_runtime_guard.py"].read_bytes() != trusted_guard.read_bytes():
            _error(
                "RUNTIME_GUARD_VERSION_MISMATCH",
                "The packaged runtime guard differs from the trusted local guard.",
                template_version=runtime_guard.SCHEMA,
            )
    except OSError as error:
        _error("RUNTIME_GUARD_UNREADABLE", "The trusted runtime guard could not be read.", error=str(error))
    for name in ("run_static.sh", "remote_prepare_static.sh"):
        _check_static_script_template(paths[name], identity, requires_manifest_read=True)
    _check_static_script_template(paths["task_env.sh"], identity, requires_manifest_read=False)
    _check_static_script_template(paths["static_postcheck.py"], identity, requires_manifest_read=False)

    execution_manifest = _read_json(paths["execution_manifest.json"], "execution_manifest")
    execution_identity = load_delivery_identity(execution_manifest)
    _check_delivery_identity_equal(execution_identity, identity, "execution_manifest.delivery_identity")
    _same(
        "execution_manifest.delivery_template",
        _required(execution_manifest, "delivery_template", "execution_manifest"),
        STATIC_PACKAGE_TEMPLATE,
    )
    _check_canonical_source_geometry_binding(input_manifest, execution_manifest, identity)
    actions = execution_manifest.get("remote_actions", {})
    if isinstance(actions, Mapping) and any(value is True for value in actions.values()):
        _error(
            "EXECUTION_STATE_NOT_LOCAL",
            "A newly built static package must not claim a remote action has started.",
            actions=dict(actions),
        )
    flags = input_manifest.get("execution_flags", {})
    if isinstance(flags, Mapping) and any(value is True for value in flags.values()):
        _error(
            "EXECUTION_STATE_NOT_LOCAL",
            "A newly built static package must not claim an execution action has started.",
            flags=dict(flags),
        )

    try:
        progress_identity = progress_evidence.load_execution_identity(paths["execution_manifest.json"])
        rendered = progress_evidence.build_remote_export_script(paths["execution_manifest.json"])
    except Exception as error:
        _error(
            "PROGRESS_EVIDENCE_RENDER_FAILED",
            "The formal execution manifest cannot be consumed directly by progress_evidence.py.",
            error=f"{type(error).__name__}: {error}",
        )
        raise AssertionError from error
    _same("progress_evidence.identity.host", progress_identity["host"], identity.host)
    _same("progress_evidence.identity.remote_batch_dir", progress_identity["remote_batch_dir"], identity.remote_batch_dir)
    _same("progress_evidence.identity.case", progress_identity["case"], identity.case)
    _same("progress_evidence.identity.remote_input_dir", progress_identity["remote_input_dir"], identity.runtime_input_dir)
    for value in (identity.remote_batch_dir, identity.case, identity.runtime_input_dir):
        if value not in rendered:
            _error(
                "PROGRESS_EVIDENCE_IDENTITY_MISSING",
                "The directly rendered progress-evidence script is missing a derived identity value.",
                value=value,
            )

    input_report = _load_executor_input_report(paths["input_manifest.json"], candidate)
    if not input_report.get("passed"):
        _error(
            "REAL_INPUT_VALIDATION_FAILED",
            "The generated POSCAR/INCAR/KPOINTS do not pass the static executor input gate.",
            validation=input_report,
        )
    outputs = _mapping(_required(input_manifest, "outputs", "input_manifest"), "input_manifest.outputs")
    for name in INPUT_FILES:
        item = _mapping(_required(outputs, name, "input_manifest.outputs"), f"input_manifest.outputs.{name}")
        _same(f"input_manifest.outputs.{name}.sha256", _required(item, "sha256", f"input_manifest.outputs.{name}"), _hash_file(candidate / name))

    requirements = _read_json(paths["output_requirements.json"], "output_requirements")
    if requirements.get("kind") != "static":
        _error("STATIC_REQUIREMENTS_INVALID", "New static packages require output_requirements.kind=static.")
    policy = _mapping(_required(requirements, "static_policy", "output_requirements"), "output_requirements.static_policy")
    if policy.get("ionic_convergence_required") is not False:
        _error("STATIC_IONIC_GATE_ENABLED", "Static output requirements must not require ionic convergence.")
    markers = _required(requirements, "markers", "output_requirements")
    if not isinstance(markers, list) or not markers:
        _error("STATIC_MARKERS_INVALID", "Static output requirements must declare explicit mechanical markers.")
    if any(isinstance(marker, Mapping) and marker.get("category") == "ionic" for marker in markers):
        _error("STATIC_IONIC_MARKER_PRESENT", "Static output requirements must not use an ionic marker as a gate.")
    _check_output_identity_contract(input_manifest, requirements)

    whitelist = _read_json(paths["remote_upload_whitelist.json"], "remote_upload_whitelist")
    files = _required(whitelist, "files", "remote_upload_whitelist")
    mutable = _required(whitelist, "mutable_files", "remote_upload_whitelist")
    if (
        not isinstance(files, list)
        or len(files) != len(set(files))
        or not isinstance(mutable, list)
        or len(mutable) != len(set(mutable))
        or "sol_review_gate.json" in files
        or "sol_review_gate.json" not in mutable
        or "execution_manifest.json" in files
    ):
        _error("UPLOAD_WHITELIST_INVALID", "Static upload whitelist has unsafe, duplicate or mutable-file overlap.")
    forbidden = {"POTCAR", "CHGCAR", "WAVECAR", "TMPCAR", "CONTCAR", "OUTCAR", "OSZICAR", "vasprun.xml"}
    if forbidden.intersection(files):
        _error("SENSITIVE_FILE_WHITELISTED", "Sensitive or returned-output files must not enter the upload whitelist.")

    checksum_path = paths["input_checksums.sha256"]
    checksum_entries: dict[str, str] = {}
    try:
        checksum_lines = checksum_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        _error("CHECKSUM_FILE_UNREADABLE", "The immutable input checksum file could not be read.", error=str(error))
    for line_number, line in enumerate(checksum_lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        if len(parts) != 2 or re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]) is None or not parts[1]:
            _error("CHECKSUM_FILE_INVALID", "The immutable input checksum file has an invalid line.", line=line_number)
        name = parts[1]
        if Path(name).name != name or name in checksum_entries:
            _error("CHECKSUM_FILE_INVALID", "The immutable input checksum file has an unsafe or duplicate filename.", file=name)
        checksum_entries[name] = parts[0].lower()
    expected_checksum_names = set(files) - {"input_checksums.sha256"} - set(mutable)
    if set(checksum_entries) != expected_checksum_names:
        _error(
            "CHECKSUM_FILE_SET_MISMATCH",
            "The immutable checksum file must cover exactly the immutable upload whitelist.",
            expected=sorted(expected_checksum_names),
            actual=sorted(checksum_entries),
        )
    for name, expected_digest in checksum_entries.items():
        actual_digest = _hash_file(candidate / name)
        if actual_digest != expected_digest:
            _error(
                "CHECKSUM_FILE_HASH_MISMATCH",
                "An immutable upload file differs from input_checksums.sha256.",
                file=name,
                expected=expected_digest,
                actual=actual_digest,
            )

    gate = _read_json(paths["sol_review_gate.json"], "sol_review_gate")
    if (
        gate.get("state") != "PENDING_SOL_REVIEW"
        or gate.get("execution_authorized") is not False
        or gate.get("decision_id") is not None
        or gate.get("task_id") != identity.task_id
        or gate.get("unit_id") != identity.unit_id
    ):
        _error("SOL_GATE_NOT_CLOSED", "A newly built static package must ship with a closed Sol review gate.")
    forbidden_local = forbidden | {"STOPCAR"}
    present_forbidden = sorted(name for name in forbidden_local if (candidate / name).exists())
    if present_forbidden:
        _error("FORBIDDEN_CANDIDATE_FILE", "Static candidate contains a forbidden sensitive or result file.", files=present_forbidden)
    return {
        "schema": SCHEMA,
        "passed": True,
        "mode": "manifest",
        "identity": identity.as_dict(),
        "checks": {
            "delivery_identity": "PASS",
            "real_inputs": "PASS",
            "execution_manifest": "PASS",
            "progress_evidence_direct_render": "PASS",
            "static_output_requirements": "PASS",
            "upload_whitelist": "PASS",
            "sol_gate_closed": "PASS",
            "template_identity_literals": "PASS",
        },
        "scope": {
            "identity_and_local_delivery_compatibility_only": True,
            "scientific_or_paw_validation": "NOT_PERFORMED",
            "source_xml_frame_selection": "NOT_PROVEN_BY_CHECKER",
            "remote_actions": "NOT_PERFORMED",
            "generated_script_execution": "NONE",
            "potcar_content_read": False,
        },
    }


def _load_executor_input_report(manifest_path: Path, candidate: Path) -> dict[str, Any]:
    try:
        from vasp_executor import validate_inputs
    except ImportError:  # pragma: no cover
        from .vasp_executor import validate_inputs
    return validate_inputs(manifest_path, candidate)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-dir", required=True, type=Path)
    parser.add_argument("--spec", type=Path, help="Historical compatibility mode only; new packages use their manifest identity.")
    args = parser.parse_args(argv)
    try:
        if args.spec is None:
            result = check_delivery_manifest(args.candidate_dir)
        else:
            candidate_manifest = args.candidate_dir / "input_manifest.json"
            if candidate_manifest.is_file():
                candidate_data = _read_json(candidate_manifest, "input_manifest")
                if "delivery_identity" in candidate_data:
                    _error(
                        "NEW_PACKAGE_OLD_ENTRY_FORBIDDEN",
                        "New static packages must be checked from input_manifest.delivery_identity without --spec.",
                    )
            result = check_delivery(args.candidate_dir, args.spec)
    except DeliveryError as error:
        result = {
            "schema": SCHEMA,
            "passed": False,
            "error": {
                "code": error.code,
                "message": error.message,
                **error.details,
            },
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

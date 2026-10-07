#!/usr/bin/env python3
"""Pure-Python runtime guards for a VASP static delivery package.

The module is deliberately stdlib-only.  It validates the manifest-derived
gate, dependency/environment descriptor, non-sensitive PAW metadata and
immutable package checksums.  It never assembles or prints POTCAR content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any, Mapping


SCHEMA = "vasp-static-runtime-guard/v1"
DELIVERY_TEMPLATE = "vasp-static-package/v1"
DEPENDENCY_SCHEMA = "vasp-static-runtime-dependencies/v1"
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_./:+=-]+$")
REQUIRED_MODULES = ("vasp_executor.py", "progress_snapshot.py", "progress_evidence.py")
STATIC_OLD_OUTPUTS = (
    "OUTCAR",
    "OSZICAR",
    "vasprun.xml",
    "CONTCAR",
    "vasp.stdout",
    "vasp.stderr",
    "run_timing.txt",
    "preflight.json",
    "postcheck.json",
    "CHGCAR",
    "WAVECAR",
    "XDATCAR",
    "EIGENVAL",
    "IBZKPT",
    "TMPCAR",
)


class RuntimeGuardError(ValueError):
    """A fail-closed package, gate, environment or PAW metadata error."""

    def __init__(self, code: str, message: str, **fields: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = fields


def _fail(code: str, message: str, **fields: Any) -> None:
    raise RuntimeGuardError(code, message, **fields)


def _json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail("RUNTIME_JSON_INVALID", f"{label} is not readable JSON.", path=str(path), error=str(error))
    if not isinstance(value, dict):
        _fail("RUNTIME_JSON_OBJECT_REQUIRED", f"{label} must be an object.", path=str(path))
    return value


def _required(value: Mapping[str, Any], key: str, label: str) -> Any:
    if key not in value:
        _fail("RUNTIME_FIELD_MISSING", f"{label}.{key} is required.", field=f"{label}.{key}")
    return value[key]


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("RUNTIME_OBJECT_REQUIRED", f"{label} must be an object.")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value or "|" in value:
        _fail("RUNTIME_TEXT_INVALID", f"{label} must be non-empty single-line text.")
    return value


def _absolute_path(value: Any, label: str) -> str:
    text = _text(value, label)
    if not text.startswith("/") or any(part in {"", ".", ".."} for part in PurePosixPath(text).parts):
        _fail("RUNTIME_PATH_INVALID", f"{label} must be an absolute POSIX path without traversal.", value=text)
    return text


def _relative_path(value: Any, label: str) -> str:
    text = _text(value, label)
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or any(character.isspace() for character in text):
        _fail("RUNTIME_RELATIVE_PATH_INVALID", f"{label} must be a safe relative POSIX path.", value=text)
    return path.as_posix()


def _digest(value: Any, label: str) -> str:
    text = _text(value, label).lower()
    if HEX64.fullmatch(text) is None:
        _fail("RUNTIME_HASH_INVALID", f"{label} must be a SHA-256 digest.", value=text)
    return text


def validate_geometry_identity(manifest: Mapping[str, Any]) -> dict[str, Any]:
    identity = _mapping(_required(manifest, "delivery_identity", "input_manifest"), "input_manifest.delivery_identity")
    source = _mapping(_required(identity, "source", "delivery_identity"), "delivery_identity.source")
    geometry = _mapping(_required(manifest, "geometry", "input_manifest"), "input_manifest.geometry")
    kind = source.get("kind", "xml_frame")
    if kind not in {"xml_frame", "file_geometry", "approved_isolated_atom_spec"}:
        _fail("RUNTIME_SOURCE_KIND_INVALID", "The delivery identity has an unsupported geometry source kind.", source_kind=kind)
    if kind == "xml_frame":
        index = source.get("calculation_index_1_based")
        if type(index) is not int or index <= 0 or geometry.get("calculation_index_1_based") != index:
            _fail("RUNTIME_SOURCE_INDEX_CONFLICT", "XML geometry and delivery identity must name the same positive calculation index.")
        if geometry.get("source_kind", "xml_frame") != "xml_frame":
            _fail("RUNTIME_SOURCE_KIND_CONFLICT", "The geometry source kind conflicts with the XML-frame identity.")
        return {"source_kind": kind, "calculation_index_1_based": index}

    if "calculation_index_1_based" in geometry or "source_calculation_index_1_based" in manifest:
        _fail("RUNTIME_XML_INDEX_FORBIDDEN", "Non-XML geometry must not contain a calculation_index_1_based field.")
    if geometry.get("source_kind") != kind or geometry.get("source_identity") != dict(source):
        _fail("RUNTIME_SOURCE_GEOMETRY_CONFLICT", "Non-XML geometry provenance must exactly match delivery_identity.source.")
    structure = _mapping(_required(manifest, "structure", "input_manifest"), "input_manifest.structure")
    for structure_key, geometry_key in (
        ("species_order", "species_order"),
        ("counts", "counts"),
        ("nions", "nions"),
        ("fixed_global_indices", "fixed_global_indices_1based"),
        ("free_global_count", "free_global_count"),
    ):
        if structure.get(structure_key) != _required(geometry, geometry_key, "input_manifest.geometry"):
            _fail("RUNTIME_SOURCE_STRUCTURE_CONFLICT", "Non-XML source geometry conflicts with the approved target structure.", field=structure_key)
    return {"source_kind": kind, "calculation_index_1_based": None}


def load_manifest(path: Path | str) -> dict[str, Any]:
    manifest = _json(Path(path), "input_manifest")
    if manifest.get("route") != "vasp":
        _fail("RUNTIME_ROUTE_INVALID", "The runtime manifest is not a VASP manifest.")
    if manifest.get("delivery_template") != DELIVERY_TEMPLATE:
        _fail("RUNTIME_TEMPLATE_INVALID", "The runtime manifest has an unsupported delivery template.")
    identity = _mapping(_required(manifest, "delivery_identity", "input_manifest"), "input_manifest.delivery_identity")
    if identity.get("schema") != "vasp-delivery-identity/v1":
        _fail("RUNTIME_IDENTITY_SCHEMA_INVALID", "The runtime manifest has an unsupported delivery identity schema.")
    _text(_required(identity, "task_id", "delivery_identity"), "delivery_identity.task_id")
    _text(_required(identity, "unit_id", "delivery_identity"), "delivery_identity.unit_id")
    _mapping(_required(identity, "source", "delivery_identity"), "delivery_identity.source")
    validate_geometry_identity(manifest)
    return manifest


def _environment(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(_required(manifest, "environment", "input_manifest"), "input_manifest.environment")


def _list_of_abs_paths(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        _fail("RUNTIME_PATH_LIST_INVALID", f"{label} must be a non-empty path list.")
    return [_absolute_path(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _mpi_args(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and SAFE_TOKEN.fullmatch(item) for item in value):
        _fail("RUNTIME_MPI_ARGS_INVALID", f"{label} must be a safe string list.")
    args = list(value)
    if "--map-by" not in args or "core" not in args or "--bind-to" not in args:
        _fail("RUNTIME_CPU_BINDING_MISSING", "MPI arguments must explicitly retain core mapping and binding.")
    bind_index = args.index("--bind-to")
    if bind_index + 1 >= len(args) or args[bind_index + 1] != "core":
        _fail("RUNTIME_CPU_BINDING_INVALID", "MPI arguments must use --bind-to core.")
    map_index = args.index("--map-by")
    if map_index + 1 >= len(args) or args[map_index + 1] != "core":
        _fail("RUNTIME_CPU_MAPPING_INVALID", "MPI arguments must use --map-by core.")
    return args


def validate_dependency_descriptor(
    manifest: Mapping[str, Any],
    dependencies: Mapping[str, Any],
) -> dict[str, Any]:
    if dependencies.get("schema") != DEPENDENCY_SCHEMA:
        _fail("RUNTIME_DEPENDENCY_SCHEMA_INVALID", "The runtime dependency descriptor has an unsupported schema.")
    environment = _environment(manifest)
    fields = {
        "python_bin": _absolute_path(_required(dependencies, "python_bin", "runtime_dependencies"), "runtime_dependencies.python_bin"),
        "toolchain_root": _absolute_path(_required(dependencies, "toolchain_root", "runtime_dependencies"), "runtime_dependencies.toolchain_root"),
        "mpi_launcher": _absolute_path(_required(dependencies, "mpi_launcher", "runtime_dependencies"), "runtime_dependencies.mpi_launcher"),
        "tmux_bin": _absolute_path(_required(dependencies, "tmux_bin", "runtime_dependencies"), "runtime_dependencies.tmux_bin"),
        "vasp_bin": _absolute_path(_required(dependencies, "vasp_bin", "runtime_dependencies"), "runtime_dependencies.vasp_bin"),
    }
    pythonpath = _list_of_abs_paths(_required(dependencies, "pythonpath", "runtime_dependencies"), "runtime_dependencies.pythonpath")
    if fields["toolchain_root"] not in pythonpath:
        _fail(
            "RUNTIME_PYTHONPATH_CLOSURE_MISSING",
            "PYTHONPATH must contain the approved toolchain root used for imports.",
            toolchain_root=fields["toolchain_root"],
            pythonpath=pythonpath,
        )
    ld_library_path = _list_of_abs_paths(_required(dependencies, "ld_library_path", "runtime_dependencies"), "runtime_dependencies.ld_library_path")
    mpi_args = _mpi_args(_required(dependencies, "mpi_args", "runtime_dependencies"), "runtime_dependencies.mpi_args")
    if dependencies.get("tmux_required") is not True:
        _fail("RUNTIME_TMUX_REQUIRED", "The static runner must require tmux explicitly.")
    omp = dependencies.get("omp_num_threads")
    if type(omp) is not int or omp != 1:
        _fail("RUNTIME_OMP_INVALID", "The runtime dependency descriptor must lock omp_num_threads=1.")
    binding = _mapping(_required(dependencies, "cpu_binding", "runtime_dependencies"), "runtime_dependencies.cpu_binding")
    if binding.get("policy") != "physical-core" or binding.get("required") is not True:
        _fail("RUNTIME_CPU_POLICY_INVALID", "The runtime descriptor must require physical-core binding.")
    if type(binding.get("physical_cores")) is not int or binding["physical_cores"] <= 0:
        _fail("RUNTIME_CPU_POLICY_INVALID", "physical_cores must be a positive integer.")
    parallel = _mapping(_required(manifest, "parallel", "input_manifest"), "input_manifest.parallel")
    if type(parallel.get("mpi_ranks")) is not int or parallel["mpi_ranks"] != binding["physical_cores"]:
        _fail(
            "RUNTIME_CPU_CORE_COUNT_CONFLICT",
            "The approved MPI rank count must equal the locked physical-core count.",
            mpi_ranks=parallel.get("mpi_ranks"),
            physical_cores=binding["physical_cores"],
        )
    modules = _required(dependencies, "modules", "runtime_dependencies")
    if not isinstance(modules, list) or not modules:
        _fail("RUNTIME_MODULES_MISSING", "runtime_dependencies.modules must be a non-empty list.")
    normalized_modules: list[dict[str, str]] = []
    module_names: set[str] = set()
    module_paths: set[str] = set()
    for index, item in enumerate(modules):
        module = _mapping(item, f"runtime_dependencies.modules[{index}]")
        name = _text(_required(module, "name", f"runtime_dependencies.modules[{index}]"), f"runtime_dependencies.modules[{index}].name")
        path = _absolute_path(_required(module, "path", f"runtime_dependencies.modules[{index}]"), f"runtime_dependencies.modules[{index}].path")
        digest = _digest(_required(module, "sha256", f"runtime_dependencies.modules[{index}]"), f"runtime_dependencies.modules[{index}].sha256")
        if PurePosixPath(name).name != name or any(character.isspace() for character in name):
            _fail("RUNTIME_MODULE_NAME_INVALID", "Runtime module names must be simple filenames.", name=name)
        expected_path = (PurePosixPath(fields["toolchain_root"]) / name).as_posix()
        if path != expected_path:
            _fail(
                "RUNTIME_MODULE_PATH_MISMATCH",
                "Each approved module path must resolve to toolchain_root/module_name.",
                name=name,
                expected=expected_path,
                actual=path,
            )
        if name in module_names:
            _fail("RUNTIME_MODULE_DUPLICATE", "runtime_dependencies.modules contains a duplicate name.", name=name)
        if path in module_paths:
            _fail("RUNTIME_MODULE_PATH_DUPLICATE", "runtime_dependencies.modules contains a duplicate path.", path=path)
        module_names.add(name)
        module_paths.add(path)
        normalized_modules.append({"name": name, "path": path, "sha256": digest})
    missing = sorted(set(REQUIRED_MODULES) - module_names)
    if missing:
        _fail("RUNTIME_MODULE_MISSING", "The runtime descriptor does not lock all required toolchain modules.", missing=missing)
    environment_id = environment.get("environment_id")
    if environment_id is not None and dependencies.get("environment_id") != environment_id:
        _fail(
            "RUNTIME_ENVIRONMENT_ID_CONFLICT",
            "runtime_dependencies.environment_id differs from the approved environment.",
            expected=environment_id,
            actual=dependencies.get("environment_id"),
        )
    expected = {
        "python_bin": environment.get("python_bin"),
        "toolchain_root": environment.get("toolchain_root"),
        "mpi_launcher": environment.get("mpi_launcher"),
        "tmux_bin": environment.get("tmux_bin"),
        "vasp_bin": environment.get("vasp_bin"),
        "pythonpath": environment.get("pythonpath"),
        "ld_library_path": environment.get("ld_library_path"),
        "mpi_args": environment.get("mpi_args"),
        "tmux_required": environment.get("tmux_required"),
        "omp_num_threads": manifest.get("parallel", {}).get("omp_num_threads") if isinstance(manifest.get("parallel"), Mapping) else None,
        "cpu_binding": environment.get("cpu_binding"),
    }
    actual = {
        "python_bin": fields["python_bin"],
        "toolchain_root": fields["toolchain_root"],
        "mpi_launcher": fields["mpi_launcher"],
        "tmux_bin": fields["tmux_bin"],
        "vasp_bin": fields["vasp_bin"],
        "pythonpath": pythonpath,
        "ld_library_path": ld_library_path,
        "mpi_args": mpi_args,
        "tmux_required": True,
        "omp_num_threads": omp,
        "cpu_binding": dict(binding),
    }
    for key, expected_value in expected.items():
        if expected_value is not None and expected_value != actual[key]:
            _fail("RUNTIME_ENV_CONFLICT", "runtime_dependencies conflicts with approved environment.", field=key, expected=expected_value, actual=actual[key])
    return {
        **fields,
        "pythonpath": pythonpath,
        "ld_library_path": ld_library_path,
        "mpi_args": mpi_args,
        "tmux_required": True,
        "omp_num_threads": omp,
        "cpu_binding": dict(binding),
        "modules": normalized_modules,
    }


def validate_gate(manifest: Mapping[str, Any], gate: Mapping[str, Any]) -> dict[str, Any]:
    identity = _mapping(_required(manifest, "delivery_identity", "input_manifest"), "input_manifest.delivery_identity")
    task_id = _text(_required(identity, "task_id", "delivery_identity"), "delivery_identity.task_id")
    unit_id = _text(_required(identity, "unit_id", "delivery_identity"), "delivery_identity.unit_id")
    if gate.get("state") != "ACCEPTED_FOR_SINGLE_RUN":
        _fail("GATE_NOT_ACCEPTED", "The Sol gate is not accepted for a single run.", state=gate.get("state"))
    if gate.get("execution_authorized") is not True:
        _fail("GATE_EXECUTION_NOT_AUTHORIZED", "The Sol gate does not explicitly authorize execution.")
    decision_id = gate.get("decision_id")
    if not isinstance(decision_id, str) or not decision_id.strip():
        _fail("GATE_DECISION_MISSING", "The accepted Sol gate must contain a decision_id.")
    if gate.get("task_id") != task_id or gate.get("unit_id") != unit_id:
        _fail("GATE_IDENTITY_CONFLICT", "The Sol gate task/unit does not match the canonical delivery identity.")
    if gate.get("reviewer_role") not in {None, "VASP Sol"}:
        _fail("GATE_REVIEWER_INVALID", "The Sol gate reviewer role is not VASP Sol.")
    return {"task_id": task_id, "unit_id": unit_id, "decision_id": decision_id}


def validate_paw_metadata(manifest: Mapping[str, Any], paw: Mapping[str, Any]) -> dict[str, Any]:
    approved = _mapping(_required(manifest, "paw_identity", "input_manifest"), "input_manifest.paw_identity")
    if paw.get("schema") != "vasp-paw-identity/v1":
        _fail("PAW_METADATA_SCHEMA_INVALID", "The PAW metadata has an unsupported schema.")
    if paw.get("content_local") is not False or paw.get("potcar_content_present") is not False:
        _fail("PAW_CONTENT_POLICY_INVALID", "PAW metadata must declare that POTCAR content is not local.")
    ordered = _required(paw, "ordered_components", "paw_identity")
    approved_ordered = _required(approved, "ordered_components", "input_manifest.paw_identity")
    if ordered != approved_ordered or not isinstance(ordered, list) or not ordered:
        _fail("PAW_ORDER_CONFLICT", "PAW ordered components differ from the approved identity.")
    components = _required(paw, "components", "paw_identity")
    if not isinstance(components, list) or len(components) != len(ordered):
        _fail("PAW_COMPONENTS_INVALID", "PAW components must match ordered_components.")
    combined_digest = _digest(_required(paw, "combined_sha256", "paw_identity"), "paw_identity.combined_sha256")
    approved_combined_digest = _digest(
        _required(approved, "combined_sha256", "input_manifest.paw_identity"),
        "input_manifest.paw_identity.combined_sha256",
    )
    if combined_digest != approved_combined_digest:
        _fail(
            "PAW_COMBINED_IDENTITY_CONFLICT",
            "The combined PAW hash differs from the approved identity.",
            expected=approved_combined_digest,
            actual=combined_digest,
        )
    names: list[str] = []
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(components):
        component = _mapping(item, f"paw_identity.components[{index}]")
        name = _text(_required(component, "name", f"paw_identity.components[{index}]"), f"paw_identity.components[{index}].name")
        path = _relative_path(_required(component, "relative_path", f"paw_identity.components[{index}]"), f"paw_identity.components[{index}].relative_path")
        digest = _digest(_required(component, "sha256", f"paw_identity.components[{index}]"), f"paw_identity.components[{index}].sha256")
        names.append(name)
        normalized.append({"name": name, "relative_path": path, "sha256": digest})
    if names != ordered:
        _fail("PAW_ORDER_CONFLICT", "PAW component names are not in approved order.", expected=ordered, actual=names)
    approved_components = approved.get("components")
    if approved_components is not None and approved_components != normalized:
        _fail("PAW_COMPONENT_IDENTITY_CONFLICT", "PAW component paths or hashes differ from the approved identity.")
    return {
        "ordered_components": list(ordered),
        "components": normalized,
        "combined_sha256": combined_digest,
    }


def _hash_file(path: Path, label: str) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        _fail("RUNTIME_FILE_UNREADABLE", f"{label} is not readable.", path=str(path), error=str(error))
        raise AssertionError from error


def _checksum_entries(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        _fail("RUNTIME_CHECKSUM_UNREADABLE", "input_checksums.sha256 is not readable.", path=str(path), error=str(error))
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 2 or HEX64.fullmatch(parts[0]) is None or Path(parts[1]).name != parts[1] or parts[1] in entries:
            _fail("RUNTIME_CHECKSUM_INVALID", "input_checksums.sha256 contains an invalid line.", line=line_number)
        entries[parts[1]] = parts[0].lower()
    return entries


def verify_package(package_dir: Path | str, manifest: Mapping[str, Any], dependencies: Mapping[str, Any]) -> dict[str, Any]:
    geometry_identity = validate_geometry_identity(manifest)
    package = Path(package_dir)
    whitelist = _json(package / "remote_upload_whitelist.json", "remote_upload_whitelist")
    files = whitelist.get("files")
    mutable = whitelist.get("mutable_files")
    if not isinstance(files, list) or len(files) != len(set(files)) or not isinstance(mutable, list) or len(mutable) != len(set(mutable)):
        _fail("RUNTIME_WHITELIST_INVALID", "The package whitelist is not a unique string list.")
    forbidden = {"POTCAR", "CHGCAR", "WAVECAR", "TMPCAR", "CONTCAR", "OUTCAR", "OSZICAR", "vasprun.xml"}
    if forbidden.intersection(files) or "sol_review_gate.json" in files or "sol_review_gate.json" not in mutable:
        _fail("RUNTIME_WHITELIST_INVALID", "The package whitelist contains a forbidden or mutable entry.")
    entries = _checksum_entries(package / "input_checksums.sha256")
    expected = set(files) - {"input_checksums.sha256"} - set(mutable)
    if set(entries) != expected:
        _fail("RUNTIME_CHECKSUM_SET_MISMATCH", "The package checksum set differs from the immutable whitelist.")
    for name, digest in entries.items():
        actual = _hash_file(package / name, name)
        if actual != digest:
            _fail("RUNTIME_CHECKSUM_HASH_MISMATCH", "An immutable package file changed after checksums were finalized.", file=name, expected=digest, actual=actual)
    return {"files_checked": sorted(entries), "geometry_identity": geometry_identity, "passed": True}


def verify_environment(dependencies: Mapping[str, Any]) -> dict[str, Any]:
    checked: list[str] = []
    toolchain_root = Path(dependencies["toolchain_root"])
    if not toolchain_root.is_dir():
        _fail("RUNTIME_TOOLCHAIN_MISSING", "The approved toolchain root is missing.", path=str(toolchain_root))
    checked.append(str(toolchain_root))
    for field in ("pythonpath", "ld_library_path"):
        for value in dependencies[field]:
            path = Path(value)
            if not path.is_dir():
                _fail("RUNTIME_LIBRARY_PATH_MISSING", "An approved runtime search path is missing.", field=field, path=str(path))
            checked.append(str(path))
    for key in ("python_bin", "mpi_launcher", "tmux_bin", "vasp_bin"):
        path = Path(dependencies[key])
        if not path.is_file() or not os.access(path, os.X_OK):
            _fail("RUNTIME_EXECUTABLE_MISSING", "An approved runtime executable is missing or not executable.", field=key, path=str(path))
        checked.append(str(path))
    for module in dependencies["modules"]:
        path = Path(module["path"])
        if not path.is_file():
            _fail("RUNTIME_MODULE_MISSING", "An approved runtime module is missing.", name=module["name"], path=str(path))
        actual = _hash_file(path, module["name"])
        if actual != module["sha256"]:
            _fail("RUNTIME_MODULE_HASH_MISMATCH", "An approved runtime module hash differs.", name=module["name"], expected=module["sha256"], actual=actual)
        checked.append(str(path))
    return {"checked": checked, "passed": True}


def verify_paw_files(paw_root: Path | str, paw: Mapping[str, Any]) -> dict[str, Any]:
    root = Path(_absolute_path(str(paw_root), "paw_root"))
    metadata = validate_paw_metadata({"paw_identity": paw}, paw)
    checked: list[str] = []
    for component in metadata["components"]:
        path = root / component["relative_path"]
        if not path.is_file():
            _fail("PAW_COMPONENT_MISSING", "An approved PAW component is missing.", name=component["name"], path=str(path))
        actual = _hash_file(path, component["name"])
        if actual != component["sha256"]:
            _fail("PAW_COMPONENT_HASH_MISMATCH", "An approved PAW component hash differs.", name=component["name"], expected=component["sha256"], actual=actual)
        checked.append(str(path))
    return {"checked": checked, "passed": True}


def verify_combined_paw_file(path: Path | str, paw: Mapping[str, Any]) -> dict[str, Any]:
    expected = _digest(_required(paw, "combined_sha256", "paw_identity"), "paw_identity.combined_sha256")
    actual = _hash_file(Path(path), "combined POTCAR")
    if actual != expected:
        _fail(
            "PAW_COMBINED_HASH_MISMATCH",
            "The assembled POTCAR hash differs from the approved combined PAW identity.",
            expected=expected,
            actual=actual,
        )
    return {"path": str(path), "sha256": actual, "passed": True}


def check_static_case_state(case_dir: Path | str) -> dict[str, Any]:
    """Reject stop controls, locks and prior static outputs before launch."""

    case = Path(case_dir)
    stopcar = case / "STOPCAR"
    if stopcar.exists():
        _fail("STATIC_STOPCAR_PRESENT", "A fresh static case must not contain STOPCAR.", path=str(stopcar))
    lock = case / ".run_once"
    if lock.exists():
        _fail("STATIC_RUN_ONCE_LOCK_PRESENT", "A fresh static case already has its single-run lock.", path=str(lock))
    stale = [name for name in STATIC_OLD_OUTPUTS if (case / name).exists()]
    if stale:
        _fail(
            "STATIC_OLD_OUTPUT_PRESENT",
            "A fresh static case contains prior output that would be overwritten.",
            files=stale,
        )
    return {"passed": True, "case_dir": str(case), "stale_files": []}


def emit_env(dependencies: Mapping[str, Any]) -> str:
    return "|".join(
        [
            dependencies["python_bin"],
            dependencies["toolchain_root"],
            ":".join(dependencies["pythonpath"]),
            ":".join(dependencies["ld_library_path"]),
            dependencies["mpi_launcher"],
            dependencies["tmux_bin"],
            " ".join(dependencies["mpi_args"]),
            str(dependencies["omp_num_threads"]),
            dependencies["cpu_binding"]["policy"],
        ]
    )


def emit_runtime(manifest: Mapping[str, Any], dependencies: Mapping[str, Any], paw: Mapping[str, Any]) -> str:
    identity = _mapping(manifest["delivery_identity"], "input_manifest.delivery_identity")
    environment = _environment(manifest)
    components = validate_paw_metadata(manifest, paw)
    return "|".join(
        [
            str(identity["remote_batch_dir"]),
            str(identity["case"]),
            str(identity["runtime_input_dir"]),
            str(identity["tmux_session"]),
            str(environment["vasp_bin"]),
            str(dependencies["mpi_launcher"]),
            str(manifest["parallel"]["mpi_ranks"]),
            " ".join(item for item in dependencies["mpi_args"]),
            str(environment["paw_root"]),
            " ".join(item["relative_path"] for item in components["components"]),
            str(components["combined_sha256"]),
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--dependencies", required=True, type=Path)
    parser.add_argument("--package-dir", type=Path)
    parser.add_argument("--gate", type=Path)
    parser.add_argument("--paw-identity", type=Path)
    parser.add_argument("--paw-root", type=Path)
    parser.add_argument("--combined-path", type=Path)
    parser.add_argument("--case-dir", type=Path)
    parser.add_argument("--check-package", action="store_true")
    parser.add_argument("--check-environment", action="store_true")
    parser.add_argument("--check-gate", action="store_true")
    parser.add_argument("--check-paw", action="store_true")
    parser.add_argument("--check-combined", action="store_true")
    parser.add_argument("--check-case-state", action="store_true")
    parser.add_argument("--emit-env", action="store_true")
    parser.add_argument("--emit-runtime", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        dependencies = validate_dependency_descriptor(manifest, _json(args.dependencies, "runtime_dependencies"))
        package_result = None
        if args.check_package:
            if args.package_dir is None:
                _fail("RUNTIME_PACKAGE_DIR_MISSING", "--package-dir is required for --check-package.")
            package_result = verify_package(args.package_dir, manifest, dependencies)
        environment_result = verify_environment(dependencies) if args.check_environment else None
        gate_result = None
        if args.check_gate:
            if args.gate is None:
                _fail("RUNTIME_GATE_PATH_MISSING", "--gate is required for --check-gate.")
            gate_result = validate_gate(manifest, _json(args.gate, "sol_review_gate"))
        paw = None
        paw_result = None
        if args.check_paw or args.check_combined or args.emit_runtime:
            if args.paw_identity is None:
                _fail("RUNTIME_PAW_METADATA_PATH_MISSING", "--paw-identity is required.")
            paw = _json(args.paw_identity, "paw_identity")
            validate_paw_metadata(manifest, paw)
        if args.check_paw:
            if args.paw_root is None:
                _fail("RUNTIME_PAW_ROOT_MISSING", "--paw-root is required for --check-paw.")
            paw_result = verify_paw_files(args.paw_root, paw)
        combined_result = None
        if args.check_combined:
            if args.combined_path is None:
                _fail("RUNTIME_COMBINED_PATH_MISSING", "--combined-path is required for --check-combined.")
            if paw is None:
                _fail("RUNTIME_PAW_METADATA_PATH_MISSING", "--paw-identity is required for --check-combined.")
            combined_result = verify_combined_paw_file(args.combined_path, paw)
        case_state_result = None
        if args.check_case_state:
            if args.case_dir is None:
                _fail("RUNTIME_CASE_DIR_MISSING", "--case-dir is required for --check-case-state.")
            case_state_result = check_static_case_state(args.case_dir)
        if args.emit_env:
            print(emit_env(dependencies))
        if args.emit_runtime:
            if paw is None:
                _fail("RUNTIME_PAW_METADATA_PATH_MISSING", "--paw-identity is required for --emit-runtime.")
            print(emit_runtime(manifest, dependencies, paw))
        if not args.emit_env and not args.emit_runtime:
            print(json.dumps({
                "schema": SCHEMA,
                "passed": True,
                "package": package_result,
                "environment": environment_result,
                "gate": gate_result,
                "paw": paw_result,
                "combined_paw": combined_result,
                "case_state": case_state_result,
            }, ensure_ascii=False, sort_keys=True))
        return 0
    except RuntimeGuardError as error:
        print(json.dumps({"schema": SCHEMA, "passed": False, "error": {"code": error.code, "message": error.message, **error.fields}}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

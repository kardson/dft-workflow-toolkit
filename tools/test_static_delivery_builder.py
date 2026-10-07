#!/usr/bin/env python3
"""Self-contained tests for the fresh static delivery builder."""

from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

TOOL_DIR = Path(__file__).resolve().parent
PROJECT = TOOL_DIR.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import static_delivery_builder as builder
import static_delivery_check as checker
import static_runtime_guard as runtime_guard


TEST_TMP_ROOT = TOOL_DIR / ".static-single-source-builder-test-tmp"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _source_files(root: Path) -> None:
    (root / "POSCAR").write_text(
        "\n".join(
            [
                "accepted source",
                "1.0",
                "1 0 0",
                "0 1 0",
                "0 0 1",
                "A B",
                "1 1",
                "Selective Dynamics",
                "Direct",
                "0 0 0 F F F",
                "0.5 0.5 0.5 T T T",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (root / "vasprun.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<modeling>
  <calculation>
    <structure>
      <crystal>
        <varray name="basis">
          <v>1 0 0</v>
          <v>0 1 0</v>
          <v>0 0 1</v>
        </varray>
      </crystal>
      <varray name="positions">
        <v>0.1 0.2 0.3</v>
        <v>0.6 0.7 0.8</v>
      </varray>
    </structure>
  </calculation>
  <calculation>
    <structure>
      <crystal>
        <varray name="basis">
          <v>1 0 0</v>
          <v>0 1 0</v>
          <v>0 0 1</v>
        </varray>
      </crystal>
      <varray name="positions">
        <v>0.2 0.3 0.4</v>
        <v>0.7 0.8 0.9</v>
      </varray>
    </structure>
  </calculation>
</modeling>
""",
        encoding="utf-8",
    )


def _manifest(source: Path) -> dict:
    identity = {
        "schema": checker.DELIVERY_IDENTITY_SCHEMA,
        "task_id": "test_static_delivery_SYNTHETIC",
        "unit_id": "V-TEST-STATIC-SYNTHETIC",
        "source": {
            "path": "vasprun.xml",
            "sha256": _sha(source / "vasprun.xml"),
            "calculation_index_1_based": 1,
            "poscar_path": "POSCAR",
            "poscar_sha256": _sha(source / "POSCAR"),
        },
        "host": {"user": "vasp", "address": "example.invalid", "port": 22},
        "remote_batch_dir": "/srv/dft/calculations/test_static_delivery_SYNTHETIC",
        "case": "test_static_case",
        "runtime_input_dir": "/srv/dft/calculations/test_static_delivery_SYNTHETIC/test_static_case",
        "case_dir": "/srv/dft/calculations/test_static_delivery_SYNTHETIC/test_static_case",
        "tmux_session": "vasp-test-static-SYNTHETIC",
    }
    manifest = {
        "schema_version": 1,
        "route": "vasp",
        "task_id": identity["task_id"],
        "unit_id": identity["unit_id"],
        "delivery_identity": identity,
        "authorization": {
            "user_authorization_inherited": True,
            "upload": False,
            "launch": False,
            "remote_prepare": False,
            "automatic_retry": False,
            "follow_on_task": False,
        },
        "structure": {
            "species_order": ["A", "B"],
            "paw_order": ["A", "B"],
            "counts": [1, 1],
            "nions": 2,
            "nelect": 18.0,
            "cell_A": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            "fixed_global_indices": [1],
            "free_global_count": 1,
        },
        "incar": {
            "SYSTEM": "small static test",
            "PREC": "Accurate",
            "ENCUT_eV": 300,
            "EDIFF_eV": 1e-6,
            "ALGO": "Normal",
            "NELM": 60,
            "NELMIN": 2,
            "ISMEAR": 0,
            "SIGMA_eV": 0.05,
            "ISPIN": 2,
            "MAGMOM": "1*0.0 1*1.0",
            "NUPDOWN": None,
            "ISYM": 0,
            "LREAL": False,
            "LASPH": True,
            "ADDGRID": True,
            "NBANDS": 8,
            "ISTART": 0,
            "ICHARG": 2,
            "IBRION": -1,
            "POTIM": 0.2,
            "NSW": 0,
            "ISIF": 2,
            "EDIFFG_eV_per_A": None,
            "LDIPOL": True,
            "IDIPOL": 3,
            "DIPOL": [0.5, 0.5, 0.5],
            "LWAVE": False,
            "LCHARG": False,
            "external_field": False,
            "soc": False,
            "dispersion": False,
            "projection_output": False,
        },
        "kpoints": {"generation": "Gamma", "mesh": [1, 1, 1], "shift": [0, 0, 0]},
        "restart": {
            "ISTART": 0,
            "ICHARG": 2,
            "fresh": True,
            "restart_files_present": False,
            "potcar_present": False,
        },
        "parallel": {"mpi_ranks": 1, "kpar": 1, "ncore": 1, "omp_num_threads": 1},
        "environment": {
            "environment_id": "test_vasp_651",
            "vasp_version": "6.5.1",
            "vasp_bin": "/opt/vasp/bin/vasp_std",
            "mpi_launcher": "/opt/mpi/bin/mpirun",
            "paw_root": "/opt/vasp/potcars/PBE.64",
            "python_bin": "/opt/python/bin/python3",
            "toolchain_root": "/opt/vasp/toolchain",
            "pythonpath": ["/opt/vasp/toolchain"],
            "ld_library_path": ["/opt/mpi/lib", "/opt/hdf5/lib"],
            "mpi_args": ["--map-by", "core", "--bind-to", "core"],
            "tmux_bin": "/usr/bin/tmux",
            "tmux_required": True,
            "cpu_binding": {"policy": "physical-core", "required": True, "physical_cores": 1},
        },
        "paw_identity": {
            "family": "PBE.64",
            "ordered_components": ["A", "B"],
            "components": [
                {"name": "A", "relative_path": "A/POTCAR", "sha256": "a" * 64},
                {"name": "B", "relative_path": "B/POTCAR", "sha256": "b" * 64},
            ],
            "combined_sha256": "c" * 64,
            "zval": [8.0, 10.0],
            "expected_nelect": 18.0,
        },
        "runtime_dependencies": {
            "modules": [
                {"name": "vasp_executor.py", "path": "/opt/vasp/toolchain/vasp_executor.py", "sha256": "1" * 64},
                {"name": "progress_snapshot.py", "path": "/opt/vasp/toolchain/progress_snapshot.py", "sha256": "2" * 64},
                {"name": "progress_evidence.py", "path": "/opt/vasp/toolchain/progress_evidence.py", "sha256": "3" * 64},
            ]
        },
        "input_policy": {"allow_poscar_trailing_blank_lines": True},
    }
    manifest["host"] = copy.deepcopy(identity["host"])
    manifest["remote_batch_dir"] = identity["remote_batch_dir"]
    manifest["case"] = identity["case"]
    manifest["runtime_input_dir"] = identity["runtime_input_dir"]
    manifest["case_dir"] = identity["case_dir"]
    manifest["tmux_session"] = identity["tmux_session"]
    manifest["source"] = copy.deepcopy(identity["source"])
    manifest["remote_target"] = {
        "host": copy.deepcopy(identity["host"]),
        "port": identity["host"]["port"],
        "batch_dir": identity["remote_batch_dir"],
        "case_dir": identity["runtime_input_dir"],
        "tmux_session": identity["tmux_session"],
    }
    manifest["progress_evidence"] = {
        "host": copy.deepcopy(identity["host"]),
        "remote_batch_dir": identity["remote_batch_dir"],
        "case": identity["case"],
        "runtime_input_dir": identity["runtime_input_dir"],
    }
    return manifest


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


class StaticDeliveryBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP_ROOT.mkdir(exist_ok=True)
        self.root = TEST_TMP_ROOT / ("case-" + uuid.uuid4().hex)
        self.root.mkdir(mode=0o777)
        self.source = self.root / "source"
        self.source.mkdir()
        _source_files(self.source)
        self.spec_path = self.root / "approved-input-manifest.json"
        _write_json(self.spec_path, _manifest(self.source))
        self.output = self.root / "candidate"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        try:
            TEST_TMP_ROOT.rmdir()
        except OSError:
            pass

    def read_spec(self) -> dict:
        return json.loads(self.spec_path.read_text(encoding="utf-8"))

    def write_spec(self, value: dict) -> None:
        _write_json(self.spec_path, value)

    def test_correct_build_is_closed_and_directly_consumable(self) -> None:
        result = builder.build_static_delivery(self.spec_path, self.source, self.output)
        self.assertTrue(result["passed"])
        checked = checker.check_delivery_manifest(self.output)
        self.assertTrue(checked["passed"])
        manifest = json.loads((self.output / "input_manifest.json").read_text(encoding="utf-8"))
        dependencies = json.loads((self.output / "runtime_dependencies.json").read_text(encoding="utf-8"))
        self.assertTrue(runtime_guard.verify_package(self.output, manifest, dependencies)["passed"])
        self.assertEqual(manifest["delivery_identity"]["source"]["calculation_index_1_based"], 1)
        self.assertEqual(manifest["execution_plan"]["run_count"], 1)
        self.assertIn("0.1 0.2 0.3 F F F", (self.output / "POSCAR").read_text(encoding="utf-8"))
        self.assertIn("0.6 0.7 0.8 T T T", (self.output / "POSCAR").read_text(encoding="utf-8"))
        self.assertFalse((self.output / "POTCAR").exists())
        self.assertEqual(
            json.loads((self.output / "sol_review_gate.json").read_text(encoding="utf-8"))["state"],
            "PENDING_SOL_REVIEW",
        )
        identity = manifest["delivery_identity"]
        for name in ("task_id", "unit_id", "source_path", "remote_batch_dir", "case", "tmux_session"):
            literal = identity["source"].get(name) if name == "source_path" else identity.get(name)
            if literal:
                for script_name in ("run_static.sh", "remote_prepare_static.sh", "task_env.sh"):
                    self.assertNotIn(str(literal), (self.output / script_name).read_text(encoding="utf-8"))
        self.assertFalse(result["scripts_executed"])
        self.assertFalse(result["potcar_content_read"])

    def test_file_geometry_deletes_only_frozen_source_row_and_preserves_map(self) -> None:
        coordinate_text = "\n".join([
            "accepted CONTCAR source", "1.0", "2 0 0", "0 3 0", "0 0 4",
            "A B C", "1 1 1", "Direct", "0.12345678 0.1 0.2",
            "0.4 0.5 0.6", "0.77777777 0.8 0.9", "", "1 2 3", "4 5 6", "7 8 9", "",
        ])
        mask_text = "\n".join([
            "accepted source mask", "1.0", "2 0 0", "0 3 0", "0 0 4",
            "A B C", "1 1 1", "Selective Dynamics", "Direct",
            "0.0 0.0 0.0 F F F", "0.5 0.5 0.5 T T T", "0.9 0.9 0.9 T T T", "",
        ])
        coordinate = self.source / "CONTCAR"
        mask = self.source / "POSCAR"
        coordinate.write_text(coordinate_text, encoding="utf-8")
        mask.write_text(mask_text, encoding="utf-8")
        spec = self.read_spec()
        descriptor = {
            "kind": "file_geometry",
            "coordinate_file": "CONTCAR",
            "coordinate_sha256": _sha(coordinate),
            "mask_poscar_file": "POSCAR",
            "mask_poscar_sha256": _sha(mask),
            "remove_global_indices_1based": [3],
            "removed_species": ["C"],
        }
        spec["delivery_identity"]["source"] = descriptor
        spec["source"] = copy.deepcopy(descriptor)
        spec["structure"]["cell_A"] = [[2.0, 0.0, 0.0], [0.0, 3.0, 0.0], [0.0, 0.0, 4.0]]
        spec["incar"]["LDIPOL"] = False
        spec["incar"]["IDIPOL"] = None
        spec["incar"]["DIPOL"] = None
        spec["incar"]["NUPDOWN"] = 2
        spec["output_contract"] = {"expected_identity": {"NKPTS": 10}}
        spec["kpoints"]["mesh"] = [3, 6, 1]
        self.write_spec(spec)

        result = builder.build_static_delivery(self.spec_path, self.source, self.output)
        self.assertTrue(result["passed"])
        poscar = (self.output / "POSCAR").read_text(encoding="utf-8")
        self.assertIn("0.12345678 0.1 0.2 F F F", poscar)
        self.assertIn("0.4 0.5 0.6 T T T", poscar)
        self.assertNotIn("0.77777777", poscar)
        self.assertNotIn("1 2 3", poscar)
        manifest = json.loads((self.output / "input_manifest.json").read_text(encoding="utf-8"))
        execution = json.loads((self.output / "execution_manifest.json").read_text(encoding="utf-8"))
        requirements = json.loads((self.output / "output_requirements.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["geometry"]["removed_atoms"], [{"source_index_1based": 3, "species": "C"}])
        self.assertEqual(manifest["geometry"]["source_to_target_index_1based"], [1, 2, None])
        self.assertEqual(requirements["expected_identity"]["NUPDOWN"], 2)
        self.assertEqual(requirements["expected_identity"]["NKPTS"], 10)
        self.assertNotEqual(requirements["expected_identity"]["NKPTS"], 18)
        self.assertFalse(_contains_key(manifest, "calculation_index_1_based"))
        self.assertFalse(_contains_key(execution, "calculation_index_1_based"))
        self.assertTrue(runtime_guard.verify_package(
            self.output,
            manifest,
            json.loads((self.output / "runtime_dependencies.json").read_text(encoding="utf-8")),
        )["passed"])
        conflicted = copy.deepcopy(manifest)
        conflicted["geometry"]["source_identity"]["remove_global_indices_1based"] = []
        with self.assertRaises(runtime_guard.RuntimeGuardError) as raised:
            runtime_guard.verify_package(
                self.output,
                conflicted,
                json.loads((self.output / "runtime_dependencies.json").read_text(encoding="utf-8")),
            )
        self.assertEqual(raised.exception.code, "RUNTIME_SOURCE_GEOMETRY_CONFLICT")
        self.assertTrue(checker.check_delivery_manifest(self.output)["passed"])

    def test_approved_isolated_atom_build_needs_no_source_dir_or_xml_index(self) -> None:
        spec = self.read_spec()
        geometry = {
            "species_order": ["B"],
            "counts": [1],
            "cell_A": [[20.0, 0.0, 0.0], [0.0, 20.0, 0.0], [0.0, 0.0, 20.0]],
            "coordinate_mode": "direct",
            "fractional_positions": [[0.5, 0.5, 0.5]],
            "selective_dynamics_flags": [[True, True, True]],
        }
        descriptor = {
            "kind": "approved_isolated_atom_spec",
            "geometry": geometry,
            "geometry_sha256": hashlib.sha256(json.dumps(
                geometry, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")).hexdigest(),
        }
        spec["delivery_identity"]["source"] = descriptor
        spec["source"] = copy.deepcopy(descriptor)
        spec["structure"].update({
            "species_order": ["B"], "paw_order": ["B"], "counts": [1], "nions": 1,
            "nelect": 6.0, "cell_A": copy.deepcopy(geometry["cell_A"]),
            "fixed_global_indices": [], "free_global_count": 1,
        })
        spec["incar"].update({"SYSTEM": "isolated B reference", "MAGMOM": "1*2.0", "NUPDOWN": 2,
                              "NBANDS": 16, "LDIPOL": False})
        spec["incar"]["IDIPOL"] = None
        spec["incar"]["DIPOL"] = None
        spec["parallel"].update({"mpi_ranks": 4, "kpar": 1, "ncore": 1})
        spec["environment"]["cpu_binding"]["physical_cores"] = 4
        spec["paw_identity"].update({
            "ordered_components": ["B"],
            "components": [spec["paw_identity"]["components"][1]],
            "zval": [6.0],
            "expected_nelect": 6.0,
        })
        spec["output_contract"] = {"expected_identity": {"NKPTS": 1}}
        self.write_spec(spec)

        result = builder.build_static_delivery(self.spec_path, None, self.output)
        self.assertTrue(result["passed"])
        poscar = (self.output / "POSCAR").read_text(encoding="utf-8")
        self.assertIn("B", poscar)
        self.assertIn("0.5 0.5 0.5 T T T", poscar)
        manifest = json.loads((self.output / "input_manifest.json").read_text(encoding="utf-8"))
        execution = json.loads((self.output / "execution_manifest.json").read_text(encoding="utf-8"))
        requirements = json.loads((self.output / "output_requirements.json").read_text(encoding="utf-8"))
        self.assertFalse(_contains_key(manifest, "calculation_index_1_based"))
        self.assertFalse(_contains_key(execution, "calculation_index_1_based"))
        self.assertEqual(requirements["expected_identity"]["NUPDOWN"], 2)
        self.assertEqual(requirements["expected_identity"]["NKPTS"], 1)
        self.assertIn("LDIPOL = .FALSE.", (self.output / "INCAR").read_text(encoding="utf-8"))
        incar_text = (self.output / "INCAR").read_text(encoding="utf-8")
        self.assertNotIn("\nIDIPOL =", incar_text)
        self.assertNotIn("\nDIPOL =", incar_text)
        self.assertTrue(checker.check_delivery_manifest(self.output)["passed"])

    def test_public_cli_builds_candidate(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(TOOL_DIR / "static_delivery_builder.py"),
                "build",
                "--spec",
                str(self.spec_path),
                "--source-dir",
                str(self.source),
                "--output-dir",
                str(self.output),
            ],
            cwd=PROJECT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["passed"])

    def test_top_level_task_identity_conflict_is_rejected(self) -> None:
        spec = self.read_spec()
        spec["task_id"] = "different-task"
        self.write_spec(spec)
        with self.assertRaises(builder.BuilderError) as raised:
            builder.build_static_delivery(self.spec_path, self.source, self.output)
        self.assertEqual(raised.exception.code, "DELIVERY_IDENTITY_CONFLICT")

    def test_source_frame_missing_is_rejected(self) -> None:
        spec = self.read_spec()
        spec["delivery_identity"]["source"]["calculation_index_1_based"] = 3
        spec["source"]["calculation_index_1_based"] = 3
        self.write_spec(spec)
        with self.assertRaises(builder.BuilderError) as raised:
            builder.build_static_delivery(self.spec_path, self.source, self.output)
        self.assertEqual(raised.exception.code, "SOURCE_FRAME_MISSING")

    def test_canonical_frame_legacy_view_conflict_is_rejected(self) -> None:
        spec = self.read_spec()
        spec["delivery_identity"]["source"]["calculation_index_1_based"] = 2
        spec["source"]["calculation_index_1_based"] = 1
        self.write_spec(spec)
        with self.assertRaises(builder.BuilderError) as raised:
            builder.build_static_delivery(self.spec_path, self.source, self.output)
        self.assertEqual(raised.exception.code, "DELIVERY_IDENTITY_CONFLICT")

    def test_canonical_frame_change_uses_selected_frame(self) -> None:
        spec = self.read_spec()
        spec["delivery_identity"]["source"]["calculation_index_1_based"] = 2
        spec["source"]["calculation_index_1_based"] = 2
        self.write_spec(spec)
        result = builder.build_static_delivery(self.spec_path, self.source, self.output)
        self.assertTrue(result["passed"])
        poscar = (self.output / "POSCAR").read_text(encoding="utf-8")
        self.assertIn("0.2 0.3 0.4 F F F", poscar)
        self.assertIn("0.7 0.8 0.9 T T T", poscar)
        manifest = json.loads((self.output / "input_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["geometry"]["calculation_index_1_based"], 2)

    def test_closed_gate_rejects_without_side_effects(self) -> None:
        spec = self.read_spec()
        gate = builder._closed_gate(checker.load_delivery_identity(spec))
        before = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
        with self.assertRaises(runtime_guard.RuntimeGuardError) as raised:
            runtime_guard.validate_gate(spec, gate)
        after = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
        self.assertEqual(raised.exception.code, "GATE_NOT_ACCEPTED")
        self.assertEqual(after, before)

    def test_runtime_guard_main_loads_paw_for_combined_check_and_rejects_hash_error(self) -> None:
        builder.build_static_delivery(self.spec_path, self.source, self.output)
        args = [
            "--manifest",
            str(self.output / "input_manifest.json"),
            "--dependencies",
            str(self.output / "runtime_dependencies.json"),
            "--paw-identity",
            str(self.output / "paw_identity.json"),
            "--combined-path",
            str(self.root / "not-a-potcar"),
            "--check-combined",
        ]
        with mock.patch.object(runtime_guard, "verify_combined_paw_file", return_value={"passed": True}) as verify:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(runtime_guard.main(args), 0)
        verify.assert_called_once()
        with mock.patch.object(
            runtime_guard,
            "verify_combined_paw_file",
            side_effect=runtime_guard.RuntimeGuardError("PAW_COMBINED_HASH_MISMATCH", "forced hash mismatch"),
        ) as verify_error:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(runtime_guard.main(args), 2)
        verify_error.assert_called_once()

    def test_static_case_state_rejects_stopcar_and_old_outputs_without_launcher(self) -> None:
        case = self.root / "fresh-case"
        case.mkdir()
        (case / "STOPCAR").write_text("stop", encoding="utf-8")
        with self.assertRaises(runtime_guard.RuntimeGuardError) as raised:
            runtime_guard.check_static_case_state(case)
        self.assertEqual(raised.exception.code, "STATIC_STOPCAR_PRESENT")
        (case / "STOPCAR").unlink()
        (case / "OUTCAR").write_text("old output", encoding="utf-8")
        with self.assertRaises(runtime_guard.RuntimeGuardError) as raised:
            runtime_guard.check_static_case_state(case)
        self.assertEqual(raised.exception.code, "STATIC_OLD_OUTPUT_PRESENT")
        (case / "OUTCAR").unlink()
        (case / ".run_once").write_text("", encoding="utf-8")
        with self.assertRaises(runtime_guard.RuntimeGuardError) as raised:
            runtime_guard.check_static_case_state(case)
        self.assertEqual(raised.exception.code, "STATIC_RUN_ONCE_LOCK_PRESENT")

    def test_template_tamper_is_rejected_even_if_checksum_is_rewritten(self) -> None:
        builder.build_static_delivery(self.spec_path, self.source, self.output)
        runner = self.output / "run_static.sh"
        runner.write_bytes(runner.read_bytes() + b"\n# tampered after template release\n")
        checksum_path = self.output / "input_checksums.sha256"
        lines = []
        for line in checksum_path.read_text(encoding="utf-8").splitlines():
            if line.endswith("  run_static.sh"):
                line = f"{_sha(runner)}  run_static.sh"
            lines.append(line)
        checksum_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(checker.DeliveryError) as raised:
            checker.check_delivery_manifest(self.output)
        self.assertEqual(raised.exception.code, "STATIC_TEMPLATE_VERSION_MISMATCH")

    def test_static_input_tamper_is_rejected(self) -> None:
        builder.build_static_delivery(self.spec_path, self.source, self.output)
        incar = self.output / "INCAR"
        text = incar.read_text(encoding="utf-8")
        self.assertIn("IBRION = -1", text)
        incar.write_text(text.replace("IBRION = -1", "IBRION = 2", 1), encoding="utf-8")
        with self.assertRaises(checker.DeliveryError) as raised:
            checker.check_delivery_manifest(self.output)
        self.assertEqual(raised.exception.code, "REAL_INPUT_VALIDATION_FAILED")

    def test_runtime_module_path_must_bind_to_approved_toolchain_root(self) -> None:
        spec = self.read_spec()
        spec["runtime_dependencies"]["modules"][0]["path"] = "/opt/other/vasp_executor.py"
        self.write_spec(spec)
        with self.assertRaises(builder.BuilderError) as raised:
            builder.build_static_delivery(self.spec_path, self.source, self.output)
        self.assertEqual(raised.exception.code, "RUNTIME_MODULE_PATH_MISMATCH")

    def test_runtime_dependency_hash_descriptor_cannot_diverge_from_manifest(self) -> None:
        builder.build_static_delivery(self.spec_path, self.source, self.output)
        dependency_path = self.output / "runtime_dependencies.json"
        dependencies = json.loads(dependency_path.read_text(encoding="utf-8"))
        dependencies["modules"][0]["sha256"] = "f" * 64
        _write_json(dependency_path, dependencies)
        with self.assertRaises(checker.DeliveryError) as raised:
            checker.check_delivery_manifest(self.output)
        self.assertEqual(raised.exception.code, "RUNTIME_DEPENDENCY_PACKAGE_CONFLICT")

    def test_single_canonical_task_change_updates_all_derived_views(self) -> None:
        spec = self.read_spec()
        new_task_id = "test_static_delivery_derived_task_SYNTHETIC-DERIVED"
        spec["delivery_identity"]["task_id"] = new_task_id
        del spec["task_id"]
        self.write_spec(spec)
        result = builder.build_static_delivery(self.spec_path, self.source, self.output)
        self.assertTrue(result["passed"])
        manifest = json.loads((self.output / "input_manifest.json").read_text(encoding="utf-8"))
        execution = json.loads((self.output / "execution_manifest.json").read_text(encoding="utf-8"))
        gate = json.loads((self.output / "sol_review_gate.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["task_id"], new_task_id)
        self.assertEqual(manifest["delivery_identity"]["task_id"], new_task_id)
        self.assertEqual(execution["task_id"], new_task_id)
        self.assertEqual(execution["delivery_identity"]["task_id"], new_task_id)
        self.assertEqual(gate["task_id"], new_task_id)
        self.assertIn(new_task_id, (self.output / "README.md").read_text(encoding="utf-8"))

    def test_paw_combined_identity_tamper_is_rejected_without_reading_potcar(self) -> None:
        builder.build_static_delivery(self.spec_path, self.source, self.output)
        paw_path = self.output / "paw_identity.json"
        paw = json.loads(paw_path.read_text(encoding="utf-8"))
        paw["combined_sha256"] = "d" * 64
        _write_json(paw_path, paw)
        with self.assertRaises(checker.DeliveryError) as raised:
            checker.check_delivery_manifest(self.output)
        self.assertEqual(raised.exception.code, "PAW_COMBINED_IDENTITY_CONFLICT")
        self.assertFalse((self.output / "POTCAR").exists())

    def test_staging_failure_preserves_existing_empty_destination(self) -> None:
        self.output.mkdir()
        with mock.patch.object(
            builder,
            "check_delivery_manifest",
            side_effect=builder.BuilderError("FORCED_CHECK_FAILURE", "forced test failure"),
        ):
            with self.assertRaises(builder.BuilderError) as raised:
                builder.build_static_delivery(
                    self.spec_path,
                    self.source,
                    self.output,
                    allow_empty_destination=True,
                )
        self.assertEqual(raised.exception.code, "FORCED_CHECK_FAILURE")
        self.assertTrue(self.output.is_dir())
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertFalse(any(path.name.startswith(".vasp_static_builder_") for path in self.root.iterdir()))

    def test_source_xml_hash_mismatch_is_rejected(self) -> None:
        spec = self.read_spec()
        wrong = "0" * 64
        spec["delivery_identity"]["source"]["sha256"] = wrong
        spec["source"]["sha256"] = wrong
        self.write_spec(spec)
        with self.assertRaises(builder.BuilderError) as raised:
            builder.build_static_delivery(self.spec_path, self.source, self.output)
        self.assertEqual(raised.exception.code, "SOURCE_XML_HASH_MISMATCH")

    def test_source_constraint_conflict_is_rejected(self) -> None:
        spec = self.read_spec()
        spec["structure"]["fixed_global_indices"] = [2]
        self.write_spec(spec)
        with self.assertRaises(builder.BuilderError) as raised:
            builder.build_static_delivery(self.spec_path, self.source, self.output)
        self.assertEqual(raised.exception.code, "SOURCE_APPROVED_CONSTRAINT_CONFLICT")

    def test_host_compatibility_conflict_is_rejected(self) -> None:
        spec = self.read_spec()
        spec["remote_target"]["host"]["address"] = "other.invalid"
        self.write_spec(spec)
        with self.assertRaises(builder.BuilderError) as raised:
            builder.build_static_delivery(self.spec_path, self.source, self.output)
        self.assertEqual(raised.exception.code, "DELIVERY_IDENTITY_CONFLICT")

    def test_canonical_host_change_cannot_desynchronize_derived_views(self) -> None:
        spec = self.read_spec()
        spec["delivery_identity"]["host"]["address"] = "other.invalid"
        self.write_spec(spec)
        with self.assertRaises(builder.BuilderError) as raised:
            builder.build_static_delivery(self.spec_path, self.source, self.output)
        self.assertEqual(raised.exception.code, "DELIVERY_IDENTITY_CONFLICT")

    def test_missing_canonical_identity_field_is_rejected(self) -> None:
        spec = self.read_spec()
        del spec["delivery_identity"]["source"]["poscar_sha256"]
        self.write_spec(spec)
        with self.assertRaises(builder.BuilderError) as raised:
            builder.build_static_delivery(self.spec_path, self.source, self.output)
        self.assertEqual(raised.exception.code, "MISSING_FIELD")

    def test_nonempty_destination_is_preserved(self) -> None:
        self.output.mkdir()
        sentinel = self.output / "keep.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        with self.assertRaises(builder.BuilderError) as raised:
            builder.build_static_delivery(self.spec_path, self.source, self.output)
        self.assertEqual(raised.exception.code, "DESTINATION_NOT_EMPTY")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")


if __name__ == "__main__":
    unittest.main()

"""Synthetic T16-T20 tests for read-only parameter advice."""
from __future__ import annotations

import copy
import tempfile
import unittest
import uuid
from pathlib import Path

from parameter_advice import (PROJECT_ROOT, TOOLS, _resolve_input, _resolve_output,
                              review, _write_outputs)


def bundle():
    # Minimal schema-valid atom reference fixture; never sent to prepare().
    return {
        "schema": "vasp-approved-bundle/v1",
        "template": "atomref",
        "inputs": {
            "route": "vasp",
            "incar": {"NSW": 0, "ISPIN": 0},
            "delivery_identity": {"source": {"kind": "approved_isolated_atom_spec"}},
        },
    }


def change(key="inputs.incar.SIGMA", proposed=0.0, *, source_ref=None,
           version="VASP test scope", system="synthetic fixture", reason="Synthetic test only",
           changes_method=False, changes_restart=False, changes_environment=False):
    return {
        "key": key,
        "proposed_value": proposed,
        "source_ref": source_ref,
        "reason": reason,
        "VASP_version_scope": version,
        "system_scope": system,
        "changes_method": changes_method,
        "changes_restart": changes_restart,
        "changes_environment": changes_environment,
    }


def proposal(changes=None, comparison_contract=None):
    data = {
        "schema": "vasp-parameter-proposal/v1",
        "proposal_id": "synthetic-proposal",
        "changes": changes if changes is not None else [change()],
    }
    if comparison_contract is not None:
        data["comparison_contract"] = comparison_contract
    return data


def comparison(**overrides):
    data = {
        "requested": True,
        "interpretation": "FIXED_GEOMETRY_INTERACTION",
        "energy_basis": "E0",
        "source_energy_bases": ["E0", "E0", "E0"],
        "mp_correction_states": ["NOT_APPLIED", "NOT_APPLIED", "NOT_APPLIED"],
        "approved_differences": ["atom_box", "kpoints", "spin"],
        "observed_differences": ["atom_box", "kpoints", "spin"],
        "threshold": 0.02,
        "threshold_source_ref": "evidence.json#/threshold",
    }
    data.update(overrides)
    return data


class ParameterAdviceTests(unittest.TestCase):
    def test_t16_same_value_holds_omitted_differs_from_zero_and_unknown_key_rejects(self):
        data = proposal([
            change("inputs.incar.ISPIN", 0),
            change("inputs.incar.SIGMA", 0.0),
        ])
        result = review(bundle(), data, bundle_ref="approved.json", proposal_ref="proposal.json",
                        source_root=Path(tempfile.gettempdir()))
        same, omitted = result["changes"]
        self.assertEqual((same["old_value_state"], same["old_value"], same["change_state"]),
                         ("PRESENT", 0, "NO_CHANGE"))
        self.assertEqual((omitted["old_value_state"], omitted["old_value"], omitted["proposed_value"]),
                         ("OMITTED", "OMITTED", 0.0))
        self.assertFalse(result["execution_authorized"])

        bad = proposal([change("inputs.incar.UNRECOGNIZED", 0)])
        with self.assertRaisesRegex(ValueError, "unknown proposal key"):
            review(bundle(), bad, bundle_ref="approved.json", proposal_ref="proposal.json")

    def test_t17_missing_source_version_or_system_blocks_advice(self):
        item = change(source_ref="missing-evidence.json#/claim", version=None, system=None)
        result = review(bundle(), proposal([item]), bundle_ref="approved.json", proposal_ref="proposal.json",
                        source_root=Path(tempfile.gettempdir()))
        self.assertEqual(result["status"], "INSUFFICIENT_ADVICE_EVIDENCE")
        blockers = set(result["changes"][0]["blockers"])
        self.assertTrue({"SOURCE_FILE_MISSING", "VASP_VERSION_SCOPE_MISSING", "SYSTEM_SCOPE_MISSING"}.issubset(blockers))
        self.assertFalse(result["execution_authorized"])
        self.assertFalse(result["input_written"])

    def test_t18_restart_advice_is_only_a_declared_diff_and_bundle_is_unchanged(self):
        approved = bundle()
        original = copy.deepcopy(approved)
        item = change("inputs.restart.fresh", True, source_ref=None, changes_restart=True)
        result = review(approved, proposal([item]), bundle_ref="approved.json", proposal_ref="proposal.json")
        self.assertEqual(approved, original)
        self.assertEqual(result["changes"][0]["old_value_state"], "OMITTED")
        self.assertTrue(result["changes"][0]["changes_restart"])
        self.assertTrue(result["changes"][0]["requires_scientific_decision"])
        self.assertFalse(result["restart_changed"])
        self.assertFalse(result["input_written"])
        self.assertEqual(result["scientific_acceptance"], "NOT_EVALUATED")

    def test_t19_energy_basis_mp_correction_and_reference_difference_contract_stay_separate(self):
        bad_contract = comparison(
            source_energy_bases=["E0", "F", "E0"],
            mp_correction_states=["UNKNOWN", "APPLIED", "NOT_APPLIED"],
            approved_differences=["kpoints"],
            observed_differences=["atom_box"],
        )
        result = review(bundle(), proposal([change()], bad_contract),
                        bundle_ref="approved.json", proposal_ref="proposal.json",
                        source_root=Path(tempfile.gettempdir()))
        blockers = {item["blocker"] for item in result["blockers"]}
        self.assertIn("ENERGY_BASIS_MISMATCH_E0_F_TOTEN_SEPARATED", blockers)
        self.assertIn("MP_CORRECTION_STATUS_UNKNOWN", blockers)
        self.assertIn("MP_CORRECTION_MISMATCH", blockers)
        self.assertIn("FIXED_GEOMETRY_APPROVED_DIFFERENCES_INCOMPLETE", blockers)
        self.assertIn("UNAPPROVED_REFERENCE_DIFFERENCE:atom_box", blockers)
        self.assertEqual(result["comparison_contract"]["energy_basis"], "E0")
        self.assertEqual(result["comparison_contract"]["source_energy_bases"], ["E0", "F", "E0"])
        self.assertEqual(result["comparison_contract"]["approved_differences"], ["kpoints"])
        self.assertEqual(result["comparison_contract"]["scientific_conclusion"], "NOT_EVALUATED")

        broadened = comparison(approved_differences=["atom_box", "kpoints", "spin", "paw"])
        result = review(bundle(), proposal([change()], broadened),
                        bundle_ref="approved.json", proposal_ref="proposal.json",
                        source_root=Path(tempfile.gettempdir()))
        self.assertIn("UNSUPPORTED_APPROVED_REFERENCE_DIFFERENCE:paw",
                      {item["blocker"] for item in result["blockers"]})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "evidence.json").write_text("{}", encoding="utf-8")
            clean = review(bundle(), proposal([change(source_ref="evidence.json#/source")], comparison()),
                           bundle_ref="approved.json", proposal_ref="proposal.json", source_root=root)
        self.assertEqual(clean["comparison_contract"]["comparison_state"], "NOT_EVALUATED")
        self.assertEqual(clean["comparison_contract"]["threshold_state"], "DECLARED_NOT_EVALUATED")
        self.assertEqual(clean["comparison_contract"]["approved_differences"], ["atom_box", "kpoints", "spin"])
        self.assertFalse(clean["execution_authorized"])

    def test_malformed_json_key_and_comparison_types_are_rejected_without_traceback(self):
        malformed_key = change()
        malformed_key["key"] = {"path": "inputs.incar.SIGMA"}
        with self.assertRaisesRegex(ValueError, "unknown proposal key"):
            review(bundle(), proposal([malformed_key]), bundle_ref="approved.json", proposal_ref="proposal.json")

        malformed = comparison(energy_basis={"basis": "E0"}, source_energy_bases=[{}],
                               mp_correction_states=[["APPLIED"]])
        result = review(bundle(), proposal([change()], malformed),
                        bundle_ref="approved.json", proposal_ref="proposal.json")
        codes = {item["blocker"] for item in result["blockers"]}
        self.assertIn("ENERGY_BASIS_MISSING_OR_UNSUPPORTED", codes)
        self.assertIn("SOURCE_ENERGY_BASIS_MISSING_OR_UNSUPPORTED", codes)
        self.assertIn("MP_CORRECTION_STATE_MISSING_OR_INVALID", codes)

    def test_t20_missing_scientific_threshold_only_reports_evidence_gap(self):
        missing = comparison(threshold=None, threshold_source_ref=None)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "evidence.json").write_text("{}", encoding="utf-8")
            result = review(bundle(), proposal([change(source_ref="evidence.json#/claim")], missing),
                            bundle_ref="approved.json", proposal_ref="proposal.json", source_root=root)
        self.assertEqual({item["blocker"] for item in result["blockers"]}, {"SCIENTIFIC_THRESHOLD_MISSING"})
        self.assertEqual(result["comparison_contract"]["comparison_state"], "NOT_EVALUATED")
        self.assertEqual(result["comparison_contract"]["scientific_conclusion"], "NOT_EVALUATED")
        self.assertEqual(result["comparison_contract"]["threshold_state"], "NOT_PROVIDED")
        self.assertEqual(result["scientific_acceptance"], "NOT_EVALUATED")

    def test_source_table_projects_only_declared_columns_and_creates_review_artifacts(self):
        import csv
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "advice"
            result = review(bundle(), proposal([change()]), bundle_ref="approved.json", proposal_ref="proposal.json")
            _write_outputs(result, output)
            with (output / "source_table.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["key"], "inputs.incar.SIGMA")
            self.assertTrue((output / "parameter_advice.json").is_file())
            self.assertTrue((output / "blockers.json").is_file())
            names = {path.name.casefold() for path in output.iterdir()}
            self.assertFalse({"incar", "poscar", "potcar", "contcar", "wavecar", "chgcar"} & names)

    def test_input_and_output_symlink_paths_cannot_escape_workspace(self):
        with tempfile.TemporaryDirectory(dir=TOOLS) as temporary:
            link = Path(temporary) / "synthetic-external-link"
            target = PROJECT_ROOT.parent / f"synthetic-parameter-advice-target-{uuid.uuid4().hex}"
            try:
                link.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"directory symlink creation unsupported on this host: {error}")
            with self.assertRaisesRegex(ValueError, "workspace-local JSON"):
                _resolve_input(link / "proposal.json", "proposal")
            with self.assertRaisesRegex(ValueError, "inside the workspace"):
                _resolve_output(link / "new-advice")


if __name__ == "__main__":
    unittest.main()

"""T21-T24 checks for explicit, provenance-preserving workflow metrics."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from workflow_metrics import MANIFEST_SCHEMA, METRICS_SCHEMA, build_report, contract_coverage
import workflow_metrics


def write_json(path: Path, value) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def metrics_receipt(identity, role, elapsed, active_human=None):
    return {
        "schema": METRICS_SCHEMA,
        "identity": identity,
        "comparison_role": role,
        "metrics": {
            "preparation_command_elapsed_seconds": {
                "value": elapsed, "unit": "s", "evidence_ref": "/metrics/preparation_command_elapsed_seconds/value",
            },
            "active_human_seconds": {
                "value": active_human, "unit": "s",
                "evidence_ref": "/metrics/active_human_seconds/value" if active_human is not None else None,
                "missing_reason": "not recorded" if active_human is None else None,
            },
        },
    }


class WorkflowMetricsTests(unittest.TestCase):
    def test_t21_missing_human_time_and_unmarked_zero_repairs_remain_null(self):
        with tempfile.TemporaryDirectory(dir=workflow_metrics.TOOLS) as temporary:
            root = Path(temporary)
            write_json(root / "prep.json", {
                "schema": "vasp-practical-preparation/v1", "passed": True,
                "elapsed_seconds": 12.5, "active_human_seconds": None,
                "manual_repairs": 0, "execution": "NOT_SUBMITTED",
            })
            write_json(root / "observations.json", {
                "schema": MANIFEST_SCHEMA,
                "declared_inventory_count": 1,
                "observations": [{"observation_id": "prep-1", "kind": "practical_preparation",
                                  "source_ref": "prep.json"}],
                "result_records": [], "metrics_receipts": [],
            })
            report = build_report(root / "observations.json", workspace_root=root)

        metrics = {item["name"]: item for item in report["metrics"]}
        self.assertEqual(metrics["preparation_command_elapsed_seconds"]["values"][0]["value"], 12.5)
        self.assertEqual(metrics["active_human_seconds"]["measured_count"], 0)
        self.assertEqual(metrics["active_human_seconds"]["missingness"]["by_reason"], {"explicit_null": 1})
        self.assertEqual(metrics["approved_to_delivery_wall_seconds"]["measured_count"], 0)
        self.assertEqual(metrics["mechanical_repairs_events"]["measured_count"], 0)
        self.assertEqual(metrics["mechanical_repairs_events"]["missingness"]["by_reason"],
                         {"unmarked_legacy_zero_not_a_measurement": 1})
        self.assertEqual(metrics["first_pass"]["values"], [])

    def test_t22_groups_require_full_identity_and_failed_attempts_keep_receipt_refs(self):
        identity = {
            "template": "fixed-cell-relax", "approved_input_identity": "approved-input-A",
            "environment": "environment-A", "generation_scope": "complete-package",
            "analysis_scope": "preparation", "metric_definition": "preparation-command-seconds/v1",
        }
        with tempfile.TemporaryDirectory(dir=workflow_metrics.TOOLS) as temporary:
            root = Path(temporary)
            write_json(root / "baseline.json", metrics_receipt(identity, "baseline", 100.0, None))
            write_json(root / "candidate.json", metrics_receipt(identity, "candidate", 70.0, None))
            other_environment = dict(identity, environment="environment-B")
            write_json(root / "other-env.json", metrics_receipt(other_environment, "candidate", 20.0, None))
            write_json(root / "failed-attempt.json", {
                "schema": "vasp-result-record/v1", "execution_id": "exec-1", "case_id": "case-1",
                "attempt": 2, "run_state": "FAILED_OR_INCOMPLETE", "result_state": "PARTIAL",
            })
            entries = [
                {"observation_id": "baseline", "kind": "metrics_observation", "source_ref": "baseline.json"},
                {"observation_id": "candidate", "kind": "metrics_observation", "source_ref": "candidate.json"},
                {"observation_id": "other-env", "kind": "metrics_observation", "source_ref": "other-env.json"},
            ]
            write_json(root / "observations.json", {
                "schema": MANIFEST_SCHEMA, "observations": entries,
                "result_records": [{"observation_id": "failed-attempt-2", "source_ref": "failed-attempt.json"}],
                "metrics_receipts": [],
            })
            report = build_report(root / "observations.json", workspace_root=root)

        comparison = report["comparability"]
        comparable = [item for item in comparison["groups"] if item["improvement_rate"] is not None]
        self.assertEqual(len(comparable), 1)
        self.assertAlmostEqual(comparable[0]["improvement_rate"], 0.30)
        self.assertEqual(comparable[0]["identity"]["environment"], "environment-A")
        failed = next(item for item in report["metrics"] if item["name"] == "failed_attempts")
        self.assertEqual(failed["measured_count"], 1)
        self.assertEqual(failed["values"][0]["value"], 1)
        self.assertTrue(failed["values"][0]["value_source_ref"].endswith("failed-attempt.json#/run_state"))
        human = next(item for item in report["metrics"] if item["name"] == "active_human_seconds")
        self.assertEqual(human["measured_count"], 0)
        self.assertEqual(comparison["eligible_group_count"], 2)
        env_b_group = next(item for item in comparison["groups"] if item["identity"]["environment"] == "environment-B")
        self.assertIsNone(env_b_group["improvement_rate"])
        audit = contract_coverage()
        self.assertTrue(any("closed gate" in row["area"] for row in audit))
        self.assertTrue(all(row["not_observed"] for row in audit))

    def test_t23_empty_and_incomparable_observations_never_emit_improvement_or_energy_comparison(self):
        with tempfile.TemporaryDirectory(dir=workflow_metrics.TOOLS) as temporary:
            root = Path(temporary)
            write_json(root / "empty.json", {"schema": MANIFEST_SCHEMA, "observations": [],
                                               "result_records": [], "metrics_receipts": []})
            empty = build_report(root / "empty.json", workspace_root=root)
            identity_a = {
                "template": "static", "approved_input_identity": "input-A", "environment": "env-A",
                "generation_scope": "static-package", "analysis_scope": "preparation",
                "metric_definition": "preparation-command-seconds/v1",
            }
            identity_b = dict(identity_a, environment="env-B")
            write_json(root / "a.json", metrics_receipt(identity_a, "baseline", 10.0))
            write_json(root / "b.json", metrics_receipt(identity_b, "candidate", 5.0))
            write_json(root / "incomparable.json", {"schema": MANIFEST_SCHEMA, "observations": [
                {"observation_id": "a", "kind": "metrics_observation", "source_ref": "a.json"},
                {"observation_id": "b", "kind": "metrics_observation", "source_ref": "b.json"},
            ], "result_records": [], "metrics_receipts": []})
            incomparable = build_report(root / "incomparable.json", workspace_root=root)

        self.assertEqual(empty["status"], "EVIDENCE_INSUFFICIENT")
        self.assertIsNone(empty["performance_claim"]["improvement_rate"])
        self.assertEqual(empty["comparability"]["groups"], [])
        self.assertNotIn("energy", json.dumps(empty).casefold())
        self.assertEqual(incomparable["comparability"]["status"], "EVIDENCE_INSUFFICIENT")
        self.assertIsNone(incomparable["performance_claim"]["improvement_rate"])
        groups = incomparable["comparability"]["groups"]
        self.assertEqual(len(groups), 2)
        self.assertEqual({group["identity"]["environment"] for group in groups}, {"env-A", "env-B"})
        self.assertTrue(all(group["improvement_rate"] is None for group in groups))

    def test_b5_metric_definition_missing_stays_missing_and_blocks_comparison(self):
        identity = {
            "template": "fixed-cell-relax", "approved_input_identity": "approved-input-A",
            "environment": "environment-A", "generation_scope": "complete-package",
            "analysis_scope": "preparation",
        }
        with tempfile.TemporaryDirectory(dir=workflow_metrics.TOOLS) as temporary:
            root = Path(temporary)
            (root / "sources").mkdir()
            (root / "manifests").mkdir()
            write_json(root / "sources/baseline.json", metrics_receipt(identity, "baseline", 100.0))
            write_json(root / "sources/candidate.json", metrics_receipt(identity, "candidate", 70.0))
            write_json(root / "manifests/observations.json", {
                "schema": MANIFEST_SCHEMA,
                "observations": [
                    {"observation_id": "baseline", "kind": "metrics_observation", "source_ref": "sources/baseline.json"},
                    {"observation_id": "candidate", "kind": "metrics_observation", "source_ref": "sources/candidate.json"},
                ],
            })
            report = build_report(root / "manifests/observations.json", workspace_root=root)

        source_rows = report["inventory"]["observation_sources"]
        for row in source_rows:
            metric_definition = row["identity"]["metric_definition"]
            self.assertIsNone(metric_definition["value"])
            self.assertIsNone(metric_definition["source_ref"])
        comparison = report["comparability"]
        self.assertEqual(comparison["status"], "EVIDENCE_INSUFFICIENT")
        self.assertEqual(comparison["eligible_group_count"], 0)
        self.assertTrue(all("metric_definition" in item["missing_identity_fields"]
                            for item in comparison["excluded_observations"]))

    def test_b5_manifest_metric_definition_uses_its_actual_manifest_pointer(self):
        identity = {
            "template": "fixed-cell-relax", "approved_input_identity": "approved-input-A",
            "environment": "environment-A", "generation_scope": "complete-package",
            "analysis_scope": "preparation",
        }
        with tempfile.TemporaryDirectory(dir=workflow_metrics.TOOLS) as temporary:
            root = Path(temporary)
            (root / "sources").mkdir()
            (root / "manifests").mkdir()
            write_json(root / "sources/baseline.json", metrics_receipt(identity, "baseline", 100.0))
            write_json(root / "sources/candidate.json", metrics_receipt(identity, "candidate", 70.0))
            manifest_path = root / "manifests/observations.json"
            write_json(manifest_path, {
                "schema": MANIFEST_SCHEMA,
                "observations": [
                    {"observation_id": "baseline", "kind": "metrics_observation", "source_ref": "sources/baseline.json",
                     "metric_definition": "preparation-command-seconds/v1"},
                    {"observation_id": "candidate", "kind": "metrics_observation", "source_ref": "sources/candidate.json",
                     "metric_definition": "preparation-command-seconds/v1"},
                ],
            })
            report = build_report(manifest_path, workspace_root=root)

        baseline = report["inventory"]["observation_sources"][0]["identity"]["metric_definition"]
        self.assertEqual(baseline["value"], "preparation-command-seconds/v1")
        self.assertEqual(baseline["source_ref"], "manifests/observations.json#/observations/0/metric_definition")

    def test_b5_first_pass_decrease_is_a_regression_not_an_improvement(self):
        identity = {
            "template": "fixed-cell-relax", "approved_input_identity": "approved-input-A",
            "environment": "environment-A", "generation_scope": "complete-package",
            "analysis_scope": "attempt-outcome", "metric_definition": "first-pass-outcome/v1",
        }
        with tempfile.TemporaryDirectory(dir=workflow_metrics.TOOLS) as temporary:
            root = Path(temporary)
            (root / "sources").mkdir()
            (root / "manifests").mkdir()
            for name, role, value in (("baseline", "baseline", True), ("candidate", "candidate", False)):
                receipt = {"schema": METRICS_SCHEMA, "identity": identity, "comparison_role": role,
                           "metrics": {"first_pass": {"value": value, "unit": "boolean",
                                                       "evidence_ref": "/metrics/first_pass/value"}}}
                write_json(root / f"sources/{name}.json", receipt)
            manifest_path = root / "manifests/observations.json"
            write_json(manifest_path, {"schema": MANIFEST_SCHEMA, "observations": [
                {"observation_id": name, "kind": "metrics_observation", "source_ref": f"sources/{name}.json"}
                for name in ("baseline", "candidate")
            ]})
            report = build_report(manifest_path, workspace_root=root)

        group = next(item for item in report["comparability"]["groups"] if item["metric"] == "first_pass")
        self.assertEqual(group["improvement_direction"], "higher_is_better")
        self.assertAlmostEqual(group["improvement_rate"], -1.0)
        self.assertEqual(group["status"], "EXPLORATORY_COMPARISON_ONLY")

    def test_b5_first_pass_zero_baseline_keeps_comparability_but_has_no_relative_rate(self):
        identity = {
            "template": "fixed-cell-relax", "approved_input_identity": "approved-input-A",
            "environment": "environment-A", "generation_scope": "complete-package",
            "analysis_scope": "attempt-outcome", "metric_definition": "first-pass-outcome/v1",
        }
        with tempfile.TemporaryDirectory(dir=workflow_metrics.TOOLS) as temporary:
            root = Path(temporary)
            (root / "sources").mkdir()
            (root / "manifests").mkdir()
            for name, role, value in (("baseline", "baseline", False), ("candidate", "candidate", True)):
                receipt = {"schema": METRICS_SCHEMA, "identity": identity, "comparison_role": role,
                           "metrics": {"first_pass": {"value": value, "unit": "boolean",
                                                       "evidence_ref": "/metrics/first_pass/value"}}}
                write_json(root / f"sources/{name}.json", receipt)
            manifest_path = root / "manifests/observations.json"
            write_json(manifest_path, {"schema": MANIFEST_SCHEMA, "observations": [
                {"observation_id": name, "kind": "metrics_observation", "source_ref": f"sources/{name}.json"}
                for name in ("baseline", "candidate")
            ]})
            report = build_report(manifest_path, workspace_root=root)

        group = next(item for item in report["comparability"]["groups"] if item["metric"] == "first_pass")
        self.assertIsNone(group["improvement_rate"])
        self.assertEqual(group["status"], "BASELINE_ZERO_RATE_UNDEFINED")
        self.assertEqual(report["comparability"]["status"], "COMPARABLE_OBSERVATIONS_AVAILABLE")
        self.assertEqual(report["comparability"]["comparable_group_count"], 1)
        self.assertEqual(report["comparability"]["directional_rate_group_count"], 0)

    def test_b5_stopped_attempt_is_separate_and_missing_reason_is_not_failure(self):
        with tempfile.TemporaryDirectory(dir=workflow_metrics.TOOLS) as temporary:
            root = Path(temporary)
            (root / "sources").mkdir()
            (root / "manifests").mkdir()
            write_json(root / "sources/stopped.json", {
                "schema": "vasp-result-record/v1", "execution_id": "exec-stop", "case_id": "case-1",
                "attempt": 1, "run_state": "STOPPED",
            })
            manifest_path = root / "manifests/observations.json"
            write_json(manifest_path, {"schema": MANIFEST_SCHEMA, "observations": [],
                "result_records": [{"observation_id": "stopped-1", "source_ref": "sources/stopped.json"}],
                "metrics_receipts": []})
            report = build_report(manifest_path, workspace_root=root)

        metrics = {item["name"]: item for item in report["metrics"]}
        failed = metrics["failed_attempts"]
        self.assertEqual(failed["measured_count"], 0)
        self.assertEqual(failed["missingness"]["by_reason"], {"stopped_attempt_recorded_separately": 1})
        stopped = metrics["stopped_attempts"]
        self.assertEqual(stopped["values"][0]["value"], 1)
        self.assertIsNone(stopped["values"][0]["stop_reason"])
        self.assertEqual(stopped["values"][0]["stop_reason_status"], "NOT_RECORDED")

    def test_b5_output_refuses_source_and_calculation_directories(self):
        with tempfile.TemporaryDirectory(dir=workflow_metrics.TOOLS) as temporary:
            root = Path(temporary)
            (root / "sources").mkdir()
            (root / "manifests").mkdir()
            write_json(root / "sources/prep.json", {
                "schema": "vasp-practical-preparation/v1", "passed": True, "elapsed_seconds": 1.0,
            })
            manifest_path = root / "manifests/observations.json"
            write_json(manifest_path, {"schema": MANIFEST_SCHEMA, "observations": [
                {"observation_id": "prep-1", "kind": "practical_preparation", "source_ref": "sources/prep.json"},
            ]})
            report = build_report(manifest_path, workspace_root=root)
            source_output = root / "sources/metrics-output"
            calculation_output = root / "private_runs/metrics-output"
            with self.assertRaisesRegex(ValueError, "calculation or explicit source"):
                workflow_metrics._write_new_output(report, source_output, workspace_root=root)
            with self.assertRaisesRegex(ValueError, "calculation or explicit source"):
                workflow_metrics._write_new_output(report, calculation_output, workspace_root=root)
            self.assertFalse(source_output.exists())
            self.assertFalse(calculation_output.exists())
            valid_output = root / "candidates/metrics-output"
            workflow_metrics._write_new_output(report, valid_output, workspace_root=root)
            self.assertTrue((valid_output / "metrics_report.json").is_file())

    def test_b5_output_symlink_alias_to_source_is_refused(self):
        with tempfile.TemporaryDirectory(dir=workflow_metrics.TOOLS) as temporary:
            root = Path(temporary)
            source_dir = root / "sources"
            source_dir.mkdir()
            (root / "manifests").mkdir()
            write_json(source_dir / "prep.json", {
                "schema": "vasp-practical-preparation/v1", "passed": True, "elapsed_seconds": 1.0,
            })
            manifest_path = root / "manifests/observations.json"
            write_json(manifest_path, {"schema": MANIFEST_SCHEMA, "observations": [
                {"observation_id": "prep-1", "kind": "practical_preparation", "source_ref": "sources/prep.json"},
            ]})
            report = build_report(manifest_path, workspace_root=root)
            alias = root / "source-alias"
            try:
                alias.symlink_to(source_dir, target_is_directory=True)
            except OSError as error:
                if getattr(error, "winerror", None) == 1314 or getattr(error, "errno", None) in {1, 13}:
                    self.skipTest(f"directory symlink creation unavailable: {error}")
                raise
            linked_output = alias / "metrics-output"
            with self.assertRaisesRegex(ValueError, "calculation or explicit source"):
                workflow_metrics._write_new_output(report, linked_output, workspace_root=root)
            self.assertFalse((source_dir / "metrics-output").exists())


if __name__ == "__main__":
    unittest.main()

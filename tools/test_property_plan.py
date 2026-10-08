"""Synthetic T11-T15 tests for the planning-only property DAG."""
from __future__ import annotations

import copy
import tempfile
import unittest
import uuid
from pathlib import Path

from property_plan import PROJECT_ROOT, TOOLS, _resolve_output, _resolve_request, build_plan


CONTRACT_NAMES = ("geometry", "environment", "paw", "restart")
BUNDLE_REF = "tools/candidates/synthetic/approved_bundle.json"


def stage(stage_id="stage-a", *, case_id="case-a", property_name="RELAXED_GEOMETRY",
          template="relax_fresh", parents_marker=True):
    identities = {name: f"{name}-identity-1" for name in CONTRACT_NAMES}
    parents = [{
        "parent_id": "parent-a",
        "evidence_ref": "tools/candidates/synthetic/result.json#/record",
        "execution_id": "exec-a", "case_id": "case-parent", "attempt": 1,
        "run_state": "COMPLETED", "result_state": "COMPLETE_XML",
        "geometry_relaxation_status": "CONVERGED",
        "contract_identities": dict(identities),
    }] if parents_marker else []
    return {
        "stage_id": stage_id,
        "case_id": case_id,
        "property": property_name,
        "template": template,
        "bundle_ref": BUNDLE_REF,
        "depends_on": [],
        "parents": parents,
        "required_artifacts": ["CONTCAR"],
        "contract_refs": {name: f"{BUNDLE_REF}#/inputs/{name}" for name in CONTRACT_NAMES},
        "contract_identities": identities,
        "required_acceptance_scope": "CONVERGED_GEOMETRY",
        "produce": ["CONTCAR"],
    }


def request(stages=None, budget=4):
    return {
        "schema": "vasp-property-request/v1",
        "request_id": "synthetic-property-plan",
        "purpose": "T11-T15 contract tests only",
        "case_budget": budget,
        "stop_condition": "Stop after writing this local plan; no calculation.",
        "stages": stages if stages is not None else [stage()],
    }


class PropertyPlanTests(unittest.TestCase):
    def test_t11_rejects_duplicate_invalid_over_budget_unknown_and_cyclic_nodes(self):
        first = stage("same")
        duplicate_id = [first, copy.deepcopy(first)]
        with self.assertRaisesRegex(ValueError, "duplicate stage_id"):
            build_plan(request(duplicate_id))

        duplicate_node_a = stage("node-a")
        duplicate_node_b = copy.deepcopy(duplicate_node_a)
        duplicate_node_b["stage_id"] = "node-b"
        with self.assertRaisesRegex(ValueError, "duplicate property-plan node"):
            build_plan(request([duplicate_node_a, duplicate_node_b]))

        invalid = stage("invalid")
        invalid["template"] = "relax_latest"
        with self.assertRaisesRegex(ValueError, "template"):
            build_plan(request([invalid]))

        over_budget = [stage("a", case_id="case-a"), stage("b", case_id="case-b")]
        with self.assertRaisesRegex(ValueError, "exceeds case_budget"):
            build_plan(request(over_budget, budget=1))

        cycle_a = stage("cycle-a")
        cycle_b = stage("cycle-b")
        cycle_a["depends_on"] = ["cycle-b"]
        cycle_b["depends_on"] = ["cycle-a"]
        with self.assertRaisesRegex(ValueError, "contains a cycle"):
            build_plan(request([cycle_a, cycle_b]))

        unknown = stage("unknown")
        unknown["depends_on"] = ["not-listed"]
        with self.assertRaisesRegex(ValueError, "unknown dependencies"):
            build_plan(request([unknown]))

    def test_t12_missing_parent_identity_and_contract_conflicts_block_readiness(self):
        no_parent = stage("no-parent", parents_marker=False)
        plan = build_plan(request([no_parent]))
        node = plan["nodes"][0]
        self.assertIn("PARENT_EVIDENCE_MISSING", node["blockers"])
        self.assertFalse(node["execution_ready"])

        missing_file = stage("missing-file")
        plan = build_plan(request([missing_file]))
        node = plan["nodes"][0]
        self.assertIn("BUNDLE_FILE_MISSING", node["blockers"])
        self.assertIn("PARENT_EVIDENCE_FILE_MISSING:parent-a", node["blockers"])
        self.assertEqual(node["parents"][0]["evidence_state"], "FILE_MISSING")
        self.assertEqual(node["template"], "relax_fresh")

        unvalidated = stage("unvalidated-source", template="UNVALIDATED")
        unvalidated["bundle_ref"] = None
        plan = build_plan(request([unvalidated]))
        self.assertEqual(plan["nodes"][0]["template"], "UNVALIDATED")
        self.assertIn("TEMPLATE_UNVALIDATED", plan["nodes"][0]["blockers"])

        incomplete = stage("incomplete")
        incomplete["parents"][0]["execution_id"] = None
        incomplete["parents"][0]["attempt"] = None
        incomplete["parents"][0]["evidence_ref"] = None
        plan = build_plan(request([incomplete]))
        blockers = plan["nodes"][0]["blockers"]
        self.assertIn("PARENT_EVIDENCE_REF_MISSING:parent-a", blockers)
        self.assertIn("PARENT_EXECUTION_CASE_ATTEMPT_INCOMPLETE:parent-a", blockers)

        for contract in ("environment", "paw", "restart"):
            conflicting = stage(f"conflict-{contract}")
            conflicting["parents"][0]["contract_identities"][contract] = "different-approved-contract"
            plan = build_plan(request([conflicting]))
            self.assertIn(f"{contract.upper()}_CONTRACT_CONFLICT:parent-a", plan["nodes"][0]["blockers"])

        missing_contract = stage("missing-contract")
        missing_contract["contract_refs"]["geometry"] = None
        plan = build_plan(request([missing_contract]))
        self.assertIn("GEOMETRY_CONTRACT_REF_MISSING", plan["nodes"][0]["blockers"])

    def test_t13_four_templates_remain_plan_only_and_band_pdos_stay_data_gated(self):
        cases = [
            stage("static", case_id="case-static", property_name="STATIC_ENERGY", template="static"),
            stage("atom", case_id="case-atom", property_name="ATOM_REFERENCE", template="atomref"),
            stage("fresh", case_id="case-fresh", property_name="RELAXED_GEOMETRY", template="relax_fresh"),
            stage("warm", case_id="case-warm", property_name="RELAXED_GEOMETRY", template="relax_warm"),
        ]
        plan = build_plan(request(cases, budget=4))
        self.assertEqual([item["support_state"] for item in plan["nodes"]], ["SUPPORTED_FOR_PLANNING_ONLY"] * 4)
        self.assertTrue(all(item["execution_ready"] is False for item in plan["nodes"]))
        self.assertFalse(plan["gate_opened"])
        self.assertFalse(plan["execution_authorized"])

        for property_name in ("BAND", "PDOS"):
            gated = stage(f"{property_name.lower()}-gated", property_name=property_name, template="UNVALIDATED")
            gated["bundle_ref"] = None
            gated["contract_refs"] = {name: None for name in CONTRACT_NAMES}
            gated["contract_identities"] = {name: None for name in CONTRACT_NAMES}
            result = build_plan(request([gated]))["nodes"][0]
            self.assertEqual(result["support_state"], "DATA_GATED")
            self.assertFalse(result["execution_ready"])
            self.assertIn("PROPERTY_NOT_VALIDATED_FOR_EXECUTION", result["blockers"])

    def test_t14_finite_step_completion_and_partial_parent_do_not_satisfy_converged_geometry(self):
        finite = stage("finite-step")
        finite["parents"][0]["geometry_relaxation_status"] = "FINITE_STEP_COMPLETED"
        plan = build_plan(request([finite]))
        self.assertIn("PARENT_GEOMETRY_NOT_CONVERGED:parent-a", plan["nodes"][0]["blockers"])

        partial = stage("partial")
        partial["parents"][0]["result_state"] = "PARTIAL"
        partial["parents"][0]["geometry_relaxation_status"] = "FINITE_STEP_COMPLETED"
        plan = build_plan(request([partial]))
        blockers = plan["nodes"][0]["blockers"]
        self.assertIn("PARENT_RESULT_NOT_COMPLETE:parent-a", blockers)
        self.assertIn("PARENT_GEOMETRY_NOT_CONVERGED:parent-a", blockers)

    def test_t15_plan_does_not_mutate_input_or_emit_matrix_execution_contracts(self):
        source = request([stage()])
        original = copy.deepcopy(source)
        plan = build_plan(source)
        self.assertEqual(source, original)
        self.assertFalse(plan["execution_authorized"])
        self.assertFalse(plan["gate_opened"])
        self.assertFalse(plan["generated_vasp_inputs"])
        self.assertFalse(plan["generated_scripts"])
        self.assertNotIn("execution", plan["nodes"][0])
        self.assertNotIn("requirements", plan["nodes"][0])
        self.assertEqual(plan["nodes"][0]["bundle_ref"], BUNDLE_REF)
        self.assertEqual(plan["nodes"][0]["restart_contract_ref"],
                         f"{BUNDLE_REF}#/inputs/restart")

    def test_request_and_output_symlink_paths_cannot_escape_workspace(self):
        with tempfile.TemporaryDirectory(dir=TOOLS) as temporary:
            link = Path(temporary) / "synthetic-external-link"
            target = PROJECT_ROOT.parent / f"synthetic-property-plan-target-{uuid.uuid4().hex}"
            try:
                link.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"directory symlink creation unsupported on this host: {error}")
            with self.assertRaisesRegex(ValueError, "workspace-local"):
                _resolve_request(link / "request.json")
            with self.assertRaisesRegex(ValueError, "inside the workspace"):
                _resolve_output(link / "new-plan")


if __name__ == "__main__":
    unittest.main()

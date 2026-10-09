"""One command registry for generic CLI/catalog and legacy private CLI/catalog."""
from __future__ import annotations
from pathlib import Path
PRIVATE_OPERATIONS = {
    "prepare": ("vasp_practical.py", "vasp_practical", "Prepare a candidate from an approved input manifest.", ["POSCAR and approved manifest", "optional source KPOINTS"], "vasp-practical-preparation/v1", ["source_read", "local_artifact_write", "remote_tool_call"], "No VASP launch. Default INCAR backend can call VASPKIT task 101 through the configured isolated tools host."),
    "prepare-bundle": ("approved_bundle.py", "approved_bundle", "Prepare a complete approved bundle and closed execution package.", ["vasp-approved-bundle/v1", "source geometry and optional KPOINTS"], "vasp-practical-preparation/v1", ["source_read", "local_artifact_write", "remote_tool_call"], "No VASP launch. VASPKIT task 101 may be called; gate remains closed."),
    "results": ("vasp_result_analysis.py", "vasp_result_analysis", "Analyze existing VASP outputs locally.", ["existing calculation output directory"], "vasp-practical-analysis/v1", ["source_read", "local_artifact_write"], "Missing or incomplete outputs yield partial evidence; no calculation is started."),
    "potential": ("vasp_result_analysis.py", "vasp_result_analysis", "Analyze an existing potential with the configured VASPKIT backend.", ["existing LOCPOT", "explicit supported axis"], "vasp-planar-potential/v1", ["source_read", "local_artifact_write", "remote_tool_call"], "May call VASPKIT task 426 in its isolated tool directory; does not imply work-function or vacuum conclusions."),
    "record": ("result_registry.py", "result_registry", "Create an immutable result record and rebuild its disposable index.", ["existing vasp-practical-analysis/v1", "explicit execution/case/attempt"], "vasp-result-record/v1", ["source_read", "local_artifact_write"], "Rejects identity/status mismatch and records outside the source analysis directory."),
    "rebuild-index": ("result_registry.py", "result_registry", "Rebuild the disposable result index from existing records.", ["existing result JSON directory"], "SQLite index", ["source_read", "local_artifact_write"], "Does not alter JSON records."),
    "diagnose": ("analysis_addons.py", "analysis_addons", "Run an enabled read-only diagnostic on existing outputs.", ["existing OUTCAR and related calculation evidence"], "vasp-readonly-diagnostic/v1", ["source_read", "local_artifact_write"], "Check-only; missing evidence stays explicit. No correct/run/restart action."),
    "macro": ("analysis_addons.py", "analysis_addons", "Produce a candidate average-potential analysis from existing data.", ["existing VASPKIT planar-average table and explicit analysis options"], "vasp-macro-average/v1", ["source_read", "local_artifact_write"], "Analysis interpreter; candidate windows are explicit and are not automatic surface conclusions."),
    "total-dos": ("analysis_addons.py", "analysis_addons", "Produce a total DOS view from existing outputs.", ["existing DOS-capable VASP outputs"], "vasp-sumo-total-dos/v1", ["source_read", "local_artifact_write"], "Analysis interpreter; does not launch or change a calculation."),
    "hdf5": ("analysis_addons.py", "analysis_addons", "Summarize an existing HDF5-backed VASP result.", ["existing same-run HDF5 result and matching OUTCAR"], "vasp-py4vasp-hdf5-summary/v1", ["source_read", "local_artifact_write"], "Analysis interpreter; source-run cross-check is required."),
    "geometry": ("scientific_helpers.py", "scientific_helpers", "Compare supplied structures under the declared geometry contract.", ["explicit initial and final structures or accepted geometry inputs"], "vasp-surface-geometry/v1", ["source_read", "local_artifact_write"], "Reports geometry facts only; no relaxation is run."),
    "reference": ("scientific_helpers.py", "scientific_helpers", "Evaluate an explicit fixed-geometry reference comparison.", ["explicit reference contract and source outputs"], "vasp-fixed-geometry-reference-result/v1", ["source_read", "local_artifact_write"], "Uses the supplied reference contract; no implicit energy reference is selected."),
    "matrix": ("convergence_matrix.py", "convergence_matrix", "Propose matrix candidates or, with an explicit flag, prepare approved local packages.", ["explicit matrix plan and approved bundle when preparing"], "vasp-matrix-proposals/v1 or vasp-matrix-local-packages/v1", ["source_read", "local_artifact_write"], "Without --prepare-approved only proposes. With it, package generation may call VASPKIT. No VASP launch."),
    "environment": ("vasp_workflow.py", "vasp_workflow", "Report configured local runtime and installed package metadata.", ["configured local tool environments"], "Unversioned environment JSON (existing contract)", [], "May query local installed metadata; does not connect remotely or write artifacts."),
    "capabilities": ("vasp_workflow.py", "vasp_workflow", "List actual workflow capabilities and their effects.", ["local source metadata"], "vasp-agent-capabilities/v1", ["source_read"], "AST/source inspection only; does not import execution backends."),
    "evidence": ("tool_evidence.py", "tool_evidence", "Map one existing supported receipt to a compact evidence sidecar.", ["existing JSON receipt"], "vasp-tool-evidence/v1", ["source_read", "local_artifact_write"], "Never reruns a tool or reads calculation files beyond the supplied receipt."),
    "metrics-report": ("workflow_metrics.py", "workflow_metrics", "Aggregate only explicitly referenced workflow observation receipts and report evidence coverage.", ["explicit vasp-workflow-observations/v1 manifest", "listed JSON receipts only"], "vasp-workflow-metrics-report/v1", ["source_read", "local_artifact_write"], "No directory scan, raw calculation-file read, workflow execution, remote call, or performance claim without comparable observations."),
    "plan-properties": ("property_plan.py", "property_plan", "Build a read-only deterministic property dependency DAG from one explicit request.", ["explicit vasp-property-request/v1 JSON", "listed bundle and parent references only"], "vasp-property-plan/v1", ["source_read", "local_artifact_write"], "Planning only; no VASP input, script, gate, new case, calculation launch, remote call, or automatic matrix expansion."),
    "parameter-advice": ("parameter_advice.py", "parameter_advice", "Review explicit candidate changes against one approved bundle and report evidence blockers.", ["explicit approved vasp-approved-bundle/v1 JSON", "explicit vasp-parameter-proposal/v1 JSON"], "vasp-parameter-advice/v1", ["source_read", "local_artifact_write"], "Read-only review; source contents are not fetched, candidate changes are never accepted, and no INCAR/POSCAR/PAW/restart or execution is written."),
}

ANALYSIS_MODES = {"diagnose", "macro", "total-dos", "hdf5"}
PRIVATE_REQUIREMENTS = {
    "prepare": "--spec and --output-dir are parser-required. Source and execution/output contracts follow vasp_practical.prepare; --inputs-only does not change the declared VASPKIT backend capability.",
    "prepare-bundle": "--bundle and --output-dir are parser-required; approved bundle validation selects the template-specific input contract.",
    "results": "--source and --output-dir are parser-required.",
    "potential": "--source and --output-dir are parser-required; --axis is restricted to x/y/z and the configured backend may call VASPKIT task 426.",
    "record": "--records-dir and --index are parser-required; register additionally needs --summary, --execution-id, --case-id, and --attempt. --status and --status-case-path must be supplied together.",
    "rebuild-index": "--records-dir and --index are parser-required; other register-only flags are ignored by rebuild.",
    "diagnose": "--source and --output-dir are parser-required.",
    "macro": "--source and --output-dir are parser-required; runtime validation additionally requires --smoothing-length-A and at least one --window.",
    "total-dos": "--source and --output-dir are parser-required; --xmin/--xmax have parser defaults.",
    "hdf5": "--source and --output-dir are parser-required.",
    "geometry": "The shared parser requires --output; geometry mode also requires --initial, --final, --baseline, --target, and --normal. --pair and --vasprun are optional.",
    "reference": "The shared parser requires --output; reference mode additionally requires --contract.",
    "matrix": "--plan and --output-dir are parser-required; --prepare-approved additionally requires each case to carry a separately approved bundle.",
    "environment": "No delegated arguments; root mode is required.",
    "capabilities": "No delegated arguments; root mode is required.",
    "evidence": "--receipt is parser-required; --output is optional and must name a new file outside the source calculation directory.",
    "metrics-report": "--observations and --output-dir are parser-required; every source is an explicit workspace-relative JSON receipt, and output-dir must be new.",
    "plan-properties": "--request and --output-dir are parser-required; only explicitly named JSON references are considered, and output-dir must be new and workspace-local.",
    "parameter-advice": "--approved-bundle, --proposal, and --output-dir are parser-required; source references are checked without opening their contents, and output-dir must be new and workspace-local.",
}


GENERIC_OPERATIONS = {
    "validate-bundle": ("approved_bundle_validation.py", "approved_bundle_validation", "Validate an explicit bundle envelope with no deployment or authority.", ["vasp-approved-bundle/v1"], "vasp-approved-bundle/v1", ["source_read"], "Generic policy rejects private approvals and Auto; a valid envelope never authorizes execution."),
    "preflight": ("vasp_executor_core.py", "vasp_executor_core", "Read-only preflight under generic policy.", ["explicit manifest and local inputs"], "vasp-executor-check/v1", ["source_read"], "No launcher is supplied or exposed; private profiles are disabled."),
    "postcheck": ("vasp_executor_core.py", "vasp_executor_core", "Read-only postcheck of explicit existing outputs.", ["explicit manifest, case and output contract"], "vasp-executor-check/v1", ["source_read"], "Program exit and convergence evidence remain separate; no scientific acceptance."),
    "evidence": PRIVATE_OPERATIONS["evidence"],
    "capabilities": ("agent_capabilities.py", "agent_capabilities", "Describe only installed generic commands and their declared effects.", ["source metadata"], "vasp-agent-capabilities/v1", ["source_read"], "Does not import deployment, configuration or execution backends."),
}
GENERIC_REQUIREMENTS = {
    "validate-bundle": "--bundle required; --workspace is explicit or defaults to current directory.",
    "preflight": "--manifest required; supplied input/case/requirements paths must be workspace-contained.",
    "postcheck": "--manifest and --case-dir required; output contract follows the existing executor schema.",
    "evidence": PRIVATE_REQUIREMENTS["evidence"] + " All supplied paths must be workspace-contained.",
    "capabilities": "No delegated arguments; metadata inspection only.",
}
GENERIC_DEPENDENCIES = {
    "validate-bundle": ("approved_bundle_validation.py", "vasp_profile_policy.py"),
    "preflight": ("vasp_executor_core.py", "vasp_contracts.py", "vasp_profile_policy.py", "progress_snapshot.py", "vasp_execution_state.py"),
    "postcheck": ("vasp_executor_core.py", "vasp_contracts.py", "vasp_profile_policy.py", "progress_snapshot.py", "vasp_execution_state.py"),
    "evidence": ("tool_evidence.py",),
    "capabilities": ("agent_capabilities.py", "workflow_command_catalog.py"),
}

def missing_files(command, tools_dir):
    return [n for n in GENERIC_DEPENDENCIES[command] if not (Path(tools_dir) / n).is_file()]

def command_names(scope="private", tools_dir=None):
    if scope == "private":
        return list(PRIVATE_OPERATIONS)
    if scope != "generic":
        raise ValueError("Unknown command scope")
    root = Path(tools_dir) if tools_dir is not None else Path(__file__).resolve().parent
    return [name for name in GENERIC_OPERATIONS if not missing_files(name, root)]

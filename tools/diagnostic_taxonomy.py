"""Read-only VASP diagnostic taxonomy and evidence classification.

Only the pre-existing BRMIX Custodian check remains in the default check set.
Other categories are catalogued as proposed until their real-sample gates pass.
This module never runs jobs, applies corrections, or changes calculation files.
"""
from __future__ import annotations

from copy import deepcopy
from importlib import metadata
from pathlib import Path
import re
import xml.etree.ElementTree as ET


DEFAULT_CHECKS = ("brmix",)
LOCAL_CUSTODIAN_VERSION = "2025.12.14"
UPSTREAM_REFERENCE = {
    "repository": "materialsproject/custodian",
    "commit": "56cb4b652c5a43b7e59e13f695910a7b71a7cc6a",
    "use": "fixed source reference only; no upgrade or installation",
}


def _proposal(action, basis, files, parameters, nature, changes_method_or_restart, stop_conditions):
    return {
        "candidate_action": action,
        "basis": basis,
        "affected_files": list(files),
        "affected_parameters": list(parameters),
        "nature": nature,
        "changes_method_or_restart": changes_method_or_restart,
        "required_approval": "VASP Sol and user approval before any input, restart, or execution change",
        "stop_conditions": list(stop_conditions),
    }


_BRMIX_PROPOSAL = _proposal(
    "Review the raw BRMIX lines with the meaning and approval basis of any explicitly declared NELECT value and approved mixing settings; make no change during diagnosis.",
    "A raw BRMIX signature is present; the finding records whether the installed Custodian check classified or suppressed it.",
    ("INCAR", "vasp.stdout"),
    ("NELECT", "IMIX", "AMIX", "BMIX", "AMIX_MAG", "BMIX_MAG"),
    "REVIEW_ONLY; any later mixing or electron-count change is a numerical/scientific input decision",
    True,
    ("Stop if the intended meaning or approval basis of an explicitly declared NELECT value or source-case identity is uncertain.",
     "Stop before changing INCAR, restart files, or a running/completed calculation."),
)


def _unvalidated_proposal(category, action, basis, files, parameters=(), nature="REVIEW_ONLY", changes=True):
    return _proposal(
        action,
        basis,
        files,
        parameters,
        nature,
        changes,
        ("Do not act until a representative real sample and category-specific validation are recorded.",
         "Stop before changing inputs, restart files, or calculation execution."),
    )


_CATALOG = [
    {
        "detector_id": "brmix",
        "category": "SCF_NUMERICAL",
        "detector_version": "custodian-2025.12.14-brmix-check+b2-v1",
        "required_files": ["INCAR", "vasp.stdout"],
        "raw_signatures": ["BRMIX: very serious problems"],
        "context_rules": ["Custodian VaspErrorHandler.check suppresses BRMIX when INCAR contains an explicit NELECT declaration; this alone does not establish net charge."],
        "support_status": "EXISTING_DEFAULT_UNCHANGED",
        "validation_status": "PARTIAL_REAL_NONHIT_ONLY; real positive/suppression evidence is absent",
        "legacy_default": True,
        "newly_enabled": False,
        "possible_actions": [_BRMIX_PROPOSAL],
        "changes_method_or_restart": True,
    },
    {
        "detector_id": "run_identity_evidence",
        "category": "IDENTITY_AND_MISSING_EVIDENCE",
        "detector_version": "b2-proposed-v1",
        "required_files": ["INCAR", "POSCAR", "OUTCAR", "vasprun.xml"],
        "raw_signatures": [],
        "context_rules": ["A directory name or nonempty output does not bind execution, case, attempt, or input identity."],
        "support_status": "PROPOSED/NOT_VALIDATED",
        "validation_status": "NOT_VALIDATED_REAL_SAMPLE_MATRIX_MISSING",
        "legacy_default": False,
        "newly_enabled": False,
        "possible_actions": [_unvalidated_proposal("run_identity_evidence", "Collect or reconcile the approved execution/case/attempt manifest and required artifacts.", "Required files or immutable run identity evidence are absent or inconsistent.", ("INCAR", "POSCAR", "OUTCAR", "vasprun.xml"), ("execution_id", "case_id", "attempt"), "MECHANICAL_EVIDENCE_COLLECTION", False)],
        "changes_method_or_restart": False,
    },
    {
        "detector_id": "restart_compatibility",
        "category": "IDENTITY_AND_RESTART",
        "detector_version": "b2-proposed-v1",
        "required_files": ["INCAR", "POSCAR", "WAVECAR", "CHGCAR", "OUTCAR"],
        "raw_signatures": ["WAVECAR.*incompatible", "CHGCAR.*incompatible", "ISTART.*restart"],
        "context_rules": ["File presence alone does not establish source identity or restart compatibility."],
        "support_status": "PROPOSED/NOT_VALIDATED",
        "validation_status": "NOT_VALIDATED_REAL_SAMPLE_MATRIX_MISSING",
        "legacy_default": False,
        "newly_enabled": False,
        "possible_actions": [_unvalidated_proposal("restart_compatibility", "Compare the exact parent attempt, geometry, environment, and restart contract before deciding whether restart use is compatible.", "Only explicit source-to-target identity and parser evidence can support a compatibility review.", ("INCAR", "POSCAR", "WAVECAR", "CHGCAR"), ("ISTART", "ICHARG", "ENCUT", "NBANDS", "ISPIN"), "SCIENTIFIC_RESTART_COMPATIBILITY_REVIEW", True)],
        "changes_method_or_restart": True,
    },
    {
        "detector_id": "scf_instability",
        "category": "SCF_NUMERICAL",
        "detector_version": "b2-proposed-v1",
        "required_files": ["INCAR", "vasp.stdout", "OUTCAR"],
        "raw_signatures": ["BRMIX", "Sub-Space-Matrix", "DAV", "RMM"],
        "context_rules": ["DAV/RMM iteration lines alone do not establish an error, convergence, or nonconvergence."],
        "support_status": "PROPOSED/NOT_VALIDATED",
        "validation_status": "NOT_VALIDATED_REAL_POSITIVE_AND_SUPPRESSION_MATRIX_MISSING",
        "legacy_default": False,
        "newly_enabled": False,
        "possible_actions": [_unvalidated_proposal("scf_instability", "Review the complete electronic history and approved mixing/spin/electron-count settings.", "Use complete same-run outputs and preserve raw iteration evidence; a slow DAV sequence alone is not a failure.", ("INCAR", "vasp.stdout", "OUTCAR"), ("ALGO", "NELM", "NELMIN", "IMIX", "AMIX", "BMIX", "NELECT", "ISPIN"), "SCIENTIFIC_NUMERICAL_REVIEW", True)],
        "changes_method_or_restart": True,
    },
    {
        "detector_id": "geometry_instability",
        "category": "GEOMETRY_AND_SCIENCE",
        "detector_version": "b2-proposed-v1",
        "required_files": ["INCAR", "POSCAR", "OUTCAR", "OSZICAR"],
        "raw_signatures": ["ZBRENT", "trial step too large", "EDIFFG"],
        "context_rules": ["A geometry warning is not a unique root cause and is separate from electronic convergence."],
        "support_status": "PROPOSED/NOT_VALIDATED",
        "validation_status": "NOT_VALIDATED_REAL_POSITIVE_AND_NEGATIVE_MATRIX_MISSING",
        "legacy_default": False,
        "newly_enabled": False,
        "possible_actions": [_unvalidated_proposal("geometry_instability", "Review the complete force/energy trajectory, constraints, and approved optimizer settings.", "A raw geometry signature is only a candidate clue; inspect all simultaneous findings.", ("INCAR", "POSCAR", "CONTCAR", "OUTCAR", "OSZICAR"), ("IBRION", "POTIM", "ISIF", "NSW", "EDIFFG"), "SCIENTIFIC_GEOMETRY_METHOD_REVIEW", True)],
        "changes_method_or_restart": True,
    },
    {
        "detector_id": "startup_resource",
        "category": "STARTUP_AND_RESOURCES",
        "detector_version": "b2-proposed-v1",
        "required_files": ["vasp.stdout", "run_timing.txt"],
        "raw_signatures": ["MPI_ABORT", "error while loading shared libraries", "out of memory", "Killed"],
        "context_rules": ["Launch, environment, scheduler, and resource evidence must remain distinguishable."],
        "support_status": "PROPOSED/NOT_VALIDATED",
        "validation_status": "NOT_VALIDATED_REAL_POSITIVE_AND_NEGATIVE_MATRIX_MISSING",
        "legacy_default": False,
        "newly_enabled": False,
        "possible_actions": [_unvalidated_proposal("startup_resource", "Review the exact launcher, exit receipt, environment, and scheduler/resource evidence.", "Do not infer VASP scientific failure from a launch or resource error alone.", ("vasp.stdout", "run_timing.txt", "execution_receipt.json"), ("MPI ranks", "memory request", "launcher/environment"), "EXECUTION_RESOURCE_REVIEW", True)],
        "changes_method_or_restart": True,
    },
    {
        "detector_id": "xml_incomplete",
        "category": "PARSER_AND_EVIDENCE_COMPLETENESS",
        "detector_version": "b2-proposed-v1",
        "required_files": ["vasprun.xml", "OSZICAR"],
        "raw_signatures": ["XML_PARSE_ERROR", "INCOMPLETE_XML"],
        "context_rules": ["A nonempty OSZICAR cannot turn malformed/truncated vasprun.xml into parser or convergence success."],
        "support_status": "PROPOSED/NOT_VALIDATED",
        "validation_status": "NOT_VALIDATED_REAL_TRUNCATED_XML_SAMPLE_MISSING",
        "legacy_default": False,
        "newly_enabled": False,
        "possible_actions": [_unvalidated_proposal("xml_incomplete", "Preserve all same-run outputs and review XML completeness with the independent run receipt.", "OSZICAR presence is not evidence that vasprun.xml parsed or the run converged.", ("vasprun.xml", "OSZICAR", "OUTCAR", "run_timing.txt"), (), "MECHANICAL_EVIDENCE_REVIEW", False)],
        "changes_method_or_restart": False,
    },
]

_BY_ID = {item["detector_id"]: item for item in _CATALOG}
_NELECT_RE = re.compile(r"^\s*NELECT\s*=", re.IGNORECASE)
_SCF_LINE_RE = re.compile(r"^\s*(?:DAV|RMM):", re.IGNORECASE)
_BRMIX_RE = re.compile(r"BRMIX:\s*very serious problems", re.IGNORECASE)


def taxonomy_catalog() -> dict:
    """Return versioned detector metadata and the unchanged legacy default."""
    return {
        "schema": "vasp-diagnostic-taxonomy/v1",
        "default_checks": list(DEFAULT_CHECKS),
        "newly_enabled_detectors": [],
        "custodian": {
            "local_version": LOCAL_CUSTODIAN_VERSION,
            "installed_version": _installed_custodian_version(),
            "local_environment": ".venv-analysis-v1",
            "upstream_reference": dict(UPSTREAM_REFERENCE),
        },
        "activation_rule": "New detectors remain disabled until a real-sample category matrix and batch validation pass.",
        "detectors": deepcopy(_CATALOG),
    }


def _has_nelect(incar_text: str) -> bool:
    for line in incar_text.splitlines():
        line = re.split(r"[#!]", line, maxsplit=1)[0]
        if _NELECT_RE.search(line):
            return True
    return False


def _raw_hits(stdout_text: str) -> list[dict]:
    hits = []
    for number, line in enumerate(stdout_text.splitlines(), 1):
        if _BRMIX_RE.search(line):
            hits.append({"file": "vasp.stdout", "line": number,
                         "signature": "BRMIX: very serious problems", "text": line[:500]})
    return hits


def _custodian_check(source: Path) -> bool:
    from custodian.vasp.handlers import VaspErrorHandler

    handler = VaspErrorHandler(output_filename="vasp.stdout", errors_subset_to_catch=["brmix"])
    return bool(handler.check(str(source)))


def _installed_custodian_version() -> str | None:
    try:
        return metadata.version("custodian")
    except metadata.PackageNotFoundError:
        return None


def diagnose_brmix(source, checker=None) -> dict:
    """Classify raw BRMIX evidence through the installed check-only handler."""
    source = Path(source).resolve()
    required_files = _BY_ID["brmix"]["required_files"]
    missing = [name for name in required_files if not (source / name).is_file()]
    raw_hits = []
    scf_lines = []
    nelect_present = None
    check_detected = None
    check_error = None
    custodian_version = _installed_custodian_version()
    stdout_text = None
    if (source / "vasp.stdout").is_file():
        try:
            stdout_text = (source / "vasp.stdout").read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            missing.append("vasp.stdout unreadable")
            check_error = type(error).__name__
    if stdout_text is not None:
        raw_hits = _raw_hits(stdout_text)
        scf_lines = [number for number, line in enumerate(stdout_text.splitlines(), 1)
                     if _SCF_LINE_RE.search(line)]
    if missing:
        state = "INSUFFICIENT_EVIDENCE"
    else:
        try:
            incar_text = (source / "INCAR").read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            missing = ["INCAR unreadable"]
            check_error = type(error).__name__
            state = "INSUFFICIENT_EVIDENCE"
        else:
            nelect_present = _has_nelect(incar_text)
            try:
                if checker is None and custodian_version != LOCAL_CUSTODIAN_VERSION:
                    raise RuntimeError("Installed Custodian version differs from the recorded local version.")
                check_detected = bool((checker or _custodian_check)(source))
            except Exception as error:  # Parser/check failure is an evidence gap, never a clean result.
                check_error = type(error).__name__
                state = "INSUFFICIENT_EVIDENCE"
            else:
                state = "DETECTED" if check_detected else "SUPPRESSED" if raw_hits else "NO_WHITELIST_HIT"

    suppression_context = None
    if raw_hits and check_detected is False:
        suppression_context = (
            "An explicit NELECT declaration is present; Custodian 2025.12.14 skips BRMIX classification in this input context."
            if nelect_present is True else
            "Custodian did not classify the raw BRMIX signature; the suppression cause is unresolved."
        )
    elif raw_hits and check_detected is None:
        suppression_context = "Custodian check was not run or could not complete because required evidence is missing."

    finding = {
        "detector_id": "brmix",
        "category": _BY_ID["brmix"]["category"],
        "detector_version": _BY_ID["brmix"]["detector_version"],
        "state": state,
        "required_files": list(required_files),
        "missing": missing,
        "raw_hits": raw_hits,
        "context": {
            "nelect_declared": nelect_present,
            "custodian_detected": check_detected,
            "suppression_context": suppression_context,
        },
        "check_error_type": check_error,
        "support_status": _BY_ID["brmix"]["support_status"],
        "validation_status": _BY_ID["brmix"]["validation_status"],
    }
    proposals = []
    if raw_hits:
        proposal = deepcopy(_BRMIX_PROPOSAL)
        proposal["basis"] = [
            {"file": hit["file"], "line": hit["line"], "signature": hit["signature"],
             "suppression_context": finding["context"]["suppression_context"]}
            for hit in raw_hits
        ]
        proposals.append(proposal)
    identity_finding = {
        "detector_id": "run_identity_evidence",
        "category": _BY_ID["run_identity_evidence"]["category"],
        "state": "INSUFFICIENT_EVIDENCE",
        "evaluated": False,
        "missing": ["execution/case/attempt identity manifest was not supplied to this source-only check"],
        "support_status": _BY_ID["run_identity_evidence"]["support_status"],
    }
    identity_proposal = deepcopy(_BY_ID["run_identity_evidence"]["possible_actions"][0])
    identity_proposal["basis"] = list(identity_finding["missing"])
    proposals.append(identity_proposal)
    return {
        "schema": "vasp-readonly-diagnostic/v2",
        "source": str(source),
        "mode": "CHECK_ONLY",
        "checks": list(DEFAULT_CHECKS),
        "findings": [finding, identity_finding],
        "state": state,
        "missing": missing,
        "raw_hits": raw_hits,
        "custodian_detected": check_detected,
        "custodian_version": custodian_version,
        "suppression": finding["context"]["suppression_context"],
        "unclassified_scf_activity": {
            "status": "UNCLASSIFIED_NOT_AN_ERROR_DETECTOR",
            "dav_rmm_line_count": len(scf_lines),
            "line_numbers": scf_lines,
        },
        "limitations": [
            "NO_WHITELIST_HIT means only that the configured BRMIX check found no whitelist match; it does not mean error-free or converged.",
            "DAV/RMM line counts are context only; the SCF detector is PROPOSED/NOT_VALIDATED and is not run by default.",
            "No run identity, final convergence, geometry acceptance, or scientific acceptance is inferred.",
        ],
        "recovery_proposals": proposals,
        "taxonomy": taxonomy_catalog(),
    }


def inspect_xml_completeness(source) -> dict:
    """Check parser evidence only; OSZICAR content never substitutes for XML validity."""
    source = Path(source).resolve()
    required = ("vasprun.xml", "OSZICAR")
    missing = [name for name in required if not (source / name).is_file()]
    if missing:
        return {"detector_id": "xml_incomplete", "state": "INSUFFICIENT_EVIDENCE",
                "missing": missing, "oszicar_nonempty": None, "scientific_convergence": "NOT_EVALUATED"}
    try:
        oszicar_nonempty = (source / "OSZICAR").stat().st_size > 0
    except OSError as error:
        return {
            "detector_id": "xml_incomplete", "state": "INSUFFICIENT_EVIDENCE",
            "raw_hits": [], "missing": ["OSZICAR unreadable"],
            "read_error": {"file": "OSZICAR", "type": type(error).__name__, "message": str(error)},
            "oszicar_nonempty": None, "scientific_convergence": "NOT_EVALUATED",
            "limitation": "An OSZICAR read failure is missing evidence and yields no convergence conclusion.",
        }
    try:
        ET.parse(source / "vasprun.xml")
    except ET.ParseError as error:
        position = getattr(error, "position", None)
        return {
            "detector_id": "xml_incomplete", "state": "DETECTED",
            "raw_hits": [{"file": "vasprun.xml", "line": position[0] if position else None, "signature": "XML_PARSE_ERROR",
                          "text": type(error).__name__}],
            "missing": [], "oszicar_nonempty": oszicar_nonempty,
            "scientific_convergence": "NOT_EVALUATED",
            "limitation": "A nonempty OSZICAR does not repair malformed or truncated vasprun.xml.",
        }
    except OSError as error:
        return {
            "detector_id": "xml_incomplete", "state": "INSUFFICIENT_EVIDENCE",
            "raw_hits": [], "missing": ["vasprun.xml unreadable"],
            "read_error": {"file": "vasprun.xml", "type": type(error).__name__, "message": str(error)},
            "oszicar_nonempty": oszicar_nonempty, "scientific_convergence": "NOT_EVALUATED",
            "limitation": "A vasprun.xml read failure does not establish malformed XML, file damage, or convergence.",
        }
    return {"detector_id": "xml_incomplete", "state": "NO_WHITELIST_HIT",
            "raw_hits": [], "missing": [], "oszicar_nonempty": oszicar_nonempty,
            "scientific_convergence": "NOT_EVALUATED",
            "limitation": "Well-formed XML does not itself prove electronic, ionic, or scientific acceptance."}

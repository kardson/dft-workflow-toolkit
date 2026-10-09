"""Read-only catalog of the VASP workflow CLI as implemented in this tree."""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
WORKFLOW = TOOLS / "vasp_workflow.py"

# This table describes behavior and gates; CLI fields are extracted from the
# actual argparse sources below so this is not a second argument definition.
from workflow_command_catalog import (
    PRIVATE_OPERATIONS as OPERATIONS, PRIVATE_REQUIREMENTS as OPERATION_REQUIREMENTS,
    ANALYSIS_MODES, GENERIC_OPERATIONS, GENERIC_REQUIREMENTS, GENERIC_DEPENDENCIES,
    command_names, missing_files,
)

def _literal(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


def _expression(node):
    """Retain non-literal argparse defaults without executing their expression."""
    return ast.unparse(node) if node is not None else None


def parser_arguments(path: Path) -> list[dict]:
    """Extract add_argument declarations without importing or initializing tools."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "add_argument":
            continue
        names = [_literal(arg) for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
        if not names:
            continue
        kw = {item.arg: item.value for item in node.keywords if item.arg}
        action = _literal(kw.get("action")) if "action" in kw else None
        kind = "flag" if action in {"store_true", "store_false"} else "string"
        if "type" in kw:
            kind = ast.unparse(kw["type"]).split(".")[-1]
        param = {
            "names": names,
            "required": (_literal(kw.get("required")) is True) or (not names[0].startswith("-") and _literal(kw.get("nargs")) not in ("?", "*")),
            "type": kind,
            "choices": _literal(kw.get("choices")) if "choices" in kw else None,
            "nargs": _literal(kw.get("nargs")) if "nargs" in kw else None,
            "action": _literal(kw.get("action")) if "action" in kw else None,
            "default": _literal(kw.get("default")) if "default" in kw else None,
            "default_expression": _expression(kw.get("default")),
            "source": str(path),
            "line": node.lineno,
        }
        result.append(param)
    # argparse declaration order is useful to a caller; AST walk is unordered.
    result.sort(key=lambda item: item["line"])
    return result


def _interpreter(mode: str) -> str:
    env = ".venv-analysis-v1" if mode in ANALYSIS_MODES else ".venv-practical"
    return str(TOOLS / env / "Scripts" / "python.exe") if sys.platform == "win32" else str(TOOLS / env / "bin" / "python")


def catalog(*, scope=None, tools_dir=None) -> dict:
    root = Path(tools_dir) if tools_dir is not None else TOOLS
    scope = scope or ('private' if (root / 'vasp_workflow.py').is_file() else 'generic')
    if scope == 'generic':
        return generic_catalog(root)
    if scope != 'private':
        raise ValueError('Unknown catalog scope')
    workflow_args = parser_arguments(WORKFLOW)
    entry_choices = command_names("private", root)
    entries = []
    for name, (filename, module, summary, files, schema, effects, failure) in OPERATIONS.items():
        source = TOOLS / filename
        args = [] if name in {"environment", "capabilities"} else parser_arguments(source)
        if name in {"prepare", "prepare-bundle", "results", "potential", "record", "rebuild-index", "diagnose", "macro", "total-dos", "hdf5", "geometry", "reference", "matrix"}:
            # Workflow delegates these exact modes to the listed module; record
            # the root mode choice separately from the delegated options.
            args = [p for p in args if not (p["names"] == ["mode"])]
        entries.append({
            "name": name,
            "cli": ["vasp_workflow.py", name],
            "entry_required": True,
            "entry_parameter": {"name": "mode", "required": True, "type": "choice", "choices": entry_choices, "source": str(WORKFLOW)},
            "entry_mode_choices_from_parser": entry_choices,
            "delegated_parser": str(source),
            "delegated_parameters": args,
            "invocation_requirements": OPERATION_REQUIREMENTS[name],
            "interpreter": _interpreter(name),
            "required_files": files,
            "supported_scope": summary,
            "output_schema": schema,
            "effects": {key: key in effects or (name == "matrix" and key == "remote_tool_call") for key in ("source_read", "local_artifact_write", "remote_tool_call", "remote_compute_control")},
            "effect_conditions": (
                {"remote_tool_call": "only with --prepare-approved"} if name == "matrix" else
                {"local_artifact_write": "only when --output is supplied"} if name == "evidence" else {}
            ),
            "effect_semantics": "possible effects of this CLI operation; not effects observed in a prior invocation",
            "failure_semantics": failure,
            "data_gate": "See required_files; absence or unsupported evidence must remain explicit.",
        })
    return {"schema": "vasp-agent-capabilities/v1", "source": str(WORKFLOW), "catalog_method": "AST extraction of current argparse declarations; no backend imports", "commands": entries}



def generic_catalog(root):
    root = Path(root)
    names = command_names("generic", root)
    entries = []
    for name in names:
        filename, module, summary, files, schema, effects, failure = GENERIC_OPERATIONS[name]
        source = root / filename
        params = [] if name == "capabilities" else parser_arguments(source)
        # Executor subparser declarations repeat the same flags; the real core
        # parser applies mode-specific requiredness when invoked.
        if name in {"preflight", "postcheck"}:
            unique = {}
            for param in params:
                if param["names"] == ["mode"]: continue
                unique.setdefault(tuple(param["names"]), param)
            params = list(unique.values())
            if name == "postcheck":
                for param in params:
                    if "--case-dir" in param["names"]: param["required"] = True
        entries.append(dict(name=name, cli=["vasp_core_cli.py",name], entry_required=True,
            entry_mode_choices_from_parser=names, delegated_parser=str(source), delegated_parameters=params,
            invocation_requirements=GENERIC_REQUIREMENTS[name], interpreter=sys.executable,
            required_files=files, supported_scope=summary, output_schema=schema,
            dependencies=list(GENERIC_DEPENDENCIES[name]), scope="generic",
            effects={key:key in effects for key in ("source_read","local_artifact_write","remote_tool_call","remote_compute_control")},
            effect_semantics="possible effects of this CLI operation; not effects observed in a prior invocation",
            failure_semantics=failure, scientific_acceptance="NOT_AUTHORIZED"))
    unavailable = [{"name": n, "missing_files": missing_files(n,root)} for n in GENERIC_OPERATIONS if n not in names]
    return dict(schema="vasp-agent-capabilities/v1",source=str(root/"vasp_core_cli.py"),
        catalog_method="Shared command registry plus AST parser metadata; no backend imports",
        scope="generic",commands=entries,unavailable_commands=unavailable)

def print_command_help(name):
    """Describe legacy help from its argparse AST without importing optional backends."""
    if name not in OPERATIONS: raise ValueError("Unknown private command")
    filename = OPERATIONS[name][0]
    parameters = [] if name in {"environment","capabilities"} else parser_arguments(TOOLS/filename)
    lines = ["usage: vasp_workflow.py " + name + " [options]", "", OPERATIONS[name][2], "", "options:"]
    seen = set()
    for item in parameters:
        if item["names"] == ["mode"] or tuple(item["names"]) in seen: continue
        seen.add(tuple(item["names"]))
        names = ", ".join(item["names"])
        detail = "required" if item["required"] else "optional"
        if item["choices"]: detail += "; choices=" + str(item["choices"])
        lines.append("  " + names + "  (" + detail + ")")
    lines.extend(["  -h, --help", "", OPERATION_REQUIREMENTS[name]])
    print("\n".join(lines))
    return 0

def main() -> int:
    print(json.dumps(catalog(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

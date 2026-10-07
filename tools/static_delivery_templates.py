#!/usr/bin/env python3
"""Trusted versioned templates for a VASP static delivery package.

The local builder and static checker both consume this module.  A candidate
script must match these bytes exactly; a self-updated package checksum is not
accepted as a template-version proof.
"""

from __future__ import annotations


TEMPLATE_VERSION = "vasp-static-package/v1"


def task_env_text() -> bytes:
    return r"""#!/usr/bin/env bash
set -eo pipefail
TASK_ENV_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
MANIFEST_PATH="$TASK_ENV_DIR/input_manifest.json"
DEPENDENCY_PATH="$TASK_ENV_DIR/runtime_dependencies.json"
EXISTING_PYTHONPATH="${PYTHONPATH-}"
EXISTING_LD_LIBRARY_PATH="${LD_LIBRARY_PATH-}"
set -u
export VASP_STATIC_MANIFEST="$MANIFEST_PATH"
export VASP_STATIC_TEMPLATE="vasp-static-package/v1"
if [ ! -f "$MANIFEST_PATH" ] || [ ! -f "$DEPENDENCY_PATH" ] || [ ! -f "$TASK_ENV_DIR/static_runtime_guard.py" ]; then
    echo "static package runtime descriptor is incomplete" >&2
    return 2 2>/dev/null || exit 2
fi
BOOTSTRAP_PYTHON="$(command -v python3 || true)"
if [ -z "$BOOTSTRAP_PYTHON" ]; then
    echo "python3 is required only to read the approved runtime descriptor" >&2
    return 2 2>/dev/null || exit 2
fi
BOOTSTRAP_ENV="$(
    "$BOOTSTRAP_PYTHON" "$TASK_ENV_DIR/static_runtime_guard.py" \
        --manifest "$MANIFEST_PATH" \
        --dependencies "$DEPENDENCY_PATH" \
        --emit-env
)"
IFS='|' read -r APPROVED_PYTHON _BOOTSTRAP_REST <<< "$BOOTSTRAP_ENV"
if [ -z "$APPROVED_PYTHON" ]; then
    echo "approved python path is missing from runtime_dependencies.json" >&2
    return 2 2>/dev/null || exit 2
fi
IFS='|' read -r APPROVED_PYTHON APPROVED_TOOLCHAIN APPROVED_PYTHONPATH APPROVED_LD_LIBRARY_PATH MPI_LAUNCHER TMUX_BIN MPI_ARGS OMP_THREADS CPU_POLICY < <(
    "$APPROVED_PYTHON" "$TASK_ENV_DIR/static_runtime_guard.py" \
        --manifest "$MANIFEST_PATH" \
        --dependencies "$DEPENDENCY_PATH" \
        --package-dir "$TASK_ENV_DIR" \
        --check-package \
        --check-environment \
        --emit-env
)
export VASP_PYTHON_BIN="$APPROVED_PYTHON"
export VASP_TOOLCHAIN_ROOT="$APPROVED_TOOLCHAIN"
export PYTHONPATH="$APPROVED_PYTHONPATH:$EXISTING_PYTHONPATH"
export LD_LIBRARY_PATH="$APPROVED_LD_LIBRARY_PATH:$EXISTING_LD_LIBRARY_PATH"
export VASP_MPI_LAUNCHER="$MPI_LAUNCHER"
export VASP_TMUX_BIN="$TMUX_BIN"
export VASP_MPI_ARGS="$MPI_ARGS"
export OMP_NUM_THREADS="$OMP_THREADS"
export VASP_CPU_BINDING_POLICY="$CPU_POLICY"
""".encode("utf-8")


def runner_text() -> bytes:
    return r"""#!/usr/bin/env bash
set -eo pipefail
TMUX_VALUE="${TMUX-}"
set -u
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
MANIFEST_PATH="$SCRIPT_DIR/input_manifest.json"
DEPENDENCY_PATH="$SCRIPT_DIR/runtime_dependencies.json"
GATE_PATH="$SCRIPT_DIR/sol_review_gate.json"
PAW_PATH="$SCRIPT_DIR/paw_identity.json"
# static_runtime_guard.py reads input_manifest.delivery_identity at runtime.
. "$SCRIPT_DIR/task_env.sh"

"$VASP_PYTHON_BIN" "$SCRIPT_DIR/static_runtime_guard.py" \
    --manifest "$MANIFEST_PATH" \
    --dependencies "$DEPENDENCY_PATH" \
    --package-dir "$SCRIPT_DIR" \
    --gate "$GATE_PATH" \
    --check-package \
    --check-gate

IFS='|' read -r BATCH_DIR CASE_ID RUNTIME_DIR TMUX_SESSION VASP_BIN MPI_LAUNCHER MPI_RANKS MPI_ARGS PAW_ROOT COMPONENTS POTCAR_SHA256 < <(
"$VASP_PYTHON_BIN" "$SCRIPT_DIR/static_runtime_guard.py" \
    --manifest "$MANIFEST_PATH" \
    --dependencies "$DEPENDENCY_PATH" \
    --paw-identity "$PAW_PATH" \
    --emit-runtime
)
if [ "$RUNTIME_DIR" != "$BATCH_DIR/$CASE_ID" ]; then
    echo "derived runtime directory does not match delivery identity" >&2
    exit 2
fi
if [ -z "$VASP_BIN" ] || [ -z "$MPI_LAUNCHER" ] || [ -z "$MPI_RANKS" ]; then
    echo "manifest execution environment is incomplete" >&2
    exit 2
fi
if [ -z "$TMUX_VALUE" ]; then
    echo "the static runner must execute inside tmux" >&2
    exit 2
fi
ACTIVE_TMUX_SESSION="$("$VASP_TMUX_BIN" display-message -p '#S' 2>/dev/null)"
if [ "$ACTIVE_TMUX_SESSION" != "$TMUX_SESSION" ]; then
    echo "active tmux session does not match delivery identity" >&2
    exit 2
fi
CASE_DIR="$RUNTIME_DIR"
if [ ! -d "$CASE_DIR" ]; then
    echo "prepared case directory is missing: $CASE_DIR" >&2
    exit 2
fi
if [ ! -f "$CASE_DIR/POTCAR" ]; then
    echo "prepared case POTCAR is missing" >&2
    exit 2
fi
"$VASP_PYTHON_BIN" "$SCRIPT_DIR/static_runtime_guard.py" \
    --manifest "$MANIFEST_PATH" \
    --dependencies "$DEPENDENCY_PATH" \
    --paw-identity "$PAW_PATH" \
    --combined-path "$CASE_DIR/POTCAR" \
    --check-combined
"$VASP_PYTHON_BIN" "$SCRIPT_DIR/static_runtime_guard.py" \
    --manifest "$MANIFEST_PATH" \
    --dependencies "$DEPENDENCY_PATH" \
    --case-dir "$CASE_DIR" \
    --check-case-state

"$VASP_PYTHON_BIN" "$SCRIPT_DIR/static_postcheck.py" preflight \
    --manifest "$MANIFEST_PATH" \
    --input-dir "$SCRIPT_DIR" \
    --case-dir "$CASE_DIR" \
    --receipt "$CASE_DIR/preflight.json"

cd -- "$CASE_DIR"
if [ -e .run_once ]; then
    echo "single-run lock already exists" >&2
    exit 2
fi
( set -o noclobber; : > .run_once ) 2>/dev/null || {
    echo "could not acquire single-run lock" >&2
    exit 2
}
START_EPOCH="$(date +%s)"
set +e
OMP_NUM_THREADS=1 "$MPI_LAUNCHER" $MPI_ARGS -np "$MPI_RANKS" "$VASP_BIN" > vasp.stdout 2> vasp.stderr
VASP_STATUS=$?
set -e
END_EPOCH="$(date +%s)"
{
    printf 'exit_code=%s\n' "$VASP_STATUS"
    printf 'start_epoch=%s\n' "$START_EPOCH"
    printf 'end_epoch=%s\n' "$END_EPOCH"
} > run_timing.txt

POSTCHECK_STATUS=0
"$VASP_PYTHON_BIN" "$SCRIPT_DIR/static_postcheck.py" postcheck \
    --manifest "$MANIFEST_PATH" \
    --input-dir "$SCRIPT_DIR" \
    --case-dir "$CASE_DIR" \
    --requirements "$SCRIPT_DIR/output_requirements.json" \
    --receipt "$CASE_DIR/postcheck.json" || POSTCHECK_STATUS=$?
if [ "$VASP_STATUS" -ne 0 ]; then
    exit "$VASP_STATUS"
fi
exit "$POSTCHECK_STATUS"
""".encode("utf-8")


def preparer_text() -> bytes:
    return r"""#!/usr/bin/env bash
set -eo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
MANIFEST_PATH="$SCRIPT_DIR/input_manifest.json"
DEPENDENCY_PATH="$SCRIPT_DIR/runtime_dependencies.json"
GATE_PATH="$SCRIPT_DIR/sol_review_gate.json"
PAW_PATH="$SCRIPT_DIR/paw_identity.json"
# static_runtime_guard.py reads input_manifest.delivery_identity at runtime.
. "$SCRIPT_DIR/task_env.sh"

IFS='|' read -r BATCH_DIR CASE_ID RUNTIME_DIR TMUX_SESSION VASP_BIN MPI_LAUNCHER MPI_RANKS MPI_ARGS PAW_ROOT COMPONENTS POTCAR_SHA256 < <(
    "$VASP_PYTHON_BIN" "$SCRIPT_DIR/static_runtime_guard.py" \
        --manifest "$MANIFEST_PATH" \
        --dependencies "$DEPENDENCY_PATH" \
        --paw-identity "$PAW_PATH" \
        --emit-runtime
)

"$VASP_PYTHON_BIN" "$SCRIPT_DIR/static_runtime_guard.py" \
    --manifest "$MANIFEST_PATH" \
    --dependencies "$DEPENDENCY_PATH" \
    --package-dir "$SCRIPT_DIR" \
    --gate "$GATE_PATH" \
    --paw-identity "$PAW_PATH" \
    --paw-root "$PAW_ROOT" \
    --check-package \
    --check-gate \
    --check-paw
if [ "$RUNTIME_DIR" != "$BATCH_DIR/$CASE_ID" ]; then
    echo "derived runtime directory does not match delivery identity" >&2
    exit 2
fi
if [ -e "$RUNTIME_DIR" ] || [ -L "$RUNTIME_DIR" ]; then
    echo "refusing to reuse an existing case directory: $RUNTIME_DIR" >&2
    exit 2
fi
mkdir -p -- "$BATCH_DIR"
mkdir -- "$RUNTIME_DIR"
for name in POSCAR INCAR KPOINTS; do
    cp -- "$SCRIPT_DIR/$name" "$RUNTIME_DIR/$name"
done

"$VASP_PYTHON_BIN" "$SCRIPT_DIR/static_runtime_guard.py" \
    --manifest "$MANIFEST_PATH" \
    --dependencies "$DEPENDENCY_PATH" \
    --paw-identity "$PAW_PATH" \
    --paw-root "$PAW_ROOT" \
    --check-paw
: > "$RUNTIME_DIR/POTCAR"
for component in $COMPONENTS; do
    component_file="$PAW_ROOT/$component"
    cat -- "$component_file" >> "$RUNTIME_DIR/POTCAR"
done
"$VASP_PYTHON_BIN" "$SCRIPT_DIR/static_runtime_guard.py" \
    --manifest "$MANIFEST_PATH" \
    --dependencies "$DEPENDENCY_PATH" \
    --paw-identity "$PAW_PATH" \
    --combined-path "$RUNTIME_DIR/POTCAR" \
    --check-combined
"$VASP_PYTHON_BIN" "$SCRIPT_DIR/static_postcheck.py" preflight \
    --manifest "$MANIFEST_PATH" \
    --input-dir "$SCRIPT_DIR" \
    --case-dir "$RUNTIME_DIR" \
    --receipt "$RUNTIME_DIR/preflight.json"
""".encode("utf-8")


def postcheck_text() -> bytes:
    return r"""#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

toolchain_root = os.environ.get("VASP_TOOLCHAIN_ROOT")
if not toolchain_root:
    raise SystemExit("VASP_TOOLCHAIN_ROOT must be supplied by task_env.sh")
sys.path.insert(0, toolchain_root)
try:
    from vasp_executor import postcheck, preflight_inputs
except ImportError as error:
    raise SystemExit("approved vasp_executor dependency is unavailable") from error

parser = argparse.ArgumentParser(description="Static-package mechanical preflight/postcheck.")
subparsers = parser.add_subparsers(dest="mode", required=True)
for mode in ("preflight", "postcheck"):
    subparser = subparsers.add_parser(mode)
    subparser.add_argument("--manifest", required=True, type=Path)
    subparser.add_argument("--input-dir", type=Path)
    subparser.add_argument("--case-dir", required=(mode == "postcheck"), type=Path)
    subparser.add_argument("--requirements", type=Path)
    subparser.add_argument("--receipt", required=True, type=Path)
args = parser.parse_args()
if args.mode == "preflight":
    result = preflight_inputs(args.manifest, args.input_dir, args.case_dir, args.requirements)
else:
    result = postcheck(args.manifest, args.input_dir, args.case_dir, args.requirements)
receipt = {
    "schema": "vasp-static-postcheck-receipt/v1",
    "mode": args.mode,
    "passed": bool(result.get("passed")),
    "status": "PASS" if result.get("passed") else "FAIL",
    "supervisor": "VASP Sol",
    "handoff": "Mechanical input/output status is persisted for Sol review; it is not scientific acceptance.",
    "remote_behavior": "Remote shell, tmux, MPI, PAW assembly and VASP behavior are not proven by this receipt.",
    "interface": "vasp_executor.preflight_inputs/postcheck",
    "result": result,
}
args.receipt.parent.mkdir(parents=True, exist_ok=True)
args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
raise SystemExit(0 if result.get("passed") else 2)
""".encode("utf-8")


def template_bytes(name: str) -> bytes:
    mapping = {
        "task_env.sh": task_env_text,
        "run_static.sh": runner_text,
        "remote_prepare_static.sh": preparer_text,
        "static_postcheck.py": postcheck_text,
    }
    try:
        return mapping[name]()
    except KeyError as error:
        raise KeyError(f"unknown static template: {name}") from error

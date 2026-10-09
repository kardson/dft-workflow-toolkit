"""Portable generic VASP checks; deployment and private approval are unavailable."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from workflow_command_catalog import command_names, GENERIC_OPERATIONS, missing_files

TOOLS = Path(__file__).resolve().parent

def workspace_path(value, root):
    path = Path(value)
    path = (path if path.is_absolute() else root / path).resolve()
    if not path.is_relative_to(root):
        raise ValueError('Path must resolve inside the workspace: ' + str(value))
    return path

def main(argv=None):
    names = command_names('generic', TOOLS)
    missing = {n: missing_files(n,TOOLS) for n in GENERIC_OPERATIONS if n not in names}
    parser = argparse.ArgumentParser(description=__doc__, epilog='Unavailable module closures: '+str(missing) if missing else None)
    parser.add_argument('--workspace', type=Path, default=Path.cwd(), help='Workspace root; defaults to current directory')
    parser.add_argument('mode', choices=names)
    args, rest = parser.parse_known_args(argv)
    try:
        root = args.workspace.resolve()
        if not root.is_dir(): raise ValueError('Workspace directory is unavailable')
        if args.mode == 'capabilities':
            if rest: parser.error('unrecognized arguments: '+' '.join(rest))
            from agent_capabilities import catalog
            print(json.dumps(catalog(scope='generic',tools_dir=TOOLS),indent=2))
            return 0
        if args.mode == 'validate-bundle':
            from approved_bundle_validation import main as validate
            return validate(['--workspace',str(root),*rest])
        if args.mode in ('preflight','postcheck'):
            from vasp_executor_core import build_parser, main as check
            parsed = build_parser().parse_args([args.mode,*rest])
            bound = [args.mode]
            for key in ('manifest','input_dir','case_dir','requirements'):
                value = getattr(parsed,key)
                if value is not None:
                    bound.extend(['--'+key.replace('_','-'),str(workspace_path(value,root))])
            return check(bound)
        if args.mode == 'evidence':
            # Parse the delegated contract without invoking its read/write seam.
            from agent_capabilities import parser_arguments
            delegated = argparse.ArgumentParser(prog='vasp_core_cli.py evidence')
            for option in parser_arguments(TOOLS/'tool_evidence.py'):
                delegated.add_argument(*option['names'],required=option['required'])
            parsed = delegated.parse_args(rest)
            bound=['--receipt',str(workspace_path(parsed.receipt,root))]
            if parsed.output is not None: bound.extend(['--output',str(workspace_path(parsed.output,root))])
            from tool_evidence import main as evidence
            return evidence(bound)
    except (OSError, ValueError, ImportError, AttributeError, NameError) as error:
        parser.exit(2,f'generic workflow stopped: {type(error).__name__}: {error}\n')
    raise AssertionError('Unregistered command')

if __name__ == '__main__':
    raise SystemExit(main())

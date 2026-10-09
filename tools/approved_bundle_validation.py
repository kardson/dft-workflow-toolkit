"""Complete approved-bundle envelope validation, independent of deployment backends."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
try:
    from vasp_profile_policy import GENERIC_PROFILE_POLICY
except ImportError:
    if not __package__: raise
    from .vasp_profile_policy import GENERIC_PROFILE_POLICY

TEMPLATES = {'static', 'atomref', 'relax_fresh', 'relax_warm', 'performance_diagnostic_warm'}

def validate_bundle(bundle, *, workspace=None, profile_policy=GENERIC_PROFILE_POLICY):
    if bundle.get('schema') != 'vasp-approved-bundle/v1':
        raise ValueError('Unsupported approved bundle schema')
    template = bundle.get('template')
    if template not in TEMPLATES:
        raise ValueError('Explicit approved template is required')
    inputs = bundle.get('inputs')
    if not isinstance(inputs, dict) or inputs.get('route') != 'vasp':
        raise ValueError('Approved VASP inputs are required')
    incar = inputs.get('incar', {})
    if not profile_policy.allow_private_fields and (any(k in inputs for k in ('performance_diagnostic', 'warm_rmm_relaxation')) or incar.get('LREAL') == 'Auto'):
        raise ValueError('Private approval fields require an explicit profile policy; no execution authority is inferred')
    if template in {'static', 'atomref'}:
        if 'delivery_identity' not in inputs or incar.get('NSW') != 0:
            raise ValueError('Static/atomref template needs static delivery and NSW=0')
        source_kind = inputs['delivery_identity'].get('source', {}).get('kind')
        if template == 'atomref' and source_kind != 'approved_isolated_atom_spec':
            raise ValueError('Atom reference requires an approved isolated atom geometry')
        if template == 'static' and source_kind == 'approved_isolated_atom_spec':
            raise ValueError('Isolated atom geometry must use atomref template')
        if bundle.get('execution') is not None or bundle.get('requirements') is not None:
            raise ValueError('Static delivery cannot silently inherit execution permission')
    elif template == 'performance_diagnostic_warm':
        if profile_policy.diagnostic_errors(inputs) or not profile_policy.is_diagnostic(inputs):
            raise ValueError('Performance diagnostic template requires the exact locked diagnostic input contract')
        if 'delivery_identity' in inputs or not isinstance(bundle.get('execution'), dict) or not isinstance(bundle.get('requirements'), dict):
            raise ValueError('Performance diagnostic requires explicit execution and output contracts')
        restart = inputs.get('restart', {})
        if restart.get('fresh') is not False or restart.get('mode') != 'wavefunction_and_charge_scf':
            raise ValueError('Performance diagnostic requires the explicit warm WAVECAR+CHGCAR contract')
    else:
        if 'delivery_identity' in inputs or incar.get('ISIF') != 2 or type(incar.get('IBRION')) is not int or incar.get('IBRION') not in (1, 2) or type(incar.get('NSW')) is not int or incar.get('NSW', 0) <= 0:
            raise ValueError('Relaxation template must be fixed-cell ionic relaxation')
        if not isinstance(bundle.get('execution'), dict) or not isinstance(bundle.get('requirements'), dict):
            raise ValueError('Relaxation requires execution and output contracts')
        restart = inputs.get('restart', {})
        if template == 'relax_fresh' and restart.get('fresh') is not True:
            raise ValueError('Fresh relaxation requires an explicit fresh contract')
        if template == 'relax_warm' and restart.get('fresh') is not False:
            raise ValueError('Warm relaxation requires an explicit restart contract')
    source_dir = bundle.get('source_dir')
    if source_dir is None:
        source_kind = inputs.get('delivery_identity', {}).get('source', {}).get('kind')
        if template != 'atomref' or source_kind != 'approved_isolated_atom_spec':
            raise ValueError('Explicit existing source directory is required')
    else:
        if not isinstance(source_dir, str):
            raise ValueError('Explicit existing source directory is required')
        source = Path(source_dir)
        if workspace is not None:
            root = Path(workspace).resolve()
            if not root.is_dir():
                raise ValueError('Workspace directory is unavailable')
            source = (source if source.is_absolute() else root / source).resolve()
            if not source.is_relative_to(root):
                raise ValueError('Source directory must resolve inside the workspace')
        if not source.is_dir():
            raise ValueError('Explicit existing source directory is required')
    return template



def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True)
    parser.add_argument('--workspace', type=Path, default=Path.cwd(), help='Explicit workspace; defaults to current directory')
    args = parser.parse_args(argv)
    try:
        root = args.workspace.resolve()
        source = Path(args.bundle)
        source = (source if source.is_absolute() else root / source).resolve()
        if not source.is_relative_to(root):
            raise ValueError('Bundle must resolve inside the workspace')
        bundle = json.loads(source.read_text(encoding='utf-8-sig'))
        template = validate_bundle(bundle, workspace=root)
    except (OSError, ValueError) as error:
        parser.exit(2, f'validate-bundle stopped: {type(error).__name__}: {error}\n')
    print(json.dumps({'schema': bundle['schema'], 'template': template, 'execution_authorized': False}))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())

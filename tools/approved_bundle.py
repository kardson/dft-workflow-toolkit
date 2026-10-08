"""Generated approved-contract validator; no preparation backend."""
from pathlib import Path
TEMPLATES = {'static', 'atomref', 'relax_fresh', 'relax_warm'}
def validate_bundle(bundle):
    if bundle.get('schema') != 'vasp-approved-bundle/v1':
        raise ValueError('Unsupported approved bundle schema')
    template = bundle.get('template')
    if template not in TEMPLATES:
        raise ValueError('Explicit approved template is required')
    inputs = bundle.get('inputs')
    if not isinstance(inputs, dict) or inputs.get('route') != 'vasp':
        raise ValueError('Approved VASP inputs are required')
    incar = inputs.get('incar', {})
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
    elif not isinstance(source_dir, str) or not Path(source_dir).is_dir():
        raise ValueError('Explicit existing source directory is required')
    return template

"""Explicit, offline geometry and fixed-geometry energy comparisons."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

import numpy as np

from practical_geometry import inspect_geometry
from progress_snapshot import parse_poscar


def _geometry(path):
    path = Path(path).resolve()
    inspect_geometry(path)
    parsed = parse_poscar(path.read_text(), path)
    cell = np.asarray(parsed['lattice'], dtype=float)
    coords = np.asarray([atom['coordinates'] for atom in parsed['atoms']], dtype=float)
    if parsed['coordinate_mode'].lower().startswith('d'):
        fractional = coords
    else:
        fractional = coords @ np.linalg.inv(cell)
    labels = [atom['species'] for atom in parsed['atoms']]
    masks = [atom['flags'] for atom in parsed['atoms']]
    return path, parsed, cell, fractional, labels, masks


def geometry_comparison(initial, final, pairs, baseline_indices, target_indices, normal, vasprun=None):
    if not baseline_indices or not target_indices:
        raise ValueError('Explicit baseline and target indices are required')
    initial_path, a, cell_a, frac_a, labels_a, masks_a = _geometry(initial)
    final_path, b, cell_b, frac_b, labels_b, masks_b = _geometry(final)
    if labels_a != labels_b or masks_a != masks_b or not np.allclose(cell_a, cell_b, atol=1e-6):
        raise ValueError('Species order, selective dynamics or cell differ')
    n = len(labels_a)
    indices = set(baseline_indices) | set(target_indices) | {x for pair in pairs for x in pair}
    if not indices or any(not isinstance(i, int) or i < 1 or i > n for i in indices):
        raise ValueError('Explicit 1-based atom indices must be in range')
    axis = np.asarray(normal, dtype=float)
    if axis.shape != (3,) or not np.isfinite(axis).all() or np.linalg.norm(axis) == 0:
        raise ValueError('Explicit finite surface normal is required')
    axis /= np.linalg.norm(axis)
    delta_frac = frac_b - frac_a
    delta_frac -= np.round(delta_frac)
    displacement = delta_frac @ cell_a
    anchor = np.mean(displacement[np.asarray(baseline_indices) - 1], axis=0)
    final_cart = frac_b @ cell_b
    baseline_z = float(np.mean(final_cart[np.asarray(baseline_indices) - 1] @ axis))
    from ase import Atoms
    atoms = Atoms(symbols=[('H' if re.fullmatch(r'H(?:\.\d+|\d+(?:\.\d+)?)', label) else label) for label in labels_b],
                  scaled_positions=frac_b, cell=cell_b, pbc=True)
    distances = [{'pair_1based': list(pair), 'labels': [labels_b[pair[0]-1], labels_b[pair[1]-1]],
                  'minimum_image_A': float(atoms.get_distance(pair[0]-1, pair[1]-1, mic=True)),
                  'same_cell_A': float(atoms.get_distance(pair[0]-1, pair[1]-1, mic=False))}
                 for pair in pairs]
    report = {'schema': 'vasp-surface-geometry/v1', 'initial': str(initial_path), 'final': str(final_path),
            'normal_cartesian': axis.tolist(), 'periodic_displacement': True,
            'rigid_shift_anchor_A': anchor.tolist(), 'baseline_indices_1based': baseline_indices,
            'target_heights_above_baseline_A': {str(i): float(final_cart[i-1] @ axis - baseline_z)
                                                 for i in target_indices},
            'target_displacements_after_anchor_A': {str(i): (displacement[i-1]-anchor).tolist()
                                                      for i in target_indices},
            'pairs': distances, 'labels_preserved': True,
            'scientific_acceptance': 'NOT_EVALUATED'}
    if vasprun is not None:
        from pymatgen.io.vasp.outputs import Vasprun
        run = Vasprun(vasprun, parse_dos=False, parse_eigen=False, parse_potcar_file=False)
        if len(run.final_structure) != n or list(run.atomic_symbols) != atoms.get_chemical_symbols():
            raise ValueError('Vasprun species/order differs from compared POSCAR')
        if not run.ionic_steps or 'forces' not in run.ionic_steps[-1]:
            raise ValueError('Vasprun final force block is missing')
        forces = np.asarray(run.ionic_steps[-1]['forces'], dtype=float)
        if forces.shape != (n, 3) or not np.isfinite(forces).all():
            raise ValueError('Final force array is invalid')
        free = forces * np.asarray(masks_b, dtype=bool)
        report['force_metrics'] = {
            'source': str(Path(vasprun).resolve()),
            'max_all_eV_A': float(np.linalg.norm(forces, axis=1).max()),
            'max_free_eV_A': float(np.linalg.norm(free, axis=1).max()),
            'targets': {str(i): {'raw_eV_A': forces[i-1].tolist(),
                                 'free_eV_A': free[i-1].tolist()} for i in target_indices}}
    return report


def reference_energy(contract):
    """Combine explicit E0 records; never promote fixed geometry to adsorption energy."""
    if contract.get('schema') != 'vasp-fixed-geometry-reference/v1' or contract.get('energy_basis') != 'E0':
        raise ValueError('Explicit fixed-geometry E0 contract required')
    if contract.get('interpretation') != 'FIXED_GEOMETRY_INTERACTION':
        raise ValueError('Unsupported reference interpretation')
    records = {}
    manifests = {}
    for role in ('combined', 'clean', 'atom'):
        source = contract.get('sources', {}).get(role)
        if not isinstance(source, dict) or not all(source.get(key) for key in
            ('record_path', 'execution_id', 'input_manifest_path', 'status_path', 'status_case_path')):
            raise ValueError(f'Explicit {role} result identity required')
        record = json.loads(Path(source['record_path']).read_text(encoding='utf-8'))
        if record.get('schema') != 'vasp-result-record/v1' or record.get('execution_id') != source['execution_id']:
            raise ValueError(f'{role} result identity differs')
        if record.get('result_state') != 'COMPLETE_XML' or record.get('energy', {}).get('E0_eV') is None:
            raise ValueError(f'{role} lacks complete E0 evidence')
        status = json.loads(Path(source['status_path']).read_text(encoding='utf-8'))
        if status.get('execution_id') != source['execution_id'] or not any(
            case.get('path') == source['status_case_path'] for case in status.get('cases', [])):
            raise ValueError(f'{role} result does not match run status identity')
        def return_prefix(path):
            parts = Path(path).resolve().parts
            marker = next((i for i, part in enumerate(parts) if part.lower() == 'returns'), None)
            return parts[:marker + 2] if marker is not None and marker + 1 < len(parts) else None
        if not return_prefix(record.get('source', '')) or return_prefix(record['source']) != return_prefix(source['status_path']):
            raise ValueError(f'{role} status and result come from different returned runs')
        records[role] = record
        manifest = json.loads(Path(source['input_manifest_path']).read_text(encoding='utf-8-sig'))
        if manifest.get('route') != 'vasp' or 'paw_identity' not in manifest:
            raise ValueError(f'{role} lacks approved PAW identity')
        manifests[role] = manifest
    identities = [manifest['paw_identity'] for manifest in manifests.values()]
    if len({identity.get('paw_family') for identity in identities}) != 1 or len({identity.get('environment_id') for identity in identities}) != 1:
        raise ValueError('PAW family or environment differs')
    components = {}
    for identity in identities:
        for role in identity.get('ordered_roles', []):
            digest = identity.get(role, {}).get('component_sha256')
            if not digest or (role in components and components[role] != digest):
                raise ValueError(f'PAW component differs or is missing: {role}')
            components[role] = digest
    allowed = set(contract.get('approved_differences', []))
    if not {'atom_box', 'kpoints', 'spin'} <= allowed:
        raise ValueError('Atom box, K-point and spin differences need explicit approval')
    value = (records['combined']['energy']['E0_eV'] - records['clean']['energy']['E0_eV']
             - records['atom']['energy']['E0_eV'])
    return {'schema': 'vasp-fixed-geometry-reference-result/v1', 'interaction_E0_eV': value,
            'source_execution_ids': {role: record['execution_id'] for role, record in records.items()},
            'energy_basis': 'E0', 'interpretation': 'FIXED_GEOMETRY_INTERACTION',
            'paw_family': identities[0]['paw_family'], 'paw_components_checked': sorted(components),
            'approved_differences': sorted(allowed),
            'scientific_acceptance': 'NOT_EVALUATED'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['geometry', 'reference'])
    parser.add_argument('--initial')
    parser.add_argument('--final')
    parser.add_argument('--pair', nargs=2, type=int, action='append', default=[])
    parser.add_argument('--baseline', type=int, nargs='+')
    parser.add_argument('--target', type=int, nargs='+')
    parser.add_argument('--normal', type=float, nargs=3)
    parser.add_argument('--vasprun')
    parser.add_argument('--contract')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.mode == 'geometry':
        if not all([args.initial, args.final, args.baseline, args.target, args.normal]):
            parser.error('Geometry comparison needs two POSCARs, baseline, target and normal')
        result = geometry_comparison(args.initial, args.final, args.pair, args.baseline,
                                     args.target, args.normal, args.vasprun)
    else:
        if not args.contract:
            parser.error('Reference calculation needs an approved contract')
        result = reference_energy(json.loads(Path(args.contract).read_text(encoding='utf-8')))
    output = Path(args.output).resolve()
    if output.exists():
        raise ValueError('Output already exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()

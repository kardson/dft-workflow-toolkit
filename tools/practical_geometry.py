"""ASE/pymatgen geometry adapter that keeps VASP labels and approved POSCAR bytes."""
from __future__ import annotations
import io
import json
import re
from pathlib import Path
import numpy as np
from ase.io import read, write
from ase.io.vasp import _handle_ase_constraints
from pymatgen.io.vasp.inputs import Poscar
from progress_snapshot import parse_poscar

def inspect_geometry(path, output=None):
    path=Path(path)
    text=path.read_text()
    actual=parse_poscar(text,path)
    if not actual['valid'] or actual.get('species_unresolved'):
        raise ValueError('Geometry adapter requires valid, explicit VASP species')
    # Pseudo-H PAW labels are preserved separately. Library readers receive
    # chemical symbols only, in memory; the approved POSCAR is never rewritten.
    lines=text.splitlines()
    symbols=[]
    for label in actual['species']:
        match=re.fullmatch(r'(H)(?:\.\d+|\d+(?:\.\d+)?)?',label)
        symbols.append('H' if match else label)
    lines[5]=' '.join(symbols)
    normalized='\n'.join(lines)+'\n'
    atoms=read(io.StringIO(normalized),format='vasp')
    structure=Poscar.from_str(normalized,read_velocities=False)
    if not np.allclose(atoms.cell.array,structure.structure.lattice.matrix,atol=1e-10,rtol=0):
        raise ValueError('ASE/pymatgen lattice mismatch')
    if not np.allclose(atoms.positions,structure.structure.cart_coords,atol=1e-9,rtol=0):
        raise ValueError('ASE/pymatgen coordinate mismatch')
    masks=np.asarray([atom['flags'] for atom in actual['atoms']],dtype=bool)
    if not np.array_equal(~_handle_ase_constraints(atoms),masks):
        raise ValueError('ASE constraints differ from actual POSCAR')
    if structure.selective_dynamics is not None and not np.array_equal(structure.selective_dynamics,masks):
        raise ValueError('pymatgen constraints differ from actual POSCAR')
    labels=[atom['species'] for atom in actual['atoms']]
    atoms.new_array('vasp_label',np.asarray(labels))
    atoms.info['vasp_species_order']=actual['species']
    report=dict(schema='vasp-practical-geometry/v1',nions=len(atoms),
                chemical_symbols=atoms.get_chemical_symbols(),
                species_order=actual['species'],counts=actual['counts'],
                mask_status=actual['mask_status'],fixed_indices_1based=actual['fixed_indices_1based'],
                ase_pymatgen_agree=True,poscar_rewritten=False,pseudo_labels_preserved=True)
    if output is not None:
        output=Path(output)
        write(output/'geometry.extxyz',atoms,format='extxyz')
        (output/'geometry_inspection.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    return report

def export_frame(structure,labels,masks,path):
    """Export an inspected pymatgen frame with the original label/constraint arrays."""
    from ase import Atoms
    from ase.constraints import FixAtoms, FixScaled
    atoms=Atoms(symbols=[site.specie.symbol for site in structure],positions=structure.cart_coords,
                cell=structure.lattice.matrix,pbc=True)
    atoms.new_array('vasp_label',np.asarray(labels))
    atoms.new_array('vasp_free',np.asarray(masks,dtype=bool))
    fixed=[i for i,mask in enumerate(masks) if not any(mask)]
    constraints=[FixAtoms(indices=fixed)] if fixed else []
    constraints.extend(FixScaled(i,mask=np.logical_not(mask)) for i,mask in enumerate(masks) if any(mask) and not all(mask))
    atoms.set_constraint(constraints)
    write(path,atoms,format='extxyz')

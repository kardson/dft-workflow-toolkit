"""Opt-in offline analysis in the isolated analysis environment."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

import numpy as np


def _new_output(path, source):
    path = Path(path).resolve()
    source = Path(source).resolve()
    if path == source or source in path.parents:
        raise ValueError('Output must be outside source calculation data')
    if path.exists():
        raise ValueError('Output directory already exists')
    path.mkdir(parents=True)
    return path


def diagnose(source, output):
    """Read only the validated BRMIX detector; never invoke a corrector."""
    from diagnostic_taxonomy import diagnose_brmix

    source = Path(source).resolve()
    out = _new_output(output, source)
    report = diagnose_brmix(source)
    report['schema'] = 'vasp-readonly-diagnostic/v1'
    (out / 'diagnostic.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def macro_average(planar, output, smoothing_length_A, windows, sensitivity_lengths_A=(), *, provenance_required=True):
    """Smooth an approved VASPKIT planar table; windows remain candidates."""
    from macrodensity.averages import macroscopic_average
    planar = Path(planar).resolve()
    provenance = None
    if provenance_required:
        summary_path = planar.with_name('summary.json')
        if planar.name != 'PLANAR_AVERAGE.dat' or not summary_path.is_file():
            raise ValueError('Verified VASPKIT planar table and adjacent summary.json are required')
        provenance = json.loads(summary_path.read_text(encoding='utf-8'))
        if (provenance.get('schema') != 'vasp-planar-potential/v1'
                or not str(provenance.get('backend', '')).startswith('VASPKIT 1.5.1 task 426')
                or not isinstance(provenance.get('max_difference_eV'), (int, float))
                or not np.isfinite(provenance['max_difference_eV'])
                or not 0 <= provenance['max_difference_eV'] <= 2e-5):
            raise ValueError('Planar source has not passed VASPKIT/LOCPOT grid agreement')
    data = np.loadtxt(planar, comments='#')
    if data.ndim != 2 or data.shape[1] < 2 or len(data) < 3 or not np.isfinite(data[:, :2]).all():
        raise ValueError('Invalid planar average table')
    distance, potential = data[:, 0], data[:, 1]
    spacing = np.diff(distance)
    # VASPKIT prints four decimals, so an exactly uniform native grid has
    # neighbouring displayed spacings that differ by up to 0.0001 Å.
    if not np.all(spacing > 0) or not np.allclose(spacing, spacing.mean(), atol=1e-4, rtol=1e-4):
        raise ValueError('MacroDensity requires a uniform increasing grid')
    if not np.isfinite(smoothing_length_A) or not 2 * spacing[0] <= smoothing_length_A < distance[-1] - distance[0]:
        raise ValueError('Smoothing length must be explicit and within the sampled axis')
    smoothed = np.asarray(macroscopic_average(potential, smoothing_length_A, float(spacing.mean())))
    if smoothed.shape != potential.shape or not np.isfinite(smoothed).all():
        raise ValueError('MacroDensity returned invalid values')
    alternatives = {}
    for width in sensitivity_lengths_A:
        if not np.isfinite(width) or not 2 * spacing[0] <= width < distance[-1] - distance[0]:
            raise ValueError('Each sensitivity length must fit the sampled axis')
        value = np.asarray(macroscopic_average(potential, width, float(spacing.mean())))
        if value.shape != potential.shape or not np.isfinite(value).all():
            raise ValueError('MacroDensity sensitivity output is invalid')
        alternatives[str(width)] = value
    candidates = []
    for lo, hi in windows:
        if not np.isfinite([lo, hi]).all() or lo >= hi:
            raise ValueError('Each explicit window needs finite increasing bounds')
        mask = (distance >= lo) & (distance <= hi)
        if mask.sum() < 3:
            raise ValueError('Each candidate window needs at least three grid points')
        slope, intercept = np.polyfit(distance[mask], smoothed[mask], 1)
        candidates.append({'window_A': [lo, hi], 'points': int(mask.sum()),
                           'mean_eV': float(smoothed[mask].mean()),
                           'slope_eV_per_A': float(slope), 'intercept_eV': float(intercept),
                           'sensitivity_mean_eV': {width: float(value[mask].mean())
                                                   for width, value in alternatives.items()}})
    out = _new_output(output, planar.parent)
    np.savetxt(out / 'MACRO_AVERAGE.dat', np.column_stack((distance, smoothed)),
               header='distance_A macro_average_eV')
    report = {'schema': 'vasp-macro-average/v1', 'source': str(planar),
              'source_LOCPOT': provenance.get('source') if provenance else None,
              'potential_component': 'VASPKIT_426_LOCPOT_LOCAL_POTENTIAL_UNDECOMPOSED',
              'planar_grid_max_difference_eV': provenance.get('max_difference_eV') if provenance else None,
              'backend': 'MacroDensity 3.1.0+source.a9b56cce',
              'smoothing_length_A': smoothing_length_A, 'spacing_A': float(spacing.mean()),
              'windows': candidates, 'vacuum_level': 'NOT_SELECTED',
              'work_function': 'NOT_EVALUATED'}
    (out / 'summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def total_dos(source, output, xmin=-6, xmax=6):
    from pymatgen.io.vasp.outputs import Vasprun
    from sumo.electronic_structure.dos import load_dos
    from sumo.plotting.dos_plotter import SDOSPlotter
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    source = Path(source).resolve()
    if not source.is_file() or xmin >= xmax:
        raise ValueError('An existing vasprun.xml and increasing energy range are required')
    run = Vasprun(source, parse_potcar_file=False)
    dos, pdos = load_dos(run, total_only=True, adjust_fermi=False)
    if len(dos.energies) < 2 or not np.isfinite(dos.energies).all():
        raise ValueError('Usable total DOS data is missing')
    out = _new_output(output, source.parent)
    SDOSPlotter(dos, pdos).get_plot(xmin=xmin, xmax=xmax, zero_to_efermi=True)
    plt.savefig(out / 'total_dos.png', dpi=150)
    plt.close('all')
    report = {'schema': 'vasp-sumo-total-dos/v1', 'source': str(source),
              'grid_points': len(dos.energies), 'spin_channels': len(dos.densities),
              'energy_reference': 'E - E_F (eV)', 'density_unit': 'states/eV per VASP cell',
              'scientific_acceptance': 'NOT_EVALUATED',
              'pdos': 'NOT_AVAILABLE', 'bands': 'NOT_AVAILABLE'}
    (out / 'summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def hdf5_summary(source, output):
    """Read final static quantities with py4vasp and check same-run OUTCAR."""
    import py4vasp
    source = Path(source).resolve()
    if source.name != 'vaspout.h5' or not source.is_file():
        raise ValueError('An existing vaspout.h5 is required')
    outcar = source.with_name('OUTCAR')
    if not outcar.is_file():
        raise ValueError('Same-directory OUTCAR is required for HDF5 cross-check')
    calculation = py4vasp.Calculation.from_path(str(source.parent))
    energies = calculation.energy.read()
    structure = calculation.structure.read()
    toten = float(energies['free energy    TOTEN'])
    positions = np.asarray(structure['positions'])
    lattice = np.asarray(structure['lattice_vectors'])
    if (not np.isfinite(toten) or positions.ndim != 2 or positions.shape[1] != 3
            or len(positions) < 1 or not np.isfinite(positions).all()
            or lattice.shape != (3, 3) or not np.isfinite(lattice).all()):
        raise ValueError('Invalid HDF5 energy or structure')
    matches = re.findall(r'free  energy   TOTEN\s*=\s*([-+\d.Ee]+)',
                         outcar.read_text(encoding='utf-8', errors='replace'))
    if not matches:
        raise ValueError('OUTCAR has no TOTEN for cross-check')
    outcar_toten = float(matches[-1])
    delta = abs(toten - outcar_toten)
    if not np.isfinite(outcar_toten) or delta > 1e-5:
        raise ValueError(f'HDF5 and OUTCAR final TOTEN disagree by {delta} eV')
    report = {'schema': 'vasp-py4vasp-hdf5-summary/v1', 'source': str(source),
              'outcar': str(outcar), 'backend': 'py4vasp-core',
              'final_toten_eV': toten, 'outcar_final_toten_eV': outcar_toten,
              'toten_difference_eV': delta, 'ion_count': int(len(positions)),
              'lattice_vectors_A': lattice.tolist(),
              'scientific_acceptance': 'NOT_EVALUATED',
              'pdos': 'NOT_EVALUATED', 'bands': 'NOT_EVALUATED'}
    out = _new_output(output, source.parent)
    (out / 'summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['diagnose', 'macro', 'total-dos', 'hdf5'])
    parser.add_argument('--source', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--smoothing-length-A', type=float)
    parser.add_argument('--sensitivity-length-A', type=float, action='append', default=[])
    parser.add_argument('--window', nargs=2, type=float, action='append', default=[])
    parser.add_argument('--xmin', type=float, default=-6)
    parser.add_argument('--xmax', type=float, default=6)
    args = parser.parse_args()
    if args.mode == 'diagnose':
        result = diagnose(args.source, args.output_dir)
    elif args.mode == 'macro':
        if args.smoothing_length_A is None or not args.window:
            parser.error('Macro averaging requires an explicit smoothing length and candidate windows')
        result = macro_average(args.source, args.output_dir, args.smoothing_length_A,
                               args.window, args.sensitivity_length_A)
    elif args.mode == 'total-dos':
        result = total_dos(args.source, args.output_dir, args.xmin, args.xmax)
    else:
        result = hdf5_summary(args.source, args.output_dir)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()

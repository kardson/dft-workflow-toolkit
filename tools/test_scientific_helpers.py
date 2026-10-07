"""Small geometry boundary checks for the read-only VASP adapter."""
from pathlib import Path
import tempfile
import unittest

from scientific_helpers import geometry_comparison


POSCAR = """synthetic boundary pair
1.0
10 0 0
0 10 0
0 0 20
Si O
1 1
Selective dynamics
Direct
0.95 0.50 0.10 F F F
0.05 0.50 0.20 T T T
"""


class GeometryTests(unittest.TestCase):
    def test_periodic_pair_and_constraints(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / 'POSCAR'; first.write_text(POSCAR)
            second = root / 'CONTCAR'; second.write_text(POSCAR.replace('0.05 0.50 0.20', '0.06 0.50 0.20'))
            report = geometry_comparison(first, second, [(1, 2)], [1], [2], [0, 0, 1])
            self.assertLess(report['pairs'][0]['minimum_image_A'], report['pairs'][0]['same_cell_A'])
            self.assertEqual(report['pairs'][0]['labels'], ['Si', 'O'])
            self.assertAlmostEqual(report['target_heights_above_baseline_A']['2'], 2.0)
            with self.assertRaises(FileNotFoundError):
                geometry_comparison(first, root / 'missing', [(1, 2)], [1], [2], [0, 0, 1])


if __name__ == '__main__':
    unittest.main()

"""Backend-independent VASP schema and field contracts (single source)."""
from __future__ import annotations

SCHEMA = "vasp-executor-check/v1"
SCIENTIFIC_REVIEW = {
    "status": "NOT_EVALUATED",
    "owner": "VASP Sol",
    "reason": "Execution tooling never grants scientific acceptance or release.",
}

INCAR_FIELDS: dict[str, tuple[str, str]] = {
    "PREC": ("PREC", "text"),
    "ENCUT_eV": ("ENCUT", "float"),
    "EDIFF_eV": ("EDIFF", "float"),
    "ALGO": ("ALGO", "text"),
    "NELM": ("NELM", "int"),
    "NELMIN": ("NELMIN", "int"),
    "ISMEAR": ("ISMEAR", "int"),
    "SIGMA_eV": ("SIGMA", "float"),
    "ISPIN": ("ISPIN", "int"),
    "MAGMOM": ("MAGMOM", "text"),
    "NUPDOWN": ("NUPDOWN", "int"),
    "ISYM": ("ISYM", "int"),
    "LREAL": ("LREAL", "lreal"),
    "LASPH": ("LASPH", "bool"),
    "ADDGRID": ("ADDGRID", "bool"),
    "NBANDS": ("NBANDS", "int"),
    "ISTART": ("ISTART", "int"),
    "ICHARG": ("ICHARG", "int"),
    "IBRION": ("IBRION", "int"),
    "POTIM": ("POTIM", "float"),
    "NSW": ("NSW", "int"),
    "ISIF": ("ISIF", "int"),
    "EDIFFG_eV_per_A": ("EDIFFG", "float"),
    "LDIPOL": ("LDIPOL", "bool"),
    "IDIPOL": ("IDIPOL", "int"),
    "DIPOL": ("DIPOL", "vector"),
    "LWAVE": ("LWAVE", "bool"),
    "LCHARG": ("LCHARG", "bool"),
    "KPAR": ("KPAR", "int"),
    "NCORE": ("NCORE", "int"),
}

FORBIDDEN_FEATURE_TAGS = {
    "external_field": ("EFIELD", "EFIELD_PEAD"),
    "soc": ("LSORBIT",),
    "dispersion": ("IVDW",),
    "projection_output": ("LORBIT",),
}

STANDARD_RESTART_FILES = ("WAVECAR", "CHGCAR", "CHG", "TMPCAR")
WARM_RESTART_MODE = "wavefunction_and_charge_scf"
WARM_RESTART_COMPATIBILITY_KEYS = frozenset(
    {"geometry", "environment", "paw", "encut", "kpoints", "nbands", "spin"}
)
GEOMETRY_SOURCE_KEYS = frozenset({"parent_repo_path", "parent_sha256", "source_kind", "provenance"})
WARM_RESTART_SOURCE_KEYS = frozenset(
    {
        "unit_id",
        "task_id",
        "case_id",
        "case_path",
        "poscar_sha256",
        "environment_id",
        "paw_family",
        "ordered_paw_roles",
        "combined_potcar_sha256",
        "encut_eV",
        "kpoints",
        "nbands",
        "ispin",
        "magmom",
        "nupdown",
        "compatibility",
        "files",
        "copy_policy",
        "remote_preflight_state",
    }
)
WARM_RESTART_FILE_KEYS = frozenset(
    {
        "source_path",
        "source_postcheck_nonempty",
        "source_size_bytes",
        "source_observed_utc",
        "local_state",
        "target_preflight_state",
    }
)
RESTART_SPEC_KEYS = frozenset(
    {"ISTART", "ICHARG", "fresh", "restart_files_present", "potcar_present", "mode", "source"}
)
INPUT_FILES = ("POSCAR", "INCAR", "KPOINTS")
STANDARD_OUTPUT_FILES = (
    "OUTCAR", "OSZICAR", "vasprun.xml", "CONTCAR", "vasp.stdout", "vasp.stderr",
    "IBZKPT", "EIGENVAL", "XDATCAR", "DOSCAR", "PROCAR", "LOCPOT", "ELFCAR",
    "PARCHG", "vaspout.h5", "run_timing.txt", "status.json",
)
EXPLICIT_FALSE_TAGS = frozenset({"LSORBIT", "LNONCOLLINEAR", "LDAU", "LHFCALC"})
APPROVED_INCAR_TAGS = frozenset({tag for tag, _ in INCAR_FIELDS.values()} | {"SYSTEM"})


INCAR_RENDER_FIELDS = tuple((name, tag, kind) for name, (tag, kind) in INCAR_FIELDS.items() if name not in {"KPAR", "NCORE"})

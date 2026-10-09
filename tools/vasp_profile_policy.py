"""Explicit profile policy contract; generic use cannot enable private cases."""
from __future__ import annotations

class GenericProfilePolicy:
    allow_private_fields = False
    def is_diagnostic(self, manifest): return False
    def is_rmm_diagnostic(self, manifest): return False
    def is_warm_relaxation(self, manifest): return False
    def diagnostic_errors(self, manifest):
        if "warm_rmm_relaxation" in manifest:
            return [{"code": "INVALID_APPROVED_WARM_RMM_RELAXATION", "message": "Private warm approval requires an explicitly supplied policy."}]
        if "performance_diagnostic" in manifest:
            return [{"code": "INVALID_PERFORMANCE_DIAGNOSTIC_SPEC", "message": "Private diagnostic approval requires an explicitly supplied policy."}]
        incar = manifest.get("incar")
        if isinstance(incar, dict) and incar.get("LREAL") == "Auto":
            return [{"code": "LREAL_AUTO_REQUIRES_PERFORMANCE_DIAGNOSTIC", "message": "LREAL=Auto requires an explicitly supplied approved profile policy."}]
        return []

GENERIC_PROFILE_POLICY = GenericProfilePolicy()

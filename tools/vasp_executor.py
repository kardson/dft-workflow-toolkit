"""Public compatibility name for the generic executor; no private policy is loaded."""
import vasp_executor_core as _core
globals().update({name: value for name, value in vars(_core).items() if not name.startswith("__")})
if __name__ == "__main__":
    raise SystemExit(_core.main())

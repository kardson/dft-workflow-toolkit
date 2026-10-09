"""Public compatibility import for pure approved-bundle validation only."""
from approved_bundle_validation import TEMPLATES, validate_bundle, main
if __name__ == '__main__':
    raise SystemExit(main())

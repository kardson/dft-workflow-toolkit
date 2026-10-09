# Clean science environment recipe

Scope: the reviewed O2 public candidate on Windows AMD64 with Python 3.12.14. These are observed baseline constraints, not universal minimum versions. Acquire Python independently. Create a new environment without system site packages; preserve an existing directory and choose a new suffix rather than overwriting it.

Run from the public toolkit repository root. The constraints and resolved dependency snapshot are at that root. Create a fresh trial directory beside the toolkit checkout so environments and temporary results remain outside the public file allowlist. Commands still run from the toolkit root:

```powershell
$trialRoot = Join-Path (Split-Path (Get-Location).Path -Parent) 'toolkit-clean-trial'
if (Test-Path -LiteralPath $trialRoot) { throw 'Choose a fresh trial directory; preserve existing work.' }
New-Item -ItemType Directory -Path (Join-Path $trialRoot 'temp') -Force | Out-Null
$env:TEMP = (Resolve-Path (Join-Path $trialRoot 'temp')).Path
$env:TMP = $env:TEMP
python -m venv (Join-Path $trialRoot 'clean-science')
$trialPython = Join-Path $trialRoot 'clean-science/Scripts/python.exe'
$env:PIP_CONFIG_FILE = 'NUL'
& $trialPython -E -s -m pip --isolated install --index-url https://pypi.org/simple --no-input --no-cache-dir -c constraints-science.txt -r requirements-science.txt
& $trialPython -E -s -m pip --isolated check
& $trialPython -E -s -B -m unittest discover -v -s tools -p 'test_*.py'
& $trialPython -E -s -B -m unittest discover -v -s tests -p test_generic_cli_integration.py
```

For the exact observed dependency environment, install `requirements-science-windows-py312.lock.txt` rather than resolving only the direct constraints. It includes pip as the observed installer version. No private index, authentication configuration, PYTHONPATH or user-site is required. Linux/macOS are not validated by this Windows recipe.

`docs/compatibility-clean-science.json` is the machine-readable interface for software/support tables: runtime versions, source identity, check exits/evidence, isolation assertions, validation scope and remaining gaps. Test reuse keys include relevant source and test identity, Python, platform, relevant resolved dependency versions and test configuration. Pure wording changes do not invalidate unrelated tests. Source requirements, installed versions, synthetic verification and real execution evidence are separate categories; this receipt supplies only installation and synthetic verification.



The evidence log filenames in the compatibility JSON refer to retained private validation receipts. They are not public downloadable logs. Virtual environments, installation/test logs, verification driver, synthetic structures and geometry records are excluded from this repository.

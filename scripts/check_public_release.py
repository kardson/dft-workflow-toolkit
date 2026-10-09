"""Check an explicit public file allowlist, package identity and common leak markers.

This local check supports human review; it cannot identify every unpublished idea.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

_scan_spec = importlib.util.spec_from_file_location('shared_credential_scan', Path(__file__).with_name('credential_scan.py'))
credential_scan = importlib.util.module_from_spec(_scan_spec)
_scan_spec.loader.exec_module(credential_scan)
RULES = (
    ('private_network_address', r'\b(?:10\.(?:\d{1,3}\.){2}\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b'),
    ('personal_windows_path', r'(?i)[A-Z]:[/\\]Users[/\\][^\s"\'<>]+'),
    ('paper_identifier', r'(?i)\b10\.\d{4,9}/[^\s"\'<>]+|\barxiv:\s*\d{4}\.\d{4,5}\b'),
)
FORBIDDEN_NAMES = {'POTCAR', 'POSCAR', 'CONTCAR', 'INCAR', 'KPOINTS', 'OUTCAR',
                   'OSZICAR', 'WAVECAR', 'CHGCAR', 'LOCPOT', 'vasprun.xml',
                   'vaspout.h5', '.env', '.gitmodules'}
FORBIDDEN_SUFFIXES = {'.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx',
                      '.cif', '.xsd', '.cell', '.param', '.castep', '.bib',
                      '.zip', '.7z', '.tar', '.gz', '.sqlite', '.db'}
OPTIONAL_IMPORTS = {'numpy', 'ase', 'pymatgen', 'matplotlib', 'custodian',
                    'py4vasp', 'sumo', 'macrodensity'}


def private_match_exempt(text, relative, rule, match, policy):
    """Exact reviewed protection literals only; never waive credentials or files."""
    run_directory = '_'.join(('04', 'runs'))
    model_directory = '_'.join(('03', 'models'))
    scopes = {'tools/evidence_verifier.py': ('write_receipt', {run_directory, model_directory}),
              'tools/test_evidence_verifier.py': ('test_paths_and_output_protection',
                                                {run_directory, run_directory + '/result.json'})}
    if rule != 'private_route_paths' or relative not in scopes or not policy:
        return False
    function, literals = scopes[relative]
    entries = policy.get('semantic_exceptions', [])
    identity = hashlib.sha256(text.encode('utf-8')).hexdigest()
    approved = any(e == {'path': relative, 'rule': rule, 'function': function,
                         'allowed_literals': sorted(literals), 'sha256': identity,
                         'purpose': 'calculation_directory_protection'} for e in entries)
    if not approved:
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    lines = text.splitlines(keepends=True)
    def offset(line, column):
        return sum(len(s) for s in lines[:line - 1]) + len(lines[line - 1].encode('utf-8')[:column].decode('utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function:
            for literal in ast.walk(node):
                if isinstance(literal, ast.Constant) and type(literal.value) is str and literal.value in literals:
                    if (offset(literal.lineno, literal.col_offset) <= match.start()
                            and match.end() <= offset(literal.end_lineno, literal.end_col_offset)):
                        return True
    return False


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                            text=True, encoding='utf-8', timeout=30)
    if result.returncode:
        raise ValueError(result.stderr.strip())
    return result.stdout.strip()


def git_blob(root, object_name):
    result = subprocess.run(['git', '-C', str(root), 'show', object_name],
                            capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError(result.stderr.decode('utf-8', errors='replace'))
    return result.stdout


def check(root: Path, private_policy=None, require_repository=False, *, check_index=True):
    root = root.resolve()
    errors = []
    manifest_path = root / 'release_manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('schema') != 'public-release-allowlist/v1':
        raise ValueError('Unsupported release manifest')
    entries = manifest['files']
    expected = {}
    for item in entries:
        relative = item['path']
        posix = PurePosixPath(relative)
        if (posix.is_absolute() or '..' in posix.parts or '\\' in relative
                or not relative or posix.as_posix() != relative or posix.parts[0] == '.git'):
            raise ValueError('Unsafe public manifest path')
        if relative in expected:
            raise ValueError('Duplicate public manifest path')
        expected[relative] = item['sha256']
    expected_paths = set(expected) | {'release_manifest.json'}
    actual = set()
    rules = list(RULES)
    policy = None
    if private_policy:
        policy = json.loads(Path(private_policy).read_text(encoding='utf-8'))
        rules += [(r['id'], r['regex']) for r in policy['patterns']]
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix()
        if relative == '.git' or relative.startswith('.git/'):
            continue
        if path.is_symlink():
            errors.append(f'Symlink forbidden: {relative}')
            continue
        # Refuse Windows junctions and other reparse points too.
        attributes = getattr(path.lstat(), 'st_file_attributes', 0)
        if attributes & 0x400:
            errors.append(f'Reparse point forbidden: {relative}')
            continue
        if not path.is_file():
            continue
        actual.add(relative)
        if path.name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            errors.append(f'Forbidden research/data file: {relative}')
        raw = path.read_bytes()
        for hit in credential_scan.scan_bytes(raw, relative):
            errors.append(f'Credential marker {hit["type"]}: {hit["path"]} ({hit["state"]})')
        if relative in expected and hashlib.sha256(raw).hexdigest() != expected[relative]:
            errors.append(f'Changed after manifest review: {relative}')
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            errors.append(f'Unexpected binary file: {relative}')
            continue
        for rule, pattern in rules:
            if any(not private_match_exempt(text, relative, rule, match, policy)
                   for match in re.finditer(pattern, text, re.I)):
                errors.append(f'Leak marker {rule}: {relative}')
        if path.suffix == '.py':
            try:
                tree = ast.parse(text, filename=relative)
            except SyntaxError as error:
                errors.append(f'Python syntax error: {relative}:{error.lineno}')
                continue
            if relative.startswith('tools/'):
                local = {PurePosixPath(p).stem for p in expected if p.startswith('tools/') and p.endswith('.py')}
                for node in ast.walk(tree):
                    modules = ([a.name for a in node.names] if isinstance(node, ast.Import)
                               else [node.module or ''] if isinstance(node, ast.ImportFrom) else [])
                    for module in modules:
                        top = module.split('.')[0]
                        if top and top not in sys.stdlib_module_names | OPTIONAL_IMPORTS | local:
                            errors.append(f'Unlisted import {module}: {relative}')
    errors += [f'Unlisted file: {p}' for p in sorted(actual - expected_paths)]
    errors += [f'Missing file: {p}' for p in sorted(expected_paths - actual)]
    repository = (root / '.git').is_dir()
    history_commits = None
    if repository:
        if Path(git(root, 'rev-parse', '--show-toplevel')).resolve() != root:
            errors.append('Public directory is not its own repository root')
        commits = git(root, 'rev-list', '--all').splitlines()
        history_commits = len(commits)
        # Preparation starts with zero commits. A release may only grow a clean,
        # independent history; inspect each commit tree, not only the latest tree.
        for commit in commits:
            metadata = git(root, 'show', '-s', '--format=fuller', commit)
            for hit in credential_scan.scan_bytes(metadata.encode(), 'commit metadata'):
                errors.append(f'Credential marker {hit["type"]} in Git commit metadata')
            for rule, pattern in rules:
                if re.search(pattern, metadata, re.I):
                    errors.append(f'Leak marker {rule} in Git commit metadata')
            historical_paths = set(git(root, 'ls-tree', '-r', '--name-only', commit).splitlines())
            if historical_paths - expected_paths:
                errors.append('Unlisted paths in public Git history')
            for relative in historical_paths:
                historical_raw = git_blob(root, f'{commit}:{relative}')
                for hit in credential_scan.scan_bytes(historical_raw, relative):
                    errors.append(f'Credential marker {hit["type"]} in public Git history: {hit["path"]}')
                historical = historical_raw.decode('utf-8')
                for rule, pattern in rules:
                    if any(not private_match_exempt(historical, relative, rule, match, policy)
                           for match in re.finditer(pattern, historical, re.I)):
                        errors.append(f'Leak marker {rule} in public Git history: {relative}')
        # The index may differ from both working files and commit history.
        for relative in (git(root, 'ls-files').splitlines() if check_index else []):
            if relative not in expected_paths:
                errors.append(f'Unlisted staged/tracked path: {relative}')
            indexed_raw = git_blob(root, f':{relative}')
            for hit in credential_scan.scan_bytes(indexed_raw, relative):
                errors.append(f'Credential marker {hit["type"]} in Git index: {hit["path"]}')
            indexed = indexed_raw.decode('utf-8')
            for rule, pattern in rules:
                if any(not private_match_exempt(indexed, relative, rule, match, policy)
                       for match in re.finditer(pattern, indexed, re.I)):
                    errors.append(f'Leak marker {rule} in Git index: {relative}')
            if relative in expected and hashlib.sha256(indexed_raw).hexdigest() != expected[relative]:
                # Git may normalize Windows CRLF to LF while staging. Permit
                # only that transformation against the already checked working file.
                reviewed = (root / relative).read_bytes() if relative in actual else b''
                if indexed_raw.replace(b'\r\n', b'\n') != reviewed.replace(b'\r\n', b'\n'):
                    errors.append(f'Index content differs from reviewed manifest: {relative}')
    elif require_repository:
        errors.append('An independent public Git repository is required')
    return {'passed': not errors, 'checked_files': len(actual),
            'independent_repository': repository, 'history_commits': history_commits,
            'errors': sorted({credential_scan.redact(error) for error in errors}),
            'scope': 'Allowlist, integrity, syntax, imports and known leak markers; human semantic review remains required'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--private-policy', type=Path)
    parser.add_argument('--require-repository', action='store_true')
    args = parser.parse_args()
    try:
        result = check(args.root, args.private_policy, args.require_repository)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        result = {'passed': False, 'errors': [credential_scan.redact(error)]}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

"""Check an explicit public file allowlist, package identity and common leak markers.

This local check supports human review; it cannot identify every unpublished idea.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

RULES = (
    ('private_key', r'-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----'),
    ('github_token', r'\bgh[pousr]_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{30,}\b'),
    ('cloud_key', r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    ('credential_assignment', r'''(?i)(?:password|api_key|access_token)\s*[=:]\s*["'][^"'\s]{8,}["']'''),
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
        if relative in expected and hashlib.sha256(raw).hexdigest() != expected[relative]:
            errors.append(f'Changed after manifest review: {relative}')
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            errors.append(f'Unexpected binary file: {relative}')
            continue
        for rule, pattern in rules:
            if re.search(pattern, text, re.I):
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
            for rule, pattern in rules:
                if re.search(pattern, metadata, re.I):
                    errors.append(f'Leak marker {rule} in Git commit metadata')
            historical_paths = set(git(root, 'ls-tree', '-r', '--name-only', commit).splitlines())
            if historical_paths - expected_paths:
                errors.append('Unlisted paths in public Git history')
            for relative in historical_paths:
                historical = git_blob(root, f'{commit}:{relative}').decode('utf-8')
                for rule, pattern in rules:
                    if re.search(pattern, historical, re.I):
                        errors.append(f'Leak marker {rule} in public Git history: {relative}')
        # The index may differ from both working files and commit history.
        for relative in (git(root, 'ls-files').splitlines() if check_index else []):
            if relative not in expected_paths:
                errors.append(f'Unlisted staged/tracked path: {relative}')
            indexed_raw = git_blob(root, f':{relative}')
            indexed = indexed_raw.decode('utf-8')
            for rule, pattern in rules:
                if re.search(pattern, indexed, re.I):
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
            'errors': sorted(set(errors)),
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
        result = {'passed': False, 'errors': [str(error)]}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

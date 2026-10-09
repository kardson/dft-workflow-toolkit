"""Bounded credential-pattern inspection; results never include matched values.

This scans known byte/text patterns, including UTF-16 text and supported archives.
It is not a document parser or a proof that arbitrary secrets are absent.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
import zipfile

RULES = (
    ('private_key', r'-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----'),
    ('github_token', r'\bgh[pousr]_[A-Za-z0-9]{20,255}\b|\bgithub_pat_[A-Za-z0-9_]{30,255}\b'),
    ('cloud_key', r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    ('provider_token', r'\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{20,255}|AIza[A-Za-z0-9_-]{35}|hf_[A-Za-z0-9]{25,255}|glpat-[A-Za-z0-9_-]{20,255}|xox[baprs]-[A-Za-z0-9-]{20,255})'),
    ('credential_assignment', r'''(?im)["']?(?:password|passwd|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|secret[_-]?key|aws_secret_access_key)["']?\s*[=:]\s*(?:["'][^"'\r\n]{8,512}["']|[A-Za-z0-9_/-]{20,255}(?:\s|$))'''),
    ('url_password', r'\b(?:https?|ssh|ftp)://[^\s/@:]{1,128}:[^\s/@]{4,255}@'),
    ('bearer_token', r'(?i)\bBearer\s+[A-Za-z0-9_.-]{20,512}'),
)
COMPILED = [(name, re.compile(pattern.encode())) for name, pattern in RULES]
PREFIXES = tuple(s.encode() for s in ('private key', 'ghp_', 'gho_', 'ghu_', 'ghs_',
    'ghr_', 'github_pat_', 'akia', 'asia', 'sk-', 'aiza', 'hf_', 'glpat-', 'xox',
    'password', 'passwd', 'api', 'token', 'secret', '://', 'bearer'))
AUTH_NAMES = {'.netrc', '_netrc', 'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519',
              'credentials', 'credentials.json', 'auth.json'}
AUTH_SUFFIXES = {'.pem', '.key', '.p12', '.pfx', '.jks', '.keystore'}
ARCHIVE_SUFFIXES = {'.zip', '.gz', '.tgz', '.tar', '.bz2', '.xz', '.7z', '.rar'}
DEFAULT_LIMITS = {'depth': 4, 'members': 2000, 'member_bytes': 64 * 1024**2,
                  'expanded_bytes': 256 * 1024**2, 'archive_bytes': 128 * 1024**2}
OVERLAP = 16384
CHUNK_BYTES = 1024**2


def rule_identity():
    # Covers implementation and policy limits, not only the pattern table.
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def authentication_path(relative):
    parts = PurePosixPath(str(relative).replace('\\', '/')).parts
    name = parts[-1].lower() if parts else ''
    return (any(p.lower() in {'.ssh', '.aws'} for p in parts)
            or name in AUTH_NAMES or name == '.env' or name.startswith('.env.')
            and name != '.env.example' or Path(name).suffix in AUTH_SUFFIXES)


def markers(raw):
    found = set()
    for value in (raw, raw.replace(b'\0', b'') if b'\0' in raw else b''):
        lower = value.lower()
        if not value or not any(prefix in lower for prefix in PREFIXES):
            continue
        found.update(name for name, pattern in COMPILED if pattern.search(value))
    return sorted(found)


def _finding(kind, path, state='REJECTED'):
    return {'type': kind, 'path': redact(path), 'state': state}


def redact(value):
    safe = str(value).encode('utf-8')
    for _, pattern in COMPILED:
        safe = pattern.sub(b'<redacted>', safe)
    return safe.decode('utf-8', errors='replace')


def _text_stream(stream, path):
    tail = b''
    names = set()
    while chunk := stream.read(CHUNK_BYTES):
        names.update(markers(tail + chunk))
        tail = (tail + chunk)[-OVERLAP:]
    return [_finding(name, path) for name in sorted(names)]


def _inspect(stream, path, limits, budget, depth):
    hits = [_finding('authentication_path', path)] if authentication_path(path.split('!')[-1]) else []
    prefix = stream.read(512)
    # Recombine prefix with the remaining stream without loading ordinary files.
    class Prefixed:
        def __init__(self):
            self.prefix = prefix
        def read(self, n=-1):
            if n < 0:
                return self.prefix + stream.read()
            first, self.prefix = self.prefix[:n], self.prefix[n:]
            return first + stream.read(n - len(first))
    joined = Prefixed()
    suffix = Path(path.split('!')[-1]).suffix.lower()
    is_zip = prefix.startswith((b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08'))
    is_gzip = prefix.startswith(b'\x1f\x8b')
    is_tar = len(prefix) >= 262 and prefix[257:262] == b'ustar'
    unsupported_magic = prefix.startswith((b'7z\xbc\xaf\x27\x1c', b'Rar!\x1a\x07', b'BZh', b'\xfd7zXZ\x00'))
    archive = is_zip or is_gzip or is_tar or unsupported_magic or suffix in ARCHIVE_SUFFIXES
    if not archive:
        return hits + _text_stream(joined, path)
    if depth >= limits['depth']:
        return hits + [_finding('archive_depth_limit', path, 'UNCHECKED')]
    raw = joined.read(limits['archive_bytes'] + 1)
    if len(raw) > limits['archive_bytes']:
        return hits + [_finding('archive_size_limit', path, 'UNCHECKED')]
    def member(data, name):
        budget['members'] += 1
        budget['bytes'] += len(data)
        if (budget['members'] > limits['members'] or len(data) > limits['member_bytes']
                or budget['bytes'] > limits['expanded_bytes']):
            raise ValueError('archive_limit')
        return _inspect(io.BytesIO(data), path + '!' + name, limits, budget, depth + 1)
    try:
        if is_zip:
            with zipfile.ZipFile(io.BytesIO(raw)) as package:
                for item in package.infolist():
                    if item.is_dir():
                        continue
                    if item.flag_bits & 1:
                        hits.append(_finding('encrypted_archive_member', path + '!' + item.filename, 'UNCHECKED'))
                        continue
                    if item.file_size > limits['member_bytes']:
                        raise ValueError('archive_limit')
                    with package.open(item) as handle:
                        data = handle.read(limits['member_bytes'] + 1)
                    hits += member(data, item.filename)
        elif is_gzip:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as package:
                data = package.read(limits['member_bytes'] + 1)
            hits += member(data, 'gzip-content')
        elif is_tar or suffix == '.tar':
            with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as package:
                for item in package:
                    if item.isdir():
                        continue
                    if not item.isfile():
                        hits.append(_finding('archive_special_member', path + '!' + item.name, 'UNCHECKED'))
                        continue
                    if item.size > limits['member_bytes']:
                        raise ValueError('archive_limit')
                    with package.extractfile(item) as handle:
                        data = handle.read(limits['member_bytes'] + 1)
                    hits += member(data, item.name)
        else:
            hits.append(_finding('unsupported_archive', path, 'UNCHECKED'))
    except (ValueError, OSError, EOFError, zipfile.BadZipFile, tarfile.TarError, RuntimeError):
        # Do not echo exception text: archive names/errors can carry credentials.
        hits.append(_finding('archive_limit_or_read_error', path, 'UNCHECKED'))
    return hits


def _exceptions(hits, raw_identity, relative, exceptions):
    # Authentication paths and incomplete inspection are never waived.
    allowed = {item['rule'] for item in exceptions
               if item.get('path') == relative and item.get('sha256') == raw_identity
               and item.get('purpose') == 'synthetic_fixture'
               and item.get('reason') and item.get('rule') in dict(RULES)}
    return [hit for hit in hits if hit['state'] == 'UNCHECKED' or hit['type'] not in allowed]


def scan_bytes(raw, relative='payload', *, limits=None, exceptions=()):
    selected = dict(DEFAULT_LIMITS, **(limits or {}))
    hits = _inspect(io.BytesIO(raw), relative, selected, {'members': 0, 'bytes': 0}, 0)
    return _exceptions(hits, hashlib.sha256(raw).hexdigest() if exceptions else None,
                       relative, exceptions)


def scan_path(path, relative=None, *, limits=None, exceptions=()):
    path = Path(path)
    relative = relative or path.name
    before = path.stat()
    if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
        return [_finding('linked_source', relative, 'UNCHECKED')]
    with path.open('rb') as handle:
        hits = _inspect(handle, relative, dict(DEFAULT_LIMITS, **(limits or {})),
                        {'members': 0, 'bytes': 0}, 0)
    identity = None
    if exceptions:
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            while chunk := handle.read(CHUNK_BYTES):
                digest.update(chunk)
        identity = digest.hexdigest()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        hits.append(_finding('source_changed_during_scan', relative, 'UNCHECKED'))
    return _exceptions(hits, identity, relative, exceptions)

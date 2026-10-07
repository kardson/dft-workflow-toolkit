"""Immutable JSON result records with a disposable SQLite search index."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from contextlib import closing

TOKEN = re.compile(r'^[A-Za-z0-9_.-]+$')


def _token(value, name):
    if not isinstance(value, str) or not TOKEN.fullmatch(value) or value in {'.', '..'}:
        raise ValueError(f'Invalid {name}')
    return value


def _record(summary, execution_id, case_id, attempt, status=None, status_case_path=None):
    execution_id = _token(execution_id, 'execution_id')
    case_id = _token(case_id, 'case_id')
    if not isinstance(attempt, int) or attempt < 1:
        raise ValueError('attempt must be a positive integer')
    if summary.get('schema') != 'vasp-practical-analysis/v1':
        raise ValueError('Unsupported analysis summary')
    steps = summary.get('steps') or []
    last = steps[-1] if steps else {}
    complete = summary.get('status') == 'COMPLETE_XML'
    run_state = 'UNKNOWN'
    if status is not None:
        if status.get('execution_id') != execution_id or not status_case_path or not any(
            case.get('path') == status_case_path for case in status.get('cases', [])):
            raise ValueError('Run status identity/case does not match requested record')
        run_state = status.get('status') or 'UNKNOWN'
    return {'schema': 'vasp-result-record/v1', 'execution_id': execution_id,
            'case_id': case_id, 'attempt': attempt,
            'source': summary.get('source'), 'parser': summary.get('parser'),
            'run_state': run_state,
            'status_case_path': status_case_path,
            'result_state': 'COMPLETE_XML' if complete else 'PARTIAL',
            'electronic_converged': summary.get('electronic_converged') if complete else None,
            'ionic_converged': summary.get('ionic_converged') if complete else None,
            'scientific_acceptance': 'NOT_EVALUATED',
            'energy': {'F_eV': last.get('free_energy_eV'), 'E0_eV': last.get('energy_zero_eV'),
                       'source_file': 'vasprun.xml' if complete else ('OSZICAR' if steps else None)},
            'nions': summary.get('nions'), 'parse_error': summary.get('parse_error')}


def _canonical(record):
    return json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def _index_file(connection, path):
    record = json.loads(path.read_text(encoding='utf-8'))
    if record.get('schema') != 'vasp-result-record/v1':
        raise ValueError(f'Unsupported record: {path}')
    digest = hashlib.sha256(_canonical(record)).hexdigest()
    connection.execute('''INSERT INTO results
        (execution_id, case_id, attempt, record_path, digest, run_state, result_state,
         electronic_converged, ionic_converged, scientific_acceptance, F_eV, E0_eV)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (record['execution_id'], record['case_id'], record['attempt'], str(path.resolve()), digest,
         record['run_state'], record['result_state'], record['electronic_converged'], record['ionic_converged'],
         record['scientific_acceptance'], record['energy']['F_eV'], record['energy']['E0_eV']))


def rebuild(records_dir, index_path):
    records_dir = Path(records_dir).resolve()
    index_path = Path(index_path).resolve()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = index_path.with_suffix(index_path.suffix + '.tmp')
    if temporary.exists():
        raise ValueError('Previous incomplete index build exists')
    try:
        with closing(sqlite3.connect(temporary)) as connection:
            connection.execute('''CREATE TABLE results (
                execution_id TEXT NOT NULL, case_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                record_path TEXT NOT NULL, digest TEXT NOT NULL, run_state TEXT NOT NULL,
                result_state TEXT NOT NULL,
                electronic_converged INTEGER, ionic_converged INTEGER,
                scientific_acceptance TEXT NOT NULL, F_eV REAL, E0_eV REAL,
                PRIMARY KEY (execution_id, case_id, attempt))''')
            for path in sorted(records_dir.glob('*.json')):
                _index_file(connection, path)
            count = connection.execute('SELECT COUNT(*) FROM results').fetchone()[0]
            connection.commit()
        temporary.replace(index_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return count


def register(summary_path, records_dir, index_path, execution_id, case_id, attempt,
             status_path=None, status_case_path=None):
    summary_path = Path(summary_path).resolve()
    records_dir = Path(records_dir).resolve()
    if records_dir == summary_path.parent or summary_path.parent in records_dir.parents:
        raise ValueError('Store records outside source analysis directory')
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    if bool(status_path) != bool(status_case_path):
        raise ValueError('Status path and status case path must be supplied together')
    status = json.loads(Path(status_path).read_text(encoding='utf-8')) if status_path else None
    record = _record(summary, execution_id, case_id, attempt, status, status_case_path)
    records_dir.mkdir(parents=True, exist_ok=True)
    path = records_dir / f'{execution_id}__{case_id}__{attempt}.json'
    if path.exists():
        if _canonical(json.loads(path.read_text(encoding='utf-8'))) != _canonical(record):
            raise ValueError('Existing execution/case/attempt has different evidence')
    else:
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding='utf-8')
    count = rebuild(records_dir, index_path)
    return {'record': str(path), 'index': str(Path(index_path).resolve()), 'indexed_count': count}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['register', 'rebuild'])
    parser.add_argument('--records-dir', required=True)
    parser.add_argument('--index', required=True)
    parser.add_argument('--summary')
    parser.add_argument('--execution-id')
    parser.add_argument('--case-id')
    parser.add_argument('--attempt', type=int)
    parser.add_argument('--status')
    parser.add_argument('--status-case-path')
    args = parser.parse_args()
    if args.mode == 'register':
        result = register(args.summary, args.records_dir, args.index,
                          args.execution_id, args.case_id, args.attempt,
                          args.status, args.status_case_path)
    else:
        result = {'indexed_count': rebuild(args.records_dir, args.index)}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()

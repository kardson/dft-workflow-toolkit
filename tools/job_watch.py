"""Supervise one approved batch locally on the workstation; no AI or network polling."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from datetime import datetime, timezone
import uuid

from vasp_execution_state import (
    COMPLETED_PENDING_REVIEW,
    FAILED_OR_INCOMPLETE,
    RUNNING,
    STARTING,
    SUPERVISOR_ERROR,
)


def save(path, value, *, status_changed=True):
    value['updated_epoch'] = time.time()
    value['updated_utc'] = datetime.fromtimestamp(
        value['updated_epoch'], timezone.utc).isoformat().replace('+00:00', 'Z')
    value['record_written_utc'] = value['updated_utc']
    if status_changed:
        value['state_changed_utc'] = value['updated_utc']
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def check_case(root, case):
    directory = (root / case['path']).resolve()
    if not directory.is_relative_to(root.resolve()):
        raise ValueError('Case path escapes job directory')
    required = ['OUTCAR', 'OSZICAR', 'vasprun.xml', 'CONTCAR', 'run_timing.txt']
    missing = [name for name in required if not (directory / name).is_file()]
    if missing:
        return {'path': case['path'], 'ok': False, 'reason': 'missing: ' + ', '.join(missing)}
    timing = {}
    for line in (directory / 'run_timing.txt').read_text().splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            timing[key] = value
    normal = electronic = ionic = False
    with (directory / 'OUTCAR').open(errors='replace') as stream:
        for line in stream:
            normal |= 'General timing and accounting informations' in line
            electronic |= 'aborting loop because EDIFF is reached' in line
            ionic |= 'reached required accuracy' in line
    kind = case['kind']
    if kind not in ('static', 'relaxation'):
        raise ValueError('Case kind must be static or relaxation')
    if 'postcheck_receipt' in case:
        # New runtime packages supply the existing executor's complete result.
        # Its explicit output contract handles capped ionic observations; the
        # supervisor must not replace that policy with a convergence claim.
        name=case['postcheck_receipt']
        if name!='postcheck.json':
            raise ValueError('Unsupported executor receipt path')
        result=json.loads((directory/name).read_text(encoding='utf-8'))
        execution=json.loads((directory/'execution_receipt.json').read_text(encoding='utf-8'))
        for key in ('task_id','unit_id','execution_id'):
            if not isinstance(case.get(key),str) or execution.get(key)!=case[key]:
                raise ValueError('Executor receipt identity differs from watch configuration')
        post=result.get('postcheck',{})
        ok=(result.get('schema')=='vasp-executor-check/v1' and result.get('mode')=='execute'
            and result.get('passed') is True and post.get('passed') is True
            and post.get('pass_semantics')=='MECHANICAL_EVIDENCE_ONLY'
            and post.get('program_exit',{}).get('raw')==0 and timing.get('exit_code')=='0')
        return {'path':case['path'],'ok':ok,'exit_code':timing.get('exit_code'),
                'normal_end':normal,'electronic_marker':electronic,'ionic_marker':ionic,
                'completion_basis':'APPROVED_EXECUTOR_OUTPUT_CONTRACT',
                'scientific_acceptance':'NOT_EVALUATED'}
    ok = timing.get('exit_code') == '0' and normal and electronic
    if kind == 'relaxation':
        ok = ok and ionic
    return {'path': case['path'], 'ok': ok, 'exit_code': timing.get('exit_code'),
            'normal_end': normal, 'electronic_marker': electronic,
            'ionic_marker': ionic if kind == 'relaxation' else None}


def supervise(config_path, require_tmux=True):
    preflight_started = time.monotonic()
    config_path = Path(config_path).resolve()
    root = config_path.parent
    config = json.loads(config_path.read_text(encoding='utf-8'))
    command = config['command']
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise ValueError('command must be a nonempty argv list')
    cases = config['cases']
    if not cases or len({c['path'] for c in cases}) != len(cases):
        raise ValueError('cases must be nonempty and unique')
    for case in cases:
        if case['kind'] not in ('static', 'relaxation') or not (root / case['path']).resolve().is_relative_to(root):
            raise ValueError('Invalid case specification')
    execution_id = config.get('execution_id') or uuid.uuid4().hex
    task_id = config.get('task_id', config.get('job', root.name))
    if not isinstance(execution_id, str) or not execution_id.strip():
        raise ValueError('execution_id must be a nonempty string when supplied')
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError('task_id/job must be a nonempty string')
    if require_tmux:
        pane = os.environ.get('TMUX_PANE')
        if not os.environ.get('TMUX') or not pane:
            raise RuntimeError('Start the supervisor inside the approved tmux session')
        subprocess.run(['tmux', 'display-message', '-p', '-t', pane, '#{session_name}'], check=True)
    preflight_seconds = round(time.monotonic() - preflight_started, 3)
    state_dir = root / '.job_watch'
    state_dir.mkdir()  # Exclusive, persistent one-run lock. Never auto-delete or restart.
    started_epoch = time.time()
    state = {'job': config.get('job', root.name), 'status': STARTING,
             'task_id': task_id, 'unit_id': config.get('unit_id'),
             'execution_id': execution_id,
             'started_epoch': started_epoch,
             'started_utc': datetime.fromtimestamp(
                 started_epoch, timezone.utc).isoformat().replace('+00:00', 'Z'),
             'supervisor_pid': os.getpid(),
             'stage_seconds': {'preflight': preflight_seconds, 'runner': None,
                               'postcheck': None, 'notification_adapter': None},
             'scientific_acceptance': 'PENDING_USER_AND_SOL_REVIEW'}
    status_path = state_dir / 'status.json'
    save(status_path, state)
    code = 1
    try:
        runner_started = time.monotonic()
        with (state_dir / 'runner.log').open('xb') as log:
            process = subprocess.Popen(command, cwd=root, stdout=log, stderr=subprocess.STDOUT)
            state.update(status=RUNNING, runner_pid=process.pid)
            save(status_path, state)
            # OS wait: no agent, API, SSH polling, or token consumption.
            code = process.wait()
        state['stage_seconds']['runner'] = round(time.monotonic() - runner_started, 3)
        postcheck_started = time.monotonic()
        results = [check_case(root, case) for case in cases]
        state['stage_seconds']['postcheck'] = round(time.monotonic() - postcheck_started, 3)
        state.update(runner_exit_code=code, cases=results)
        state['status'] = (COMPLETED_PENDING_REVIEW if code == 0 and all(c['ok'] for c in results)
                           else FAILED_OR_INCOMPLETE)
    except Exception as error:
        state.update(status=SUPERVISOR_ERROR, error=str(error))
    ended_epoch = time.time()
    state['ended_epoch'] = ended_epoch
    state['ended_utc'] = datetime.fromtimestamp(
        ended_epoch, timezone.utc).isoformat().replace('+00:00', 'Z')
    state['wall_seconds'] = round(state['ended_epoch'] - state['started_epoch'], 3)
    state['notification_event_id'] = f"{execution_id}:{state['status']}"
    save(status_path, state)
    # A notification adapter receives only this small status file, never POTCAR/output contents.
    notifier = config.get('notify_argv', [])
    if notifier:
        adapter_started = time.monotonic()
        try:
            result = subprocess.run(notifier + [str(status_path)], cwd=root,
                                    capture_output=True, timeout=30)
            state['notification'] = {
                'status': ('ADAPTER_RETURNED_ZERO' if result.returncode == 0 else 'FAILED'),
                'exit_code': result.returncode,
                'user_acknowledged': None,
            }
        except Exception as error:
            state['notification'] = {'status': 'FAILED', 'error': str(error),
                                     'user_acknowledged': None}
        state['stage_seconds']['notification_adapter'] = round(
            time.monotonic() - adapter_started, 3)
    else:
        state['notification'] = {
            'status': 'FILE_ONLY', 'reason': 'No notification channel configured',
            'user_acknowledged': None,
        }
    state['stage_seconds']['total_including_preflight'] = round(
        time.monotonic() - preflight_started, 3)
    save(status_path, state, status_changed=False)
    print(json.dumps(state, ensure_ascii=False))
    return 0 if state['status'] == 'COMPLETED_PENDING_REVIEW' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', help='JSON stored in the approved batch directory')
    args = parser.parse_args()
    raise SystemExit(supervise(args.config))

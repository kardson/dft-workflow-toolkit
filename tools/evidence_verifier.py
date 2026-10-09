"""Explicit offline JSON equality checks; never grants scientific authority."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re

MANIFEST_SCHEMA = 'vasp-evidence-manifest/v1'
RECEIPT_SCHEMA = 'vasp-evidence-verification/v1'
SUPPORTED = frozenset(('vasp-approved-bundle/v1','vasp-practical-preparation/v1','vasp-executor-check/v1','vasp-execution-state/v1','vasp-tool-evidence/v1','toolkit-clean-science-validation/v1'))
IDENTITIES = frozenset(('execution','case','attempt','source','environment','version'))

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()

def path_in(value, workspace):
    root=Path(workspace).resolve()
    if not isinstance(value,str) or not value or '://' in value: raise ValueError('Explicit local JSON path required')
    p=Path(value)
    p=(p if p.is_absolute() else root/p).resolve()
    if not p.is_relative_to(root) or p.suffix.lower() != '.json': raise ValueError('JSON path must stay inside workspace')
    return p

def load(path):
    def pairs(items):
        result={}
        for k,v in items:
            if k in result: raise ValueError('Duplicate JSON object key')
            result[k]=v
        return result
    def constant(value): raise ValueError('Nonfinite JSON number')
    return json.loads(Path(path).read_text(encoding='utf-8-sig'),object_pairs_hook=pairs,parse_constant=constant)

def pointer(value, ref):
    if not isinstance(ref,str) or (ref and not ref.startswith('/')) or re.search(r'~(?![01])',ref): raise ValueError('INVALID_POINTER')
    if not ref: return value
    for token in ref[1:].split('/'):
        token=token.replace('~1','/').replace('~0','~')
        if isinstance(value,dict):
            if token not in value: raise KeyError('FIELD_MISSING')
            value=value[token]
        elif isinstance(value,list):
            if not re.fullmatch(r'0|[1-9][0-9]*',token): raise ValueError('INVALID_POINTER')
            if int(token)>=len(value): raise KeyError('FIELD_MISSING')
            value=value[int(token)]
        else: raise TypeError('POINTER_CONTAINER_TYPE_MISMATCH')
    return value

def equal(a,b):
    if type(a) is not type(b): return False
    if isinstance(a,dict): return a.keys()==b.keys() and all(equal(a[k],b[k]) for k in a)
    if isinstance(a,list): return len(a)==len(b) and all(equal(x,y) for x,y in zip(a,b))
    return a==b

def verify(manifest, workspace):
    if not isinstance(manifest,dict) or manifest.get('schema')!=MANIFEST_SCHEMA: raise ValueError('Unsupported manifest schema')
    files=manifest.get('files'); checks=manifest.get('checks'); subject=manifest.get('subject')
    if not isinstance(files,list) or not files or not isinstance(checks,list) or not checks: raise ValueError('Explicit nonempty files/checks required')
    if not isinstance(subject,dict) or set(subject)!={'kind','sha256'} or not isinstance(subject['kind'],str) or not re.fullmatch('[0-9a-f]{64}',str(subject['sha256'])): raise ValueError('Explicit subject kind and digest required')
    objects={}; records=[]; ids=set()
    for f in files:
        if not isinstance(f,dict) or set(f)-{'id','path','allowed_schemas','schema_mapping'}: raise ValueError('Invalid file declaration')
        name=f.get('id'); allowed=f.get('allowed_schemas')
        if not isinstance(name,str) or not name or name in ids: raise ValueError('Unique file id required')
        ids.add(name)
        if not isinstance(allowed,list) or not allowed or any(not isinstance(s,str) for s in allowed): raise ValueError('Explicit allowed schemas required')
        p=path_in(f.get('path'),workspace)
        row={'id':name,'path':p.relative_to(Path(workspace).resolve()).as_posix(),'state':'NOT_CHECKED'}
        try:
            data=load(p)
            if not isinstance(data,dict): raise TypeError('ROOT_TYPE_MISMATCH')
            observed=data.get('schema')
            mapping=f.get('schema_mapping')
            if mapping is not None:
                if not isinstance(mapping,dict) or set(mapping)!={'pointer','expected','schema'}: raise ValueError('INVALID_SCHEMA_MAPPING')
                if 'schema' in data: raise ValueError('SCHEMA_MAPPING_CANNOT_OVERRIDE_EXPLICIT_SCHEMA')
                if mapping['pointer'] != '/schema_version' or type(mapping['expected']) is not int or mapping['expected'] != 1 or mapping['schema'] not in SUPPORTED:
                    raise ValueError('UNSUPPORTED_LEGACY_SCHEMA_MAPPING')
                if type(data.get('schema_version')) is not int or data['schema_version'] != 1:
                    raise ValueError('UNSUPPORTED_LEGACY_SCHEMA_VERSION')
                observed=mapping['schema']
            row['observed_schema']=observed
            row['content_sha256']=digest(data)
            if observed not in SUPPORTED: row['state']='UNKNOWN_SCHEMA'
            elif observed not in allowed: row['state']='SCHEMA_NOT_ALLOWED'
            else: row['state']='SCHEMA_MATCHED'; objects[name]=data
        except FileNotFoundError: row['state']='FILE_MISSING'
        except (OSError,ValueError,KeyError,TypeError) as error: row.update(state='JSON_OR_SCHEMA_ERROR',detail=str(error))
        records.append(row)
    results=[]; check_ids=set()
    for c in checks:
        if not isinstance(c,dict) or set(c)-{'id','left','right','expected','identity'}: raise ValueError('Invalid check declaration')
        if ('expected' in c)==('right' in c): raise ValueError('Exactly one expected literal or right reference required')
        if not isinstance(c.get('id'),str) or not c['id'] or c['id'] in check_ids: raise ValueError('Unique explicit check id required')
        check_ids.add(c['id'])
        if c.get('identity') not in IDENTITIES: raise ValueError('Explicit execution/case/attempt/source/environment/version relationship required')
        row={'id':c.get('id'),'identity':c['identity'],'state':'NOT_CHECKED'}
        def resolve(r):
            if not isinstance(r,dict) or set(r)!={'file','pointer'} or r['file'] not in ids: raise ValueError('UNDECLARED_REFERENCE')
            if r['file'] not in objects: raise LookupError('SOURCE_UNAVAILABLE')
            return pointer(objects[r['file']],r['pointer'])
        try:
            left=resolve(c.get('left')); right=resolve(c['right']) if 'right' in c else c['expected']
            row['state']='MATCHED' if equal(left,right) else ('TYPE_MISMATCH' if type(left) is not type(right) else 'IDENTITY_VALUE_MISMATCH')
        except KeyError: row['state']='FIELD_MISSING'
        except TypeError: row['state']='POINTER_CONTAINER_TYPE_MISMATCH'
        except ValueError as e: row['state']=str(e)
        except LookupError: row['state']='SOURCE_UNAVAILABLE'
        results.append(row)
    ok=all(r['state']=='SCHEMA_MATCHED' for r in records) and all(r['state']=='MATCHED' for r in results)
    return {'schema':RECEIPT_SCHEMA,'verifier_version':1,'manifest_sha256':digest(manifest),'subject':subject,'files':records,'checks':results,'state':'OBJECTIVE_CHECKS_MATCHED' if ok else 'INCOMPLETE_OR_MISMATCH','execution_authorized':False,'gate_opened':False,'scientific_acceptance':'NOT_EVALUATED'}

def consume(manifest_path, receipt_path, workspace, subject, kind):
    workspace=Path(workspace).resolve()
    manifest=load(path_in(str(manifest_path),workspace)); receipt=load(path_in(str(receipt_path),workspace))
    if manifest.get('subject')!={'kind':kind,'sha256':digest(subject)}: raise ValueError('Verification subject identity mismatch')
    current=verify(manifest,workspace)
    if not equal(current,receipt): raise ValueError('Stale or incompatible verification receipt; rerun explicit verifier')
    if current['state']!='OBJECTIVE_CHECKS_MATCHED': raise ValueError('Verification contains gaps or mismatches')
    return current

def write_receipt(output, receipt, workspace, sources):
    p=path_in(str(output),workspace)
    root=Path(workspace).resolve()
    if p.exists() or any(part.lower() in ('04_runs','03_models','private_runs','case','returned','returns') for part in p.parts): raise ValueError('Output must be new and outside calculation directories, including a workspace inside such a tree')
    if p in sources or any(p.parent==s.parent for s in sources): raise ValueError('Output must be outside source directories')
    with p.open('x',encoding='utf-8') as stream: json.dump(receipt,stream,indent=2,ensure_ascii=False)

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace',type=Path,default=Path.cwd())
    p.add_argument('--manifest',required=True)
    p.add_argument('--output',required=True)
    a=p.parse_args(argv)
    try:
        root=a.workspace.resolve()
        mpath=path_in(a.manifest,root); m=load(mpath); r=verify(m,root)
        sources=[mpath,*[path_in(f['path'],root) for f in m['files']]]
        write_receipt(a.output,r,root,sources)
    except (OSError,ValueError,KeyError,TypeError) as error: p.exit(2,f'evidence-verifier stopped: {type(error).__name__}: {error}\n')
    print(json.dumps({'state':r['state'],'output':a.output,'scientific_acceptance':'NOT_EVALUATED'}))
    return 0 if r['state']=='OBJECTIVE_CHECKS_MATCHED' else 1

if __name__=='__main__': raise SystemExit(main())

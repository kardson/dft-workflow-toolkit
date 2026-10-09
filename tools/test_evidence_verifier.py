import copy
import json
import subprocess
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
from contextlib import contextmanager
import tempfile

@contextmanager
def runtime_directory():
    with tempfile.TemporaryDirectory(prefix="generic-evidence-") as directory:
        yield Path(directory)
from evidence_verifier import *

class EvidenceTests(unittest.TestCase):
    def test_relative_and_absolute_workspace_cli(self):
        with runtime_directory() as root:
            workspace=root/'toolkit-private-trial'; workspace.mkdir()
            caller=root/'public-candidate'; caller.mkdir()
            m=self.fixture(workspace); (workspace/'manifest.json').write_text(json.dumps(m))
            (workspace/'reports').mkdir()
            def invoke(w,out,manifest='manifest.json'):
                return subprocess.run([sys.executable,'-E','-s','-S','-B',str(Path(__file__).parent/'evidence_verifier.py'),'--workspace',str(w),'--manifest',manifest,'--output',out],cwd=caller,capture_output=True,text=True)
            a=invoke('../toolkit-private-trial','reports/relative.json')
            b=invoke(workspace.resolve(),'reports/absolute.json')
            self.assertEqual(a.returncode,0,a.stderr); self.assertEqual(b.returncode,0,b.stderr)
            self.assertEqual(load(workspace/'reports/relative.json'),load(workspace/'reports/absolute.json'))
            for out,manifest in [('reports/rejected.json','../outside.json'),('../outside.json','manifest.json'),('private_runs/result.json','manifest.json')]:
                p=invoke('../toolkit-private-trial',out,manifest)
                self.assertEqual(p.returncode,2,p.stdout+p.stderr)
            self.assertFalse((root/'outside.json').exists())
            self.assertFalse((workspace/'reports/rejected.json').exists())

    def fixture(self,root,subject=None,kind='synthetic'):
        data={'schema':'vasp-execution-state/v1','execution':'x','flag':True,'a/b':{'~key':[3]},'version':1}
        (root/'source.json').write_text(json.dumps(data))
        return {'schema':MANIFEST_SCHEMA,'subject':{'kind':kind,'sha256':digest(subject or {})},'files':[{'id':'a','path':'source.json','allowed_schemas':['vasp-execution-state/v1']}],'checks':[{'id':'execution','identity':'execution','left':{'file':'a','pointer':'/execution'},'expected':'x'}]}

    def test_match_readonly_and_no_side_effect(self):
        with runtime_directory() as root:
            m=self.fixture(root); before=(root/'source.json').read_bytes()
            with patch('subprocess.Popen',side_effect=AssertionError),patch('socket.socket',side_effect=AssertionError):
                r=verify(m,root)
            self.assertEqual(r['state'],'OBJECTIVE_CHECKS_MATCHED'); self.assertFalse(r['execution_authorized'])
            self.assertEqual(before,(root/'source.json').read_bytes())
            self.assertEqual(pointer(load(root/'source.json'),'/a~1b/~0key/0'),3)

    def test_failure_states_and_strict_types(self):
        with runtime_directory() as root:
            m=self.fixture(root)
            for ptr,expected,state in [('/missing',1,'FIELD_MISSING'),('bad',1,'INVALID_POINTER'),('/~2',1,'INVALID_POINTER'),('/flag',1,'TYPE_MISMATCH'),('/execution','other','IDENTITY_VALUE_MISMATCH'),('/flag/a',1,'POINTER_CONTAINER_TYPE_MISMATCH')]:
                n=copy.deepcopy(m); n['checks'][0].update(left={'file':'a','pointer':ptr},expected=expected)
                self.assertEqual(verify(n,root)['checks'][0]['state'],state)
            n=copy.deepcopy(m); n['checks'][0]['left']['file']='unlisted'
            self.assertEqual(verify(n,root)['checks'][0]['state'],'UNDECLARED_REFERENCE')
            (root/'source.json').write_text('{"schema":"unknown"}')
            self.assertEqual(verify(m,root)['files'][0]['state'],'UNKNOWN_SCHEMA')
            (root/'source.json').unlink()
            self.assertEqual(verify(m,root)['files'][0]['state'],'FILE_MISSING')

    def test_stale_subject_manifest_and_content(self):
        with runtime_directory() as root:
            subject={'source_ref':'source.json'}; m=self.fixture(root,subject)
            (root/'manifest.json').write_text(json.dumps(m)); (root/'receipt.json').write_text(json.dumps(verify(m,root)))
            consume('manifest.json','receipt.json',root,subject,'synthetic')
            with self.assertRaises(ValueError): consume('manifest.json','receipt.json',root,{},'synthetic')
            data=load(root/'source.json'); data['version']=2; (root/'source.json').write_text(json.dumps(data))
            with self.assertRaises(ValueError): consume('manifest.json','receipt.json',root,subject,'synthetic')

    def test_two_endpoints_schema_and_json_errors(self):
        with runtime_directory() as root:
            m=self.fixture(root)
            (root/'second.json').write_text((root/'source.json').read_text())
            m['files'].append({'id':'b','path':'second.json','allowed_schemas':['vasp-execution-state/v1']})
            m['checks'][0].pop('expected'); m['checks'][0]['right']={'file':'b','pointer':'/execution'}
            self.assertEqual(verify(m,root)['state'],'OBJECTIVE_CHECKS_MATCHED')
            m['files'][1]['allowed_schemas']=['vasp-approved-bundle/v1']
            self.assertEqual(verify(m,root)['files'][1]['state'],'SCHEMA_NOT_ALLOWED')
            (root/'second.json').write_text('{')
            self.assertEqual(verify(m,root)['files'][1]['state'],'JSON_OR_SCHEMA_ERROR')
            m['files'][1]['path']='../outside.json'
            with self.assertRaises(ValueError): verify(m,root)

    def test_paths_and_output_protection(self):
        with runtime_directory() as root:
            m=self.fixture(root); r=verify(m,root)
            for p in ('../other.json','https://example.com/a.json','a.txt'):
                with self.assertRaises(ValueError): path_in(p,root)
            with self.assertRaises(ValueError): write_receipt('source.json',r,root,[root/'source.json'])
            with self.assertRaises(ValueError): write_receipt('out.json',r,root,[root/'source.json'])
            (root/'reports').mkdir(); write_receipt('reports/result.json',r,root,[root/'source.json'])
            with self.assertRaises(ValueError): write_receipt('reports/result.json',r,root,[])
            with self.assertRaises(ValueError): write_receipt('04_runs/result.json',r,root,[])
            with self.assertRaises(ValueError): write_receipt('private_runs/reports/result.json',r,root,[])
            (root/'private_runs').mkdir()
            with self.assertRaises(ValueError): write_receipt('result.json',r,root/'private_runs',[])
            (root/'04_runs').mkdir()
            with self.assertRaises(ValueError): write_receipt('result.json',r,root/'04_runs',[])

    def test_symlink_escape(self):
        with runtime_directory() as root:
            try: (root/'escape').symlink_to(root.parent,target_is_directory=True)
            except OSError as e: self.skipTest('Directory symlink privilege unavailable: '+str(e))
            with self.assertRaises(ValueError): path_in('escape/outside.json',root)

    def test_explicit_legacy_schema_mapping(self):
        with runtime_directory() as root:
            m=self.fixture(root); (root/'source.json').write_text('{"schema_version":1,"execution":"x"}')
            self.assertEqual(verify(m,root)['files'][0]['state'],'UNKNOWN_SCHEMA')
            m['files'][0]['schema_mapping']={'pointer':'/schema_version','expected':1,'schema':'vasp-execution-state/v1'}
            self.assertEqual(verify(m,root)['state'],'OBJECTIVE_CHECKS_MATCHED')
            for data in ({'schema':'unsupported-explicit-schema','schema_version':1,'execution':'x'}, {'schema':None,'schema_version':1,'execution':'x'}, {'schema':'vasp-execution-state/v1','schema_version':1,'execution':'x'}, {'schema_version':True,'execution':'x'}, {'schema_version':2,'execution':'x'}):
                (root/'source.json').write_text(json.dumps(data))
                self.assertEqual(verify(m,root)['files'][0]['state'],'JSON_OR_SCHEMA_ERROR')
                self.assertNotEqual(verify(m,root)['state'],'OBJECTIVE_CHECKS_MATCHED')
            (root/'source.json').write_text('{"schema_version":1,"execution":"x"}')
            for mapping in ({'pointer':'/execution','expected':1,'schema':'vasp-execution-state/v1'}, {'pointer':'/schema_version','expected':True,'schema':'vasp-execution-state/v1'}, {'pointer':'/schema_version','expected':2,'schema':'vasp-execution-state/v1'}, {'pointer':'/schema_version','expected':1,'schema':'unknown'}):
                m['files'][0]['schema_mapping']=mapping
                self.assertEqual(verify(m,root)['files'][0]['state'],'JSON_OR_SCHEMA_ERROR')

    def test_optin_preserves_plan_and_advice_states(self):
        from property_plan import build_plan
        from parameter_advice import review
        from test_property_plan import request
        from test_parameter_advice import bundle,proposal
        with runtime_directory() as root:
            for subject,kind,call in [(request(),'property-plan-request',lambda **kw:build_plan(request(),source_root=root,**kw)),({'bundle':bundle(),'proposal':proposal()},'parameter-advice-inputs',lambda **kw:review(bundle(),proposal(),bundle_ref='bundle.json',proposal_ref='proposal.json',source_root=root,**kw))]:
                old=call(); m=self.fixture(root,subject,kind)
                (root/'manifest.json').write_text(json.dumps(m)); (root/'receipt.json').write_text(json.dumps(verify(m,root)))
                new=call(evidence_manifest='manifest.json',evidence_receipt='receipt.json')
                self.assertIn('objective_evidence_verification',new); new.pop('objective_evidence_verification')
                self.assertEqual(new,old)
                with self.assertRaises(ValueError): call(evidence_manifest='manifest.json')

if __name__=='__main__': unittest.main()

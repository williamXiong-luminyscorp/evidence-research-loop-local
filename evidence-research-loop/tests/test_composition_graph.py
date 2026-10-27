"""Mechanism regressions with real subprocess verifiers, not model-search evidence."""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
import composition_graph as engine

VERIFY='''import json,sys,time
from pathlib import Path
request=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
artifact=json.loads(Path(request["artifact"]).read_text(encoding="utf-8"))
stage=request["phase"]
result=Path(sys.argv[2])/"result.json"
mode=artifact.get("mode")
if mode=="timeout": time.sleep(4)
if mode=="missing": raise SystemExit(0)
if mode=="nan":
    result.write_text('{"passed":true,"value":NaN}')
    raise SystemExit(0)
children={k:json.loads(Path(v["artifact"]).read_text(encoding="utf-8")) for k,v in request["child_artifacts"].items()}
passed=stage not in artifact.get("fail",[])
if stage=="integration" and artifact.get("need_child_value") is not None:
    passed=passed and all(v.get("value")==artifact["need_child_value"] for v in children.values())
result.write_text(json.dumps({"passed":passed,"stage":stage}),encoding="utf-8")
if mode=="nonzero": raise SystemExit(7)
'''


class CompositionGraphTests(unittest.TestCase):
    def setUp(self):
        base=Path(os.environ.get('ERL_TEST_TMP',Path(__file__).parent/'.tmp-composition'))
        self.root=base/uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.verifier=self.root/'verifier.py'
        self.verifier.write_text(VERIFY,encoding='utf-8')
        self.project=self.root/'project'
        claims=[]
        for cid in ('A','B','T'):
            claims.append({'id':cid,'statement':'Test claim '+cid,'scope':'bounded fixture',
                'contract':{'name':cid},'environment':{'test':1},'guarantee':'empirical',
                'verifiers':{stage:{'command':[sys.executable,str(self.verifier),'{request}','{result_dir}'],
                    'result_file':'result.json','pass_field':'passed','timeout_sec':2.0}
                    for stage in ('direct','composition','integration','holdout','refutation')}})
        self.blueprint={'schema_version':1,'graph_id':'test','title':'test','claims':claims,'goal_claim_ids':['T']}
        self.bp=self.root/'blueprint.json'

    def init(self, project=None, blueprint=None):
        self.bp.write_text(json.dumps(blueprint or self.blueprint),encoding='utf-8')
        return engine.initialize(project or self.project,self.bp)

    def artifact(self, **value):
        path=self.root/(uuid.uuid4().hex+'.json')
        path.write_text(json.dumps(value),encoding='utf-8')
        return str(path)

    def submit(self, kind='direct', cid='T', **values):
        return engine.submit(self.project,{'kind':kind,'claim_id':cid,**values})

    def test_conditional_and_actual_bindings_need_root_acceptance(self):
        self.init()
        reduction=self.submit('reduction',premises=['A'],artifact=self.artifact(need_child_value=2))
        self.assertEqual(reduction['outcome']['status'],'conditionally_validated')
        self.assertFalse(engine.audit(self.project)['accepted'])
        child=self.submit(cid='A',artifact=self.artifact(value=1))
        wrong=self.submit('validate',reduction_id=reduction['evidence_id'],bindings={'A':child['evidence_id']})
        self.assertEqual(wrong['outcome']['status'],'failed')
        self.assertFalse(engine.audit(self.project)['accepted'])
        child2=self.submit(cid='A',artifact=self.artifact(value=2))
        self.submit('validate',reduction_id=reduction['evidence_id'],bindings={'A':child2['evidence_id']})
        self.assertTrue(engine.audit(self.project)['accepted'])
        engine.revoke(self.project,child2['evidence_id'],'invalidate exact child version')
        self.assertFalse(engine.audit(self.project)['accepted'])

    def test_alternative_route_survives_and_cycles_do_not_close(self):
        self.init()
        a=self.submit(cid='A',artifact=self.artifact())['evidence_id']
        b=self.submit(cid='B',artifact=self.artifact())['evidence_id']
        ra=self.submit('reduction',premises=['A'],artifact=self.artifact())['evidence_id']
        rb=self.submit('reduction',premises=['B'],artifact=self.artifact())['evidence_id']
        self.submit('validate',reduction_id=ra,bindings={'A':a})
        self.submit('validate',reduction_id=rb,bindings={'B':b})
        engine.revoke(self.project,a,'failed route A')
        self.assertTrue(engine.audit(self.project)['accepted'])
        with self.assertRaises(engine.CompositionError):
            self.submit('reduction',cid='A',premises=['T'],artifact=self.artifact())

    def test_nonzero_missing_nan_and_timeout_are_failure(self):
        self.init()
        for mode in ('nonzero','missing','nan','timeout'):
            with self.subTest(mode=mode):
                result=self.submit(artifact=self.artifact(mode=mode))
                self.assertEqual(result['outcome']['status'],'failed')
                self.assertFalse(engine.audit(self.project)['accepted'])

    def test_holdout_failure_cannot_pass(self):
        self.init()
        result=self.submit(artifact=self.artifact(fail=['holdout']))
        self.assertEqual(result['outcome']['status'],'failed')
        self.assertTrue(engine.audit(self.project)['valid'])
        self.assertFalse(engine.audit(self.project)['accepted'])

    def test_crash_before_started_recovers_without_rerun_or_duplicate(self):
        self.init()
        artifact=self.artifact()
        code='''import os,sys
sys.path.insert(0,sys.argv[1])
import composition_graph as e
original=e._event
def crash(project,kind,**kw):
    if kind=="started": os._exit(93)
    return original(project,kind,**kw)
e._event=crash
e.submit(sys.argv[2],{"kind":"direct","claim_id":"T","artifact":sys.argv[3]})
'''
        process=subprocess.run([sys.executable,'-c',code,str(SCRIPTS),str(self.project),artifact],capture_output=True,timeout=10)
        self.assertEqual(process.returncode,93)
        recovered=engine.resume(self.project)
        self.assertTrue(recovered['valid'],recovered)
        self.assertFalse(recovered['accepted'])
        count=recovered['event_count']
        self.assertEqual(engine.resume(self.project)['event_count'],count)
        self.assertEqual(set(recovered['evidence'].values()),{'interrupted'})

    def test_forged_asset_cannot_relabel_verified_source_claim(self):
        self.init()
        self.submit(cid='A',artifact=self.artifact())
        exported=self.root/'asset.json'
        engine.export_asset(self.project,'A',exported)
        asset=json.loads(exported.read_text('utf-8'))
        bp=engine._load(self.project)
        claim_b=engine._claim(bp,'B')
        asset['claim']=claim_b
        asset['claim_identity']=engine.claim_identity(claim_b)
        asset['asset_hash']=engine._hash({k:v for k,v in asset.items() if k!='asset_hash'})
        exported.write_text(json.dumps(asset),encoding='utf-8')
        with self.assertRaises(engine.CompositionError):
            engine.import_asset(self.project,'B',exported)

    def test_formal_rejected_and_milestone_requires_explicit_record(self):
        formal=copy.deepcopy(self.blueprint)
        formal['claims'][0]['guarantee']='formal'
        with self.assertRaises(engine.CompositionError): self.init(blueprint=formal)
        self.blueprint['claims'][-1]['requires_human_review']=True
        self.init()
        self.submit(artifact=self.artifact())
        self.assertFalse(engine.audit(self.project)['accepted'])
        rb=engine.record_readback(self.project,'T','The test target has the stated bounded contract.',{'kind':'agent'},True)
        self.assertFalse(rb['independence_verified'])
        engine.attest_milestone(self.project,'T','fixture human attestation','approve',rb['readback_id'])
        self.assertTrue(engine.audit(self.project)['accepted'])


if __name__=='__main__': unittest.main()

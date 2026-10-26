#!/usr/bin/env python3
"""Engineering AND-OR composition evidence; not a formal proof kernel.

Claims and submissions are immutable. A conditional reduction is not a successful
implementation. Actual selected children, integration and root holdout are bound
to each accepted path. There is no automatic mathematical validity inference.
"""
from __future__ import annotations

import argparse
import base64
import copy
import contextlib
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid
from typing import Any

import research_graph as rg
from solution_evolver import run_process_tree


class CompositionError(RuntimeError):
    pass


@contextlib.contextmanager
def _lock(project: Path, timeout: float = 15):
    """OS-owned advisory lock: process death releases it without stale-file deletion."""
    project.mkdir(parents=True, exist_ok=True)
    stream = (project/'.composition.lock').open('a+b')
    if stream.tell()==0:
        stream.write(b'0')
        stream.flush()
    deadline = time.monotonic()+timeout
    acquired = False
    try:
        while not acquired:
            try:
                stream.seek(0)
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                acquired=True
            except OSError:
                if time.monotonic() >= deadline:
                    raise CompositionError('composition lock timeout')
                time.sleep(.02)
        yield
    finally:
        if acquired:
            stream.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
        stream.close()


def _hash(value: Any) -> str:
    return rg.sha256_bytes(rg.canonical(value))


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8', newline='\n') as f:
        json.dump(value, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())


def _root(project: Path | str) -> Path:
    return Path(project).resolve()


def _events(project: Path) -> list[dict]:
    return rg.read_jsonl(project/'composition-events.jsonl')


def _event(project: Path, kind: str, **values: Any) -> dict:
    return rg.append_chain(project/'composition-events.jsonl', {
        'event_id': 'evt-'+uuid.uuid4().hex, 'at': rg.utc_now(), 'type': kind, **values}, 'event')


def _load(project: Path) -> dict:
    value = rg.load_json(project/'composition.json')
    expected = value.get('blueprint_hash')
    payload = {k:v for k,v in value.items() if k != 'blueprint_hash'}
    if expected != _hash(payload):
        raise CompositionError('immutable blueprint hash mismatch')
    errors = []
    for item in value['engine_manifest']:
        if not Path(item['path']).is_file() or rg.sha256_file(Path(item['path'])) != item['sha256']:
            errors.append('engine changed: '+item['path'])
    if errors:
        raise CompositionError('; '.join(errors))
    return value


def _claim(blueprint: dict, claim_id: str) -> dict:
    for claim in blueprint['claims']:
        if claim['id'] == claim_id:
            return claim
    raise CompositionError('unknown claim: '+str(claim_id))


def _verifier_identity(verifier: dict) -> dict:
    result = copy.deepcopy(verifier)
    files = {x['path']: x['sha256'] for x in result.pop('frozen_file_manifest', [])}
    result.pop('contract_hash', None)
    result.pop('frozen_files', None)
    result['command'] = [files.get(x, x) for x in result['command']]
    result['dependency_hashes'] = sorted(set(files.values()))
    return result


def claim_identity(claim: dict) -> str:
    return _hash({k: claim[k] for k in ('statement','contract','environment','scope','guarantee')} | {
        'verifiers': {k:_verifier_identity(v) for k,v in claim['verifiers'].items()},
        'requires_human_review': claim.get('requires_human_review', False)})


def initialize(project: Path | str, blueprint_file: Path | str) -> dict:
    project = _root(project)
    source = Path(blueprint_file).resolve()
    blueprint = rg.load_json(source)
    if blueprint.get('schema_version') != 1 or not isinstance(blueprint.get('claims'), list):
        raise CompositionError('schema_version 1 and claims list required')
    if any(not isinstance(blueprint.get(k),str) or not blueprint[k] for k in ('graph_id','title')):
        raise CompositionError('graph_id and title required')
    ids = [c.get('id') for c in blueprint['claims']]
    if not ids or any(not isinstance(x,str) or not x for x in ids) or len(set(ids)) != len(ids):
        raise CompositionError('claim IDs must be nonempty and unique')
    goals = blueprint.get('goal_claim_ids')
    if not isinstance(goals,list) or not goals or not set(goals) <= set(ids):
        raise CompositionError('nonempty goal_claim_ids must name claims')
    for claim in blueprint['claims']:
        for key in ('statement','scope'):
            if not isinstance(claim.get(key),str) or not claim[key]:
                raise CompositionError(f'claim {claim["id"]} needs {key}')
        if not all(isinstance(claim.get(k),dict) for k in ('contract','environment','verifiers')):
            raise CompositionError('contract, environment and verifiers must be objects')
        if claim.get('guarantee') != 'empirical':
            raise CompositionError('only empirical engineering guarantee supported; formal claims require a formal adapter')
        if not isinstance(claim.get('requires_human_review',False),bool):
            raise CompositionError('requires_human_review must be boolean')
        if claim['id'] in goals and not {'integration','holdout'} <= set(claim['verifiers']):
            raise CompositionError('goal requires integration and holdout verifiers')
        for phase, verifier in list(claim['verifiers'].items()):
            if phase not in {'direct','composition','integration','holdout','refutation'}:
                raise CompositionError('unknown verifier phase '+phase)
            if not isinstance(verifier.get('command'),list) or not verifier['command'] or not all(isinstance(x,str) for x in verifier['command']):
                raise CompositionError('verifier command must be a nonempty string array')
            verifier.setdefault('result_file','result.json')
            verifier.setdefault('pass_field','passed')
            verifier.setdefault('timeout_sec',30)
            result_name = Path(verifier['result_file'])
            if result_name.is_absolute() or '..' in result_name.parts or result_name.as_posix() in {'receipt.json','request.json'}:
                raise CompositionError('result_file must stay inside the fresh stage directory')
            if not isinstance(verifier['timeout_sec'],(float,int)) or not math.isfinite(verifier['timeout_sec']) or not 0 < verifier['timeout_sec'] <= 120:
                raise CompositionError('invalid verifier timeout')
            verifier['command'] = [x.replace('{python}',sys.executable) for x in verifier['command']]
            claim['verifiers'][phase] = rg.freeze_verifier(verifier,source.parent,project)
    blueprint['engine_manifest'] = [{'path': str(p), 'sha256': rg.sha256_file(p)} for p in (
        Path(__file__).resolve(), Path(rg.__file__).resolve(), Path(__file__).with_name('solution_evolver.py').resolve())]
    blueprint['blueprint_hash'] = _hash(blueprint)
    with _lock(project):
        if (project/'composition.json').exists():
            raise CompositionError('already initialized; immutable claims need a new project version')
        _write(project/'composition.json',blueprint)
        _event(project,'initialized',blueprint_hash=blueprint['blueprint_hash'])
    return status(project)


def _attempts(project: Path) -> dict[str,dict]:
    result = {}
    for path in sorted((project/'composition-attempts').glob('*/request.json')):
        request = rg.load_json(path)
        done = path.with_name('outcome.json')
        result[request['evidence_id']] = {'request':request,'outcome':rg.load_json(done) if done.exists() else None}
    return result


def _revoked(project: Path) -> set[str]:
    return {x['evidence_id'] for x in _events(project) if x['type']=='revoked'}


def _integrity(project: Path, blueprint: dict, attempts: dict) -> list[str]:
    errors = rg.verify_chain(_events(project),'event')
    events = _events(project)
    initialization = [e for e in events if e['type']=='initialized']
    if len(initialization)!=1 or initialization[0]['blueprint_hash']!=blueprint['blueprint_hash']:
        errors.append('initialization event does not bind immutable blueprint')
    starts = {e['evidence_id']:e for e in events if e['type']=='started'}
    finishes = [e for e in events if e['type']=='finished']
    if len({e['evidence_id'] for e in finishes}) != len(finishes):
        errors.append('duplicate finished event')
    for claim in blueprint['claims']:
        for phase, verifier in claim['verifiers'].items():
            errors.extend(f'{claim["id"]}/{phase}: {e}' for e in rg.verifier_integrity_errors(verifier))
    for evidence_id, item in attempts.items():
        request,outcome = item['request'],item['outcome']
        claim = _claim(blueprint,request['claim_id'])
        if request.get('claim_identity')!=claim_identity(claim) or request.get('blueprint_hash')!=blueprint['blueprint_hash']:
            errors.append('request contract changed: '+evidence_id)
        if evidence_id not in starts or starts[evidence_id]['request_hash'] != _hash(request):
            errors.append('missing or mismatched start: '+evidence_id)
        artifact = project/request['artifact_path']
        if not artifact.is_file() or rg.sha256_file(artifact) != request['artifact_sha256']:
            errors.append('artifact changed: '+evidence_id)
        if outcome:
            matching = [e for e in finishes if e['evidence_id']==evidence_id]
            if len(matching)!=1 or matching[0]['outcome_hash'] != _hash(outcome):
                errors.append('missing or mismatched finish: '+evidence_id)
            for phase in outcome.get('phases',[]):
                stage = project/phase['path']
                for name,digest in phase.get('files',{}).items():
                    f = stage/name
                    if not f.is_file() or rg.sha256_file(f)!=digest:
                        errors.append('phase evidence changed: '+evidence_id+'/'+name)
            errors.extend(_semantic_errors(project,blueprint,request,outcome,attempts,events))
    for evidence_id in starts:
        if evidence_id not in attempts:
            errors.append('missing started request: '+evidence_id)
    return errors


def _semantic_errors(project: Path, bp: dict, request: dict, outcome: dict,
                     attempts: dict, events: list[dict]) -> list[str]:
    errors = []
    eid,kind,cid = request['evidence_id'],request['kind'],request['claim_id']
    claim = _claim(bp,cid)
    if kind=='reduction': expected=['composition']
    elif kind=='validate': expected=['composition','integration']
    elif kind=='direct': expected=['direct']
    elif kind=='refutation': expected=['refutation']
    else: return ['unknown evidence kind: '+eid]
    if kind in {'direct','validate'} and cid in bp['goal_claim_ids']:
        expected=list(dict.fromkeys(expected+['integration','holdout']))
    if expected!=request.get('phases'):
        errors.append('required phases mismatch: '+eid)
    if kind=='validate':
        reduction=attempts.get(request.get('reduction_id'),{}).get('request',{})
        if reduction.get('kind')!='reduction' or reduction.get('claim_id')!=cid or reduction.get('artifact_sha256')!=request['artifact_sha256'] or reduction.get('premises')!=request['premises']:
            errors.append('reduction binding mismatch: '+eid)
        if set(request.get('bindings',{}))!=set(request['premises']):
            errors.append('premise bindings incomplete: '+eid)
        for child,child_id in request.get('bindings',{}).items():
            child_req=attempts.get(child_id,{}).get('request',{})
            if child_req.get('claim_id')!=child:
                errors.append('bound child claim mismatch: '+eid)
    elif request.get('bindings'):
        errors.append('unexpected bindings: '+eid)
    phases=outcome.get('phases',[])
    if [p.get('phase') for p in phases]!=expected[:len(phases)]:
        errors.append('phase order/duplication mismatch: '+eid)
    computed=[]
    for phase in phases:
        name=phase['phase']
        if name not in claim['verifiers']:
            errors.append('unknown verifier phase: '+eid)
            continue
        verifier=claim['verifiers'][name]
        stage=project/phase['path']
        if stage.resolve()!=project/'composition-attempts'/eid/name:
            errors.append('stage path mismatch: '+eid)
            continue
        try:
            receipt=rg.load_json(stage/'receipt.json')
            context=rg.load_json(stage/'request.json')
        except (OSError,ValueError):
            errors.append('phase receipt missing or illegal: '+eid)
            continue
        starts=[e for e in events if e['type']=='phase_started' and e['evidence_id']==eid and e['phase']==name]
        if len(starts)!=1 or starts[0]['verifier_hash']!=verifier['contract_hash'] or starts[0]['bindings']!=request.get('bindings',{}) or starts[0]['command']!=receipt.get('command'):
            errors.append('phase start not bound to receipt: '+eid+'/'+name)
        if context.get('phase')!=name or context.get('claim')!=claim or context.get('bindings')!=request.get('bindings'):
            errors.append('phase context mismatch: '+eid+'/'+name)
        for child,child_id in request.get('bindings',{}).items():
            child_req=attempts.get(child_id,{}).get('request',{})
            wanted={'evidence_id':child_id,'artifact':str(project/child_req.get('artifact_path','')),
                    'artifact_sha256':child_req.get('artifact_sha256'), 'claim_identity':claim_identity(_claim(bp,child))}
            if context.get('child_artifacts',{}).get(child)!=wanted or receipt.get('bindings',{}).get(child)!=wanted:
                errors.append('actual child artifact binding mismatch: '+eid+'/'+name)
        output=None
        try:
            output=rg.load_json(stage/verifier['result_file'])
            json.dumps(output,allow_nan=False)
        except (OSError,ValueError,TypeError):
            output=None
        execution=receipt.get('execution',{})
        passed=(execution.get('return_code')==0 and execution.get('timed_out') is False
                and not execution.get('invocation_error') and isinstance(output,dict)
                and output.get(verifier['pass_field']) is True and not receipt.get('errors'))
        if receipt.get('output')!=output and passed:
            errors.append('receipt output differs from raw output: '+eid+'/'+name)
        if receipt.get('passed') is not passed or phase.get('passed') is not passed:
            errors.append('phase pass flag inconsistent with raw result: '+eid+'/'+name)
        computed.append(passed)
    complete=len(phases)==len(expected) and len(computed)==len(expected) and all(computed)
    wanted=('conditionally_validated' if kind=='reduction' else 'verified_refutation' if kind=='refutation' else 'validated') if complete else 'failed'
    if outcome.get('status')=='interrupted':
        if complete:
            errors.append('complete evidence incorrectly marked interrupted: '+eid)
    elif outcome.get('status')!=wanted:
        errors.append('terminal status inconsistent with required raw phases: '+eid)
    return errors


def _derive(project: Path, blueprint: dict, attempts: dict) -> dict:
    revoked = _revoked(project)
    claim_states = {c['id']:'open' for c in blueprint['claims']}
    evidence = {}
    for eid,item in attempts.items():
        outcome = item['outcome']
        evidence[eid] = 'running' if outcome is None else ('revoked' if eid in revoked else outcome['status'])
    # Least fixed point: conditional cycles cannot bootstrap accepted evidence.
    accepted: set[str] = set()
    refuted: set[str] = set()
    for eid,item in attempts.items():
        if eid not in revoked and item['outcome'] and item['outcome']['status']=='verified_refutation':
            refuted.add(item['request']['claim_id'])
    reviews = {e['claim_id']:e for e in _events(project) if e['type']=='milestone_attested'}
    for _ in range(len(attempts)+1):
        previous = set(accepted)
        for eid,item in attempts.items():
            req,outcome = item['request'],item['outcome']
            claim = _claim(blueprint,req['claim_id'])
            if eid in revoked or not outcome or outcome['status']!='validated':
                continue
            reduction = req.get('reduction_id')
            if reduction and (reduction in revoked or evidence.get(reduction)!='conditionally_validated'):
                continue
            bindings = req.get('bindings',{})
            if any(child not in accepted or cid in refuted for cid,child in bindings.items()):
                continue
            if claim.get('requires_human_review') and reviews.get(claim['id'],{}).get('decision')!='approve':
                continue
            accepted.add(eid)
        if accepted==previous:
            break
    for cid in claim_states:
        positive = [eid for eid in accepted if attempts[eid]['request']['claim_id']==cid]
        if positive and cid in refuted:
            claim_states[cid]='evidence_conflict'
        elif cid in refuted:
            claim_states[cid]='refuted'
        elif positive:
            claim_states[cid]='accepted'
        elif any(item['request']['claim_id']==cid and evidence[eid]=='conditionally_validated' for eid,item in attempts.items()):
            claim_states[cid]='conditionally_validated'
    # Conflict invalidates dependent accepted paths, not independent alternatives.
    for _ in range(len(attempts)+1):
        removed = {eid for eid in accepted if any(claim_states[cid]!='accepted' or child not in accepted
                    for cid,child in attempts[eid]['request'].get('bindings',{}).items())}
        if not removed:
            break
        accepted -= removed
        for cid in claim_states:
            positive = any(attempts[eid]['request']['claim_id']==cid for eid in accepted)
            if cid not in refuted and not positive:
                claim_states[cid]='open'
    for eid,item in attempts.items():
        if evidence[eid]=='validated':
            evidence[eid] = 'accepted' if eid in accepted else 'blocked'
    return {'claims':claim_states,'evidence':evidence,
            'accepted':all(claim_states[x]=='accepted' for x in blueprint['goal_claim_ids']),
            'guarantee':'empirical','goal_claim_ids':blueprint['goal_claim_ids']}


def status(project: Path | str) -> dict:
    project = _root(project)
    with _lock(project):
        bp = _load(project)
        attempts = _attempts(project)
        errors = _integrity(project,bp,attempts)
        result = _derive(project,bp,attempts)
        result.update({'valid':not errors,'errors':errors,'graph_id':bp['graph_id'],
                       'attempt_count':len(attempts),'event_count':len(_events(project))})
        result['accepted'] = result['accepted'] and not errors
        return result


def audit(project: Path | str) -> dict:
    result = status(project)
    result['audit_scope'] = 'local evidence integrity and engineering acceptance; not formal proof or test sufficiency'
    return result


def _check_cycle(attempts: dict, target: str, premises: list[str]) -> None:
    edges: dict[str,set[str]] = {}
    for item in attempts.values():
        req = item['request']
        if req['kind']=='reduction':
            edges.setdefault(req['claim_id'],set()).update(req['premises'])
    edges.setdefault(target,set()).update(premises)
    def visit(node: str, stack: set[str]) -> None:
        if node in stack:
            raise CompositionError('cyclic reductions are rejected')
        for child in edges.get(node,set()):
            visit(child,stack|{node})
    visit(target,set())


def _run_phase(project: Path, bp: dict, request: dict, phase: str) -> dict:
    claim = _claim(bp,request['claim_id'])
    verifier = claim['verifiers'][phase]
    directory = project/'composition-attempts'/request['evidence_id']/phase
    directory.mkdir()
    errors = rg.verifier_integrity_errors(verifier)
    artifact = project/request['artifact_path']
    if rg.sha256_file(artifact)!=request['artifact_sha256']:
        errors.append('artifact hash mismatch')
    binding_files = {}
    for cid,child in request.get('bindings',{}).items():
        child_req = rg.load_json(project/'composition-attempts'/child/'request.json')
        path = project/child_req['artifact_path']
        if not path.is_file() or rg.sha256_file(path)!=child_req['artifact_sha256']:
            errors.append('bound child changed: '+cid)
        binding_files[cid] = {'evidence_id':child,'artifact':str(path),
                              'artifact_sha256':child_req['artifact_sha256'],
                              'claim_identity':claim_identity(_claim(bp,cid))}
    public_request = {**request,'phase':phase,'artifact':str(artifact),
        'claim':claim, 'premise_claims':[_claim(bp,cid) for cid in request.get('premises',[])],
        'child_artifacts':binding_files,'guarantee':'empirical'}
    _write(directory/'request.json',public_request)
    mapping = {'project':str(project),'artifact':str(artifact),'request':str(directory/'request.json'),
               'result_dir':str(directory),'attempt_dir':str(directory),'claim_id':claim['id'],
               'python':sys.executable}
    command = []
    for raw in verifier['command']:
        for key,value in mapping.items():
            raw = raw.replace('{'+key+'}',value)
        command.append(raw)
    with _lock(project):
        _event(project,'phase_started',evidence_id=request['evidence_id'],phase=phase,
               command=command,verifier_hash=verifier['contract_hash'],bindings=request.get('bindings',{}))
    result: dict = {'return_code':None,'timed_out':False,'stdout':'','stderr':'', 'duration_sec':0}
    output = None
    if not errors:
        result = run_process_tree(command,project,rg.filtered_verifier_environment(verifier.get('env_allowlist')),verifier['timeout_sec'])
        path = directory/verifier['result_file']
        if path.is_file():
            try:
                output = rg.load_json(path)
                if not isinstance(output,dict) or output.get(verifier['pass_field']) is not True:
                    errors.append('verifier result did not pass')
                # No NaN/Infinity anywhere in claimed evidence.
                json.dumps(output,allow_nan=False)
            except (ValueError,TypeError) as exc:
                errors.append('illegal verifier result: '+str(exc))
        else:
            errors.append('missing verifier output')
        if result['return_code']!=0 or result['timed_out']:
            errors.append('verifier failed or timed out')
    errors.extend(rg.verifier_integrity_errors(verifier))
    if not artifact.is_file() or rg.sha256_file(artifact)!=request['artifact_sha256']:
        errors.append('candidate changed during verification')
    for child in binding_files.values():
        if not Path(child['artifact']).is_file() or rg.sha256_file(Path(child['artifact']))!=child['artifact_sha256']:
            errors.append('bound child changed during verification')
    receipt = {'phase':phase,'passed':not errors,'errors':errors,'execution':result,
               'output':output,'command':command,'bindings':binding_files}
    _write(directory/'receipt.json',receipt)
    return {'phase':phase,'passed':not errors,'path':directory.relative_to(project).as_posix(),
            'files':{p.relative_to(directory).as_posix():rg.sha256_file(p) for p in directory.rglob('*') if p.is_file()}}


def submit(project: Path | str, request_file: Path | str | dict) -> dict:
    project = _root(project)
    source_dir = project
    if isinstance(request_file,dict):
        incoming = copy.deepcopy(request_file)
    else:
        source = Path(request_file).resolve()
        source_dir = source.parent
        incoming = rg.load_json(source)
    with _lock(project):
        bp = _load(project)
        attempts = _attempts(project)
        errors = _integrity(project,bp,attempts)
        if errors:
            raise CompositionError('audit failure before submit: '+'; '.join(errors))
        kind = incoming.get('kind')
        if kind not in {'direct','reduction','validate','refutation'}:
            raise CompositionError('unknown submission kind')
        bindings = incoming.get('bindings',{})
        reduction_id = None
        if kind=='validate':
            reduction_id = incoming.get('reduction_id')
            item = attempts.get(reduction_id)
            states = _derive(project,bp,attempts)
            if not item or item['request']['kind']!='reduction' or states['evidence'].get(reduction_id)!='conditionally_validated':
                raise CompositionError('validate requires a live conditionally_validated reduction')
            original = item['request']
            cid = original['claim_id']
            premises = original['premises']
            artifact = project/original['artifact_path']
            if not isinstance(bindings,dict) or set(bindings)!=set(premises):
                raise CompositionError('exactly one bound evidence per premise required')
            for child, eid in bindings.items():
                if states['claims'][child]!='accepted' or states['evidence'].get(eid)!='accepted' or attempts[eid]['request']['claim_id']!=child:
                    raise CompositionError('bound child evidence is not accepted: '+child)
        else:
            cid = incoming.get('claim_id')
            premises = incoming.get('premises',[]) if kind=='reduction' else []
            if not isinstance(premises,list) or len(set(premises))!=len(premises):
                raise CompositionError('premises must be a unique list')
            for child in premises:
                _claim(bp,child)
            if bindings:
                raise CompositionError('bindings are only valid for validate submissions')
            raw = Path(incoming.get('artifact',''))
            artifact = raw if raw.is_absolute() else source_dir/raw
        claim = _claim(bp,cid)
        if not artifact.is_file():
            raise CompositionError('artifact must be an existing file')
        if kind=='reduction':
            if not premises:
                raise CompositionError('reduction needs at least one premise; use direct for leaves')
            _check_cycle(attempts,cid,premises)
            phases = ['composition']
        elif kind=='refutation':
            phases = ['refutation']
        elif kind=='validate':
            phases = ['composition','integration']
        else:
            phases = ['direct']
        if kind in {'direct','validate'} and cid in bp['goal_claim_ids']:
            phases = list(dict.fromkeys(phases+['integration','holdout']))
        if any(p not in claim['verifiers'] for p in phases):
            raise CompositionError('missing verifier for requested phase')
        eid = 'ev-'+uuid.uuid4().hex
        directory = project/'composition-attempts'/eid
        directory.mkdir(parents=True)
        dest = directory/('artifact'+artifact.suffix)
        rg.copy_file(artifact,dest)
        request = {'evidence_id':eid,'claim_id':cid,'kind':kind,'premises':premises,
                   'bindings':bindings,'reduction_id':reduction_id,'phases':phases,
                   'claim_identity':claim_identity(claim),'blueprint_hash':bp['blueprint_hash'],
                   'artifact_path':dest.relative_to(project).as_posix(),'artifact_sha256':rg.sha256_file(dest),
                   'author':incoming.get('author',{}),'origin':incoming.get('origin'),
                   'at':rg.utc_now(),'owner_pid':os.getpid()}
        _write(directory/'request.json',request)
        _event(project,'started',evidence_id=eid,request_hash=_hash(request))
    results = []
    try:
        for phase in phases:
            result = _run_phase(project,bp,request,phase)
            results.append(result)
            if not result['passed']:
                break
        passed = len(results)==len(phases) and all(x['passed'] for x in results)
        terminal = ('conditionally_validated' if kind=='reduction' else
                    'verified_refutation' if kind=='refutation' else 'validated') if passed else 'failed'
        outcome = {'status':terminal,'phases':results,'finished_at':rg.utc_now()}
    except Exception as exc:
        outcome = {'status':'failed','phases':results,'finished_at':rg.utc_now(),
                   'exception':type(exc).__name__+': '+str(exc)}
    with _lock(project):
        _write(directory/'outcome.json',outcome)
        _event(project,'finished',evidence_id=eid,outcome_hash=_hash(outcome))
    return {'evidence_id':eid,'outcome':outcome, 'graph':status(project)}


def revoke(project: Path | str, evidence_id: str, reason: str) -> dict:
    project = _root(project)
    if not reason.strip():
        raise CompositionError('revocation reason required')
    with _lock(project):
        _load(project)
        if evidence_id not in _attempts(project):
            raise CompositionError('unknown evidence')
        if evidence_id not in _revoked(project):
            _event(project,'revoked',evidence_id=evidence_id,reason=reason)
    return status(project)


def _alive(pid: int) -> bool:
    if os.name=='nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000,False,pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel.GetExitCodeProcess(ctypes.c_void_p(handle),ctypes.byref(code))
        kernel.CloseHandle(ctypes.c_void_p(handle))
        return code.value==259
    try:
        os.kill(pid,0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def resume(project: Path | str) -> dict:
    project = _root(project)
    with _lock(project):
        _load(project)
        finished = {e['evidence_id'] for e in _events(project) if e['type']=='finished'}
        started = {e['evidence_id'] for e in _events(project) if e['type']=='started'}
        for eid,item in _attempts(project).items():
            outcome = item['outcome']
            directory=project/'composition-attempts'/eid
            if eid not in started and not _alive(item['request']['owner_pid']):
                if outcome is not None or any(p.is_dir() for p in directory.iterdir()):
                    raise CompositionError('orphan execution cannot be safely reconstructed: '+eid)
                _event(project,'started',evidence_id=eid,request_hash=_hash(item['request']),recovered_pre_execution=True)
            if outcome is None and not _alive(item['request']['owner_pid']):
                outcome = {'status':'interrupted','phases':[], 'finished_at':rg.utc_now(),
                           'reason':'owner exited without terminal receipt; no automatic retry'}
                _write(project/'composition-attempts'/eid/'outcome.json',outcome)
            if outcome is not None and eid not in finished:
                _event(project,'finished',evidence_id=eid,outcome_hash=_hash(outcome),recovered=True)
    return status(project)


def record_readback(project: Path | str, claim_id: str, text: str, author: dict,
                    source_blind_declared: bool = False) -> dict:
    project = _root(project)
    if not text.strip() or not source_blind_declared:
        raise CompositionError('nonempty read-back and explicit source-blind declaration required')
    with _lock(project):
        bp = _load(project)
        claim = _claim(bp,claim_id)
        rb = 'readback-'+uuid.uuid4().hex
        _event(project,'readback_recorded',readback_id=rb,claim_id=claim_id,
               claim_identity=claim_identity(claim),text=text,author=author,
               source_blind_declared=True, independence_verified=False)
    return {'readback_id':rb,'independence_verified':False}


def attest_milestone(project: Path | str, claim_id: str, reviewer: str, decision: str,
                     readback_id: str) -> dict:
    project = _root(project)
    if decision not in {'approve','reject'} or not reviewer.strip():
        raise CompositionError('reviewer and approve/reject required')
    with _lock(project):
        bp = _load(project)
        claim = _claim(bp,claim_id)
        rows = [e for e in _events(project) if e['type']=='readback_recorded' and e['readback_id']==readback_id
                and e['claim_id']==claim_id and e['claim_identity']==claim_identity(claim)]
        if len(rows)!=1:
            raise CompositionError('matching immutable read-back required')
        _event(project,'milestone_attested',claim_id=claim_id,reviewer=reviewer,decision=decision,
               readback_id=readback_id,human_identity_verified=False)
    return status(project)


def export_asset(project: Path | str, claim_id: str, dest: Path | str) -> dict:
    project = _root(project)
    check = audit(project)
    if not check['valid'] or check['claims'].get(claim_id)!='accepted':
        raise CompositionError('only valid accepted claims may be exported')
    with _lock(project):
        bp = _load(project)
        attempts = _attempts(project)
        eid = next(e for e in check['evidence'] if check['evidence'][e]=='accepted' and attempts[e]['request']['claim_id']==claim_id)
        req = attempts[eid]['request']
        claim = _claim(bp,claim_id)
        artifact = project/req['artifact_path']
        payload = {'schema_version':1,'source_project':str(project),'source_evidence_id':eid,
                   'claim':claim,'claim_identity':claim_identity(claim),'guarantee':'empirical',
                   'artifact_sha256':rg.sha256_file(artifact),'artifact_base64':base64.b64encode(artifact.read_bytes()).decode(),
                   'artifact_suffix':artifact.suffix,'bindings':req['bindings'],
                   'premise_identities':{cid:claim_identity(_claim(bp,cid)) for cid in req['premises']},
                   'description':claim['statement'],'source_request_hash':_hash(req)}
        payload['asset_hash'] = _hash(payload)
        _write(Path(dest).resolve(),payload)
        _event(project,'asset_exported',evidence_id=eid,asset_hash=payload['asset_hash'])
    return {'asset_hash':payload['asset_hash'],'path':str(Path(dest).resolve())}


def import_asset(project: Path | str, claim_id: str, asset_file: Path | str) -> dict:
    project = _root(project)
    asset = rg.load_json(Path(asset_file).resolve())
    if asset.get('asset_hash') != _hash({k:v for k,v in asset.items() if k!='asset_hash'}):
        raise CompositionError('asset hash mismatch')
    source = _root(asset['source_project'])
    source_check = audit(source)
    source_request = _attempts(source).get(asset['source_evidence_id'],{}).get('request',{})
    if not source_check['valid'] or source_check['evidence'].get(asset['source_evidence_id'])!='accepted' or source_check['claims'].get(source_request.get('claim_id'))!='accepted':
        raise CompositionError('live source evidence is unavailable, revoked or invalid')
    if _hash(source_request)!=asset['source_request_hash']:
        raise CompositionError('source request mismatch')
    source_bp=_load(source)
    source_claim=_claim(source_bp,source_request['claim_id'])
    source_premises={cid:claim_identity(_claim(source_bp,cid)) for cid in source_request['premises']}
    if (asset.get('claim')!=source_claim or asset.get('claim_identity')!=claim_identity(source_claim)
            or asset.get('premise_identities')!=source_premises or asset.get('bindings')!=source_request['bindings']
            or asset.get('guarantee')!='empirical'):
        raise CompositionError('asset metadata differs from verified source claim and closure')
    current = status(project)
    if not current['valid']:
        raise CompositionError('target audit invalid')
    with _lock(project):
        bp = _load(project)
        if claim_identity(_claim(bp,claim_id))!=asset['claim_identity']:
            raise CompositionError('claim/environment/verifier contract incompatible')
        payload = base64.b64decode(asset['artifact_base64'],validate=True)
        if rg.sha256_bytes(payload)!=asset['artifact_sha256'] or asset['artifact_sha256']!=source_request['artifact_sha256']:
            raise CompositionError('asset payload mismatch')
        directory = project/'asset-imports'/uuid.uuid4().hex
        directory.mkdir(parents=True)
        suffix = Path(source_request['artifact_path']).suffix
        artifact = directory/('artifact'+suffix)
        artifact.write_bytes(payload)
        attempts = _attempts(project)
        bindings = {}
        for old_cid,identity in asset['premise_identities'].items():
            matches = [c['id'] for c in bp['claims'] if claim_identity(c)==identity]
            possible = [(c,eid) for c in matches for eid,item in attempts.items()
                        if item['request']['claim_id']==c and current['evidence'].get(eid)=='accepted'
                        and current['claims'][c]=='accepted']
            if not possible:
                raise CompositionError('import matching premise assets first: '+old_cid)
            c,eid = possible[0]
            bindings[c]=eid
    origin = {'asset_hash':asset['asset_hash'],'source_project':str(source),
              'source_evidence_id':asset['source_evidence_id']}
    if bindings:
        reduction = submit(project,{'kind':'reduction','claim_id':claim_id,'premises':list(bindings),
                                    'artifact':str(artifact),'origin':origin})
        if reduction['outcome']['status']!='conditionally_validated':
            return reduction
        return submit(project,{'kind':'validate','reduction_id':reduction['evidence_id'],
                               'bindings':bindings,'origin':origin})
    return submit(project,{'kind':'direct','claim_id':claim_id,'artifact':str(artifact),'origin':origin})


def search_assets(path: Path | str, query: str) -> dict:
    tokens = set(query.casefold().split())
    results = []
    for file in Path(path).resolve().rglob('*.json'):
        try:
            asset = rg.load_json(file)
            if not isinstance(asset,dict) or 'asset_hash' not in asset or 'claim_identity' not in asset:
                continue
            description = asset.get('description','')
            score = sum(t in description.casefold() for t in tokens)
            if score:
                results.append({'path':str(file),'asset_hash':asset['asset_hash'],'description':description,'score':score})
        except (OSError,ValueError):
            continue
    return {'retrieval':'lexical_candidate_search_only','reuse_authorized':False,
            'results':sorted(results,key=lambda x:(-x['score'],x['path']))}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='command',required=True)
    p = sub.add_parser('init'); p.add_argument('project'); p.add_argument('blueprint')
    p = sub.add_parser('submit'); p.add_argument('project'); p.add_argument('request')
    for name in ('status','audit','resume'):
        p=sub.add_parser(name); p.add_argument('project')
    p=sub.add_parser('revoke'); p.add_argument('project'); p.add_argument('evidence_id'); p.add_argument('--reason',required=True)
    for name in ('export','import'):
        p=sub.add_parser(name); p.add_argument('project'); p.add_argument('claim_id'); p.add_argument('asset')
    p=sub.add_parser('search'); p.add_argument('path'); p.add_argument('query')
    p=sub.add_parser('readback'); p.add_argument('project'); p.add_argument('claim_id'); p.add_argument('text_file'); p.add_argument('--author',required=True); p.add_argument('--source-blind-declared',action='store_true')
    p=sub.add_parser('attest'); p.add_argument('project'); p.add_argument('claim_id'); p.add_argument('readback_id'); p.add_argument('--reviewer',required=True); p.add_argument('--decision',choices=['approve','reject'],required=True)
    args=ap.parse_args()
    try:
        if args.command=='init': result=initialize(args.project,args.blueprint)
        elif args.command=='submit': result=submit(args.project,args.request)
        elif args.command in {'status','audit','resume'}: result=globals()[args.command](args.project)
        elif args.command=='revoke': result=revoke(args.project,args.evidence_id,args.reason)
        elif args.command=='export': result=export_asset(args.project,args.claim_id,args.asset)
        elif args.command=='import': result=import_asset(args.project,args.claim_id,args.asset)
        elif args.command=='search': result=search_assets(args.path,args.query)
        elif args.command=='readback': result=record_readback(args.project,args.claim_id,Path(args.text_file).read_text('utf-8'),{'id':args.author},args.source_blind_declared)
        else: result=attest_milestone(args.project,args.claim_id,args.reviewer,args.decision,args.readback_id)
        print(json.dumps(result,indent=2,ensure_ascii=False))
        if args.command=='audit' and not result['valid']: raise SystemExit(1)
    except (CompositionError,rg.GraphError,OSError,ValueError) as exc:
        print(json.dumps({'error':type(exc).__name__,'message':str(exc)},ensure_ascii=False))
        raise SystemExit(1)


if __name__=='__main__':
    main()

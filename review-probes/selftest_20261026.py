"""Rerunnable infrastructure self-test; no model calls or novelty claims."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]

def main():
    run_id = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    out = ROOT / 'test-output' / 'selftest' / run_id
    out.mkdir(parents=True)
    # Keep subprocess fixtures under the permitted workspace and below legacy
    # Windows MAX_PATH limits. System Temp can have incompatible inherited ACLs.
    fixture_root = ROOT.parents[2] / '.erl-test-tmp' / uuid.uuid4().hex[:8]
    fixture_root.mkdir(parents=True, exist_ok=False)
    commands = [
        [sys.executable, '-m', 'unittest', 'discover', '-s', 'evidence-research-loop/tests', '-v'],
        [sys.executable, 'tests/run_server_regressions.py'],
    ]
    inputs = [Path(__file__), ROOT / 'server.py', ROOT / 'worker.py', ROOT / 'tests/run_server_regressions.py']
    inputs += sorted((ROOT / 'evidence-research-loop').rglob('*.py'))
    protocol = {'purpose': 'infrastructure regression, not autonomous scientific discovery',
                'model_calls': 0, 'commands': commands, 'timeout_per_suite_sec': 600,
                'fixture_root': str(fixture_root), 'fixture_env': 'ERL_TEST_TMP',
                'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}}
    (out / 'protocol.json').write_text(json.dumps(protocol, indent=2), encoding='utf-8')
    records = []
    for i, command in enumerate(commands):
        started = time.monotonic()
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        env['ERL_TEST_TMP'] = str(fixture_root)
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, timeout=600)
        (out / f'{i}-stdout.txt').write_bytes(result.stdout)
        (out / f'{i}-stderr.txt').write_bytes(result.stderr)
        record = {'command': command, 'exit_code': result.returncode, 'elapsed_sec': time.monotonic()-started}
        records.append(record)
        print(json.dumps(record), flush=True)
    summary = {'run_id': run_id, 'output': str(out), 'suites': records,
               'all_suites_passed': all(r['exit_code'] == 0 for r in records)}
    (out / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    manifest = {str(p.relative_to(out)): hashlib.sha256(p.read_bytes()).hexdigest() for p in out.rglob('*') if p.is_file()}
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps(summary), flush=True)
    return 0 if summary['all_suites_passed'] else 1

if __name__ == '__main__':
    raise SystemExit(main())

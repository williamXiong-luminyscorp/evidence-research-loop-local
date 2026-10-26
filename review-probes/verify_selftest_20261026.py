"""Recompute this self-test summary from saved logs and MCP responses."""
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'test-output' / 'selftest'

def main():
    runs = []
    for name in ['20261026T211931Z-99ed7aa1', '20261026T212041Z-5c465da0', '20261026T212202Z-3fd566c7']:
        directory = EVIDENCE / name
        manifest = json.loads((directory / 'manifest.json').read_text())
        assert all(hashlib.sha256((directory / p).read_bytes()).hexdigest() == h for p, h in manifest.items())
        core = (directory / '0-stderr.txt').read_text(encoding='utf-8')
        server = [json.loads(line) for line in (directory / '1-stdout.txt').read_text(encoding='utf-8').splitlines() if line.startswith('{')][-1]
        runs.append({'id': name, 'manifest_valid': True, 'core_tests': int(re.search(r'Ran (\d+) tests', core)[1]),
                     'core_passed': core.rstrip().endswith('OK'), 'server_passed': server['passed'], 'server_total': server['total']})
    log = json.loads((EVIDENCE / 'live-selftest-20261026-mvprffri-complete.json').read_text(encoding='utf-8'))
    def results(side, op):
        return [r['response']['structuredContent'] for r in log if r['side'] == side and r['op'] == op]
    searches = {}
    for side in ['local', 'cloud']:
        before, after = [r['result'] for r in results(side, 'audit_project')]
        assert before['valid'] and after['valid'] and after['accepted']
        assert before['event_count'] == after['event_count'] == 34
        assert before['run_count'] == after['run_count'] == 10
        assert after['event_counts']['generation_closed'] == 2
        assert after['event_counts']['generation_duplicate'] == 1
        assert after['failure_count'] == 1
        assert after['holdout_completion']['complete']
        assert results(side, 'finalize_evidence')[0]['verify']['result']['valid']
        searches[side] = {'resume_did_not_duplicate': True, 'valid': True, 'accepted': True,
                          'runs': after['run_count'], 'holdout_runs': after['holdout_run_count'], 'events': after['event_count']}
    graph = [r['result'] for r in results('local', 'audit_research_graph')]
    assert graph[0]['valid'] and not graph[0]['accepted']
    assert graph[-1]['valid'] and graph[-1]['accepted'] and graph[-1]['contribution_count'] == 2
    scientist = results('cloud', 'audit_scientist_project')[0]['result']
    assert scientist['valid'] and scientist['accepted'] and not scientist['acceptance']['novelty_claim_allowed']
    assert runs[-1]['core_passed'] and runs[-1]['server_passed'] == 9
    print(json.dumps({'regressions': runs, 'searches': searches, 'graph_failed_then_repaired': True,
                      'scientist_fixture_accepted': True, 'new_model_calls': 0,
                      'scope': 'infrastructure regression; reused seeds and predefined fixtures, not novel independent problems'}, indent=2))

if __name__ == '__main__':
    main()

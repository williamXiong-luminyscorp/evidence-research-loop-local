"""Known fixtures for composition infrastructure; no model or research score.

Only the conditional phase uses explicit hypothetical children. Validation,
integration and holdout read the actual immutable artifacts bound by the engine.
"""
import json
from pathlib import Path
import sys


def apply(config, value):
    if config.get('operation') == 'scale' and type(config.get('factor')) is int:
        return value * config['factor']
    if config.get('operation') == 'offset' and type(config.get('amount')) is int:
        return value + config['amount']
    raise ValueError('unsupported component configuration')


def check(request):
    artifact = json.loads(Path(request['artifact']).read_text('utf-8'))
    claim_id = request['claim']['id']
    phase = request['phase']
    points = {'direct': [0, 1, 2], 'composition': [0, 1, 2],
              'integration': [-3, 4, 20], 'holdout': [17, 23, -13]}[phase]
    if claim_id in ('scale', 'offset'):
        if phase != 'direct' or artifact.get('operation') != claim_id:
            raise ValueError('wrong component or phase')
        expected = (lambda x: 2*x) if claim_id == 'scale' else (lambda x: x+3)
        evaluate = lambda x: apply(artifact, x)
        conditional = False
    elif claim_id == 'pipeline':
        if request['kind'] == 'reduction' and phase == 'composition':
            if {c['id'] for c in request['premise_claims']} != {'scale', 'offset'}:
                raise ValueError('both exact premise contracts required')
            children = {'scale': {'operation': 'scale', 'factor': 2},
                        'offset': {'operation': 'offset', 'amount': 3}}
            conditional = True
        else:
            entries = request['child_artifacts']
            if set(entries) != {'scale', 'offset'}:
                raise ValueError('actual child bindings required')
            children = {k: json.loads(Path(v['artifact']).read_text('utf-8')) for k, v in entries.items()}
            conditional = False
        order = artifact.get('order')
        if not isinstance(order, list) or len(order) != 2 or set(order) != {'scale', 'offset'}:
            raise ValueError('composition requires each component exactly once')
        def evaluate(x):
            for component in order:
                x = apply(children[component], x)
            return x
        expected = lambda x: 2*x+3
    else:
        raise ValueError('unknown fixture claim')
    cases = [{'input': x, 'actual': evaluate(x), 'expected': expected(x)} for x in points]
    for row in cases:
        row['passed'] = row['actual'] == row['expected']
    return {'passed': all(row['passed'] for row in cases), 'cases': cases,
            'conditional': conditional, 'guarantee': 'empirical',
            'scope': 'fixed infrastructure smoke fixtures; not autonomous research or unseen data'}


if __name__ == '__main__':
    request = json.loads(Path(sys.argv[1]).read_text('utf-8'))
    try:
        result = check(request)
    except (KeyError, ValueError, TypeError, OSError) as exc:
        result = {'passed': False, 'error': type(exc).__name__ + ': ' + str(exc)}
    Path(sys.argv[2], 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')

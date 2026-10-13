"""Explicit allowlist for the v137 prospective dev-answer validation only."""
import json
from pathlib import Path
from .qasper_experiment import sha

PROTOCOL='9e70368ef88f49e535e47816daceb396d93510c0468caaf0fcc737af50c43c8e'
REGISTRY=Path(__file__).with_name('qasper_validation_cohorts.json')

def cohort(bundle):
    bundle=Path(bundle)
    spec=json.loads((bundle/'validation-batch.json').read_text(encoding='utf-8'))
    if set(spec)!={'protocol_sha256','batch'} or spec['protocol_sha256']!=PROTOCOL:
        raise ValueError('Unknown validation protocol')
    if type(spec['batch']) is not int or not 1<=spec['batch']<=8:
        raise ValueError('Unknown validation batch')
    registry=json.loads(REGISTRY.read_text(encoding='utf-8'))
    if registry['protocol_sha256']!=PROTOCOL:raise ValueError('Registry protocol mismatch')
    selected=registry['batches'][str(spec['batch'])]
    for name,digest in selected['files'].items():
        if sha((bundle/name).read_bytes())!=digest:raise ValueError('Validation cohort changed')
    return selected

def data_split(bundle):
    bundle=Path(bundle)
    if (bundle/'validation-batch.json').exists():
        if (bundle/'train').exists():raise ValueError('Validation must retain dev identity')
        cohort(bundle)
        return 'dev'
    if (bundle/'dev').exists():raise ValueError('Dev requires frozen validation allowlist')
    return 'train'

def check_order(bundle,requests):
    if data_split(bundle)=='dev':
        keys=[[r['question_id'],r['method']] for r in requests]
        if keys!=cohort(bundle)['order']:raise ValueError('Validation schedule changed')

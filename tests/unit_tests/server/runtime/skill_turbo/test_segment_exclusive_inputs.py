"""互斥来源按显式键选取，不能用历史来源掩盖无效显式值。"""

import copy

import pytest

from segment_state_fixtures import storage
from test_segment_inputs import make_spec, setup
from jiuwenswarm.server.runtime.skill_turbo.segment_inputs import SegmentInputError, assemble_segment_inputs


def exclusive_spec():
    spec = make_spec(properties={'manifest_path': {'type': 'string', 'minLength': 1},
                                 'page_files': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}, 'minItems': 1}})
    schema = {**spec.input_schema, 'oneOf': [{'required': ['manifest_path']}, {'required': ['page_files']}]}
    return spec.model_copy(update={'input_schema': schema})


@pytest.mark.parametrize('params', [
    {'manifest_path': 'selected.json'}, {'page_files': ['selected.pptx']},
])
async def test_explicit_source_excludes_other_inherited_source_without_ledger_mutation(storage, tmp_path, params):
    ledger, ctx = await setup(storage, tmp_path)
    ledger.data['facts'].update(manifest_path='old.json', page_files=['old.pptx'])
    before = copy.deepcopy(ledger.data['facts'])
    inputs, _ = await assemble_segment_inputs(ctx, exclusive_spec(), params, ledger)
    selected = next(iter(params))
    assert selected in inputs
    assert ({'manifest_path', 'page_files'} - {selected}).isdisjoint(inputs)
    assert ledger.data['facts'] == before


@pytest.mark.parametrize('params', [
    {'manifest_path': None}, {'manifest_path': ''}, {'page_files': None}, {'page_files': []},
    {'page_files': ['']}, {'manifest_path': None, 'page_files': ['ok.pptx']},
    {'manifest_path': 'ok.json', 'page_files': None}, {'manifest_path': 'ok.json', 'page_files': ['ok.pptx']},
])
async def test_explicit_invalid_or_ambiguous_source_never_falls_back(storage, tmp_path, params):
    ledger, ctx = await setup(storage, tmp_path)
    ledger.data['facts'].update(manifest_path='old.json', page_files=['old.pptx'])
    with pytest.raises(SegmentInputError) as caught:
        await assemble_segment_inputs(ctx, exclusive_spec(), params, ledger)
    assert caught.value.status == 'invalid_args'


async def test_default_uses_first_source_only_and_never_historical_list(storage, tmp_path):
    ledger, ctx = await setup(storage, tmp_path)
    ledger.data['facts'].update(manifest_path='old.json', page_files=['old.pptx'])
    ledger.data['facts_provenance']['manifest_path'] = {'verified': True, 'produced_by': 'generate'}
    inputs, _ = await assemble_segment_inputs(ctx, exclusive_spec(), {}, ledger)
    assert 'manifest_path' in inputs and 'page_files' not in inputs
    ledger.data['facts'].pop('manifest_path')
    with pytest.raises(SegmentInputError, match='exclusive_source_missing_or_unverified'):
        await assemble_segment_inputs(ctx, exclusive_spec(), {}, ledger)


async def test_unused_escaping_historical_source_does_not_override_explicit_source(storage, tmp_path):
    ledger, ctx = await setup(storage, tmp_path)
    ledger.data['facts']['manifest_path'] = str(tmp_path.parent / 'outside.json')
    inputs, _ = await assemble_segment_inputs(ctx, exclusive_spec(), {'page_files': ['one.pptx']}, ledger)
    assert 'manifest_path' not in inputs and len(inputs['page_files']) == 1

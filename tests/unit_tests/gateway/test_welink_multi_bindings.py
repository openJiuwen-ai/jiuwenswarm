import pytest

from jiuwenswarm.gateway.channel_manager.im_platforms.welink import welink_binding as binding


def legacy():
    return dict(account='reader', persistent_agent_id='mate_1', assistant_credential_id='ak1',
                assistant_secret='secret1', credential_dir='/old/mate_1', cli_authorized=True, enabled=True)


def test_legacy_normalization_preserves_reader_and_assistant_state():
    result = binding.normalize_welink_section(legacy())
    assert result['reader_credential_dir'] == '/old/mate_1'
    assert result['bindings'][0]['credential_dir'] == '/old/mate_1'
    assert result['bindings'][0]['enabled'] is True
    assert 'assistant_secret' not in result


def test_second_binding_keeps_first_and_shared_login():
    original = legacy()
    result = binding.apply_welink_authorization(original, dict(account='reader', persistent_agent_id='mate_2',
        assistant_credential_id='ak2', assistant_secret='secret2', credential_dir='/assistants/mate_2'))
    assert result['cli_authorized'] is True
    assert result['bindings'][0]['assistant_secret'] == 'secret1'
    assert [item['enabled'] for item in result['bindings']] == [True, True]
    assert original == legacy()


def test_revoke_only_removes_selected_binding_and_keeps_reader_login():
    current = binding.apply_welink_authorization(legacy(), dict(persistent_agent_id='mate_2',
        config_id='config_2', assistant_credential_id='ak2', assistant_secret='secret2',
        credential_dir='/assistants/mate_2'))
    result = binding.revoke_welink_authorization(current, 'config_2')
    assert [item['persistent_agent_id'] for item in result['bindings']] == ['mate_1']
    assert result['enabled'] is True
    assert result['cli_authorized'] is True


def test_same_credential_moves_legacy_office_binding_onto_its_connection_agent():
    current = dict(account='reader', cli_authorized=True, bindings=[
        dict(persistent_agent_id='mate_office', assistant_credential_id='ak-coach', assistant_secret='secret',
             credential_dir='/coach', legacy_identity_fallback=True, enabled=True),
        dict(persistent_agent_id='mate_other_conn', config_id='mate_other', assistant_credential_id='ak-other',
             assistant_secret='secret2', credential_dir='/other', enabled=True),
    ])
    result = binding.apply_welink_authorization(current, dict(
        account='reader', persistent_agent_id='mate_new_conn', config_id='mate_office',
        assistant_credential_id='ak-coach', assistant_secret='secret', credential_dir='/coach'))
    assert [(item['persistent_agent_id'], item.get('config_id'), item['assistant_credential_id'])
            for item in result['bindings']] == [
        ('mate_new_conn', 'mate_office', 'ak-coach'),
        ('mate_other_conn', 'mate_other', 'ak-other'),
    ]
    assert result['bindings'][0]['credential_dir'] == '/coach'


def test_another_colleague_still_cannot_reuse_a_credential():
    current = dict(account='reader', cli_authorized=True, bindings=[
        dict(persistent_agent_id='mate_office', assistant_credential_id='ak-coach', assistant_secret='secret',
             credential_dir='/coach', enabled=True),
    ])
    with pytest.raises(ValueError, match='another colleague'):
        binding.apply_welink_authorization(current, dict(
            persistent_agent_id='mate_stranger', config_id='mate_stranger',
            assistant_credential_id='ak-coach', assistant_secret='secret'))


def test_duplicate_credentials_and_mismatched_reader_fail_without_mutation():
    original = legacy()
    for patch in [dict(persistent_agent_id='mate_2', assistant_credential_id='ak1', assistant_secret='s'),
                  dict(persistent_agent_id='mate_2', account='another')]:
        with pytest.raises(ValueError):
            binding.apply_welink_authorization(original, patch)
    assert original == legacy()


def test_duplicate_agent_ids_in_config_fail_closed():
    current = dict(account='reader', reader_credential_dir='/reader', cli_authorized=True,
        bindings=[dict(persistent_agent_id='mate_1', assistant_credential_id='ak1'),
                  dict(persistent_agent_id='mate_1', assistant_credential_id='ak2')])
    with pytest.raises(ValueError):
        binding.normalize_welink_section(current)


def test_public_status_selects_colleague_and_never_exposes_secrets_or_paths():
    current = binding.apply_welink_authorization(legacy(), dict(persistent_agent_id='mate_2',
        assistant_credential_id='ak2', assistant_secret='secret2', credential_dir='/assistants/mate_2', assistant_account='member2'))
    result = binding.public_welink_authorization(current, 'mate_2')
    assert result['persistent_agent_id'] == 'mate_2'
    assert result['assistant_account'] == 'member2'
    assert result['cli_ready'] is True
    assert result['agent_ids'] == ['mate_1', 'mate_2']
    assert len(result['bindings']) == 2
    assert 'secret' not in str(result)
    assert '/old' not in str(result)
    missing = binding.public_welink_authorization(current, 'mate_3')
    assert missing['cli_ready'] is True
    assert missing['assistant_ready'] is False


def test_normalization_requires_assistant_directory_and_respects_global_disable():
    current = binding.normalize_welink_section(legacy())
    current['bindings'][0]['credential_dir'] = ''
    assert binding.normalize_welink_section(current)['enabled'] is False
    current = binding.normalize_welink_section(legacy())
    current['enabled'] = False
    assert binding.normalize_welink_section(current)['bindings'][0]['enabled'] is False


@pytest.mark.parametrize('interval', [0, -1, 'invalid', float('nan'), float('inf')])
def test_invalid_poll_interval_rejected(interval):
    with pytest.raises(ValueError):
        binding.apply_welink_authorization(legacy(), {'poll_interval_seconds': interval})


def test_reader_account_matching_is_case_insensitive():
    result = binding.apply_welink_authorization(legacy(), {'account': 'READER'})
    assert result['account'] == 'reader'


def test_replacing_the_credential_drops_the_learned_member_id():
    current = binding.apply_welink_authorization(legacy(), dict(persistent_agent_id='mate_1',
        assistant_account='dig_1_a'))
    kept = binding.apply_welink_authorization(current, dict(persistent_agent_id='mate_1',
        assistant_credential_id='ak1', assistant_secret='secret1'))
    assert kept['bindings'][0]['assistant_account'] == 'dig_1_a'
    rotated = binding.apply_welink_authorization(kept, dict(persistent_agent_id='mate_1',
        assistant_credential_id='ak-new', assistant_secret='secret-new'))
    assert rotated['bindings'][0]['assistant_account'] == ''


def test_learned_member_id_is_kept_for_one_credential_and_refused_for_another():
    section = {'bindings': [
        {'persistent_agent_id': 'mate_1', 'assistant_credential_id': 'ak1', 'assistant_account': ''},
        {'persistent_agent_id': 'mate_2', 'assistant_credential_id': 'ak2', 'assistant_account': 'dig_1_b'},
    ]}
    assert binding.assign_learned_assistant_account(section, agent_id='mate_1', credential_id='ak1', account='dig_1_a')
    assert section['bindings'][0]['assistant_account'] == 'dig_1_a'
    assert binding.assign_learned_assistant_account(section, agent_id='mate_1', credential_id='ak1', account='dig_1_a')
    assert not binding.assign_learned_assistant_account(section, agent_id='mate_1', credential_id='ak1', account='dig_1_other')
    assert section['bindings'][0]['assistant_account'] == 'dig_1_a'
    assert not binding.assign_learned_assistant_account(section, agent_id='mate_1', credential_id='ak1', account='dig_1_b')
    assert not binding.assign_learned_assistant_account(section, agent_id='mate_1', credential_id='ak-old', account='dig_1_c')


def test_only_migrated_legacy_binding_retains_single_assistant_identity_fallback():
    migrated = binding.normalize_welink_section(legacy())
    assert migrated['bindings'][0]['legacy_identity_fallback'] is True
    assert 'legacy_identity_fallback' not in str(binding.public_welink_authorization(migrated))
    updated = binding.apply_welink_authorization(migrated, dict(persistent_agent_id='mate_2',
        assistant_credential_id='ak2', assistant_secret='secret2', credential_dir='/assistants/mate_2'))
    assert updated['bindings'][0]['legacy_identity_fallback'] is True
    assert not updated['bindings'][1].get('legacy_identity_fallback')

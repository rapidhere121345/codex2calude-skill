import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

spec=importlib.util.spec_from_file_location('collector',Path(__file__).resolve().parents[1]/'scripts/collect-audit-health.py')
collector=importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def test_quota_allowlist_never_exports_credentials_or_raw_payloads():
    value={'available':True,'source':'claude_code_usage_command','token':'SECRET','raw':'PROMPT','five_hour':{'used_percent':30,'remaining_percent':70,'access_token':'SECRET'}}
    assert collector.safe_quota(value)=={'available':True,'source':'claude_code_usage_command','five_hour':{'used_percent':30,'remaining_percent':70}}


def test_missing_quota_stays_unknown():
    assert collector.safe_quota(None)=={'available':False,'reason':'unknown'}


def test_quota_probe_source_and_every_reason_survive_sanitization():
    from claude_research_mcp import quota_probe
    for reason in sorted(quota_probe.REASONS):
        account=collector.sanitize_account({'status':'ok','logged_in':True,'quota':{
            'available':False,'source':'anthropic_rate_limit_headers','reason':reason,
            'observed_at':'2026-11-11T03:00:00+00:00','age_seconds':42,'stale':True,
            'manual_check_url':'https://claude.ai/usage','access_token':'SECRET'}},'claude')
        quota=account['quota']
        assert quota['available'] is False
        assert quota['source']=='anthropic_rate_limit_headers'
        assert quota['reason']==reason
        assert quota['age_seconds']==42
        assert quota['stale'] is True
        assert 'SECRET' not in json.dumps(account)


def test_quota_probe_windows_and_metadata_survive_sanitization():
    account=collector.sanitize_account({'status':'ok','logged_in':True,'quota':{
        'available':True,'source':'anthropic_rate_limit_headers','reason':'probe_timeout',
        'observed_at':'2026-11-11T03:00:00+00:00','age_seconds':100,'stale':True,
        'five_hour':{'used_percent':20,'remaining_percent':80,
                     'resets_at':'2026-11-11T05:00:00+00:00','raw':'SECRET'},
        'seven_day':{'used_percent':10,'remaining_percent':90,
                     'resets_at':'2026-11-18T03:00:00+00:00'},
        'authorization':'Bearer SECRET'}},'claude')
    quota=account['quota']
    assert quota['available'] is True
    assert quota['source']=='anthropic_rate_limit_headers'
    assert quota['reason']=='probe_timeout'
    assert quota['stale'] is True
    assert quota['five_hour']=={'used_percent':20,'remaining_percent':80,
                               'resets_at':'2026-11-11T05:00:00+00:00'}
    assert quota['seven_day']['used_percent']==10
    assert 'SECRET' not in json.dumps(account)


def test_quota_unknown_source_and_reason_are_rejected():
    account=collector.sanitize_account({'status':'ok','logged_in':True,'quota':{
        'available':False,'source':'SECRET_SOURCE','reason':'SECRET_REASON',
        'token':'SECRET'}},'claude')
    assert account['quota']=={'available':False,'reason':'unknown'}
    assert 'SECRET' not in json.dumps(account)


def test_snapshot_contains_only_audit_metadata_and_known_tokens(tmp_path, monkeypatch):
    import sqlite3
    source=tmp_path/'source.sqlite3'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE audit_log (id INTEGER, principal TEXT, tool TEXT, status TEXT, created_at TEXT, input_tokens INTEGER, access_token TEXT, prompt TEXT)')
        db.execute('INSERT INTO audit_log VALUES (1,"owner","claude_once","success","2026-10-09",NULL,"SECRET","PRIVATE_PROMPT")')
        db.execute('CREATE TABLE conversation_history (content TEXT)')
        db.execute('INSERT INTO conversation_history VALUES ("PRIVATE_RESULT")')
    monkeypatch.setattr(collector,'make_group_readable',lambda p:p.chmod(0o640))
    collector.snapshot_audit('claude',tmp_path,source=source)
    output=tmp_path/'claude.sqlite3'
    with sqlite3.connect(output) as db:
        assert [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]==['audit_log']
        assert 'access_token' not in [r[1] for r in db.execute('PRAGMA table_info(audit_log)')]
        assert db.execute('SELECT input_tokens FROM audit_log').fetchone()[0] is None
    raw=output.read_bytes()
    assert all(v not in raw for v in [b'SECRET',b'PRIVATE_PROMPT',b'PRIVATE_RESULT'])
    assert not list(tmp_path.glob('.*snapshot*'))


def test_account_nested_login_and_structured_secrets():
    account=collector.sanitize_account({'status':'ok','auth':{'logged_in':False,'refresh_token':'SECRET'},'availability':{'status':{'refresh_token':'SECRET'},'eligible':{'token':'SECRET'}},'models':{'gemini-3-8-flash-high':{'reasoning_effort':{'refresh_token':'SECRET'},'cli_supported':True}},'cli_version':{'token':'SECRET'}},'gemini')
    assert account['logged_in'] is False
    assert account['models']['gemini-3-8-flash-high']=={'cli_supported':True}
    assert account['availability']=={}
    assert 'SECRET' not in str(account)


def test_conflicting_login_values_fail_closed():
    account=collector.sanitize_account({'logged_in':True,'auth':{'logged_in':False}})
    assert account['logged_in'] is False


def unified_status(*, claude=None, gemini=None):
    return {
        'principal': 'owner',
        'providers': {
            'claude': claude or {
                'status': 'ok', 'logged_in': True,
                'default_model': 'opus-5-5-high',
                'models': {
                    'opus-5-5-high': {
                        'enabled': True, 'verified': False, 'reasoning_effort': 'high'
                    },
                    'fable-5-2-high': {
                        'enabled': False, 'verified': False, 'reasoning_effort': 'high'
                    },
                },
                'quota': {'available': True, 'source': 'claude_code_statusline',
                          'five_hour': {'used_percent': 0, 'remaining_percent': 100}},
            },
            'gemini': gemini or {
                'status': 'ok', 'logged_in': True,
                'default_model': 'gemini-3-8-flash-high',
                'models': {
                    'gemini-3-8-flash-high': {
                        'enabled': True, 'verified': True, 'reasoning_effort': 'high'
                    }
                },
                'quota': {'available': False, 'reason': 'not_reported_by_agy'},
            },
        },
    }


def mock_unified_process(monkeypatch, status, *, tools=None, status_response=None):
    class FakeProcess:
        def __init__(self):
            self.stdin = io.BytesIO()

        def wait(self, timeout=None):
            return 0

    spawned = []
    def spawn(command, **kwargs):
        spawned.append((command, kwargs))
        return FakeProcess()

    # The stdio contract responds only after each request has been sent.
    expected_tools = ['research_'+suffix for suffix in (
        'status','once','start','continue','list_conversations','export_conversation','close'
    )]
    responses = [
        {'serverInfo': {'version': '1.0'}},
        {'tools': [{'name': name} for name in (tools if tools is not None else expected_tools)]},
        status_response if status_response is not None else {
            'structuredContent': status
        },
    ]
    reads = []
    def read(proc, request_id, deadline):
        reads.append((
            request_id, deadline,
            [json.loads(line) for line in proc.stdin.getvalue().splitlines()],
        ))
        return responses.pop(0)

    monkeypatch.setattr(collector.subprocess, 'Popen', spawn)
    monkeypatch.setattr(collector, 'rpc_read', read)
    return spawned, reads


def test_probe_unified_spawns_once_with_fixed_argv_and_sequential_rpc(monkeypatch):
    status = unified_status()
    status['providers']['claude']['quota'].update({
        'observed_at': '2026-11-09T10:00:00+00:00',
        'stale': True,
    })
    status['providers']['claude']['quota']['five_hour']['resets_at'] = (
        '2026-11-09T15:00:00+00:00'
    )
    spawned, reads = mock_unified_process(monkeypatch, status)
    results = collector.probe_unified()
    assert len(spawned) == 1
    command, options = spawned[0]
    assert command == [
        'runuser', '-u', 'researchmcp', '--',
        '/opt/remote-research-mcp/current/scripts/run-remote-research-forced-command.sh',
        'owner',
    ]
    assert options == {
        'stdin': subprocess.PIPE, 'stdout': subprocess.PIPE,
        'stderr': subprocess.DEVNULL, 'start_new_session': True,
    }
    assert [item[0] for item in reads] == [1, 2, 3]
    assert len({item[1] for item in reads}) == 1
    assert [item['method'] for item in reads[0][2]] == ['initialize']
    assert [item['method'] for item in reads[1][2]] == [
        'initialize', 'notifications/initialized', 'tools/list'
    ]
    assert [item['method'] for item in reads[2][2]] == [
        'initialize', 'notifications/initialized', 'tools/list', 'tools/call'
    ]
    assert reads[2][2][-1]['params'] == {
        'name': 'research_status', 'arguments': {}
    }
    assert set(results) == {'claude', 'gemini'}
    for result in results.values():
        assert result['probe_ok'] is True
        assert result['account_ready'] is True
        assert result['transport']['probe_kind'] == 'local_unified_forced_command'
        assert result['transport']['initialize_ok'] is True
        assert result['transport']['tools_count'] == 7
    assert results['claude']['account']['quota']['five_hour']['used_percent'] == 0
    assert results['claude']['account']['quota']['five_hour']['resets_at'] == (
        '2026-11-09T15:00:00+00:00'
    )
    assert results['claude']['account']['quota']['observed_at'] == (
        '2026-11-09T10:00:00+00:00'
    )
    assert results['claude']['account']['quota']['stale'] is True
    assert results['claude']['account']['models']['fable-5-2-high']['enabled'] is False
    assert results['gemini']['account']['models']['gemini-3-8-flash-high']['verified'] is True


def test_unified_logged_out_transport_healthy_but_account_and_quota_unavailable(monkeypatch):
    status = unified_status()
    claude = status['providers']['claude']
    claude['logged_in'] = True
    claude['auth'] = {'logged_in': False, 'oauth_token': 'SECRET'}
    claude['service_available'] = True
    claude['quota']['five_hour']['used_percent'] = 63
    spawned, _ = mock_unified_process(monkeypatch, status)
    result = collector.probe_unified()
    assert len(spawned) == 1
    assert result['claude']['probe_ok'] is True
    assert result['claude']['account_ready'] is False
    account = result['claude']['account']
    assert account['logged_in'] is False
    assert account['service_available'] is False
    assert account['service_unavailable_reason'] == 'requires_claude_ai_login'
    assert account['quota'] == {
        'available': False, 'reason': 'requires_claude_ai_login',
        'manual_check_url': 'https://claude.ai/usage',
    }
    assert result['gemini']['probe_ok'] is True
    assert 'SECRET' not in str(result)


def test_busy_provider_does_not_hide_other_or_fabricate_quota(monkeypatch):
    status = unified_status()
    status['providers']['claude'] = {
        'status': 'busy', 'error': 'busy', 'quota': None,
        'models': status['providers']['claude']['models'],
    }
    mock_unified_process(monkeypatch, status)
    result = collector.probe_unified()
    assert result['claude']['probe_ok'] is True
    assert result['claude']['account_ready'] is None
    assert result['claude']['provider_error_category'] == 'busy'
    assert result['claude']['account']['quota'] == {'available': False, 'reason': 'unknown'}
    assert result['gemini']['probe_ok'] is True
    assert result['gemini']['account_ready'] is True
    assert 'used_percent' not in str(result['claude'])


@pytest.mark.parametrize('negative', [
    {'availability': {'status': 'ineligible', 'eligible': False}},
    {'model_selection_ready': False},
    {'service_available': False, 'service_unavailable_reason': 'recent_api_auth_failure'},
])
def test_known_provider_negatives_override_logged_in_readiness(monkeypatch, negative):
    status = unified_status()
    status['providers']['gemini'].update(negative)
    mock_unified_process(monkeypatch, status)
    result = collector.probe_unified()
    assert result['gemini']['probe_ok'] is True
    assert result['gemini']['account']['logged_in'] is True
    assert result['gemini']['account_ready'] is False
    assert result['claude']['account_ready'] is True


@pytest.mark.parametrize('bad_status,bad_tools', [
    ({'principal': 'partner', 'providers': unified_status()['providers']}, None),
    ({'principal': 'owner', 'providers': {'claude': {'status': 'ok'}}}, None),
    ({'principal': 'owner', 'providers': {'claude': {}, 'gemini': {'status': 'ok'}}}, None),
    (None, ['research_status', 'claude_once']),
    (unified_status(), ['research_status'] * 7),
])
def test_invalid_unified_response_or_catalog_fails_both_providers_closed(
    monkeypatch, bad_status, bad_tools
):
    mock_unified_process(monkeypatch, bad_status, tools=bad_tools)
    result = collector.probe_unified()
    assert {name: value['probe_ok'] for name, value in result.items()} == {
        'claude': False, 'gemini': False
    }
    assert all('account' not in value for value in result.values())


def test_malformed_unified_status_text_fails_both_providers_closed(monkeypatch):
    mock_unified_process(
        monkeypatch, unified_status(),
        status_response={'content': [{'type': 'text', 'text': '{private-token'}]},
    )
    result = collector.probe_unified()
    assert all(value['probe_ok'] is False for value in result.values())
    assert all('account' not in value for value in result.values())
    assert 'private-token' not in json.dumps(result)


def test_structured_secret_fields_and_unknown_models_do_not_leave_collector():
    account = collector.sanitize_account({
        'status': 'ok', 'logged_in': True, 'default_model': 'pro',
        'secret': 'SECRET', 'reasoning_effort': 'SECRET',
        'service_unavailable_reason': 'SECRET',
        'models': {
            'opus-5-5-high': {'enabled': True, 'verified': False,
                              'reasoning_effort': 'xhigh', 'api_key': 'SECRET'},
            'fable-5-2-high': {'enabled': False, 'verified': False},
            'gemini-3-8-flash-high': {'enabled': True},
            'pro': {'enabled': True, 'token': 'SECRET'},
        },
        'quota': {
            'available': True, 'source': 'SECRET',
            'observed_at': '2026-11-09T10:30:00+00:00',
            'five_hour': {'used_percent': 0, 'remaining_percent': 100,
                          'resets_at': '2026-11-09T15:00:00+00:00', 'raw': 'SECRET'},
            'seven_day': {'used_percent': 'SECRET'},
        },
    }, 'claude')
    assert account['models'] == {
        'opus-5-5-high': {'enabled': True, 'verified': False, 'reasoning_effort': 'xhigh'},
        'fable-5-2-high': {'enabled': False, 'verified': False},
    }
    assert account['quota']['five_hour'] == {
        'used_percent': 0, 'remaining_percent': 100,
        'resets_at': '2026-11-09T15:00:00+00:00',
    }
    assert account['quota']['observed_at'] == '2026-11-09T10:30:00+00:00'
    assert 'SECRET' not in json.dumps(account)


def test_main_uses_one_unified_probe_but_preserves_both_audits_and_ssh_banner(
    tmp_path, monkeypatch
):
    output = tmp_path / 'health.json'
    calls = []
    monkeypatch.setattr(sys, 'argv', ['collect-audit-health.py', '--output', str(output)])
    monkeypatch.setattr(collector, 'snapshot_audit', lambda name, path: calls.append(('audit', name)))
    monkeypatch.setattr(collector, 'probe_unified', lambda: (
        calls.append(('probe', 'unified')) or {
            name: {'probe_ok': True, 'transport': {'probe_kind': 'local_unified_forced_command'}}
            for name in ('claude', 'gemini')
        }
    ))
    monkeypatch.setattr(collector, 'system_metrics', lambda: {
        'workers': {'claude': 2, 'gemini': 3}
    })
    monkeypatch.setattr(collector, 'make_group_readable', lambda path: None)

    class FakeSocket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, value): pass
        def recv(self, size): return b'SSH-2.0-fake'

    monkeypatch.setattr(collector.socket, 'create_connection', lambda address, timeout: FakeSocket())
    collector.main()
    payload = json.loads(output.read_text())
    assert calls == [('audit', 'claude'), ('audit', 'gemini'), ('probe', 'unified')]
    assert payload['system']['workers'] == {'claude': 2, 'gemini': 3}
    assert all(item['transport']['ssh_banner_ok'] is True for item in payload['bridges'].values())


def test_wal_source_remains_query_only(tmp_path):
    import sqlite3
    from contextlib import closing
    source=tmp_path/'wal.sqlite3'
    with closing(sqlite3.connect(source)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE audit_log (principal TEXT)')
        writer.execute("INSERT INTO audit_log VALUES ('owner')")
        writer.commit()
        with closing(collector.open_audit_source(source,'claude',service_identity=False)) as reader:
            assert reader.execute('SELECT principal FROM audit_log').fetchone()==('owner',)
            import pytest
            with pytest.raises(sqlite3.OperationalError):
                reader.execute("INSERT INTO audit_log VALUES ('partner')")


def test_source_identity_is_restored_when_sqlite_open_fails(monkeypatch,tmp_path):
    from types import SimpleNamespace
    import sqlite3
    import pytest
    identity={'uid':0,'gid':0};seen=[]
    monkeypatch.setattr(collector.os,'geteuid',lambda:identity['uid'])
    monkeypatch.setattr(collector.os,'getegid',lambda:identity['gid'])
    monkeypatch.setattr(collector.os,'seteuid',lambda value:identity.update(uid=value))
    monkeypatch.setattr(collector.os,'setegid',lambda value:identity.update(gid=value))
    monkeypatch.setattr(collector.pwd,'getpwnam',lambda name:SimpleNamespace(pw_uid=997,pw_gid=996))
    def fail(*args,**kwargs):
        seen.append(identity.copy())
        raise sqlite3.OperationalError('cannot open')
    monkeypatch.setattr(collector.sqlite3,'connect',fail)
    with pytest.raises(sqlite3.OperationalError):
        collector.open_audit_source(tmp_path/'missing','gemini',service_identity=True)
    assert seen==[{'uid':997,'gid':996}]
    assert identity=={'uid':0,'gid':0}

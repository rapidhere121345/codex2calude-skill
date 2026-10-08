import importlib.util
from pathlib import Path

spec=importlib.util.spec_from_file_location('collector',Path(__file__).resolve().parents[1]/'scripts/collect-audit-health.py')
collector=importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def test_quota_allowlist_never_exports_credentials_or_raw_payloads():
    value={'available':True,'source':'claude_code_usage_command','token':'SECRET','raw':'PROMPT','five_hour':{'used_percent':30,'remaining_percent':70,'access_token':'SECRET'}}
    assert collector.safe_quota(value)=={'available':True,'source':'claude_code_usage_command','five_hour':{'used_percent':30,'remaining_percent':70}}


def test_missing_quota_stays_unknown():
    assert collector.safe_quota(None)=={'available':False,'reason':'unknown'}


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
    account=collector.sanitize_account({'status':'ok','auth':{'logged_in':False,'refresh_token':'SECRET'},'availability':{'status':{'refresh_token':'SECRET'},'eligible':{'token':'SECRET'}},'models':{'pro':{'reasoning_effort':{'refresh_token':'SECRET'},'cli_supported':True}},'cli_version':{'token':'SECRET'}})
    assert account['logged_in'] is False
    assert account['models']['pro']=={'cli_supported':True}
    assert account['availability']=={}
    assert 'SECRET' not in str(account)


def test_conflicting_login_values_fail_closed():
    account=collector.sanitize_account({'logged_in':True,'auth':{'logged_in':False}})
    assert account['logged_in'] is False


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

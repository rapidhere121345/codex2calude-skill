#!/usr/bin/env python3
"""Read-only bridge health collection. No inference or credential file access."""
import argparse
from contextlib import closing
import grp
import datetime as dt
import json
import os
from pathlib import Path
import pwd
import selectors
import signal
import socket
import sqlite3
import subprocess
import time
import tempfile


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def rpc_read(proc, request_id, deadline):
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    data = bytearray()
    try:
        while time.monotonic() < deadline:
            if not sel.select(max(0, deadline-time.monotonic())):
                raise TimeoutError()
            block = os.read(proc.stdout.fileno(), 65536)
            if not block:
                raise EOFError()
            data.extend(block)
            if len(data) > 1024*1024:
                raise ValueError('response limit')
            while b'\n' in data:
                raw, _, rest = data.partition(b'\n');data=bytearray(rest)
                message=json.loads(raw)
                if message.get('id')==request_id and 'method' not in message:
                    if 'error' in message:raise ValueError('rpc error')
                    return message['result']
        raise TimeoutError()
    finally:sel.close()


def send(proc, method, params=None, request_id=None):
    message={'jsonrpc':'2.0','method':method}
    if params is not None:message['params']=params
    if request_id is not None:message['id']=request_id
    proc.stdin.write(json.dumps(message).encode()+b'\n');proc.stdin.flush()


def safe_quota(value):
    if not isinstance(value,dict):return {'available':False,'reason':'unknown'}
    allowed={'available','reason','source','observed_at','stale','staleness_seconds','age_seconds','used_percent','remaining_percent','utilization','resets_at','reset_at','five_hour','seven_day','manual_check_url'}
    return {k:safe_quota(v) if isinstance(v,dict) else v for k,v in value.items() if k in allowed and (v is None or isinstance(v,(bool,int,float,dict)) or isinstance(v,str) and len(v)<300)}



def sanitize_account(status, provider=None):
    if not isinstance(status, dict):
        status = {}
    account = {}
    auth = status.get('auth') if isinstance(status.get('auth'), dict) else {}
    logins = [v for v in (status.get('logged_in'), auth.get('logged_in')) if type(v) is bool]
    account['logged_in'] = False if False in logins else True if True in logins else None
    for key in ('model_selection_ready', 'service_available'):
        if type(status.get(key)) is bool:
            account[key] = status[key]
    labels = {
        'status': {'ok','error','busy','unknown','unavailable'},
        'default_model': {'opus-5-5-high','fable-5-2-high','gemini-3-8-flash-high'},
        'reasoning_effort': {'low','medium','high','xhigh','max'},
        'model_evidence_policy': {'execution_model','cli_session_binding'},
        'service_unavailable_reason': {'requires_claude_ai_login','recent_api_auth_failure'},
        'service_available_reason': {'auth_status_does_not_probe_model_api'},
    }
    for key, allowed in labels.items():
        value = status.get(key)
        if isinstance(value, str) and value in allowed:
            account[key] = value
    for key in ('cli_version','claude_version'):
        value = status.get(key)
        if isinstance(value, str) and len(value) <= 80 and value and value[0].isdigit() and all(c.isalnum() or c in ' .()+-' for c in value):
            account[key] = value

    def clean_quota(value, depth=0):
        if not isinstance(value, dict):
            return {'available': False, 'reason': 'unknown'}
        quota = {'available': value.get('available') is True}
        reasons = {
            'unknown', 'requires_claude_ai_login', 'statusline_observation_unavailable',
            'statusline_observation_expired', 'invalid_statusline_observation',
            'usage_command_unavailable', 'usage_command_failed',
            'invalid_usage_command_output', 'not_reported_by_agy',
        }
        sources = {'claude_code_statusline', 'claude_code_usage_command', 'antigravity_cli_usage'}
        if isinstance(value.get('reason'), str) and value['reason'] in reasons:
            quota['reason'] = value['reason']
        elif not quota['available']:
            quota['reason'] = 'unknown'
        if isinstance(value.get('source'), str) and value['source'] in sources:
            quota['source'] = value['source']
        if value.get('manual_check_url') == 'https://claude.ai/usage':
            quota['manual_check_url'] = 'https://claude.ai/usage'
        if type(value.get('stale')) is bool:
            quota['stale'] = value['stale']

        def timestamp(raw):
            if not isinstance(raw, str) or not 1 <= len(raw) <= 64:
                return None
            try:
                parsed = dt.datetime.fromisoformat(raw.replace('Z', '+00:00'))
            except (ValueError, OverflowError):
                return None
            return raw if parsed.tzinfo is not None else None

        for key in ('observed_at', 'resets_at', 'reset_at'):
            valid = timestamp(value.get(key))
            if valid is not None:
                quota[key] = valid
        for key in ('age_seconds', 'staleness_seconds'):
            raw = value.get(key)
            if type(raw) in (int, float) and 0 <= raw <= 315_360_000:
                quota[key] = raw
        if quota['available']:
            for key in ('used_percent', 'remaining_percent', 'utilization'):
                raw = value.get(key)
                if type(raw) in (int, float) and 0 <= raw <= 100:
                    quota[key] = raw
            if depth == 0:
                for window in ('five_hour', 'seven_day'):
                    raw = value.get(window)
                    if isinstance(raw, dict):
                        cleaned = clean_quota({'available': True, **raw}, 1)
                        cleaned.pop('available', None)
                        if cleaned:
                            quota[window] = cleaned
        return quota

    account['quota'] = clean_quota(status.get('quota'))
    if provider == 'claude' and account['logged_in'] is False:
        account['service_available'] = False
        account['service_unavailable_reason'] = 'requires_claude_ai_login'
        account.pop('service_available_reason', None)
        account['quota'] = {
            'available': False,
            'reason': 'requires_claude_ai_login',
            'manual_check_url': 'https://claude.ai/usage',
        }
    availability = status.get('availability')
    account['availability'] = {}
    if isinstance(availability, dict):
        if type(availability.get('eligible')) is bool:
            account['availability']['eligible'] = availability['eligible']
        value = availability.get('status')
        if isinstance(value, str) and value in {'eligible','unsupported_location','unknown','unavailable','not_authenticated','age_restricted','ineligible'}:
            account['availability']['status'] = value
    account['models'] = {}
    models = status.get('models')
    if isinstance(models, dict):
        for name, raw in models.items():
            if name not in labels['default_model'] or not isinstance(raw, dict):
                continue
            if provider == 'claude' and name == 'gemini-3-8-flash-high':
                continue
            if provider == 'gemini' and name != 'gemini-3-8-flash-high':
                continue
            model = {key:raw[key] for key in ('enabled','verified','cli_supported','selection_ready') if type(raw.get(key)) is bool}
            effort = raw.get('reasoning_effort')
            if isinstance(effort,str) and effort in labels['reasoning_effort']:
                model['reasoning_effort'] = effort
            policy = raw.get('evidence_policy')
            if isinstance(policy,str) and policy in labels['model_evidence_policy']:
                model['evidence_policy'] = policy
            account['models'][name] = model
    return account


def probe_unified():
    start=time.monotonic()
    checked_at=now()
    results={
        provider: {
            'checked_at': checked_at,
            'probe_ok': False,
            'account_ready': None,
            'transport': {
                'initialize_ok': False,
                'tools_count': None,
                'probe_kind': 'local_unified_forced_command',
            },
        }
        for provider in ('claude','gemini')
    }
    proc=None
    try:
        deadline=start+90
        proc=subprocess.Popen(
            ['runuser','-u','researchmcp','--',
             '/opt/remote-research-mcp/current/scripts/run-remote-research-forced-command.sh','owner'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        send(proc,'initialize',{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'owner-health-monitor','version':'1'}},1)
        init=rpc_read(proc,1,deadline)
        if not isinstance(init,dict) or not isinstance(init.get('serverInfo'),dict):
            raise ValueError('invalid initialize')
        for result in results.values():
            result['transport']['initialize_ok']=True
        version=init['serverInfo'].get('version')
        if isinstance(version,str) and version.isascii() and version and version[0].isdigit() and len(version)<=80 and all(c.isalnum() or c in '.+-' for c in version):
            for result in results.values():
                result['transport']['server_version']=version
        send(proc,'notifications/initialized')
        send(proc,'tools/list',{},2)
        listed=rpc_read(proc,2,deadline)
        tools=listed.get('tools') if isinstance(listed,dict) else None
        names=[tool.get('name') for tool in tools] if isinstance(tools,list) and all(isinstance(tool,dict) for tool in tools) else []
        for result in results.values():
            result['transport']['tools_count']=len(names)
        expected={'research_'+suffix for suffix in ('status','once','start','continue','list_conversations','export_conversation','close')}
        if len(names)!=7 or set(names)!=expected:
            raise ValueError('tool catalogue')
        send(proc,'tools/call',{'name':'research_status','arguments':{}},3)
        response=rpc_read(proc,3,deadline)
        if not isinstance(response,dict) or response.get('isError'):
            raise ValueError('status error')
        status=response.get('structuredContent')
        if not isinstance(status,dict):
            content=response.get('content')
            if not isinstance(content,list):
                raise ValueError('status response')
            texts=[item.get('text') for item in content if isinstance(item,dict) and item.get('type')=='text']
            if len(texts)!=1 or not isinstance(texts[0],str):
                raise ValueError('status response')
            status=json.loads(texts[0])
        if not isinstance(status,dict) or status.get('principal')!='owner':
            raise ValueError('status principal')
        providers=status.get('providers')
        if not isinstance(providers,dict) or not all(
            isinstance(providers.get(name),dict)
            and providers[name].get('status') in ('ok','busy','error','unavailable','unknown')
            for name in ('claude','gemini')
        ):
            raise ValueError('status providers')
        for name in ('claude','gemini'):
            entry=providers[name]
            account=sanitize_account(entry,name)
            result=results[name]
            result['account']=account
            result['probe_ok']=True
            category=entry.get('error')
            if entry.get('status')!='ok':
                allowed={'auth','rate_limit','model_unavailable','permission','network',
                         'invalid_argument','runtime','timeout','busy','unknown','unavailable'}
                result['provider_error_category']=category if isinstance(category,str) and category in allowed else 'unknown'
            if (account['logged_in'] is False or account['availability'].get('eligible') is False
                or account.get('model_selection_ready') is False
                or account.get('service_available') is False):
                result['account_ready']=False
            elif entry.get('status')=='ok' and account['logged_in'] is True:
                result['account_ready']=True
    except Exception as exc:
        for result in results.values():
            result['probe_ok']=False
            result['account_ready']=None
            result.pop('account',None)
            result.pop('provider_error_category',None)
            result['error_category']=type(exc).__name__
    finally:
        if proc:
            if proc.stdin:
                try:proc.stdin.close()
                except OSError:pass
            try:proc.wait(timeout=max(0, min(5, deadline-time.monotonic())))
            except subprocess.TimeoutExpired:
                try:os.killpg(proc.pid,signal.SIGTERM)
                except ProcessLookupError:pass
                try:proc.wait(timeout=max(0, min(3, deadline-time.monotonic())))
                except subprocess.TimeoutExpired:
                    try:os.killpg(proc.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                    proc.wait()
        for result in results.values():
            result['latency_ms']=round((time.monotonic()-start)*1000)
    return results


def system_metrics():
    values={}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key,value,*_=line.split();values[key.rstrip(':')]=int(value)*1024
    workers={}
    for bridge in ['claude','gemini']:
        uid=pwd.getpwnam(bridge+'mcp').pw_uid;count=0
        for p in Path('/proc').iterdir():
            if not p.name.isdigit():continue
            try:
                if p.stat().st_uid!=uid:continue
                cmd=(p/'cmdline').read_bytes()
                if b'mcp-idle-guard.py' not in cmd and (b'/bin/'+bridge.encode()+b'-research-mcp' in cmd or (bridge+'_research_mcp.server').encode() in cmd):count+=1
            except OSError:pass
        workers[bridge]=count
    return {'memory_total_bytes':values['MemTotal'],'memory_available_bytes':values['MemAvailable'],'swap_total_bytes':values['SwapTotal'],'swap_used_bytes':values['SwapTotal']-values['SwapFree'],'workers':workers,'idle_timeout_seconds':1800}


AUDIT_COLUMNS = {
    'id', 'principal', 'tool', 'model', 'status', 'duration_ms', 'created_at',
    'error_category', 'http_status', 'exit_code', 'request_id', 'raw_log_saved',
    'input_tokens', 'output_tokens', 'cache_read_input_tokens',
    'cache_creation_input_tokens', 'total_tokens', 'usage_source',
    'cache_read_tokens', 'cache_write_tokens', 'token_source', 'reasoning_tokens',
    'thinking_tokens', 'cache_creation_tokens', 'token_usage_source', 'reported_models_json',
}


def make_group_readable(path):
    path.chmod(0o640)
    try:os.chown(path,0,grp.getgrnam('mcp-audit').gr_gid)
    except KeyError:pass


def open_audit_source(source, bridge, *, service_identity):
    # WAL readers may create locks even with mode=ro. Use the bridge account
    # so concurrent workers can still access newly created -wal/-shm files.
    original_uid, original_gid = os.geteuid(), os.getegid()
    connection = None
    try:
        if service_identity and original_uid == 0:
            account = pwd.getpwnam(bridge + 'mcp')
            os.setegid(account.pw_gid)
            os.seteuid(account.pw_uid)
        connection = sqlite3.connect('file:' + str(source) + '?mode=ro', uri=True, timeout=3)
        connection.execute('PRAGMA query_only=ON')
        connection.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
        return connection
    except BaseException:
        if connection is not None:
            connection.close()
        raise
    finally:
        if os.geteuid() != original_uid:
            os.seteuid(original_uid)
        if os.getegid() != original_gid:
            os.setegid(original_gid)


def snapshot_audit(bridge, output_directory, *, source=None):
    service_identity = source is None
    source = source or Path('/var/lib/'+bridge+'-mcp/research/state/mcp.sqlite3')
    destination = output_directory / (bridge+'.sqlite3')
    fd, name = tempfile.mkstemp(prefix='.'+bridge+'-snapshot-', suffix='.sqlite3', dir=output_directory)
    os.close(fd)
    temporary = Path(name)
    try:
        with closing(open_audit_source(source, bridge, service_identity=service_identity)) as db:
            db.execute('PRAGMA query_only=ON')
            columns = [(r[1], r[2]) for r in db.execute("PRAGMA table_info(audit_log)") if r[1] in AUDIT_COLUMNS]
            if not {'principal','tool','status','created_at'}.issubset({r[0] for r in columns}):
                raise ValueError('audit schema')
            # Never execute SQL/schema strings supplied by a service account.
            definitions = ','.join('"'+name+'" '+('INTEGER' if kind.upper()=='INTEGER' else 'TEXT') for name,kind in columns)
            names = ','.join('"'+name+'"' for name,_ in columns)
            with closing(sqlite3.connect(temporary)) as out:
                out.execute('CREATE TABLE audit_log ('+definitions+')')
                rows = db.execute('SELECT '+names+' FROM audit_log')
                while batch := rows.fetchmany(1000):
                    out.executemany('INSERT INTO audit_log VALUES ('+','.join('?' for _ in columns)+')',batch)
                out.execute('CREATE INDEX audit_time ON audit_log(created_at)')
                out.commit()
        make_group_readable(temporary)
        os.replace(temporary,destination)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='/var/lib/mcp-audit/health.json');parser.add_argument('--audit-only',action='store_true');args=parser.parse_args()
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
    for bridge in ['claude','gemini']:snapshot_audit(bridge,path.parent)
    if args.audit_only:
        print(json.dumps({'audit_snapshot_at':now()}));return
    result={'observed_at':now(),'bridges':probe_unified(),'system':system_metrics()}
    try:
        with socket.create_connection(('127.0.0.1',22022),timeout=5) as sock:
            sock.settimeout(5);banner=sock.recv(256)
            ssh_banner_ok=banner.startswith(b'SSH-2.0-')
    except OSError:ssh_banner_ok=False
    for item in result['bridges'].values():item['transport']['ssh_banner_ok']=ssh_banner_ok
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(result,ensure_ascii=False)+'\n');make_group_readable(temporary);os.replace(temporary,path)
    print(json.dumps({'observed_at':result['observed_at'],'probes':{k:v['probe_ok'] for k,v in result['bridges'].items()}}))

if __name__=='__main__':main()

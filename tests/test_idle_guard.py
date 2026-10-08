import asyncio
import json
import os
from pathlib import Path
import sys

GUARD = Path(__file__).resolve().parents[1] / 'scripts/mcp-idle-guard.py'
MOCK = '''import json,sys,time,os
for raw in sys.stdin:
 m=json.loads(raw)
 if m.get('method')=='slow':time.sleep(0.65)
 print(json.dumps({'jsonrpc':'2.0','id':m.get('id'),'result':{'pid':os.getpid()}}),flush=True)
'''


def test_idle_guard_releases_worker_and_preserves_active_call():
    async def check():
        command = 'import runpy,asyncio,sys;m=runpy.run_path(sys.argv[1]);asyncio.run(m["run"](sys.argv[2:],idle_seconds=0.25))'
        p = await asyncio.create_subprocess_exec(sys.executable,'-c',command,str(GUARD),sys.executable,'-u','-c',MOCK,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        p.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"slow"}\n');await p.stdin.drain()
        reply=json.loads(await asyncio.wait_for(p.stdout.readline(),3));pid=reply['result']['pid']
        assert reply['id']==1
        # Repeated protocol pings do not prevent idle expiry.
        for i in range(2):
            p.stdin.write(json.dumps({'jsonrpc':'2.0','id':i+2,'method':'ping'}).encode()+b'\n');await p.stdin.drain()
            assert json.loads(await p.stdout.readline())['id']==i+2
            await asyncio.sleep(0.05)
        assert await asyncio.wait_for(p.wait(),3)==0
        assert b'idle timeout' in await p.stderr.read()
        try:os.kill(pid,0)
        except ProcessLookupError:pass
        else:raise AssertionError('worker leaked after idle timeout')
    asyncio.run(check())


def test_client_eof_releases_worker():
    async def check():
        p=await asyncio.create_subprocess_exec(sys.executable,str(GUARD),sys.executable,'-u','-c',MOCK,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        p.stdin.write(b'{"id":1,"method":"hello"}\n');await p.stdin.drain()
        reply=json.loads(await p.stdout.readline());pid=reply['result']['pid']
        p.stdin.close();await p.stdin.wait_closed()
        assert await asyncio.wait_for(p.wait(),3)==0
        try:os.kill(pid,0)
        except ProcessLookupError:pass
        else:raise AssertionError('worker leaked after EOF')
    asyncio.run(check())


def test_guard_kills_descendant_when_direct_worker_already_exited(tmp_path):
    import signal
    ready=tmp_path/'descendant-ready'
    descendant=f'import signal,time,pathlib;signal.signal(signal.SIGTERM,signal.SIG_IGN);pathlib.Path({str(ready)!r}).touch();time.sleep(60)'
    worker=f'''import subprocess,sys,json,time,pathlib
p=subprocess.Popen([sys.executable,'-c',{descendant!r}])
while not pathlib.Path({str(ready)!r}).exists():time.sleep(.01)
print(json.dumps({{'id':1,'result':{{'pid':p.pid}}}}),flush=True)
'''
    async def check():
        launch='import runpy,asyncio,sys;m=runpy.run_path(sys.argv[1]);asyncio.run(m["run"](sys.argv[2:],idle_seconds=0.2))'
        p=await asyncio.create_subprocess_exec(sys.executable,'-c',launch,str(GUARD),sys.executable,'-u','-c',worker,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        pid=None
        try:
            p.stdin.write(b'{"id":1,"method":"hello"}\n');await p.stdin.drain()
            pid=json.loads(await asyncio.wait_for(p.stdout.readline(),3))['result']['pid']
            assert await asyncio.wait_for(p.wait(),4)==0
            await asyncio.sleep(.05)
            stat=Path('/proc')/str(pid)/'stat'
            assert not stat.exists() or stat.read_text().split(') ',1)[1].startswith('Z'), 'live descendant leaked'
        finally:
            if pid:
                try:os.kill(pid,signal.SIGKILL)
                except ProcessLookupError:pass
            if p.returncode is None:p.kill();await p.wait()
    asyncio.run(check())

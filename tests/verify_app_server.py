"""Optional, offline-compatible integration test against an installed Codex app-server."""

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from queue import Queue, Empty
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
from threading import Thread
import time
import uuid
import zlib
from zipfile import ZipFile

NOW = datetime.now(timezone.utc)
STAMP = NOW.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def png(red, green, blue):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    header = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
    return (bytes.fromhex('89504e470d0a1a0a') + chunk(b'IHDR', header)
            + chunk(b'IDAT', zlib.compress(bytes([0, red, green, blue]))) + chunk(b'IEND', b''))


def image_url(data):
    return 'data:image/png;base64,' + base64.b64encode(data).decode()


SAMPLE_PNG = png(0, 0, 255)
TOOL_PNG = png(0, 255, 0)
DATA_URL = image_url(SAMPLE_PNG)
IMAGE_HASH = hashlib.sha256(SAMPLE_PNG).hexdigest()


def uid():
    return str(uuid.uuid4())

def state_file(home):
    return next(home.glob('state_*.sqlite'))

def make_session(home, ident, label, project, conversations, cli_version):
    name = f'rollout-{NOW.strftime("%Y-%m-%dT%H-%M-%S")}-{ident}{label}.jsonl'
    path = home / 'sessions' / NOW.strftime('%Y/%m/%d') / name
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    def add(kind, payload):
        entries.append({'timestamp': STAMP, 'type': kind, 'ordinal': len(entries), 'payload': payload})
    add('session_meta', {
        'id': ident, 'session_id': ident, 'timestamp': STAMP, 'cwd': str(project),
        'runtime_workspace_roots': [str(project)], 'originator': 'Codex Desktop',
        'cli_version': cli_version.removeprefix('codex-cli '), 'source': 'vscode', 'thread_source': 'user',
        'model_provider': 'openai', 'history_mode': 'paginated',
        'context_window': {'window_id': uid()},
    })
    for prompt, answer, picture in conversations:
        turn_id = uid()
        seconds = int(NOW.timestamp())
        milliseconds = int(NOW.timestamp()*1000)
        add('event_msg', {'type': 'task_started', 'turn_id': turn_id, 'root_turn_id': turn_id,
                          'started_at': seconds, 'model_context_window': 65536, 'collaboration_mode_kind': 'default'})
        user = [{'type': 'text', 'text': prompt, 'text_elements': []}]
        wire = [{'type': 'input_text', 'text': prompt}]
        if picture:
            user.append({'type': 'image', 'image_url': DATA_URL})
            wire.append({'type': 'input_image', 'image_url': DATA_URL, 'detail': 'auto'})
        add('event_msg', {'type': 'item_completed', 'thread_id': ident, 'turn_id': turn_id,
                         'started_at_ms': milliseconds, 'completed_at_ms': milliseconds,
                         'item': {'type': 'UserMessage', 'id': uid(), 'client_id': uid(), 'content': user}})
        add('response_item', {'type': 'message', 'role': 'user', 'content': wire})
        if picture:
            capture = {'type': 'ImageView', 'id': 'exec-' + uid(),
                       'path': (project / 'expired-preview.png').as_uri()}
            call_id = 'call_' + uid().replace('-', '')
            add('event_msg', {'type': 'item_completed', 'thread_id': ident, 'turn_id': turn_id,
                             'started_at_ms': milliseconds, 'completed_at_ms': milliseconds, 'item': capture})
            add('response_item', {'type': 'custom_tool_call_output', 'call_id': call_id,
                                 'output': [{'type': 'input_image', 'image_url': image_url(TOOL_PNG)}]})
        add('event_msg', {'type': 'item_completed', 'thread_id': ident, 'turn_id': turn_id,
                         'started_at_ms': milliseconds, 'completed_at_ms': milliseconds,
                         'item': {'type': 'AgentMessage', 'id': uid(), 'phase': 'final_answer',
                                  'content': [{'type': 'Text', 'text': answer}]}})
        add('response_item', {'type': 'message', 'role': 'assistant', 'phase': 'final_answer',
                             'content': [{'type': 'output_text', 'text': answer}]})
        add('event_msg', {'type': 'task_complete', 'turn_id': turn_id, 'root_turn_id': turn_id,
                         'last_agent_message': answer, 'started_at': seconds,
                         'completed_at': seconds, 'duration_ms': 0, 'time_to_first_token_ms': 0})
    with path.open('xb') as stream:
        for item in entries:
            stream.write((json.dumps(item) + '\n').encode())
    return path

@contextmanager
def serve(codex, home, root, label):
    home.mkdir(exist_ok=True, parents=True)
    device_home = home.parent / 'profile'
    device_home.mkdir(exist_ok=True)
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'SHELL', 'TMPDIR') if key in os.environ}
    env.update(HOME=str(device_home), CODEX_HOME=str(home), CODEX_SQLITE_HOME=str(home),
               HTTP_PROXY='http://127.0.0.1:1', HTTPS_PROXY='http://127.0.0.1:1',
               ALL_PROXY='http://127.0.0.1:1', NO_PROXY='')
    events = Queue()
    counter = 0
    with (root / f'{label}.log').open('w') as log:
        child = subprocess.Popen([codex, 'app-server', '--listen', 'stdio://', '--disable', 'goals',
                                  '-c', 'openai_base_url="http://127.0.0.1:1/v1"'],
                                 cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=log, text=True)
        def pump():
            for line in child.stdout:
                events.put(line)
            events.put(None)
        reader = Thread(target=pump, daemon=True)
        reader.start()
        def send(message):
            child.stdin.write(json.dumps(message) + '\n')
            child.stdin.flush()
        def call(method, params):
            nonlocal counter
            counter += 1
            request = counter
            send({'id': request, 'method': method, 'params': params})
            end = time.monotonic() + 15
            while True:
                try:
                    line = events.get(timeout=max(end - time.monotonic(),0))
                except Empty:
                    raise RuntimeError(f'Timeout calling {method}') from None
                if line is None:
                    raise RuntimeError(f'Codex closed during {method}')
                result = json.loads(line)
                if result.get('id') == request:
                    if 'error' in result:
                        raise RuntimeError(f"{method}: {result['error'].get('message')}")
                    return result['result']
        try:
            hello = call('initialize', {
                'clientInfo': {'name': 'local_migration_verification', 'version': '1.0.0'},
                'capabilities': {'experimentalApi': True},
            })
            assert Path(hello['codexHome']).resolve() == home.resolve()
            send({'method':'initialized'})
            yield call
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=4)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=4)

def resume(call, ident, file, project):
    read = call('thread/resume', {'threadId': ident, 'path': str(file), 'cwd': str(project),
                                  'runtimeWorkspaceRoots': [str(project)], 'excludeTurns': True})
    assert read['thread']['id'] == ident
    assert Path(read['cwd']) == project
    return read['thread']

def visible_items(call, ident):
    rows = call('thread/items/list', {'threadId': ident, 'limit': 100})
    assert not rows.get('nextCursor')
    result = []
    for row in rows['data']:
        item = row['item']
        if item['type'] == 'userMessage':
            user_content = item['content']
            text = ''.join(x['text'] for x in user_content if x['type']=='text')
            images = [hashlib.sha256(base64.b64decode(x['url'].split(',',1)[1])).hexdigest()
                      for x in user_content if x['type']=='image']
            result.append(('user', text, images))
        if item['type'] == 'agentMessage':
            result.append(('assistant', item['text'], []))
    return result

def checks(call, ident, expected):
    rows = visible_items(call, ident)
    assert rows == expected, (rows,expected)
    return len(rows)

def copy_member(archive, member, target_home):
    relative = Path(member).relative_to('native')
    result = target_home / relative
    result.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(member) as source, result.open('xb') as output:
        shutil.copyfileobj(source,output)
    return result

def run_cli(script, *args):
    cmd = [sys.executable, '-B', str(script), *map(str,args)]
    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode:
        raise RuntimeError(run.stderr)
    return json.loads(run.stdout)

def exercise(codex, root):
    release = Path(__file__).resolve().parents[1]
    cli_version = subprocess.check_output([codex, '--version'], text=True).strip()
    print(cli_version, flush=True)
    script = release / 'skills/codex-local-migration/scripts/native_bundle.py'
    source = root / 'source/.codex'
    target = root / 'target/.codex'
    project = root / 'samples' / 'old' / 'Sample Project'
    new_project = root / 'samples' / 'new' / 'Sample Project'
    existing_project = root / 'samples' / 'new' / 'Existing Project'
    for folder in (project,new_project,existing_project):
        folder.mkdir(parents=True)
    primary_id = uid()
    existing_id = uid()
    raw = make_session(source,primary_id,'',project,[
        ('What does the sample contain?', 'It contains a blue square.', True),
        ('What color did we discuss?', 'Blue.', False),
    ], cli_version)
    expected = [
        ('user','What does the sample contain?',[IMAGE_HASH]),
        ('assistant','It contains a blue square.',[]),
        ('user','What color did we discuss?',[]),
        ('assistant','Blue.',[]),
    ]
    with serve(codex,source, root, 'source') as call:
        resume(call,primary_id,raw,project)
        print('Source API:',checks(call,primary_id,expected),'messages; embedded image matches',flush=True)
    alternative = make_session(source,primary_id,'_'+uid(),project,[
        ('Different branch', 'Its content differs.', False),
    ], cli_version)
    with sqlite3.connect(state_file(source)) as s:
        s.execute('UPDATE threads SET rollout_path=? WHERE id=?',(str(alternative),primary_id))
    bundle = root/'selected-private.zip'
    print('Export:',run_cli(script,'export','--codex-home',source,'--thread-id',primary_id,'--output',bundle),flush=True)
    manifest = run_cli(script,'verify',bundle)
    thread, = manifest['threads']
    candidates = thread['rollouts']
    matched, = [entry for entry in candidates if entry['projection_check']['matched_turns']==2]
    assert not matched['registered'] and any(entry['registered'] for entry in candidates)
    print('Same-ID alternatives: found two; prior log matches both actual SQLite turn offsets',flush=True)
    with ZipFile(bundle) as archive:
        for entry in candidates:
            payload = archive.read(entry['member'])
            assert payload == Path(entry['source']).read_bytes()
        encoded_tool = image_url(TOOL_PNG).encode()
        assert encoded_tool in archive.read(matched['member'])
        assert not (project / 'expired-preview.png').exists()
        imported = copy_member(archive,matched['member'],target)
        print('Expired tool screenshot path: embedded image bytes preserved',flush=True)
    backup = make_session(target,existing_id,'',existing_project,[('Existing chat','Keep me.',False)], cli_version)
    expected_existing=[('user','Existing chat',[]),('assistant','Keep me.',[])]
    with serve(codex,target,root,'restore') as call:
        resume(call,existing_id,backup,existing_project)
        checks(call,existing_id,expected_existing)
        existing = call('project/import',{'idempotencyKey':uid(),'name':'Existing Project',
            'roots':[{'path':str(existing_project)}],'threads':[existing_id]})['project']
        resume(call,primary_id,imported,new_project)
        checks(call,primary_id,expected)
        new = call('project/import',{'idempotencyKey':uid(),'name':'Sample Project',
            'roots':[{'path':str(new_project)}],'threads':[primary_id]})['project']
        checks(call,existing_id,expected_existing)
    with sqlite3.connect(state_file(target).as_uri()+'?mode=ro',uri=True) as db:
        rows = {ident: (group, Path(path)) for ident, group, path in
                db.execute('SELECT id, project_id, cwd FROM threads')}
        assert rows == {existing_id:(existing['id'],existing_project),
                        primary_id:(new['id'],new_project)}, rows
    print('Target: original ID and message order verified; both project bindings persisted',flush=True)
    with serve(codex,target,root,'after-restart') as call:
        checks(call,primary_id,expected)
        checks(call,existing_id,expected_existing)
        listed = call('project/list',{})
        assert 'Sample Project' in json.dumps(listed) and 'Existing Project' in json.dumps(listed)
        states=call('thread/list',{'includedThreadIds':[primary_id,existing_id]})['data']
        assert {row['id'] for row in states}=={primary_id,existing_id}
    print('Restart: recovered and existing threads still readable; both project registrations listed',flush=True)
    print('No real account, desktop sidebar or model continuation was tested.',flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex', default=shutil.which('codex'), help='Path to the installed Codex executable')
    args = parser.parse_args()
    if not args.codex:
        parser.error('Could not find Codex. Pass --codex /path/to/codex.')
    codex = str(Path(args.codex).expanduser().resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix='codex-migration-integration-') as tmp:
        exercise(codex, Path(tmp).resolve())


if __name__ == '__main__':
    main()

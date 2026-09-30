"""Integration tests against a local HTTP mock, NOT against a real Docmost server."""
from __future__ import annotations
import copy
import json
import threading
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import pytest
import yaml
from docmost_publisher.cli import main
from docmost_publisher.engine import EMPTY_DOC


@pytest.fixture
def server():
    class State:
        calls = None
        fail_update = False
        sessions = None
        counter = 0
        logins = 0
        logouts = 0
        uploaded = None
        pages = None
        recursive_trash = True
        def make_page(self, pid, title, parent=None):
            self.pages[pid] = {'id':pid, 'title':title, 'slugId':pid.replace('-',''), 'parentPageId':parent,
                               'spaceId':'space-1', 'content':copy.deepcopy(EMPTY_DOC), 'deletedAt':None, 'canEdit':True}
    state = State()
    state.calls, state.sessions, state.uploaded, state.pages = [], set(), [], {}
    state.make_page('root-1','ERP documentation')
    for i in range(3):
        state.make_page(f'old-{i}',f'Old {i}','root-1')
    state.make_page('old-nested','Nested old','old-0')
    state.make_page('unrelated','Do not touch')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def send(self, value=None, status=200, cookie=None, bare=False):
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            if cookie:
                self.send_header('Set-Cookie',cookie)
            payload = b'' if value is None else json.dumps(value if bare else {'data':value}).encode()
            self.send_header('Content-Length',str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length','0')))
            endpoint = self.path.removeprefix('/api/')
            if endpoint == 'files/upload':
                message = BytesParser(policy=policy.default).parsebytes(
                    b'Content-Type: '+self.headers['Content-Type'].encode()+b'\r\nMIME-Version: 1.0\r\n\r\n'+raw)
                fields = {}
                for part in message.iter_parts():
                    name = part.get_param('name',header='content-disposition')
                    fields[name] = part.get_payload(decode=True)
                payload = {'pageId':fields['pageId'].decode()}
            else:
                payload = json.loads(raw) if raw else {}
            # Do not store passwords in mock logs either.
            state.calls.append((endpoint, {k:v for k,v in payload.items() if k!='password'}))
            if endpoint == 'auth/login':
                if payload.get('password') != 'test-password':
                    return self.send(status=401)
                state.logins += 1
                token = f'test-session-{state.logins}'
                state.sessions.add(token)
                return self.send(cookie=f'authToken={token}; Path=/; HttpOnly; Max-Age=3600; SameSite=Lax')
            cookie = self.headers.get('Cookie','')
            token = next((part.strip().split('=',1)[1] for part in cookie.split(';') if part.strip().startswith('authToken=')), '')
            if token not in state.sessions:
                return self.send(status=401)
            if endpoint == 'auth/logout':
                state.logouts += 1
                state.sessions.discard(token)
                return self.send(cookie='authToken=; Path=/; Max-Age=0')
            if endpoint == 'spaces':
                return self.send({'items':[{'id':'space-1','slug':'engineering','name':'Engineering'}], 'meta':{'hasNextPage':False}})
            if endpoint == 'pages/sidebar-pages':
                parent = payload.get('pageId')
                rows = [copy.deepcopy(p) for p in state.pages.values() if p['parentPageId']==parent and not p['deletedAt']]
                start = int(payload.get('cursor','0'))
                chunk = rows[start:start+2]
                more = start+2 < len(rows)
                return self.send({'items':chunk, 'meta':{'hasNextPage':more,'nextCursor':str(start+2) if more else None}})
            if endpoint == 'pages/info':
                page = state.pages.get(payload['pageId'])
                return self.send(page, status=200 if page else 404)
            if endpoint == 'pages/create':
                state.counter += 1
                pid = f'new-{state.counter}'
                state.make_page(pid, payload['title'], payload.get('parentPageId'))
                state.pages[pid]['content'] = payload.get('content',EMPTY_DOC)
                return self.send(state.pages[pid])
            if endpoint == 'pages/delete':
                pid = payload['pageId']
                assert payload['permanentlyDelete'] is False
                pending = [pid]
                while pending:
                    p = pending.pop()
                    state.pages[p]['deletedAt'] = '2026-09-30T00:00:00Z'
                    if state.recursive_trash:
                        pending.extend(c['id'] for c in state.pages.values() if c['parentPageId']==p and not c['deletedAt'])
                return self.send()
            if endpoint == 'pages/update':
                if state.fail_update and payload['title']=='file':
                    return self.send(status=500)
                p = state.pages[payload['pageId']]
                p['title'] = payload['title']
                assert payload['operation']=='replace'
                if payload['format']=='json':
                    p['content'] = payload['content']
                else:
                    p['markdown'] = payload['content']
                    p['content'] = {'type':'doc','content':[{'type':'paragraph','content':[{'type':'text','text':payload['content']}]}]}
                return self.send(p)
            if endpoint == 'files/upload':
                state.uploaded.append(payload['pageId'])
                return self.send({'id':'attachment-1','url':'/api/files/attachment-1/image.png'}, bare=True)
            self.send(status=404)
    http = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=http.serve_forever,daemon=True)
    thread.start()
    state.url = f'http://127.0.0.1:{http.server_port}'
    try:
        yield state
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


@pytest.fixture
def project(tmp_path, monkeypatch, cfg, server):
    root = tmp_path/'project'
    root.mkdir()
    home = tmp_path/'home'
    home.mkdir()
    monkeypatch.setenv('HOME',str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME',str(home/'.config'))
    monkeypatch.setenv('XDG_STATE_HOME',str(home/'.local/state'))
    monkeypatch.setenv('DOCMOST_EMAIL','publisher@test')
    monkeypatch.setenv('DOCMOST_PASSWORD','test-password')
    monkeypatch.chdir(root)
    cfg['docmost']['url'] = server.url
    cfg['docmost']['http']['allow_http'] = True
    (root/'.docmost.yaml').write_text(yaml.safe_dump(cfg))
    (root/'README.md').write_text('# Project\n[New](nested/file.md)\n![img](image.png)')
    (root/'nested').mkdir()
    (root/'nested/file.md').write_text('# New file\nContent\n[Home](../README.md)')
    (root/'image.png').write_bytes(b'PNG mock')
    return root, home


def test_complete_publish_twice_reuses_token_and_root(project, server, monkeypatch):
    root, home = project
    assert main(['--yes','--no-input']) == 0
    first_ids = {p['id'] for p in server.pages.values() if not p['deletedAt'] and p['parentPageId']}
    assert all(server.pages[f'old-{i}']['deletedAt'] for i in range(3))
    assert server.pages['old-nested']['deletedAt']
    assert not server.pages['root-1']['deletedAt']
    assert not server.pages['unrelated']['deletedAt']
    active = [p for p in server.pages.values() if not p['deletedAt']]
    assert {p['title'] for p in active} == {'ERP documentation','Do not touch','nested','file'}
    assert '/api/files/attachment-1/image.png' in server.pages['root-1']['markdown']
    assert 'nested/file.md' not in server.pages['root-1']['markdown']
    assert server.logins == 1 and server.logouts == 0
    monkeypatch.delenv('DOCMOST_PASSWORD')
    assert main(['--yes','--no-input']) == 0
    second_ids = {p['id'] for p in server.pages.values() if not p['deletedAt'] and p['parentPageId']}
    assert not first_ids & second_ids
    assert server.logins == 1
    tokens = list((home/'.config/docmost-publisher/sessions').glob('*.json'))
    assert len(tokens)==1 and tokens[0].stat().st_mode & 0o777 == 0o600
    assert 'test-password' not in tokens[0].read_text()
    journals = list((home/'.local/state/docmost-publisher/runs').glob('*.json'))
    assert len(journals)==2 and all(json.loads(p.read_text())['status']=='completed' for p in journals)
    assert main(['logout']) == 0
    assert server.logouts == 1 and not tokens[0].exists()


def test_dry_run_does_not_write_pages(project, server):
    assert main(['--dry-run','--no-input']) == 0
    assert server.counter==0 and not server.uploaded
    assert not any(p['deletedAt'] for p in server.pages.values())


def test_local_check_has_zero_network(project, server):
    assert main(['--check']) == 0
    assert not server.calls


def test_broken_source_fails_before_login_or_deletion(project, server):
    (project[0]/'readme.md').write_text('# Duplicate index')
    assert main(['--yes','--no-input']) == 1
    assert not server.calls


def test_space_matched_by_slug(project, server, cfg):
    cfg['docmost']['space'] = 'engineering'
    (project[0]/'.docmost.yaml').write_text(yaml.safe_dump(cfg))
    assert main(['--yes','--no-input']) == 0


def test_unknown_space_lists_available(project, server, cfg, caplog):
    cfg['docmost']['space'] = 'nope'
    (project[0]/'.docmost.yaml').write_text(yaml.safe_dump(cfg))
    assert main(['--yes','--no-input']) == 1
    assert "'Engineering' (slug 'engineering')" in caplog.text


def test_broken_links_do_not_block_publication(project, server, capsys):
    (project[0]/'README.md').write_text('[bad](missing.md) [code](../../app/x.py)')
    assert main(['--yes','--no-input']) == 0
    out = capsys.readouterr().out
    assert 'WARNING: Missing/unpublished local target' in out
    assert 'WARNING: Path escapes source directory' in out


def test_midrun_failure_journal_and_no_retry(project, server):
    server.fail_update = True
    assert main(['--yes','--no-input']) == 1
    writes = [p for endpoint,p in server.calls if endpoint=='pages/update' and p['title']=='file']
    assert len(writes)==1
    journal = next((project[1]/'.local/state/docmost-publisher/runs').glob('*.json'))
    data = json.loads(journal.read_text())
    assert data['status']=='failed' and data['operations'][-1]['status']=='pending'
    assert not server.pages['unrelated']['deletedAt']


def test_missing_recursive_trash_fails_before_old_tree_removed(project, server):
    server.recursive_trash = False
    assert main(['--yes','--no-input']) == 1
    assert not server.pages['old-0']['deletedAt']
    assert not server.pages['old-nested']['deletedAt']


def test_new_root_created_when_not_found(project, server):
    server.pages.pop('root-1')
    for key in list(server.pages):
        if key.startswith('old-'):
            server.pages.pop(key)
    assert main(['--yes','--no-input']) == 0
    active_roots = [p for p in server.pages.values() if not p['parentPageId'] and not p['deletedAt']]
    assert {p['title'] for p in active_roots} == {'ERP documentation','Do not touch'}


def test_config_not_searched_in_ancestors(project, server, monkeypatch):
    monkeypatch.chdir(project[0]/'nested')
    assert main(['--check']) == 1
    assert not server.calls


def test_explicit_config_from_another_folder(project, server, monkeypatch, tmp_path):
    config = project[0]/'.docmost.yaml'
    monkeypatch.chdir(tmp_path)
    assert main(['--check','--config',str(config)]) == 0
    assert not server.calls


def test_no_confirmation_in_noninteractive_mode_fails_without_network(project, server):
    assert main(['--no-input']) == 1
    assert not server.calls


def test_login_and_status_do_not_need_markdown(project, server):
    (project[0]/'README.md').unlink()
    assert main(['login','--no-input']) == 0
    calls = len(server.calls)
    assert main(['status']) == 0
    assert len(server.calls)==calls


def test_init_refuses_overwrite(project, server):
    original = (project[0]/'.docmost.yaml').read_text()
    assert main(['init','--url',server.url,'--space','Engineering','--root-title','Root','--allow-http','--no-input']) == 1
    assert (project[0]/'.docmost.yaml').read_text()==original


def test_init_then_local_check(project, server):
    path = project[0]/'.docmost.yaml'
    path.unlink()
    assert main(['init','--url',server.url,'--space','Engineering','--root-title','Root','--allow-http','--no-input']) == 0
    assert yaml.safe_load(path.read_text())['docmost']['root']['title']=='Root'
    assert main(['check']) == 0

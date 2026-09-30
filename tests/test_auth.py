from __future__ import annotations
import json
import os
import time
import pytest
import requests
from requests.cookies import create_cookie
from docmost_publisher.auth import SessionStore, authenticate, describe_session
from docmost_publisher.engine import APIError, DocmostClient
from docmost_publisher.markdown_tree import PublisherError

class FakeAuthClient(DocmostClient):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.logins = 0
        self.logouts = 0
        self.fail = None
    def login(self, email, password):
        self.logins += 1
        self.session.cookies.set_cookie(create_cookie('authToken', 'secret-token', domain='wiki.example.test', path='/', secure=True, expires=int(time.time()+3600)))
        self.logged_in = True
    def logout(self):
        self.logouts += 1
        self.session.cookies.clear()
    def spaces(self):
        if self.fail:
            raise self.fail
        return []


def save_session(tmp_path, cfg):
    client = FakeAuthClient(cfg['docmost'])
    client.login('user@test', 'password-never-saved')
    store = SessionStore(client.base_url, directory=tmp_path/'private')
    store.save(client, 'user@test')
    return client, store


def test_token_permissions_and_no_password(tmp_path, cfg):
    client, store = save_session(tmp_path, cfg)
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    assert store.directory.stat().st_mode & 0o777 == 0o700
    assert 'password-never-saved' not in store.path.read_text()
    assert 'secret-token' not in describe_session(store)


def test_cache_reused_without_login(tmp_path, cfg):
    _, store = save_session(tmp_path, cfg)
    client = FakeAuthClient(cfg['docmost'])
    authenticate(client, store, cfg['docmost']['auth'], interactive=False)
    assert client.logins == 0
    prepared = client.session.prepare_request(requests.Request('POST', client.base_url+'/api/spaces'))
    assert 'authToken=secret-token' in prepared.headers['Cookie']


def test_close_does_not_logout(tmp_path, cfg):
    client, store = save_session(tmp_path, cfg)
    client.close()
    assert client.logouts == 0 and store.path.exists()


def test_scoped_by_url_and_profile(tmp_path):
    a = SessionStore('https://one.test', directory=tmp_path)
    b = SessionStore('https://two.test', directory=tmp_path)
    c = SessionStore('https://one.test', profile='other', directory=tmp_path)
    assert len({a.path,b.path,c.path}) == 3


def test_insecure_permissions_rejected(tmp_path, cfg):
    client, store = save_session(tmp_path, cfg)
    os.chmod(store.path, 0o644)
    with pytest.raises(PublisherError, match='chmod 600'):
        store.load(client)


def test_expired_cookie_not_loaded(tmp_path, cfg):
    client, store = save_session(tmp_path, cfg)
    data = json.loads(store.path.read_text())
    data['cookie']['expires'] = int(time.time()-100)
    store.path.write_text(json.dumps(data))
    assert store.load(FakeAuthClient(cfg['docmost'])) is False


def test_foreign_cookie_domain_rejected(tmp_path, cfg):
    client, store = save_session(tmp_path, cfg)
    data = json.loads(store.path.read_text())
    data['cookie']['domain'] = 'attacker.test'
    store.path.write_text(json.dumps(data))
    with pytest.raises(PublisherError, match='domain does not match'):
        store.load(client)


def test_symlink_token_rejected(tmp_path, cfg):
    client, store = save_session(tmp_path, cfg)
    target = tmp_path/'real.json'
    store.path.rename(target)
    store.path.symlink_to(target)
    with pytest.raises(OSError):
        store.load(client)


def test_network_failure_does_not_trigger_login(tmp_path, cfg):
    _, store = save_session(tmp_path, cfg)
    client = FakeAuthClient(cfg['docmost'])
    client.fail = PublisherError('network failure')
    with pytest.raises(PublisherError, match='network'):
        authenticate(client, store, cfg['docmost']['auth'], interactive=False)
    assert client.logins == 0


def test_401_prompts_for_new_login_before_writes(tmp_path, cfg, monkeypatch):
    _, store = save_session(tmp_path, cfg)
    client = FakeAuthClient(cfg['docmost'])
    client.fail = APIError(401, 'spaces', 'unauthorized')
    # No credentials provided: must stop, not try random credentials/retry writes.
    monkeypatch.delenv('DOCMOST_EMAIL', raising=False)
    monkeypatch.delenv('DOCMOST_PASSWORD', raising=False)
    with pytest.raises(PublisherError, match='No valid cached session'):
        authenticate(client, store, cfg['docmost']['auth'], interactive=False)
    assert client.logins == 0 and not store.path.exists()


def test_different_user_requires_separate_profile(tmp_path, cfg):
    _, store = save_session(tmp_path, cfg)
    cfg['docmost']['auth']['email'] = 'someone-else@test'
    with pytest.raises(PublisherError, match='another account'):
        authenticate(FakeAuthClient(cfg['docmost']), store, cfg['docmost']['auth'], interactive=False)

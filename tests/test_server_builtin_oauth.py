import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from app.server import create_app
from app.store import Store

ORIGIN = 'https://vault.example'
RESOURCE = ORIGIN + '/mcp'
REDIRECT = 'https://client.example/oauth/callback'
VERIFIER = 'local-test-pkce-verifier-' + 'x' * 48
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip('=')


@pytest.fixture
def oauth_web(tmp_path):
    store = Store(tmp_path)
    initial = (tmp_path / 'first-login.txt').read_text().strip()
    store.change_password(initial, 'local-oauth-test-password')
    store.save_settings({'public_url': ORIGIN})
    for alias in ('家庭NAS', '工作服务器'):
        store.save_service({'kind': 'ssh', 'alias': alias, 'config': {
            'host': '192.0.2.10', 'port': 22, 'username': 'root', 'auth_method': 'password'},
            'secrets': {'password': 'synthetic-secret-' + alias}})
    with TestClient(create_app(tmp_path), base_url=ORIGIN) as client:
        yield client, tmp_path


def register(client):
    response = client.post('/register', json={'client_name': '测试客户端',
        'redirect_uris': [REDIRECT], 'grant_types': ['authorization_code', 'refresh_token'],
        'response_types': ['code'], 'token_endpoint_auth_method': 'none', 'scope': 'credentials:read'})
    assert response.status_code == 201, response.text
    return response.json()['client_id']


def begin(client, client_id):
    response = client.get('/authorize', params={'client_id': client_id, 'redirect_uri': REDIRECT,
        'response_type': 'code', 'scope': 'credentials:read', 'resource': RESOURCE,
        'code_challenge': CHALLENGE, 'code_challenge_method': 'S256', 'state': 'test-state'}, follow_redirects=False)
    assert response.status_code in (302, 303), response.text
    consent_url = response.headers['location']
    assert consent_url.startswith(ORIGIN + '/oauth/consent?')
    request_id = parse_qs(urlsplit(consent_url).query)['request'][0]
    page = client.get(consent_url)
    assert page.status_code == 200, page.text
    assert 'HttpOnly' in page.headers['set-cookie'] and 'Secure' in page.headers['set-cookie']
    return request_id


def login(client):
    response = client.post('/api/login', json={'password': 'local-oauth-test-password'})
    assert response.status_code == 200, response.text
    assert 'Secure' in response.headers['set-cookie']
    return {'X-CSRF-Token': response.json()['csrf_token']}


def approve(client, request_id, headers, allowed=None):
    response = client.post('/api/oauth/decision', headers=headers, json={
        'request_id': request_id, 'approve': True,
        'allowed_services': ['ssh/家庭NAS'] if allowed is None else allowed})
    assert response.status_code == 200, response.text
    redirect = urlsplit(response.json()['redirect_url'])
    assert redirect.scheme + '://' + redirect.netloc + redirect.path == REDIRECT
    parameters = parse_qs(redirect.query)
    assert parameters['state'] == ['test-state'] and parameters['iss'] == [ORIGIN]
    return parameters['code'][0]


def exchange(client, client_id, code):
    response = client.post('/token', data={'grant_type': 'authorization_code', 'client_id': client_id,
        'redirect_uri': REDIRECT, 'resource': RESOURCE, 'code': code, 'code_verifier': VERIFIER})
    assert response.status_code == 200, response.text
    assert response.headers.get('cache-control') and 'no-store' in response.headers['cache-control']
    return response.json()


def rpc(client, token, method, params=None):
    return client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}},
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'})


def content(response):
    payload = response.json()['result']
    assert not payload.get('isError'), payload
    return payload.get('structuredContent') or json.loads(payload['content'][0]['text'])


def test_discovery_challenge_and_protocol_cors(oauth_web):
    client, _ = oauth_web
    response = client.post('/mcp', json={})
    assert response.status_code == 401
    assert ORIGIN + '/.well-known/oauth-protected-resource/mcp' in response.headers['www-authenticate']
    protected = client.get('/.well-known/oauth-protected-resource/mcp').json()
    assert protected['resource'] == RESOURCE and protected['authorization_servers'] == [ORIGIN]
    assert client.get('/.well-known/oauth-protected-resource').json() == protected
    metadata = client.get('/.well-known/oauth-authorization-server').json()
    assert metadata['issuer'] == ORIGIN
    assert metadata['authorization_endpoint'] == ORIGIN + '/authorize'
    assert metadata['registration_endpoint'] == ORIGIN + '/register'
    assert metadata['code_challenge_methods_supported'] == ['S256']
    assert metadata['authorization_response_iss_parameter_supported'] is True
    assert 'none' in metadata['token_endpoint_auth_methods_supported']
    preflight = client.options('/token', headers={'Origin': 'https://client.example',
        'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type'})
    assert preflight.status_code in (200, 204), preflight.text
    assert preflight.headers.get('access-control-allow-origin') in ('*', 'https://client.example')
    assert client.post('/api/login', json={}, headers={'Origin': 'https://client.example'}).status_code == 403


def test_complete_login_consent_mcp_scope_refresh_and_admin_revoke(oauth_web):
    client, path = oauth_web
    client_id = register(client)
    request_id = begin(client, client_id)
    info = client.get('/api/oauth/request', params={'request': request_id})
    assert info.status_code == 200, info.text
    assert info.json()['authenticated'] is False and 'services' not in info.json()
    assert 'synthetic-secret' not in info.text
    headers = login(client)
    info = client.get('/api/oauth/request', params={'request': request_id}).json()
    assert info['client']['redirect_uri'] == REDIRECT
    assert {row['id'] for row in info['services']} == {'ssh/家庭NAS', 'ssh/工作服务器'}
    assert 'synthetic-secret' not in json.dumps(info)
    assert client.post('/api/oauth/decision', json={'request_id': request_id, 'approve': True,
        'allowed_services': ['*']}).status_code == 403
    assert client.post('/api/oauth/decision', headers=headers, json={'request_id': request_id,
        'approve': True, 'allowed_services': ['ssh/missing']}).status_code == 400
    code = approve(client, request_id, headers)
    assert client.post('/api/oauth/decision', headers=headers, json={
        'request_id': request_id, 'approve': True, 'allowed_services': ['*']}).status_code == 400
    tokens = exchange(client, client_id, code)
    assert client.post('/token', data={'grant_type': 'authorization_code', 'client_id': client_id,
        'redirect_uri': REDIRECT, 'resource': RESOURCE, 'code': code, 'code_verifier': VERIFIER}).status_code == 400
    listed = rpc(client, tokens['access_token'], 'tools/list')
    assert listed.status_code == 200, listed.text
    assert {tool['name'] for tool in listed.json()['result']['tools']} == {'list_credentials', 'get_credentials'}
    search = rpc(client, tokens['access_token'], 'tools/call', {'name': 'list_credentials', 'arguments': {'query': '家庭'}})
    data = content(search)
    assert data['count'] == 1 and data['credentials'][0]['id'] == 'ssh/家庭NAS'
    result = content(rpc(client, tokens['access_token'], 'tools/call', {'name': 'get_credentials',
        'arguments': {'credential_id': 'ssh/家庭NAS', 'fields': ['password']}}))
    assert result['credentials'] == {'password': 'synthetic-secret-家庭NAS'}
    denied = rpc(client, tokens['access_token'], 'tools/call', {'name': 'get_credentials',
        'arguments': {'credential_id': 'ssh/工作服务器'}}).json()['result']
    assert denied['isError'] is True and 'synthetic-secret' not in json.dumps(denied)
    refresh = client.post('/token', data={'grant_type': 'refresh_token', 'client_id': client_id,
        'resource': RESOURCE, 'refresh_token': tokens['refresh_token']})
    assert refresh.status_code == 200, refresh.text
    renewed = refresh.json()
    assert renewed['refresh_token'] != tokens['refresh_token']
    stored = (path / 'oauth.json').read_text()
    for secret in (code, tokens['access_token'], tokens['refresh_token'], renewed['access_token'], renewed['refresh_token']):
        assert secret not in stored
    grants = client.get('/api/oauth/grants').json()['grants']
    assert len(grants) == 1 and grants[0]['allowed_services'] == ['ssh/家庭NAS']
    assert client.delete('/api/oauth/grants/' + grants[0]['id'], headers=headers).status_code == 200
    assert rpc(client, renewed['access_token'], 'tools/list').status_code == 401
    assert client.post('/token', data={'grant_type': 'refresh_token', 'client_id': client_id,
        'resource': RESOURCE, 'refresh_token': renewed['refresh_token']}).status_code == 400


def test_request_browser_binding_cancel_and_no_implicit_consent(oauth_web):
    client, path = oauth_web
    client_id = register(client)
    headers = login(client)
    request_id = begin(client, client_id)
    assert client.get('/api/oauth/grants').json()['grants'] == []
    browser_cookies = dict(client.cookies)
    client.cookies.clear()
    assert client.get('/api/oauth/request', params={'request': request_id}).status_code == 400
    assert client.get('/oauth/consent', params={'request': request_id}).status_code == 409
    client.cookies.update(browser_cookies)
    denied = client.post('/api/oauth/decision', headers=headers, json={
        'request_id': request_id, 'approve': False, 'allowed_services': []})
    assert denied.status_code == 200, denied.text
    query = parse_qs(urlsplit(denied.json()['redirect_url']).query)
    assert query['error'] == ['access_denied'] and query['state'] == ['test-state'] and query['iss'] == [ORIGIN]
    assert client.get('/api/oauth/grants').json()['grants'] == []
    assert 'code' not in query


def test_refresh_survives_app_restart_and_replay_revokes_grant(oauth_web):
    client, path = oauth_web
    client_id = register(client)
    request_id = begin(client, client_id)
    tokens = exchange(client, client_id, approve(client, request_id, login(client)))
    with TestClient(create_app(path), base_url=ORIGIN) as fresh:
        assert rpc(fresh, tokens['access_token'], 'tools/list').status_code == 200
        renewed = fresh.post('/token', data={'grant_type': 'refresh_token', 'client_id': client_id,
            'resource': RESOURCE, 'refresh_token': tokens['refresh_token']})
        assert renewed.status_code == 200, renewed.text
        replay = fresh.post('/token', data={'grant_type': 'refresh_token', 'client_id': client_id,
            'resource': RESOURCE, 'refresh_token': tokens['refresh_token']})
        assert replay.status_code == 400
        assert rpc(fresh, renewed.json()['access_token'], 'tools/list').status_code == 401


def test_existing_manual_token_still_works_with_builtin_oauth(oauth_web):
    client, _ = oauth_web
    headers = login(client)
    token = client.post('/api/clients', headers=headers, json={'name': 'Existing desktop',
        'allowed_services': ['ssh/家庭NAS']}).json()['token']
    assert rpc(client, token, 'tools/list').status_code == 200
    assert client.get('/api/oauth/grants').json()['grants'] == []


def test_disabled_public_url_does_not_revive_tokens_after_restore(oauth_web):
    client, path = oauth_web
    client_id = register(client)
    request_id = begin(client, client_id)
    headers = login(client)
    tokens = exchange(client, client_id, approve(client, request_id, headers))
    assert client.put('/api/settings', headers=headers, json={'public_url': ''}).status_code == 200
    with TestClient(create_app(path), base_url='http://127.0.0.1') as local:
        headers = {'X-CSRF-Token': local.post('/api/login', json={
            'password': 'local-oauth-test-password'}).json()['csrf_token']}
        assert local.put('/api/settings', headers=headers, json={'public_url': ORIGIN}).status_code == 200
        assert rpc(local, tokens['access_token'], 'tools/list').status_code == 401
    with TestClient(create_app(path), base_url=ORIGIN) as restored:
        assert rpc(restored, tokens['access_token'], 'tools/list').status_code == 401
        assert restored.post('/token', data={'grant_type': 'refresh_token', 'client_id': client_id,
            'resource': RESOURCE, 'refresh_token': tokens['refresh_token']}).status_code in (400, 401)


def test_explicit_all_grant_includes_future_credentials(oauth_web):
    client, _ = oauth_web
    client_id = register(client)
    request_id = begin(client, client_id)
    headers = login(client)
    assert client.post('/api/oauth/decision', headers=headers, json={'request_id': request_id,
        'approve': True, 'allowed_services': []}).status_code == 400
    tokens = exchange(client, client_id, approve(client, request_id, headers, ['*']))
    assert client.post('/api/services', headers=headers, json={'kind': 'ssh', 'alias': '新增NAS',
        'config': {'host': '192.0.2.11', 'port': 22, 'username': 'root', 'auth_method': 'password'},
        'secrets': {'password': 'synthetic-new-password'}}).status_code == 200
    result = content(rpc(client, tokens['access_token'], 'tools/call', {'name': 'list_credentials', 'arguments': {}}))
    assert result['count'] == 3


def test_pending_expiry_origin_and_first_password_restrictions(oauth_web, monkeypatch):
    client, path = oauth_web
    client_id = register(client)
    request_id = begin(client, client_id)
    assert client.get('/oauth/consent', params={'request': request_id}, headers={'Host': '127.0.0.1'}).status_code == 421
    headers = login(client)
    assert client.post('/api/oauth/decision', headers={**headers, 'Origin': 'https://evil.example'}, json={
        'request_id': request_id, 'approve': True, 'allowed_services': ['*']}).status_code == 403
    builtin = client.app.app.state.builtin_oauth
    builtin._pending[request_id]['expires_at'] = 0
    assert client.get('/api/oauth/request', params={'request': request_id}).status_code == 400
    assert client.post('/api/oauth/decision', headers=headers, json={'request_id': request_id,
        'approve': True, 'allowed_services': ['*']}).status_code == 400
    fresh_path = path / 'first-use'
    store = Store(fresh_path)
    store.save_settings({'public_url': ORIGIN})
    with TestClient(create_app(fresh_path), base_url=ORIGIN) as fresh:
        request_id = begin(fresh, register(fresh))
        response = fresh.post('/api/login', json={'password': (fresh_path / 'first-login.txt').read_text().strip()})
        context = fresh.get('/api/oauth/request', params={'request': request_id}).json()
        assert context['must_change_password'] is True and 'services' not in context
        assert fresh.post('/api/oauth/decision', headers={'X-CSRF-Token': response.json()['csrf_token']}, json={
            'request_id': request_id, 'approve': True, 'allowed_services': ['*']}).status_code == 403

"""Exercise a real server across container recreation using synthetic credentials.

HTTP is the private reverse-proxy hop. Host matches the configured HTTPS origin;
this probe explicitly forwards the browser cookies a TLS proxy would receive.
No production vault, account or token is used. Tokens stay in a disposable 0600
runner file so the second container can prove refresh-token persistence.
"""
import base64
import hashlib
import http.client
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlencode, urlsplit

mode, checkpoint = sys.argv[1:3]
port = int(sys.argv[3]) if len(sys.argv) > 3 else 18093
origin = 'https://vault.example'
resource = origin + '/mcp'
callback = 'https://client.example/oauth/callback'
cookies = {}
authority = '127.0.0.1:' + str(port) if mode == 'start' else 'vault.example'


def request(method, path, payload=None, *, form=False, headers=None, status=200, raw=False):
    request_headers = {'Host': authority, **(headers or {})}
    if cookies:
        request_headers['Cookie'] = '; '.join(key + '=' + value for key, value in cookies.items())
    body = None
    if payload is not None:
        body = urlencode(payload) if form else json.dumps(payload)
        request_headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        body = response.read()
        assert response.status == status, f'{method} {path.split("?")[0]} returned {response.status}, expected {status}'
        for key, value in response.getheaders():
            if key.lower() == 'set-cookie':
                parsed = SimpleCookie(value)
                cookies.update({name: item.value for name, item in parsed.items()})
        return (dict(response.getheaders()), body) if raw else (json.loads(body) if body else None)
    finally:
        connection.close()


def verify_access(token):
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'}
    result = request('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}}, headers=headers)
    assert {tool['name'] for tool in result['result']['tools']} == {'list_credentials', 'get_credentials'}
    result = request('POST', '/mcp', {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
        'params': {'name': 'get_credentials', 'arguments': {'credential_id': 'ssh/容器持久化测试', 'fields': ['password']}}}, headers=headers)['result']
    assert not result.get('isError')
    content = result.get('structuredContent') or json.loads(result['content'][0]['text'])
    assert content['credentials']['password'] == 'synthetic-container-credential'
    assert content['connection']['host'] == '192.0.2.10'
    assert content['connection']['external_host'] == 'ssh.example.com'
    assert content['connection']['external_port'] == 2222


if mode == 'start':
    session = request('POST', '/api/login', {'password': 'container-smoke-test-password'})
    request('PUT', '/api/settings', {'public_url': origin}, headers={'X-CSRF-Token': session['csrf_token']})
    authority = 'vault.example'
    cookies.clear()
    discovery = request('GET', '/.well-known/oauth-authorization-server')
    assert discovery['issuer'] == origin and discovery['registration_endpoint'] == origin + '/register'
    client = request('POST', '/register', {'client_name': 'Container persistence test', 'redirect_uris': [callback],
        'response_types': ['code'], 'grant_types': ['authorization_code', 'refresh_token'],
        'scope': 'credentials:read', 'token_endpoint_auth_method': 'none'}, status=201)
    verifier = 'synthetic-pkce-verifier-' + 'a' * 48
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    headers, _ = request('GET', '/authorize?' + urlencode({'client_id': client['client_id'], 'response_type': 'code',
        'redirect_uri': callback, 'resource': resource, 'scope': 'credentials:read', 'state': 'container-smoke',
        'code_challenge': challenge, 'code_challenge_method': 'S256'}), status=302, raw=True)
    location = headers.get('location') or headers['Location']
    request_id = parse_qs(urlsplit(location).query)['request'][0]
    request('GET', '/oauth/consent?' + urlencode({'request': request_id}), raw=True)
    session = request('POST', '/api/login', {'password': 'container-smoke-test-password'})
    context = request('GET', '/api/oauth/request?' + urlencode({'request': request_id}))
    assert context['authenticated'] and len(context['services']) == 1
    response = request('POST', '/api/oauth/decision', {'request_id': request_id, 'approve': True,
        'allowed_services': ['ssh/容器持久化测试']}, headers={'X-CSRF-Token': session['csrf_token']})
    query = parse_qs(urlsplit(response['redirect_url']).query)
    assert query['iss'] == [origin] and query['state'] == ['container-smoke']
    tokens = request('POST', '/token', {'grant_type': 'authorization_code', 'client_id': client['client_id'],
        'code': query['code'][0], 'redirect_uri': callback, 'resource': resource, 'code_verifier': verifier}, form=True)
    verify_access(tokens['access_token'])
    path = Path(checkpoint)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump({'client_id': client['client_id'], **tokens}, stream)
    print('Real HTTP OAuth login, explicit consent, PKCE exchange and scoped MCP access passed')
else:
    saved = json.loads(Path(checkpoint).read_text())
    verify_access(saved['access_token'])
    tokens = request('POST', '/token', {'grant_type': 'refresh_token', 'client_id': saved['client_id'],
        'refresh_token': saved['refresh_token'], 'resource': resource}, form=True)
    assert tokens['refresh_token'] != saved['refresh_token']
    verify_access(tokens['access_token'])
    session = request('POST', '/api/login', {'password': 'container-smoke-test-password'})
    grants = request('GET', '/api/oauth/grants')['grants']
    assert len(grants) == 1 and grants[0]['allowed_services'] == ['ssh/容器持久化测试']
    request('DELETE', '/api/oauth/grants/' + grants[0]['id'], headers={'X-CSRF-Token': session['csrf_token']})
    request('POST', '/mcp', {}, headers={'Authorization': 'Bearer ' + tokens['access_token']}, status=401)
    print('OAuth access and refresh survived container recreation; administrator revocation passed')

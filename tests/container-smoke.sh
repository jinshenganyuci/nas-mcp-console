#!/bin/sh
# Run against a newly built image using a dedicated, disposable data volume.
set -eu
cd "$(dirname "$0")/.."
export MCP_IMAGE="${1:?Pass a locally built test image}"
export COMPOSE_PROJECT_NAME="nas-mcp-smoke-$$"
export MCP_DATA_VOLUME="nas-mcp-smoke-data-$$"
export MCP_PORT=18093
export BIND_ADDRESS=127.0.0.1
export ALLOWED_HOSTS=
compose() { docker compose -f compose.fnos.yaml "$@"; }
cleanup() { compose down --volumes --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

# No data directory or local credentials may be present in the shipped image.
docker run --rm --entrypoint python "$MCP_IMAGE" -c 'from pathlib import Path; import os; assert os.getuid() == 1000; assert not list(Path("/data").iterdir()); assert not Path("/app/.env").exists(); assert not Path("/app/data").exists()'
compose up -d --wait --wait-timeout 90
compose exec -T nas-mcp python - <<'PY'
import json, os, urllib.error, urllib.request
from pathlib import Path
from http.cookiejar import CookieJar

assert os.getuid() == 1000
data = Path('/data')
assert data.stat().st_uid == 1000
assert data.stat().st_mode & 0o777 == 0o700
for name in ('master.key', 'vault.json', 'state.json', 'first-login.txt'):
    assert (data / name).stat().st_mode & 0o777 == 0o600
try:
    urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8090/mcp', data=b'{}'), timeout=10)
except urllib.error.HTTPError as error:
    assert error.code == 401
else:
    raise AssertionError('Unauthenticated MCP request was accepted')
password = (data / 'first-login.txt').read_text().strip()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
def post(path, payload, csrf=None):
    headers = {'Content-Type': 'application/json'}
    if csrf:
        headers['X-CSRF-Token'] = csrf
    request = urllib.request.Request('http://127.0.0.1:8090' + path, data=json.dumps(payload).encode(), headers=headers)
    with opener.open(request, timeout=10) as response:
        return json.load(response)
login = post('/api/login', {'password': password})
session = post('/api/password', {'current_password': password, 'new_password': 'container-smoke-test-password'}, login['csrf_token'])
post('/api/services', {'kind': 'ssh', 'alias': 'container-smoke', 'config': {'host': '192.0.2.10', 'port': 22, 'username': 'root', 'auth_method': 'password'}, 'secrets': {'password': 'synthetic-container-credential'}}, session['csrf_token'])
assert not (data / 'first-login.txt').exists()
assert 'synthetic-container-credential' not in (data / 'vault.json').read_text()
print('First boot, non-root volume access, login, and encrypted save passed')
PY

# Recreate the container rather than only restarting its process.
compose up -d --force-recreate --wait --wait-timeout 90
compose exec -T nas-mcp python - <<'PY'
from pathlib import Path
from app.store import Store

store = Store(Path('/data'))
assert not store.must_change_password
assert store.verify_password('container-smoke-test-password')
record = store.export_credential('ssh/container-smoke', ['*'])
assert record['credentials']['password'] == 'synthetic-container-credential'
assert not Path('/data/first-login.txt').exists()
print('Credential persistence after container recreation passed')
PY

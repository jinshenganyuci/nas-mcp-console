#!/bin/sh
# Run against a local image using a dedicated, disposable bind-mount directory.
set -eu
cd "$(dirname "$0")/.."
smoke_image="${1:?Pass a locally available test image}"
smoke_uid="$(id -u)"
smoke_gid="$(id -g)"
if [ "$smoke_uid" -eq 0 ]; then
    echo 'Run the container smoke test as a non-root user with Docker access.' >&2
    exit 1
fi
umask 077
smoke_dir="$(mktemp -d "${TMPDIR:-/tmp}/nas-mcp-smoke-XXXXXXXX")"
smoke_project="$(basename "$smoke_dir" | tr '[:upper:]' '[:lower:]')"
compose() { docker compose --project-name "$smoke_project" -f "$smoke_dir/compose.json" "$@"; }
cleanup() {
    if [ -f "$smoke_dir/compose.json" ]; then
        compose down --remove-orphans >/dev/null 2>&1 || true
    fi
    rm -rf -- "$smoke_dir"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# Read the production configuration without starting it. Replace its image,
# port and bind mount explicitly; production no longer uses environment overrides.
mkdir -m 700 "$smoke_dir/data"
docker compose -f compose.fnos.yaml config --format json > "$smoke_dir/production.json"
python3 - "$smoke_dir" "$smoke_image" "$smoke_uid" "$smoke_gid" "$smoke_project" <<'PY'
import json, sys
from pathlib import Path

directory = Path(sys.argv[1]).resolve()
image, uid, gid, project = sys.argv[2:]
config = json.loads((directory / 'production.json').read_text())
assert set(config['services']) == {'nas-mcp'}
service = config['services']['nas-mcp']
service['image'] = image
service['pull_policy'] = 'never'
service['user'] = f'{uid}:{gid}'
service['ports'] = [{'target': 8090, 'published': '18093', 'host_ip': '127.0.0.1', 'protocol': 'tcp'}]
service['volumes'] = [{'type': 'bind', 'source': str(directory / 'data'), 'target': '/data', 'bind': {'create_host_path': False}}]
config['name'] = project
# `compose config` expands the original default network name as well.
service['networks'] = {'default': None}
config['networks'] = {'default': {'name': f'{project}_default'}}
config.pop('volumes', None)
(directory / 'compose.json').write_text(json.dumps(config))
PY

# No data directory or local credentials may be present in the shipped image.
docker run --rm --pull=never --entrypoint python "$smoke_image" -c 'from pathlib import Path; import os; assert os.getuid() == 1000; assert not list(Path("/data").iterdir()); assert not Path("/app/.env").exists(); assert not Path("/app/data").exists()'
compose up -d --wait --wait-timeout 90
compose exec -T nas-mcp python - "$smoke_uid" "$smoke_gid" <<'PY'
import json, os, sys, urllib.error, urllib.request
from pathlib import Path
from http.cookiejar import CookieJar

uid, gid = map(int, sys.argv[1:])
assert os.getuid() == uid != 0 and os.getgid() == gid
data = Path('/data')
assert (data.stat().st_uid, data.stat().st_gid) == (uid, gid)
assert data.stat().st_mode & 0o777 == 0o700
for name in ('master.key', 'vault.json', 'state.json', 'first-login.txt'):
    assert (data / name).stat().st_uid == uid
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
post('/api/services', {'kind': 'ssh', 'alias': '容器持久化测试', 'config': {'host': '192.0.2.10', 'port': 22, 'username': 'root', 'auth_method': 'password'}, 'secrets': {'password': 'synthetic-container-credential'}}, session['csrf_token'])
assert not (data / 'first-login.txt').exists()
assert 'synthetic-container-credential' not in (data / 'vault.json').read_text()
print('First boot, non-root bind-mount access, login, and encrypted save passed')
PY

python3 tests/oauth-container-probe.py start "$smoke_dir/oauth-checkpoint.json"

# Recreate the container rather than only restarting its process.
compose up -d --force-recreate --wait --wait-timeout 90
compose exec -T nas-mcp python - "$smoke_uid" "$smoke_gid" <<'PY'
import os, sys
from pathlib import Path
from app.store import Store

uid, gid = map(int, sys.argv[1:])
assert os.getuid() == uid != 0 and os.getgid() == gid
data = Path('/data')
assert (data.stat().st_uid, data.stat().st_gid) == (uid, gid)
assert data.stat().st_mode & 0o777 == 0o700
store = Store(data)
assert not store.must_change_password
assert store.verify_password('container-smoke-test-password')
record = store.export_credential('ssh/容器持久化测试', ['*'])
assert record['alias'] == '容器持久化测试'
assert record['credentials']['password'] == 'synthetic-container-credential'
assert not Path('/data/first-login.txt').exists()
for name in ('master.key', 'vault.json', 'state.json', 'oauth.json'):
    assert (data / name).stat().st_uid == uid
    assert (data / name).stat().st_mode & 0o777 == 0o600
print('Chinese credential name and credential persistence after container recreation passed')
PY

python3 tests/oauth-container-probe.py resume "$smoke_dir/oauth-checkpoint.json"

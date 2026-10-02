"""Build using the repository's pinned GitHub Actions workflow; collect exact-SHA UF2s."""
import argparse
import hashlib
import io
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from check_keymap import check

ROOT = Path(__file__).resolve().parents[1]
REPO = 'Parisella/zmk-new_corne'
STATE = ROOT / '.corne-build/state.json'


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        result = super().redirect_request(req, fp, code, msg, headers, newurl)
        if result and urllib.parse.urlparse(req.full_url).netloc != urllib.parse.urlparse(newurl).netloc:
            result.remove_header('Authorization')
        return result


def token():
    value = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if value:
        return value
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
    result = subprocess.run(['git', 'credential', 'fill'], input='protocol=https\nhost=github.com\n\n',
                            cwd=ROOT, env=env, text=True, capture_output=True)
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if result.returncode or not fields.get('password'):
        raise RuntimeError('GitHub authentication unavailable. Sign into GitHub with Git Credential Manager or set GH_TOKEN privately. No credentials are printed or saved.')
    return fields['password']


def api(endpoint, credential, data=None, raw=False):
    request = urllib.request.Request('https://api.github.com/repos/' + REPO + '/' + endpoint,
                                    data=None if data is None else json.dumps(data).encode(),
                                    headers={'Authorization': 'Bearer ' + credential,
                                             'Accept': 'application/vnd.github+json',
                                             'Content-Type': 'application/json', 'User-Agent': 'corne-keymap'})
    with urllib.request.build_opener(SafeRedirect()).open(request, timeout=30) as response:
        payload = response.read()
    return payload if raw else (json.loads(payload) if payload else None)


def save(state):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))


def resolve_run(state, credential):
    if state.get('run_id'):
        return api('actions/runs/' + str(state['run_id']), credential)
    runs = api('actions/workflows/build.yml/runs?per_page=100', credential)['workflow_runs']
    candidates = [r for r in runs if r['head_sha'] == state['sha'] and r['head_branch'] == state['branch']
                  and r['event'] == state['event'] and r['id'] not in state['prior_runs']]
    if len(candidates) > 1:
        raise RuntimeError('Multiple matching builds: resolve the intended run explicitly before collecting.')
    if not candidates:
        return None
    state['run_id'] = candidates[0]['id']
    save(state)
    return candidates[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['auth-check', 'start', 'status', 'collect'])
    parser.add_argument('--publish', action='store_true', help='Push current committed branch before dispatching')
    args = parser.parse_args()
    credential = token()
    if args.action == 'auth-check':
        api('actions/workflows/build.yml', credential)
        print('GitHub credentials available; build workflow accessible.')
        return
    if args.action == 'start':
        check(ROOT / 'config/eyelash_corne.keymap')
        if git('status', '--porcelain'):
            raise RuntimeError('Commit the reviewed changes before building; working tree must be clean.')
        branch, sha = git('branch', '--show-current'), git('rev-parse', 'HEAD')
        if not branch:
            raise RuntimeError('Use a named branch.')
        prior = api('actions/workflows/build.yml/runs?per_page=100', credential)['workflow_runs']
        if args.publish:
            if git('remote', 'get-url', 'origin') not in ['https://github.com/' + REPO + '.git', 'git@github.com:' + REPO + '.git']:
                raise RuntimeError('Unexpected origin; refusing to push.')
            subprocess.run(['git', 'push', '--set-upstream', 'origin', branch], cwd=ROOT, check=True)
        remote = api('git/ref/heads/' + urllib.parse.quote(branch, safe=''), credential)
        if remote['object']['sha'] != sha:
            raise RuntimeError('Remote branch differs from HEAD. Publish the intended commit first.')
        state = {'repository': REPO, 'sha': sha, 'branch': branch, 'event': 'push' if args.publish else 'workflow_dispatch', 'prior_runs': [r['id'] for r in prior]}
        save(state)
        if not args.publish:
            api('actions/workflows/build.yml/dispatches', credential, {'ref': branch})
        print('Dispatched exact commit ' + sha + '. Use status, then collect once successful.')
        return
    state = json.loads(STATE.read_text())
    run = resolve_run(state, credential)
    if not run:
        print('Waiting for GitHub to register the dispatched run. Retry status later.')
        return
    if run['head_sha'] != state['sha'] or run['path'] != '.github/workflows/build.yml':
        raise RuntimeError('Build identity mismatch.')
    print(f"{run['html_url']} : {run['status']} / {run['conclusion']}")
    if args.action == 'status':
        return
    if run['status'] != 'completed' or run['conclusion'] != 'success':
        raise RuntimeError('Collect requires a successful completed build.')
    if git('rev-parse', 'HEAD') != state['sha'] or git('status', '--porcelain'):
        raise RuntimeError('Working source differs from the built commit.')
    artifacts = api(f"actions/runs/{run['id']}/artifacts", credential)['artifacts']
    artifacts = [a for a in artifacts if a['name'] == 'firmware' and not a['expired']]
    if len(artifacts) != 1:
        raise RuntimeError('Expected exactly one unexpired firmware artifact.')
    archive = zipfile.ZipFile(io.BytesIO(api(f"actions/artifacts/{artifacts[0]['id']}/zip", credential, raw=True)))
    output = ROOT / '.corne-build' / str(run['id'])
    output.mkdir(exist_ok=False)
    files = []
    for entry in archive.infolist():
        if not entry.filename.lower().endswith('.uf2'):
            continue
        name = Path(entry.filename).name
        target = output / name
        if target.exists():
            raise RuntimeError('Duplicate artifact filename.')
        data = archive.read(entry)
        target.write_bytes(data)
        half = 'left' if 'left' in name else 'right' if 'right' in name else None
        variant = 'settings-reset' if 'settings_reset' in name else 'studio' if 'studio' in name else 'standard'
        files.append({'name': name, 'half': half, 'variant': variant, 'sha256': hashlib.sha256(data).hexdigest()})
    if not any(f['half'] == 'left' and f['variant'] == 'standard' for f in files) or not any(f['half'] == 'right' and f['variant'] == 'standard' for f in files):
        raise RuntimeError('Missing standard left/right UF2 images.')
    manifest = {'repository': REPO, 'commit': state['sha'], 'run_id': run['id'], 'run_url': run['html_url'], 'files': files}
    path = output / 'manifest.json'
    path.write_text(json.dumps(manifest, indent=2))
    print('Validated build artifacts: ' + str(path))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        raise SystemExit(str(error))

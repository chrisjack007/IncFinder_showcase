"""Explicit, authenticated downloads for the two pinned model sources."""

import hashlib
import json
from http.client import HTTPException
import os
import re
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .artifact_manifest import artifact_path
from .common import read_json, write_json
from .model_install import BUFFER_SIZE, _check, _publish, _regular

_API = 'https://api.github.com'
_STORAGE = {'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, next_url):
        return None


def _credential():
    for name in ('GH_TOKEN', 'GITHUB_TOKEN'):
        value = os.environ.get(name, '').strip()
        if value:
            return value
    try:
        result = subprocess.run(['gh', 'auth', 'token', '--hostname', 'github.com'],
                                capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise RuntimeError('GitHub authorization is unavailable. Sign in with gh auth login before RF download.') from None
    value = result.stdout.strip()
    if result.returncode or not value:
        raise RuntimeError('GitHub authorization is unavailable. Sign in with gh auth login before RF download.')
    return value


def _safe_storage(location):
    url = urlsplit(location)
    if (url.scheme != 'https' or url.hostname not in _STORAGE or url.username or url.password
            or url.port not in (None, 443)):
        raise ValueError('Invalid GitHub asset storage redirect')
    return location


def _etag_fingerprint(headers):
    tag = headers.get('ETag', '')
    if re.fullmatch(r'"[\x21\x23-\x7e]{1,256}"', tag):
        return hashlib.sha256(tag.encode('ascii')).hexdigest()
    return None


def _paths(cache, row):
    directory = artifact_path(cache / 'rf')
    name = row['name']
    if not re.fullmatch(r'stage[12]\.joblib(?:\.part-[0-9]{3,6})?|[A-Za-z_]+\.json', name):
        raise ValueError('Invalid RF asset cache name')
    return directory / name, directory / (name + '.partial'), directory / (name + '.resume.json')


def rf_required_bytes(cache, row):
    complete, pending, _ = _paths(cache, row)
    if _regular(complete) is not None:
        return 0
    info = _regular(pending)
    return row['size_bytes'] - min(info.st_size, row['size_bytes']) if info else row['size_bytes']


def _saved_fingerprint(state, identity):
    if _regular(state) is None:
        return None
    if state.stat().st_size > 4096:
        return None
    try:
        info = read_json(state)
    except (ValueError, UnicodeError):
        return None
    if isinstance(info, dict) and info.get('identity') == identity:
        value = info.get('etag_sha256')
        if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value):
            return value
    return None


class GitHubRF:
    def __init__(self, supply):
        self.supply = supply
        self.opener = build_opener(_NoRedirect())
        self.token = None
        self.assets = None
        self.release_id = None
        self.repo = _API + '/repos/' + supply['repository']

    def _request(self, path, accept, offset=0):
        result = {'Accept': accept,
                  'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'IncFinder-models', 'Accept-Encoding': 'identity'}
        if self.token:
            result['Authorization'] = 'Bearer ' + self.token
        if offset:
            result['Range'] = f'bytes={offset}-'
        return Request(self.repo + path, headers=result)

    def _json(self, path):
        try:
            with self.opener.open(self._request(path, 'application/vnd.github+json'), timeout=30) as response:
                data = response.read(512 * 1024 + 1)
                if len(data) > 512 * 1024:
                    raise ValueError('GitHub release metadata is too large')
                return json.loads(data)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise RuntimeError(f'GitHub RF release authorization or metadata failed (HTTP {code}).') from None
        except (URLError, HTTPException, OSError, ValueError):
            raise RuntimeError('GitHub RF release metadata could not be read.') from None

    def _validate_asset(self, row):
        if self.assets is None:
            if self.supply.get('access') != 'public':
                self.token = _credential()
            release = self._json('/releases/tags/' + quote(self.supply['tag'], safe=''))
            actual_id = release.get('id')
            if (type(actual_id) is not int or actual_id <= 0 or release.get('tag_name') != self.supply['tag']
                    or release.get('draft') is not False or self.supply.get('release_id', actual_id) != actual_id):
                raise ValueError('GitHub RF release identity differs from the pinned release')
            assets = {}
            page = 1
            while True:
                query = f'/releases/{actual_id}/assets?per_page=100'
                if page > 1:
                    query += f'&page={page}'
                entries = self._json(query)
                if not isinstance(entries, list) or len(entries) > 100:
                    raise ValueError('Invalid GitHub release asset listing')
                for asset in entries:
                    ident = asset.get('id')
                    if type(ident) is not int or ident <= 0 or ident in assets:
                        raise ValueError('Invalid or repeated GitHub release asset ID')
                    assets[ident] = asset
                if len(entries) < 100:
                    break
                page += 1
            self.release_id, self.assets = actual_id, assets
        if 'asset_id' in row:
            found = self.assets.get(row['asset_id'])
        else:
            matches = [asset for asset in self.assets.values() if asset.get('name') == row['name']]
            found = matches[0] if len(matches) == 1 else None
        if (found is None or found.get('name') != row['name'] or found.get('size') != row['size_bytes']
                or found.get('digest') != 'sha256:' + row['sha256']):
            raise ValueError(f'GitHub RF asset metadata mismatch: {row["name"]}')
        return found['id']

    def _binary(self, row, offset):
        path = f"/releases/assets/{row['asset_id']}"
        name = row['name']
        try:
            try:
                return self.opener.open(self._request(path, 'application/octet-stream', offset), timeout=40)
            except HTTPError as exc:
                code = exc.code
                location = exc.headers.get('Location', '')
                exc.close()
                if code not in (301, 302, 303, 307, 308):
                    raise RuntimeError(f'GitHub RF asset request failed for {name} (HTTP {code}).') from None
                headers = {'Accept-Encoding': 'identity'}
                if offset:
                    headers['Range'] = f'bytes={offset}-'
                return self.opener.open(Request(_safe_storage(location), headers=headers), timeout=40)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise RuntimeError(f'GitHub RF asset storage rejected {name} (HTTP {code}).') from None
        except (URLError, HTTPException, OSError, ValueError):
            raise RuntimeError(f'GitHub RF asset transfer could not start: {name}.') from None

    def _receive(self, row, pending, state, identity, offset, previous):
        size = row['size_bytes']
        name = row['name']
        try:
            with self._binary(row, offset) as response:
                code = response.status
                tag = _etag_fingerprint(response.headers)
                if code == 200:
                    start = 0
                elif (offset and code == 206 and tag is not None and tag == previous and
                      response.headers.get('Content-Range') == f'bytes {offset}-{size - 1}/{size}'):
                    start = offset
                else:
                    return None
                length = response.headers.get('Content-Length')
                if ((length is not None and length != str(size - start)) or
                        response.headers.get('Content-Encoding', 'identity').lower() != 'identity'):
                    return None
                info = _regular(pending)
                if start and (info is None or info.st_size != start):
                    raise ValueError(f'Partial RF asset changed before resume: {name}')
                _regular(state)
                with pending.open('ab' if start else 'wb') as output:
                    write_json(state, {'identity': identity, 'etag_sha256': tag})
                    remaining = size - start
                    while remaining:
                        block = response.read(min(BUFFER_SIZE, remaining))
                        if not block:
                            raise RuntimeError(f'RF download interrupted: {name}. Retry the install to resume.')
                        remaining -= len(block)
                        output.write(block)
                    if response.read(1):
                        raise RuntimeError(f'RF download exceeded its declared size: {name}')
                    output.flush()
                    os.fsync(output.fileno())
                return start
        except (URLError, HTTPException, TimeoutError, ConnectionError):
            raise RuntimeError(f'RF download interrupted: {name}. Retry the install to resume.') from None

    def fetch(self, cache, row):
        complete, pending, state = _paths(cache, row)
        cached = {'name': row['name'], 'bytes': 0, 'sha256': row['sha256'],
                  'source': 'verified_cache', 'resumed_from_bytes': 0}
        if 'asset_id' in row:
            cached['asset_id'] = row['asset_id']
        if _regular(complete) is not None:
            _check(complete, row, row['name'])
            return complete, cached
        pending.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = _regular(pending)
        if info is not None and info.st_size == row['size_bytes']:
            try:
                _check(pending, row, row['name'])
            except ValueError:
                pass  # The completed partial did not verify. Replace it with a fresh transfer.
            else:
                _regular(state)
                _publish(pending, complete)
                state.unlink(missing_ok=True)
                return complete, cached
        actual_id = self._validate_asset(row)
        if 'asset_id' not in row:
            row = dict(row, asset_id=actual_id)
        identity = {key: row[key] for key in ('asset_id', 'size_bytes', 'sha256')}
        prior = _saved_fingerprint(state, identity)
        info = _regular(pending)
        offset = info.st_size if info and prior and 0 < info.st_size < row['size_bytes'] else 0
        resumed = self._receive(row, pending, state, identity, offset, prior)
        if resumed is None and offset:
            resumed = self._receive(row, pending, state, identity, 0, None)
        if resumed is None:
            raise ValueError(f'Invalid transfer length or range for RF volume: {row["name"]}')
        try:
            _check(pending, row, row['name'])
        except ValueError:
            # Keep an interrupted transfer, but never reuse a complete body
            # that has already failed its published checksum.
            pending.unlink(missing_ok=True)
            state.unlink(missing_ok=True)
            raise
        _publish(pending, complete)
        state.unlink(missing_ok=True)
        proof = {'name': row['name'], 'asset_id': row['asset_id'], 'bytes': row['size_bytes'] - resumed,
                 'sha256': row['sha256'], 'source': 'github_release', 'resumed_from_bytes': resumed}
        if self.release_id is not None:
            proof.update(repository=self.supply['repository'], tag=self.supply['tag'], release_id=self.release_id)
        return complete, proof


def download_hub(cache, supply, filename):
    # The existing authenticated Hub client manages its own HTTP cache and
    # partial-file lock. Child output can contain signed URLs, so do not relay it.
    directory = artifact_path(cache / 'ntv3')
    output = artifact_path(directory / filename)
    if output.parent != directory:
        raise ValueError('Invalid NTv3 download filename')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    script = (
        'import sys\n'
        'from huggingface_hub import hf_hub_download\n'
        'repo, name, revision, directory = sys.argv[1:]\n'
        'hf_hub_download(repo_id=repo, filename=name, revision=revision, local_dir=directory, '
        'token=True, endpoint="https://huggingface.co")\n'
    )
    environment = {**os.environ, 'HF_HUB_CACHE': str(cache / 'hub-cache'), 'HF_XET_CACHE': str(cache / 'xet-cache'),
                   'HF_HUB_DISABLE_XET': '1', 'HF_DEBUG': '0', 'HF_HUB_DISABLE_PROGRESS_BARS': '1'}
    try:
        result = subprocess.run([sys.executable, '-s', '-B', '-c', script, supply['repository'], filename,
                                 supply['revision'], str(directory)], env=environment, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except OSError:
        raise RuntimeError(f'Could not start the authenticated Hugging Face client for {filename}.') from None
    if result.returncode or _regular(output) is None:
        raise RuntimeError(f'NTv3 download failed for {filename}. Check gated access and your hf auth login.')
    return output

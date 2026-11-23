"""Check the documented upstream release assets by their pinned identities."""
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'chrisjack007/IncFinder_engineer'
V1_COMMIT = 'b0127980199c00da01730af0b7f85c137473aaf2'
V1_TAG_OBJECT = '0fb5405a23e1d4b35b4bf31eb1d2804d96719769'
CONTROL_FILES = (
    ('MODELS_SHA256SUMS', 755676908, 786,
     '36ef9bd06b5ca1dfb2b67b50eb01f9adb8bd62696504dd4fb0b3f55a74817a4f'),
    ('RF_ASSETS.json', 755676983, 4420,
     'a4d5307d3bb04b38374a8617b1fe382a8f1e8baea03ef364f19accba4af2357a'),
    ('RF_PARTS_SHA256SUMS', 755676907, 2061,
     'f55a607cf296c78c9880f3523098992ce0f8bd605f2e1ea5b3a99c1c20f6be95'),
)


def v1_reference():
    sources = json.loads((ROOT / 'config/model_sources.json').read_bytes())
    rf = sources['rf']
    if rf['repository'] != REPOSITORY:
        raise ValueError('Unexpected RF release repository')
    controls = [
        {'name': name, 'asset_id': number, 'size_bytes': length, 'sha256': checksum}
        for name, number, length, checksum in CONTROL_FILES
    ]
    download_root = f"https://github.com/{REPOSITORY}/releases/download/{rf['tag']}/"
    assets = sorted(controls + rf['parts'], key=lambda row: row['name'])
    if len(assets) != 26 or len({row['name'] for row in assets}) != 26:
        raise ValueError('Incomplete V1 reference inventory')
    return {
        'repository': REPOSITORY, 'tag': rf['tag'], 'release_id': rf['release_id'],
        'commit': V1_COMMIT, 'tag_object': V1_TAG_OBJECT,
        'assets': [dict(row, download_url=download_root + row['name']) for row in assets],
        'ntv3_provider': sources['ntv3'],
        'retention': 'Keep the original release and asset IDs. Use IF-013 verified offline recovery '
                     'with authorized copies and the original ordered part hashes.',
    }


def check_v1_release(release, ref, tag):
    expected = v1_reference()
    release_identity = (release.get('id'), release.get('tag_name'), release.get('target_commitish'))
    if release_identity != (expected['release_id'], expected['tag'], expected['commit']):
        raise ValueError(f"V1 release identity mismatch: {expected['tag']}")
    if (ref.get('ref') != f"refs/tags/{expected['tag']}" or
            ref.get('object', {}).get('sha') != expected['tag_object'] or
            ref.get('object', {}).get('type') != 'tag' or
            tag.get('object', {}).get('sha') != expected['commit'] or
            tag.get('object', {}).get('type') != 'commit' or
            tag.get('sha') != expected['tag_object']):
        raise ValueError(f"V1 Git tag identity mismatch: {expected['tag']}")
    assets = release.get('assets')
    if not isinstance(assets, list) or any(not isinstance(a, dict) for a in assets):
        raise ValueError('Unable to confirm V1 assets from the GitHub release response')
    by_name = {row.get('name'): row for row in assets}
    if len(by_name) != len(assets) or len({row.get('id') for row in assets}) != len(assets):
        raise ValueError('Duplicate GitHub V1 asset identities')
    for item in expected['assets']:
        name = item['name']
        label = f"{expected['tag']}/{name} (asset ID {item['asset_id']})"
        actual = by_name.get(name)
        if actual is None:
            raise ValueError(f'Missing V1 release asset: {label}')
        if (actual.get('id'), actual.get('size'), actual.get('digest'), actual.get('browser_download_url'),
                actual.get('state')) != (item['asset_id'], item['size_bytes'], 'sha256:' + item['sha256'],
                                        item['download_url'], 'uploaded'):
            raise ValueError(f'V1 release asset ID, size, checksum or URL mismatch: {label}')
    return dict(expected, status='PASS', checked_at_utc=datetime.now(timezone.utc).isoformat())


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def live_v1_reference(token):
    if not token:
        raise ValueError('V1 access could not be confirmed: a repository read credential is required')
    expected = v1_reference()
    opener = build_opener(_NoRedirect())

    def fetch(resource):
        request = Request(f'https://api.github.com/repos/{REPOSITORY}/{resource}', headers={
            'Accept': 'application/vnd.github+json', 'Authorization': 'Bearer ' + token,
            'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'IncFinder-release-evidence'})
        try:
            with opener.open(request, timeout=20) as response:
                return json.load(response)
        except HTTPError as error:
            raise ValueError(f"V1 access could not be confirmed for {resource}: HTTP {error.code}") from None
        except (URLError, TimeoutError):
            raise ValueError(f'V1 access could not be confirmed for {resource}: connection failed') from None

    return check_v1_release(fetch(f"releases/{expected['release_id']}"),
                            fetch(f"git/ref/tags/{expected['tag']}"),
                            fetch(f"git/tags/{expected['tag_object']}"))


def check_v1_snapshot(snapshot):
    expected = v1_reference()
    observed = snapshot.get('checked_at_utc', '')
    try:
        valid_time = datetime.fromisoformat(observed).utcoffset() == timezone.utc.utcoffset(None)
    except (TypeError, ValueError):
        valid_time = False
    if not valid_time or snapshot != dict(expected, status='PASS', checked_at_utc=observed):
        raise ValueError('V1 reference snapshot does not match the pinned release identities')

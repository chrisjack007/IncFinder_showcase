"""Identify and load the four pinned upstream Python sources for the encoder."""

from contextlib import contextmanager
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import uuid


CODE_SOURCE = {
    'repository': 'InstaDeepAI/ntv3_base_model',
    'revision': '27682ed2f537a4c4fcc356b6b8201e7dfceca602',
}
CODE_NAMES = ('__init__.py', 'configuration_ntv3_pretrained.py',
              'modeling_ntv3_pretrained.py', 'tokenization_ntv3.py')
CODE_PATHS = frozenset('vendor/ntv3/' + name for name in CODE_NAMES)
CHECKPOINT_SOURCE = {
    'repository': 'InstaDeepAI/NTv3_650M_pre',
    'revision': '5e6050bed864a5a8fb32481096bf555495316b31',
}
# The licence text accompanies the software as a notice. Model data and its
# upstream documentation are obtained directly when the model is installed.
CHECKPOINT_PATHS = frozenset('models/ntv3/' + name for name in (
    'model.safetensors', 'config.json', 'tokenizer_config.json', 'special_tokens_map.json',
    'vocab.json', 'README.md',
))
UPSTREAM_PATHS = CODE_PATHS | CHECKPOINT_PATHS
DELIVERY = 'authenticated_upstream'


def code_source(root):
    catalog = Path(root) / 'config/model_delivery.json'
    if not catalog.exists():
        return None
    return catalog_source(json.loads(catalog.read_text(encoding='utf-8')))


def catalog_source(document):
    if 'ntv3_code' not in document:
        return None
    if document.get('schema') != 2 or document['ntv3_code'] != CODE_SOURCE:
        raise ValueError('Invalid pinned NTv3 code source in the candidate catalog')
    return CODE_SOURCE.copy()


def distribution_paths(root, version):
    catalog = Path(root) / 'config/model_delivery.json'
    document = json.loads(catalog.read_text(encoding='utf-8')) if catalog.exists() else {}
    if catalog_source(document):
        if document.get('ntv3') != CHECKPOINT_SOURCE:
            raise ValueError('Invalid pinned NTv3 checkpoint source in the candidate catalog')
        return UPSTREAM_PATHS
    if version.startswith('0.5.'):
        raise ValueError('IncFinder 0.5 packages require the pinned NTv3 code source in config/model_delivery.json')
    return frozenset()


def external_manifest_paths(manifest):
    rows = manifest['files']
    external = [row for row in rows if 'delivery' in row]
    if not external:
        return frozenset()
    names = frozenset(row['path'] for row in external)
    # The earlier internal engineering build recorded only the four Python files.
    if (len(external) != len(names) or names not in (CODE_PATHS, UPSTREAM_PATHS)
            or any(row['delivery'] != DELIVERY for row in external)
            or not any(row['path'] == 'config/model_delivery.json' for row in rows)):
        raise ValueError('Invalid external NTv3 code inventory')
    return names


@contextmanager
def encoder_classes(root, model_dir):
    if code_source(root) is None:
        sys.path.insert(0, str(root / 'vendor'))
        try:
            from ntv3.configuration_ntv3_pretrained import Ntv3PreTrainedConfig
            from ntv3.modeling_ntv3_pretrained import NTv3PreTrained
            from ntv3.tokenization_ntv3 import NTv3Tokenizer
        finally:
            sys.path.remove(str(root / 'vendor'))
        yield Ntv3PreTrainedConfig, NTv3PreTrained, NTv3Tokenizer
        return

    from .resources import checked_child, model_root

    assets = json.loads((root / 'config/assets.json').read_text(encoding='utf-8'))['files']
    rows = [row for row in assets if row['path'] in CODE_PATHS]
    if len(rows) != len(CODE_NAMES) or {row['path'] for row in rows} != CODE_PATHS:
        raise ValueError('The candidate must identify all four NTv3 source files')
    model_directory = model_root(model_dir)
    with tempfile.TemporaryDirectory(prefix='incfinder-verified-encoder-') as temporary:
        private = Path(temporary)
        for row in rows:
            name = Path(row['path']).name
            path = checked_child(model_directory, Path('ntv3') / name)
            with path.open('rb') as stream:
                data = stream.read(row['size_bytes'] + 1)
            if len(data) != row['size_bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']:
                raise ValueError('NTv3 source SHA-256 or size mismatch: ' + name)
            (private / name).write_bytes(data)

        # Only the verified copies are importable within this private package.
        namespace = '_incfinder_encoder_' + uuid.uuid4().hex
        spec = importlib.util.spec_from_file_location(
            namespace, private / '__init__.py', submodule_search_locations=[str(private)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[namespace] = package
        try:
            spec.loader.exec_module(package)
            config = importlib.import_module('.configuration_ntv3_pretrained', namespace)
            model = importlib.import_module('.modeling_ntv3_pretrained', namespace)
            tokenizer = importlib.import_module('.tokenization_ntv3', namespace)
            yield config.Ntv3PreTrainedConfig, model.NTv3PreTrained, tokenizer.NTv3Tokenizer
        finally:
            for name in tuple(sys.modules):
                if name == namespace or name.startswith(namespace + '.'):
                    sys.modules.pop(name, None)

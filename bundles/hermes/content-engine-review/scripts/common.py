"""Portable boundary: stdlib only, except lazy Pillow for real image decoding.

Build copies this single helper into every package. No repository imports,
network, database, model calls, or visual/rights approval are implied.
"""
import hashlib
import io
import ipaddress
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from urllib.parse import urlsplit
import warnings

DIMENSIONS = ('information_density', 'hook_power', 'visual_appeal', 'audience_fit',
              'emotional_resonance', 'social_shareability')
ROLES = ('research', 'write', 'assets', 'review', 'publish')
ITEM_FIELDS = ('id', 'kind', 'path', 'source_url', 'rights_note', 'sha256', 'width',
               'height', 'format', 'dhash', 'template')


class PortableError(ValueError):
    def __init__(self, message, reason_code='invalid_result'):
        super().__init__(message)
        self.reason_code = reason_code


def _require(condition, message):
    if not condition:
        raise PortableError(message)


def _json_value(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        _require(math.isfinite(value), 'JSON numbers must be finite')
    elif type(value) is list:
        for item in value:
            _json_value(item)
    elif type(value) is dict:
        _require(all(type(key) is str for key in value), 'JSON keys must be strings')
        for item in value.values():
            _json_value(item)
    else:
        raise PortableError('Only JSON-native values are allowed')


def canonical(value):
    try:
        _json_value(value)
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                          separators=(',', ':'))
    except (RecursionError, TypeError, OverflowError) as exc:
        raise PortableError('Invalid or overly nested JSON') from exc


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def file_hash(path):
    hasher = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'Duplicate JSON key')
        result[key] = value
    return result


def _constant(value):
    raise PortableError('Non-finite JSON number')


def load_json(path, max_bytes=1000000):
    _require(type(max_bytes) is int and max_bytes > 0, 'Invalid JSON byte limit')
    with Path(path).open('rb') as stream:
        raw = stream.read(max_bytes + 1)
    _require(len(raw) <= max_bytes, 'JSON exceeds byte limit')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=_constant)
        _require(type(value) is dict, 'JSON root must be an object')
        canonical(value)
        return value
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise PortableError('Invalid UTF-8 JSON object') from exc


def atomic_json(path, value):
    raw = canonical(value).encode('utf-8')
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _linked(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return (stat.S_ISLNK(info.st_mode) or
            bool(getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT))


def absolute_root(root):
    root = Path(root)
    _require(root.is_absolute() and '..' not in root.parts, 'Root must be absolute without traversal')
    _require(not str(root).startswith(('\\\\', '//')), 'UNC roots forbidden')
    for component in reversed((root, *root.parents)):
        _require(not _linked(component), 'Symlink/junction paths forbidden')
    _require(root.resolve() == root, 'Root must be normalized')
    return root


def safe_path(root, relative):
    """Resolve canonical relative POSIX paths without following reparse points."""
    root = absolute_root(root)
    _require(type(relative) is str and bool(relative), 'Path must be relative POSIX text')
    _require(not re.search(r'[\\:\x00-\x1f\x7f]', relative), 'Unsafe path character')
    parts = relative.split('/')
    _require(all(part not in ('', '.', '..') for part in parts), 'Path traversal/normalization forbidden')
    _require(not PurePosixPath(relative).is_absolute(), 'Absolute paths forbidden')
    for part in parts:
        _require(not part.endswith((' ', '.')), 'Unsafe Windows path suffix')
        _require(not re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I),
                 'Reserved Windows path name')
    path = root
    for part in parts:
        path = path / part
        _require(not _linked(path), 'Symlink/junction paths forbidden')
    resolved = path.resolve()
    _require(resolved.is_relative_to(root) and resolved == path, 'Path escapes root')
    return resolved


def _object(value, fields, label):
    _require(type(value) is dict and set(value) == set(fields), label + ': exact fields required')
    return value


def _text(value, label):
    _require(type(value) is str and bool(value.strip()) and '\x00' not in value, label + ': nonempty text required')
    return value


def _list(value, label, minimum=1):
    _require(type(value) is list and minimum <= len(value) <= 256, label + ': bounded list required')
    return value


def _number(value, label, low, high):
    _require(type(value) in (int, float) and low <= value <= high and (type(value) is int or math.isfinite(value)),
             label + ': finite number required (not bool)')
    return value


def _boolean(value, label):
    _require(type(value) is bool, label + ': boolean required')
    return value


def _hash(value, label, length=64):
    _require(type(value) is str and re.fullmatch('[0-9a-f]{' + str(length) + '}', value) is not None,
             label + ': lowercase hex hash required')
    return value


def _url(value):
    _text(value, 'URL')
    _require(not re.search(r'[\s\\\x00-\x1f\x7f]', value), 'Unsafe source URL')
    try:
        parts = urlsplit(value)
        _require(parts.scheme in ('http', 'https') and bool(parts.hostname) and
                 parts.username is None and parts.password is None, 'Public HTTP(S) source URL required')
        _ = parts.port
        host = parts.hostname.lower()
        _require(host != 'localhost' and not host.endswith(('.localhost', '.local')), 'Local source URL forbidden')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            _require('.' in host, 'Public source hostname required')
        else:
            _require(address.is_global, 'Private source URL forbidden')
    except ValueError as exc:
        raise PortableError('Invalid source URL') from exc
    return value


def validate_policy(policy):
    _object(policy, ('schema_version', 'scoring', 'runtime', 'source_digest'), 'policy')
    _require(type(policy['schema_version']) is int and policy['schema_version'] == 1, 'Unknown policy version')
    scoring = _object(policy['scoring'], ('weights', 'threshold', 'max_iterations'), 'scoring')
    weights = _object(scoring['weights'], DIMENSIONS, 'weights')
    for key, value in weights.items():
        _number(value, key, 0.000001, 1000000)
    _number(scoring['threshold'], 'threshold', 0, 10)
    _require(type(scoring['max_iterations']) is int and 1 <= scoring['max_iterations'] <= 100,
             'max_iterations must be a positive integer')
    runtime = _object(policy['runtime'], ('research', 'write', 'assets', 'review'), 'runtime')
    for key, value in runtime.items():
        _require(type(value) is int and 1 <= value <= 86400, key + ': positive runtime seconds required')
    _hash(policy['source_digest'], 'policy source digest')
    return json.loads(canonical(policy))


def _needs_input(result):
    _object(result, ('outcome', 'reason_code', 'message'), 'needs_input')
    _require(result['outcome'] == 'needs_input', 'Unknown outcome')
    _require(type(result['reason_code']) is str and re.fullmatch('[a-z][a-z0-9_]{0,63}', result['reason_code']),
             'Invalid reason code')
    _text(result['message'], 'needs_input message')


def _assets_shape(result):
    _object(result, ('items',), 'assets')
    ids, paths = set(), set()
    for item in _list(result['items'], 'items'):
        _object(item, ITEM_FIELDS, 'image')
        identifier = _text(item['id'], 'image id')
        _require(identifier not in ids, 'Duplicate image id')
        ids.add(identifier)
        _require(type(item['kind']) is str and item['kind'] in ('photo', 'card'), 'Image kind must be photo/card')
        path = _text(item['path'], 'image path')
        _require(not re.search(r'[\\:\x00-\x1f\x7f]', path) and
                 all(part not in ('', '.', '..') for part in path.split('/')), 'Unsafe relative image path')
        _require(path not in paths, 'Duplicate image path')
        paths.add(path)
        _url(item['source_url'])
        _text(item['rights_note'], 'rights note')
        _hash(item['sha256'], 'image sha256')
        _hash(item['dhash'], 'image dhash', 16)
        for dimension in ('width', 'height'):
            _require(type(item[dimension]) is int and 1 <= item[dimension] <= 20000,
                     'Image dimensions must be positive integers')
        _require(type(item['format']) is str and item['format'] in ('PNG', 'JPEG', 'WEBP'), 'Unsupported image format')
        _require((item['kind'] == 'photo' and item['template'] is None) or
                 (item['kind'] == 'card' and type(item['template']) is str and
                  item['template'] in ('cover', 'body', 'list')), 'Image template/kind mismatch')


def validate_result(stage, result, policy, platform=None):
    """Detached normalization. Assets need file validation, review frozen coverage.

    Cross-topic evidence, actual source excerpts and version gates belong to apps.
    A needs_input object is never success or an approval.
    """
    _require(stage in ROLES, 'Unknown stage')
    _require(platform in (None, 'xhs', 'mp'), 'Unknown platform')
    policy = validate_policy(policy)
    _require(type(result) is dict, 'Result must be an object')
    canonical(result)
    if 'outcome' in result:
        _needs_input(result)
        return json.loads(canonical(result))
    if stage == 'publish':
        raise PortableError('Publishing is disabled; offline validation only', 'publishing_disabled')
    if stage == 'research':
        _object(result, ('title', 'summary', 'sources', 'evidence'), 'research')
        _text(result['title'], 'title')
        _text(result['summary'], 'summary')
        urls, ids = set(), set()
        for source in _list(result['sources'], 'sources'):
            _object(source, ('url', 'title'), 'source')
            url = _url(source['url'])
            _text(source['title'], 'source title')
            _require(url not in urls, 'Duplicate source URL')
            urls.add(url)
        for evidence in _list(result['evidence'], 'evidence'):
            _object(evidence, ('id', 'claim', 'source_url', 'excerpt'), 'evidence')
            identifier = _text(evidence['id'], 'evidence id')
            _require(identifier not in ids, 'Duplicate evidence id')
            ids.add(identifier)
            for field in ('claim', 'excerpt'):
                _text(evidence[field], field)
            _require(_url(evidence['source_url']) in urls, 'Evidence source not in sources')
    elif stage == 'write':
        _object(result, ('title', 'body', 'source_urls', 'claims', 'humanizer_notes'), 'write')
        for field in ('title', 'body', 'humanizer_notes'):
            _text(result[field], field)
        if platform == 'xhs':
            _require((sum(1 if ord(c) < 128 else 2 for c in result['title']) + 1) // 2 <= 20,
                     'XHS title exceeds 20-unit limit')
        urls = [_url(url) for url in _list(result['source_urls'], 'source_urls')]
        _require(len(set(urls)) == len(urls), 'Duplicate source URL')
        texts = set()
        for claim in _list(result['claims'], 'claims'):
            _object(claim, ('text', 'evidence_ids'), 'claim')
            text = _text(claim['text'], 'claim text')
            _require(text in result['title'] or text in result['body'], 'Claim text absent from draft')
            _require(text not in texts, 'Duplicate claim')
            texts.add(text)
            ids = [_text(e, 'evidence id') for e in _list(claim['evidence_ids'], 'evidence_ids')]
            _require(len(set(ids)) == len(ids), 'Duplicate claim evidence id')
    elif stage == 'assets':
        _assets_shape(result)
    else:
        _object(result, ('decision', 'feedback', 'scores', 'facts', 'images'), 'review')
        _require(type(result['decision']) is str and result['decision'] in ('approved', 'changes_requested'),
                 'Unknown review decision')
        _text(result['feedback'], 'feedback')
        scores = _object(result['scores'], DIMENSIONS, 'scores')
        for key, value in scores.items():
            _number(value, key, 0, 5)
        facts = _object(result['facts'], ('supported', 'notes'), 'facts')
        _boolean(facts['supported'], 'facts supported')
        _text(facts['notes'], 'facts notes')
        ids = set()
        for image in _list(result['images'], 'review images'):
            _object(image, ('id', 'sha256', 'approved', 'observation'), 'review image')
            identifier = _text(image['id'], 'image id')
            _require(identifier not in ids, 'Duplicate review image')
            ids.add(identifier)
            _hash(image['sha256'], 'review sha256')
            _boolean(image['approved'], 'image approved')
            _text(image['observation'], 'image observation')
    normalized = json.loads(canonical(result))
    if stage == 'review':
        weights = policy['scoring']['weights']
        composite = sum(scores[key] * weights[key] for key in DIMENSIONS) / sum(weights.values()) * 2
        normalized['composite'] = composite
        normalized['passed'] = (result['decision'] == 'approved' and facts['supported'] and
                                all(image['approved'] for image in result['images']) and
                                composite >= policy['scoring']['threshold'])
    return normalized


def validate_assets(root, result):
    """Real decode + snapshot SHA/dHash. This does NOT visually approve images."""
    _assets_shape(result)
    root = absolute_root(root)
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise PortableError('Pillow required for image validation', 'dependency_missing') from exc
    trusted, hashes, perceptual = [], set(), []
    for item in result['items']:
        path = safe_path(root, item['path'])
        _require(path.is_file(), 'Image file missing')
        with path.open('rb') as stream:
            raw = stream.read(30 * 1024 * 1024 + 1)
        _require(len(raw) <= 30 * 1024 * 1024, 'Image exceeds byte limit')
        sha = hashlib.sha256(raw).hexdigest()
        _require(sha == item['sha256'], 'Image sha256 mismatch')
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as probe:
                    probe.verify()
                with Image.open(io.BytesIO(raw)) as image:
                    _require(getattr(image, 'n_frames', 1) == 1, 'Animated images forbidden')
                    _require(image.width * image.height <= 40000000, 'Image pixel budget exceeded')
                    image.load()
                    _require((image.width, image.height, image.format) ==
                             (item['width'], item['height'], item['format']), 'Decoded metadata mismatch')
                    pixels = list(ImageOps.exif_transpose(image).convert('L').resize((9, 8), Image.Resampling.LANCZOS).getdata())
                    number = 0
                    for y in range(8):
                        for x in range(8):
                            number = (number << 1) | int(pixels[y * 9 + x] > pixels[y * 9 + x + 1])
        except (OSError, SyntaxError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise PortableError('Image decode/metadata validation failed') from exc
        _require(f'{number:016x}' == item['dhash'], 'Image dhash mismatch')
        _require(sha not in hashes, 'Duplicate image sha256')
        # Template siblings may share layouts while carrying different text.
        # Byte-identical files are always rejected; perceptual dedup only photos.
        if item['kind'] == 'photo':
            _require(all((number ^ previous).bit_count() > 5 for previous in perceptual),
                     'Near-duplicate photo dhash')
            perceptual.append(number)
        hashes.add(sha)
        trusted.append(json.loads(canonical(item)))
    return {'items': trusted}


def validate_review(result, assets, policy):
    """Require exact id+sha coverage of the frozen image group."""
    normalized = validate_result('review', result, policy)
    if normalized.get('outcome') == 'needs_input':
        return normalized
    _assets_shape(assets)
    expected = {item['id']: item['sha256'] for item in assets['items']}
    actual = {item['id']: item['sha256'] for item in normalized['images']}
    _require(actual == expected, 'Review must cover every frozen image id and sha256 exactly')
    return normalized

"""Offline portable Skill builder. Explicit copy targets only; no installations
outside the supplied project, links, services, model or permission changes.
"""
import argparse
import ast
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import sys
import uuid

sys.dont_write_bytecode = True
TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parents[1]
SOURCE = REPO / 'skills' / 'content-engine'
ROLES = ('research', 'write', 'assets', 'review', 'publish')
HOSTS = ('codex', 'claude', 'hermes')
DESCRIPTIONS = {
    'research': 'Research original sources and traceable evidence.',
    'write': 'Write evidence-grounded content and humanize drafts.',
    'assets': 'Source photos and render structured card templates.',
    'review': 'Independently review facts, quality, and actual images.',
    'publish': 'Validate inputs offline; live publishing is disabled.',
}
_spec = importlib.util.spec_from_file_location('_portable_build_common', TOOLS / 'common.py')
common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(common)
MANAGED_BY = 'content-engine-portable-skills'
EXTERNAL = {'PIL': 'Pillow', 'markdown': 'Markdown', 'jinja2': 'Jinja2',
            'yaml': 'PyYAML', 'playwright': 'playwright'}
MAX_FILES = 512
MAX_BYTES = 2 * 1024 * 1024


def _raw(root, relative):
    path = common.safe_path(root, relative)
    common._require(path.is_file(), 'Missing package dependency: ' + relative)
    with path.open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    common._require(len(raw) <= MAX_BYTES, 'Package file exceeds byte limit: ' + relative)
    return raw


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _policy():
    """Read only the supported numeric subset of authoritative YAML sections.

    No fallback constants. Ambiguous/unsupported policy forms fail closed rather
    than silently taking a default. Other domain YAML is deliberately irrelevant.
    """
    raw = _raw(REPO, 'config/topics.yaml')
    text = raw.decode('utf-8')

    def section(name):
        matches = re.findall(r'^' + name + r':[^\n]*\n(.*?)(?=^[^\s#]|\Z)', text, re.M | re.S)
        common._require(len(matches) == 1, 'Exactly one YAML ' + name + ' section required')
        return matches[0]

    def numeric(line):
        token = line.split('#', 1)[0].strip()
        common._require(re.fullmatch(r'\d+(?:\.\d+)?', token), 'Policy requires literal positive numeric scalars')
        return float(token) if '.' in token else int(token)

    scoring = section('scoring')
    weights = {}
    key = None
    for line in scoring.splitlines():
        match = re.fullmatch(r'    - key: ([a-z_]+)\s*(?:#.*)?', line)
        if match:
            common._require(key is None or key in weights, 'Missing dimension weight')
            key = match[1]
            common._require(key not in weights, 'Duplicate scoring dimension')
        elif line.startswith('      weight:'):
            common._require(key is not None and key not in weights, 'Unbound/duplicate dimension weight')
            weights[key] = numeric(line.split(':', 1)[1])
    common._require(key is not None and key in weights, 'Missing dimension weight')

    def scalar(block, name):
        values = re.findall(r'^  ' + name + r':([^\n]*)$', block, re.M)
        common._require(len(values) == 1, 'Missing/duplicate policy field: ' + name)
        return numeric(values[0])

    runtime = section('runtime')
    policy = {'schema_version': 1, 'scoring': {'weights': weights,
              'threshold': scalar(scoring, 'threshold'),
              'max_iterations': scalar(scoring, 'max_iterations')},
              'runtime': {'research': scalar(runtime, 'writer'), 'write': scalar(runtime, 'writer'),
                          'assets': scalar(runtime, 'assets'), 'review': scalar(runtime, 'review')},
              'source_digest': _sha(raw)}
    return common.validate_policy(policy)


def _dependencies(files):
    dependencies = set()
    modules = {Path(name).stem for name in files if name.endswith('.py')}
    packages = {name.split('/')[0] for name in files if '/' in name}
    for name, raw in files.items():
        if not name.endswith('.py'):
            continue
        try:
            tree = ast.parse(raw.decode('utf-8'), filename=name)
        except (SyntaxError, UnicodeError) as exc:
            raise common.PortableError('Invalid Python source: ' + name) from exc
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                common._require(node.level == 0, 'Relative package imports are forbidden: ' + name)
                imports = [node.module or '']
            else:
                continue
            for module in imports:
                top = module.split('.')[0]
                common._require(top not in {'scripts', 'skills', 'apps'} and
                                not top.startswith('content_engine'), 'Repository/neighbor imports forbidden: ' + name)
                if top in sys.stdlib_module_names or top == '__future__':
                    continue
                if top in modules or top in packages:
                    continue
                common._require(top in EXTERNAL, 'Unknown external dependency: ' + module)
                dependencies.add(EXTERNAL[top])
    return sorted(dependencies)


def _collect(source=None, host='codex'):
    common._require(host in HOSTS, 'Unsupported Skill host')
    source = common.absolute_root(Path(source) if source is not None else SOURCE)
    common._require(source.is_dir(), 'Skill source directory missing')
    entries = list(source.iterdir())
    common._require({p.name for p in entries} == set(ROLES) and all(p.is_dir() for p in entries),
                    'Source must contain exactly five role directories')
    policy = _policy()
    helper = _raw(TOOLS, 'common.py')
    packet = _raw(TOOLS, 'packet.py')
    schemas = _raw(TOOLS, 'result-schemas.json')
    rubric = _raw(REPO, 'references/scoring-rubric.md')
    packages = {}
    for role in ROLES:
        root = common.safe_path(source, role)
        files = {}
        paths = list(root.rglob('*'))
        common._require(len(paths) <= MAX_FILES, 'Too many source files')
        for path in paths:
            relative = path.relative_to(root).as_posix()
            common.safe_path(root, relative)
            if '__pycache__' in path.parts or path.suffix in ('.pyc', '.pyo'):
                continue
            if path.is_dir():
                continue
            common._require(path.name not in ('manifest.json', 'policy.json', 'common.py', 'packet.py', 'result-schemas.json', 'runtime.json'),
                            'Generated package files cannot be source overrides')
            files[relative] = _raw(root, relative)
        common._require('SKILL.md' in files and 'scripts/cli.py' in files, role + ': SKILL.md and CLI required')
        text = files['SKILL.md'].decode('utf-8')
        common._require(re.match(r'^---\r?\nname: content-engine-' + role +
                                 r'\r?\ndescription: [^\r\n]+\r?\n---', text),
                        role + ': standard name+description frontmatter required')
        common._require('asset_ids' not in text or role != 'assets', 'Legacy assets contract is not portable')
        normalized_paths = text.replace(chr(92), '/')
        common._require(not re.search(r'(?<![A-Za-z])[A-Za-z]:/(?!/)|/d/selfmedia|scripts\.utils|scripts/scorer',
                                      normalized_paths),
                        'Repository paths/imports forbidden in Skill')
        common._require('CLAUDE_SKILL_DIR' not in text and '{{SKILL_DIR}}' not in text,
                        'Host-specific Skill directory variables forbidden')
        for match in re.finditer(r'\bpython(?:3)?\s+([^\n]+)', text):
            common._require(match[1].startswith('"<本技能目录>/scripts/cli.py"'),
                            'Python command must be package-local')
        files['runtime.json'] = common.canonical({'schema_version': 1,
            'python_minimum': '3.11', 'interpreter': 'host-configured'}).encode('utf-8')
        files['scripts/packet.py'] = packet
        if host == 'codex':
            common._require('agents/openai.yaml' not in files, 'Codex metadata is generated')
            files['agents/openai.yaml'] = common.canonical({'interface': {
                'display_name': 'content-engine-' + role,
                'short_description': DESCRIPTIONS[role]}}).encode('utf-8')
        files['scripts/common.py'] = helper
        files['result-schemas.json'] = schemas
        files['policy.json'] = common.canonical(policy).encode('utf-8')
        if role == 'review':
            asset_root = common.safe_path(source, 'assets')
            for path in asset_root.rglob('*'):
                name = path.relative_to(asset_root).as_posix()
                common.safe_path(asset_root, name)
                if not path.is_file() or not (name == 'scripts/asset_tools.py' or name == 'requirements.txt' or name.startswith('cards/')):
                    continue
                raw = _raw(asset_root, name)
                common._require(name not in files, 'Review copied dependency conflicts: ' + name)
                files[name] = raw
            existing = files.get('references/scoring-rubric.md')
            common._require(existing is None or existing == rubric, 'Review rubric must match authority')
            files['references/scoring-rubric.md'] = rubric
        dependencies = _dependencies(files)
        files_hashes = {name: _sha(raw) for name, raw in sorted(files.items())}
        identity = {'role': role, 'host': host, 'files': files_hashes, 'dependencies': dependencies, 'policy': 'policy.json'}
        manifest = {'schema_version': 1, 'managed_by': MANAGED_BY,
                    'build_id': common.digest(identity), **identity}
        files['manifest.json'] = common.canonical(manifest).encode('utf-8')
        packages[role] = (files, manifest)
    return source, packages, policy


def check(source=None, host='codex'):
    """Read-only source and dependency validation. No runtime acceptance implied."""
    try:
        source, packages, policy = _collect(source, host)
        return {'ok': True, 'source': str(source), 'roles': list(ROLES), 'policy': policy,
                'manifests': {role: manifest for role, (_, manifest) in packages.items()}}
    except (ValueError, OSError) as exc:
        return {'ok': False, 'reason_code': getattr(exc, 'reason_code', 'invalid_bundle'), 'message': str(exc)}


def _managed(root, role):
    common.absolute_root(root)
    common._require(root.is_dir(), 'Existing role target is not a directory')
    manifest_path = common.safe_path(root, 'manifest.json')
    common._require(manifest_path.is_file(), 'Unmanaged target: package manifest is missing')
    manifest = common.load_json(manifest_path)
    fields = ('schema_version', 'managed_by', 'build_id', 'role', 'files', 'dependencies', 'policy')
    common._object(manifest, fields + (('host',) if 'host' in manifest else ()), 'package manifest')
    if 'host' in manifest:
        common._require(manifest['host'] in HOSTS, 'Invalid manifest host')
    common._require(type(manifest['schema_version']) is int and manifest['schema_version'] == 1 and
                    manifest['managed_by'] == MANAGED_BY and manifest['role'] == role and
                    manifest['policy'] == 'policy.json', 'Unmanaged/incompatible target')
    common._require(type(manifest['files']) is dict and bool(manifest['files']), 'Manifest files required')
    common._require(type(manifest['dependencies']) is list and
                    all(type(d) is str for d in manifest['dependencies']), 'Invalid dependencies')
    identity = {name: manifest[name] for name in ('role', 'files', 'dependencies', 'policy')}
    if 'host' in manifest:
        identity['host'] = manifest['host']
    common._require(common.digest(identity) == manifest['build_id'], 'Modified package manifest')
    actual = set()
    expected_directories = {parent.as_posix() for name in manifest['files']
                            for parent in Path(name).parents if parent != Path('.')}
    for path in root.rglob('*'):
        name = path.relative_to(root).as_posix()
        common.safe_path(root, name)
        if path.is_dir():
            common._require(name in expected_directories, 'User-added package directory blocks upgrade')
        if path.is_file():
            actual.add(name)
    common._require(actual == set(manifest['files']) | {'manifest.json'}, 'User-added/removed package files block upgrade')
    for name, expected in manifest['files'].items():
        common._hash(expected, 'manifest file hash')
        common._require(common.file_hash(common.safe_path(root, name)) == expected,
                        'User-modified package file blocks upgrade: ' + role + '/' + name)
    return manifest


def build(target, source=None, host='codex'):
    """Build five content-engine-* directories under explicit absolute target.

    Hash-check ALL old packages before writes. Existing unrelated targets are
    never overwritten. Upgrade untouched managed packages under a local lock,
    staging on the same volume and rolling back failures. No symlinks used.
    """
    target = common.absolute_root(target)
    source, packages, policy = _collect(source, host)
    for protected in (source, TOOLS, REPO / 'config', REPO / 'references'):
        common._require(not target.is_relative_to(protected) and not protected.is_relative_to(target),
                        'Output root must be separate from source inputs')
    common._require(not target.exists() or target.is_dir(), 'Output root must be a directory')
    # First full preflight before creating even a target lock.
    previous = {}
    for role in ROLES:
        root = common.safe_path(target, 'content-engine-' + role)
        if root.exists():
            previous[role] = _managed(root, role)
    target.mkdir(parents=True, exist_ok=True)
    lock = common.safe_path(target, '.content-engine-install.lock')
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise common.PortableError('Another build owns the target lock', 'build_locked') from exc
    os.close(fd)
    staging = None
    changed = []
    try:
        # Close races between initial preflight and lock; check all again.
        for role in ROLES:
            root = common.safe_path(target, 'content-engine-' + role)
            if root.exists():
                common._require(role in previous and _managed(root, role) == previous[role], 'Target changed during preflight')
            else:
                common._require(role not in previous, 'Target disappeared during preflight')
        candidate = target / ('.content-engine-stage-' + uuid.uuid4().hex[:8])
        candidate.mkdir()
        staging = candidate
        for role, (files, _) in packages.items():
            root = staging / ('new-' + role)
            root.mkdir()
            for name, raw in files.items():
                path = common.safe_path(root, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('xb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            _managed(root, role)
        for role in ROLES:
            destination = common.safe_path(target, 'content-engine-' + role)
            backup = staging / ('old-' + role)
            # Recheck just before each replacement; do not clobber concurrent edits.
            if role in previous:
                common._require(_managed(destination, role) == previous[role], 'Target changed before upgrade')
                os.replace(destination, backup)
            else:
                common._require(not destination.exists(), 'Target appeared before install')
            changed.append((destination, backup))
            os.replace(staging / ('new-' + role), destination)
        return {'ok': True, 'host': host, 'target': str(target), 'roles': list(ROLES), 'policy': policy,
                'packages': {role: str(target / ('content-engine-' + role)) for role in ROLES},
                'manifests': {role: manifest for role, (_, manifest) in packages.items()}}
    except BaseException:
        for destination, backup in reversed(changed):
            if destination.exists():
                shutil.rmtree(destination)
            if backup.exists():
                os.replace(backup, destination)
        raise
    finally:
        try:
            if staging is not None and staging.exists():
                shutil.rmtree(staging)
        finally:
            lock.unlink()


def install(project, host='codex', hermes_home=None):
    project = common.absolute_root(project)
    common._require(project.is_dir(), 'Explicit project directory must exist')
    common._require(host in HOSTS, 'Unsupported Skill host')
    if host == 'hermes':
        common._require(hermes_home is not None, 'Hermes requires explicit hermes_home')
        home = common.absolute_root(hermes_home)
        common._require(home.is_dir(), 'Explicit Hermes home must exist')
        target = common.safe_path(home, 'skills')
    else:
        common._require(hermes_home is None, 'hermes_home only applies to Hermes')
        target = common.safe_path(project, '.agents/skills' if host == 'codex' else '.claude/skills')
    return build(target, host=host)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise common.PortableError(message, 'invalid_arguments')


def main(argv=None):
    parser = _Parser(description='Offline portable content-engine Skill packages')
    commands = parser.add_subparsers(dest='command', required=True)
    checker = commands.add_parser('check')
    checker.add_argument('--source')
    checker.add_argument('--host', choices=HOSTS, default='codex')
    builder = commands.add_parser('build')
    builder.add_argument('--target', required=True)
    builder.add_argument('--source')
    builder.add_argument('--host', choices=HOSTS, default='codex')
    installer = commands.add_parser('install')
    installer.add_argument('--project', required=True)
    installer.add_argument('--host', choices=HOSTS, default='codex')
    installer.add_argument('--hermes-home')
    try:
        args = parser.parse_args(argv)
        if args.command == 'check':
            report = check(args.source, args.host)
        elif args.command == 'build':
            report = build(args.target, args.source, args.host)
        else:
            report = install(args.project, args.host, args.hermes_home)
    except (ValueError, OSError) as exc:
        report = {'ok': False, 'reason_code': getattr(exc, 'reason_code', 'build_failed'), 'message': str(exc)}
    print(common.canonical(report))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

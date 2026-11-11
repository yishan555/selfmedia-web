"""Portable copy/JSON/path/image boundary tests; no models, services or installs."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from harness_support import bootstrap
RUN_DIR = bootstrap(__file__)
ROOT = Path(__file__).resolve().parents[1]
from PIL import Image


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


BUILDER = module(ROOT / 'tools/skill-packager/build.py', '_test_portable_builder')
COMMON = BUILDER.common
URL = 'https://example.org/original'
# Fixture only tests packaging/CLI ABI; final real packet check belongs to core.
PACKET_FIXTURE = '''
from pathlib import Path
class PacketStore:
    def __init__(self, workspace, skill_dir):
        if not Path(workspace).is_absolute():
            raise ValueError('Workspace must be absolute')
        self.workspace = str(workspace)
        self.skill_dir = str(skill_dir)
    def prepare(self, role, date=None, owner='', host='codex', content_id=None, input_id=None):
        return dict(ok=True, command='prepare', role=role, date=date, owner=owner,
                    host=host, content_id=content_id, input_id=input_id,
                    workspace=self.workspace, skill_dir=self.skill_dir, state='idle')
    def submit(self, role, attempt, token):
        return dict(ok=True, command='submit', role=role, attempt=attempt, token=token)
    def status(self, attempt=None):
        return dict(ok=True, command='status', attempt=attempt)
    def interrupt(self, attempt, token, owner_stopped=False):
        return dict(ok=True, command='interrupt', attempt=attempt, token=token,
                    owner_stopped=owner_stopped)
'''


def research():
    return {'title': '主题', 'summary': '有出处的摘要', 'sources': [{'url': URL, 'title': 'Source'}],
            'evidence': [{'id': 'e1', 'claim': '事实原句', 'source_url': URL, 'excerpt': '支撑原文'}]}


def draft():
    return {'title': '标题', 'body': '事实原句。\n\n## 解读\n保留限定。', 'source_urls': [URL],
            'claims': [{'text': '事实原句', 'evidence_ids': ['e1']}], 'humanizer_notes': '去掉机械套话'}


def review(item, score=4):
    return {'decision': 'approved', 'feedback': '证据与实际图片已核对',
            'scores': {key: score for key in COMMON.DIMENSIONS},
            'facts': {'supported': True, 'notes': '逐句核对来源'},
            'images': [{'id': item['id'], 'sha256': item['sha256'], 'approved': True,
                        'observation': '实际看到蓝色图像，此为机械测试不代表真实视觉验收'}]}


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.root = RUN_DIR / self._testMethodName
        self.root.mkdir()
        self.policy = BUILDER._policy()
        self.original_tools = BUILDER.TOOLS
        self.tools = self.root / 'fixture-tools'
        shutil.copytree(self.original_tools, self.tools, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        BUILDER.TOOLS = self.tools
        self.addCleanup(setattr, BUILDER, 'TOOLS', self.original_tools)

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return path

    def item(self, name='photo.png', color=(40, 80, 160), identifier='a1'):
        path = self.root / name
        Image.new('RGB', (300, 220), color).save(path)
        return {'id': identifier, 'kind': 'photo', 'path': name, 'source_url': URL,
                'rights_note': 'Synthetic test; no publication rights', 'sha256': COMMON.file_hash(path),
                'width': 300, 'height': 220, 'format': 'PNG', 'dhash': '0000000000000000', 'template': None}

    def build(self):
        target = self.root / 'built'
        return target, BUILDER.build(target)

    def cli(self, target, role, *args):
        result = subprocess.run([sys.executable, str(target / ('content-engine-' + role) / 'scripts/cli.py'),
                                 *map(str, args)], capture_output=True, text=True, encoding='utf-8', cwd=self.root)
        self.assertTrue(result.stdout.strip(), (role, result.returncode, result.stderr))
        output = json.loads(result.stdout)
        self.assertIs(type(output.get('ok')), bool, result.stdout)
        return result, output

    def source(self):
        source = self.root / 'source'
        shutil.copytree(BUILDER.SOURCE, source, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        return source

    def test_01_check_offline_sources(self):
        report = BUILDER.check()
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['roles'], list(BUILDER.ROLES))
        self.assertFalse((self.root / 'built').exists())

    def test_review_rubric_is_self_contained(self):
        target, report = self.build()
        rubric = (target / 'content-engine-review/references/scoring-rubric.md').read_text(encoding='utf-8')
        self.assertNotIn('skills/selfmedia/', rubric)
        self.assertEqual(rubric, (ROOT / 'references/scoring-rubric.md').read_text(encoding='utf-8'))

    def test_02_policy_comes_from_authority(self):
        import yaml
        authority = yaml.safe_load((ROOT / 'config/topics.yaml').read_text(encoding='utf-8'))
        self.assertEqual(self.policy['scoring']['weights'], {d['key']: d['weight'] for d in authority['scoring']['dimensions']})
        for field in ('threshold', 'max_iterations'):
            self.assertEqual(self.policy['scoring'][field], authority['scoring'][field])
        self.assertEqual(self.policy['runtime'], {'research': authority['runtime']['writer'],
            'write': authority['runtime']['writer'], 'assets': authority['runtime']['assets'],
            'review': authority['runtime']['review']})
        self.assertEqual(self.policy['source_digest'], COMMON.file_hash(ROOT / 'config/topics.yaml'))

    def test_03_build_contains_hashes_policy_helper_rubric(self):
        target, report = self.build()
        for role in BUILDER.ROLES:
            root = target / ('content-engine-' + role)
            manifest = COMMON.load_json(root / 'manifest.json')
            self.assertEqual(manifest['role'], role)
            self.assertEqual(manifest, report['manifests'][role])
            self.assertEqual(COMMON.load_json(root / 'policy.json'), self.policy)
            self.assertEqual(COMMON.file_hash(root / 'scripts/common.py'), COMMON.file_hash(BUILDER.TOOLS / 'common.py'))
            self.assertEqual(COMMON.load_json(root / 'runtime.json'),
                {'schema_version': 1, 'python_minimum': '3.11', 'interpreter': 'host-configured'})
            self.assertEqual(COMMON.file_hash(root / 'scripts/packet.py'), COMMON.file_hash(self.tools / 'packet.py'))
            for name, expected in manifest['files'].items():
                self.assertEqual(COMMON.file_hash(root / name), expected)
            text = (root / 'SKILL.md').read_text(encoding='utf-8')
            self.assertNotIn('{{SKILL_DIR}}', text)
            self.assertNotIn('CLAUDE_SKILL_DIR', text)
            self.assertIn('<本技能目录>/scripts/cli.py', text)
        self.assertEqual((target / 'content-engine-review/references/scoring-rubric.md').read_bytes(),
                         (ROOT / 'references/scoring-rubric.md').read_bytes())
        self.assertIn('playwright', report['manifests']['assets']['dependencies'])

    def test_04_install_only_explicit_project(self):
        project = self.root / 'project'
        project.mkdir()
        report = BUILDER.install(project)
        self.assertEqual(Path(report['target']), project / '.agents/skills')
        self.assertFalse((Path(os.environ['HERMES_HOME']) / 'skills').exists())
        with self.assertRaises(ValueError):
            BUILDER.install('relative')
        with self.assertRaises(ValueError):
            BUILDER.install(self.root / 'missing')

    def test_05_detached_full_source_runs_every_cli(self):
        # Copy ALL build inputs, load the copied builder, then remove that entire
        # input tree. Installed package execution cannot import the original source.
        snapshot = self.root / 'input-repo'
        shutil.copytree(BUILDER.SOURCE, snapshot / 'skills/content-engine')
        shutil.copytree(BUILDER.TOOLS, snapshot / 'tools/skill-packager')
        (snapshot / 'config').mkdir()
        shutil.copy2(ROOT / 'config/topics.yaml', snapshot / 'config/topics.yaml')
        (snapshot / 'references').mkdir()
        shutil.copy2(ROOT / 'references/scoring-rubric.md', snapshot / 'references/scoring-rubric.md')
        detached = module(snapshot / 'tools/skill-packager/build.py', '_detached_portable_builder')
        target = self.root / 'relocated packages'
        detached.build(target)
        snapshot.rename(self.root / 'input-moved-away')
        rfile = self.write('research.json', research())
        dfile = self.write('draft.json', draft())
        item = self.item()
        afile = self.write('assets.json', {'items': [item]})
        vfile = self.write('review.json', review(item))
        for role, args in [('research', ['--file', rfile]), ('write', ['--file', dfile, '--platform', 'xhs']),
                           ('assets', ['--file', afile, '--workspace', self.root]),
                           ('review', ['--file', vfile, '--assets', afile]), ('publish', ['--file', dfile])]:
            proc, report = self.cli(target, role, 'validate', *args)
            self.assertEqual(proc.returncode, 0, (role, report, proc.stderr))
        proc, report = self.cli(target, 'write', 'preview-mp', '--file', dfile)
        self.assertEqual(proc.returncode, 0, (report, proc.stderr))
        self.assertIn('<section', report['preview']['html'])

    def test_06_unmanaged_target_never_overwritten(self):
        target = self.root / 'built'
        occupied = target / 'content-engine-review'
        occupied.mkdir(parents=True)
        marker = occupied / 'user.txt'
        marker.write_text('keep', encoding='utf-8')
        with self.assertRaises(ValueError):
            BUILDER.build(target)
        self.assertEqual(marker.read_text(), 'keep')
        self.assertFalse((target / 'content-engine-research').exists())

    def test_07_untouched_managed_upgrade(self):
        target, original = self.build()
        source = self.source()
        with (source / 'research/SKILL.md').open('a', encoding='utf-8') as stream:
            stream.write('\n新增来源限定说明。\n')
        updated = BUILDER.build(target, source)
        self.assertNotEqual(original['manifests']['research']['build_id'], updated['manifests']['research']['build_id'])
        self.assertIn('新增来源', (target / 'content-engine-research/SKILL.md').read_text(encoding='utf-8'))

    def test_08_hash_all_before_any_upgrade(self):
        target, original = self.build()
        first = target / 'content-engine-research/SKILL.md'
        before = first.read_bytes()
        source = self.source()
        with (source / 'research/SKILL.md').open('a', encoding='utf-8') as stream:
            stream.write('\nnew source\n')
        user = target / 'content-engine-publish/scripts/cli.py'
        user.write_text('# user change', encoding='utf-8')
        with self.assertRaises(ValueError):
            BUILDER.build(target, source)
        self.assertEqual(first.read_bytes(), before)
        self.assertEqual(user.read_text(), '# user change')
        self.assertFalse((target / '.content-engine-install.lock').exists())

    def test_09_user_added_removed_files_block(self):
        target, _ = self.build()
        root = target / 'content-engine-write'
        user = root / 'user.txt'
        user.write_text('user', encoding='utf-8')
        with self.assertRaises(ValueError):
            BUILDER.build(target)
        user.unlink()
        user_dir = root / 'user-empty-dir'
        user_dir.mkdir()
        with self.assertRaises(ValueError):
            BUILDER.build(target)
        user_dir.rmdir()
        (root / 'policy.json').unlink()
        with self.assertRaises(ValueError):
            BUILDER.build(target)

    def test_10_modified_manifest_blocks(self):
        target, _ = self.build()
        path = target / 'content-engine-write/manifest.json'
        value = COMMON.load_json(path)
        value['dependencies'].append('untrusted')
        COMMON.atomic_json(path, value)
        with self.assertRaises(ValueError):
            BUILDER.build(target)

    def test_11_invalid_roots_source_overlap(self):
        for target in ['relative', ROOT, BUILDER.SOURCE, BUILDER.SOURCE / 'output', self.root / '..' / 'bad']:
            with self.subTest(target=target), self.assertRaises(ValueError):
                BUILDER.build(target)
        target = self.root / 'file'
        target.write_text('not a directory', encoding='utf-8')
        with self.assertRaises(ValueError):
            BUILDER.build(target)

    def test_12_source_contract_and_cross_repo_imports(self):
        source = self.source()
        cli = source / 'research/scripts/cli.py'
        for code in ('import scripts.utils', 'from scripts import db', 'import content_engine_assets', 'import unknown_dependency'):
            cli.write_text(code, encoding='utf-8')
            self.assertFalse(BUILDER.check(source)['ok'], code)
        cli.unlink()
        self.assertFalse(BUILDER.check(source)['ok'])

    def test_13_source_generated_files_forbidden(self):
        source = self.source()
        (source / 'research/policy.json').write_text('{}', encoding='utf-8')
        self.assertFalse(BUILDER.check(source)['ok'])

    def test_14_build_lock_refuses(self):
        target, _ = self.build()
        (target / '.content-engine-install.lock').write_text('owned', encoding='utf-8')
        with self.assertRaises(ValueError) as caught:
            BUILDER.build(target)
        self.assertEqual(caught.exception.reason_code, 'build_locked')

    def test_cleanup_failure_releases_owned_build_lock(self):
        target = self.root / 'cleanup-failure'
        with patch.object(BUILDER.shutil, 'rmtree', side_effect=PermissionError('cleanup denied')):
            with self.assertRaises(PermissionError):
                BUILDER.build(target)
        self.assertFalse((target / '.content-engine-install.lock').exists())
        self.assertTrue(BUILDER.build(target)['ok'])

    def test_staging_uses_normal_directory_inheritance(self):
        target = self.root / 'inherited-staging'
        original = Path.mkdir
        modes = []
        def record(path, mode=0o777, parents=False, exist_ok=False):
            if path.name.startswith('.content-engine-stage-'):
                modes.append(mode)
            return original(path, mode=mode, parents=parents, exist_ok=exist_ok)
        with patch.object(Path, 'mkdir', record):
            self.assertTrue(BUILDER.build(target)['ok'])
        self.assertEqual(modes, [0o777])

    def test_15_upgrade_failure_rolls_back(self):
        target, report = self.build()
        original = os.replace
        def fail(source, destination):
            if Path(source).name == 'new-write':
                raise OSError('synthetic replace failure')
            return original(source, destination)
        with patch.object(BUILDER.os, 'replace', side_effect=fail):
            with self.assertRaises(OSError):
                BUILDER.build(target)
        for role in BUILDER.ROLES:
            self.assertEqual(COMMON.load_json(target / ('content-engine-' + role) / 'manifest.json'), report['manifests'][role])
        self.assertEqual({p.name for p in target.iterdir()}, {'content-engine-' + role for role in BUILDER.ROLES})

    def test_16_canonical_digest_strict_finite(self):
        self.assertEqual(COMMON.canonical({'b': 1, 'a': '汉字'}), '{"a":"汉字","b":1}')
        self.assertEqual(COMMON.digest({'a': 1, 'b': 2}), COMMON.digest({'b': 2, 'a': 1}))
        for value in [float('nan'), float('inf'), {1: 'bad'}, ('not', 'json'), {'v': object()}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                COMMON.canonical(value)

    def test_17_json_duplicate_nan_nonobject_bounds(self):
        path = self.root / 'invalid.json'
        for raw in ['{"x":1,"x":2}', '{"v":NaN}', '{"v":Infinity}', '[]', 'null', '{"v":1e999}', '{"x":']:
            path.write_text(raw, encoding='utf-8')
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                COMMON.load_json(path)
        path.write_text('{"valid":true}', encoding='utf-8')
        with self.assertRaises(ValueError):
            COMMON.load_json(path, max_bytes=3)
        path.write_bytes(b'\xff')
        with self.assertRaises(ValueError):
            COMMON.load_json(path)

    def test_18_atomic_json_old_survives_failure(self):
        path = self.write('value.json', {'old': True})
        with patch.object(COMMON.os, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                COMMON.atomic_json(path, {'new': True})
        self.assertEqual(COMMON.load_json(path), {'old': True})
        self.assertEqual(set(self.root.iterdir()), {path, self.tools})
        COMMON.atomic_json(path, {'new': True})
        self.assertEqual(COMMON.load_json(path), {'new': True})

    def test_19_safe_path_rejects_windows_and_posix_escapes(self):
        for relative in ['/absolute', '../escape', 'a/../escape', 'a//b', './x', 'a\\b', 'C:/x',
                         '//server/share', '\\\\server\\share', 'a:x', 'a/', '', 'x\x00y', 'NUL.png', 'x.']:
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                COMMON.safe_path(self.root, relative)
        self.assertEqual(COMMON.safe_path(self.root, 'valid/sub.json'), self.root / 'valid/sub.json')

    def test_20_reparse_path_rejected(self):
        # Windows CI can lack symlink privilege; inject the actual reparse flag
        # and independently try real symlinks if this account supports them.
        target = self.root / 'linked'
        original = Path.lstat
        def fake(path, *args, **kwargs):
            if path == target:
                class Info:
                    st_mode = 0
                    st_file_attributes = 1024
                return Info()
            return original(path, *args, **kwargs)
        with patch.object(Path, 'lstat', fake), self.assertRaises(ValueError):
            COMMON.safe_path(self.root, 'linked/x')
        outside = self.root / 'outside'
        outside.mkdir()
        try:
            target.symlink_to(outside, target_is_directory=True)
        except OSError:
            return
        with self.assertRaises(ValueError):
            COMMON.safe_path(self.root, 'linked/x')

    def test_21_research_detached_and_evidence_mapping(self):
        raw = research()
        normalized = COMMON.validate_result('research', raw, self.policy)
        normalized['evidence'][0]['claim'] = 'changed'
        self.assertEqual(raw['evidence'][0]['claim'], '事实原句')
        raw['evidence'][0]['source_url'] = 'https://example.org/other'
        with self.assertRaises(ValueError):
            COMMON.validate_result('research', raw, self.policy)

    def test_22_unknown_fields_all_stages(self):
        item = self.item()
        for stage, raw in [('research', research()), ('write', draft()), ('assets', {'items': [item]}), ('review', review(item))]:
            raw['unknown'] = True
            with self.subTest(stage=stage), self.assertRaises(ValueError):
                COMMON.validate_result(stage, raw, self.policy)
        raw = research()
        raw['sources'][0]['unknown'] = True
        with self.assertRaises(ValueError):
            COMMON.validate_result('research', raw, self.policy)

    def test_23_source_url_invalid_private_credentials(self):
        for url in ['file:///private', 'http://localhost/x', 'http://127.0.0.1/x', 'http://10.1.2.3/x',
                    'http://[::1]/x', 'https://user:secret@example.org/x', 'javascript:alert(1)',
                    'https://example.org/a b', 'https://example.org:bad/x']:
            raw = research()
            raw['sources'][0]['url'] = raw['evidence'][0]['source_url'] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                COMMON.validate_result('research', raw, self.policy)

    def test_24_duplicate_sources_evidence_rejected(self):
        for field in ('sources', 'evidence'):
            raw = research()
            raw[field].append(copy.deepcopy(raw[field][0]))
            with self.assertRaises(ValueError):
                COMMON.validate_result('research', raw, self.policy)

    def test_25_claims_presence_and_ids(self):
        for change in ({'text': 'not in draft'}, {'evidence_ids': []}, {'evidence_ids': ['e1', 'e1']}):
            raw = draft()
            raw['claims'][0].update(change)
            with self.assertRaises(ValueError):
                COMMON.validate_result('write', raw, self.policy)

    def test_26_xhs_title_measurement(self):
        raw = draft()
        for title in ('a' * 40, '汉' * 20):
            raw['title'] = title
            COMMON.validate_result('write', raw, self.policy, 'xhs')
        for title in ('a' * 41, '汉' * 21):
            raw['title'] = title
            with self.assertRaises(ValueError):
                COMMON.validate_result('write', raw, self.policy, 'xhs')
            COMMON.validate_result('write', raw, self.policy, 'mp')

    def test_27_policy_bool_nan_missing_rejected(self):
        for field in ('threshold', 'max_iterations'):
            raw = copy.deepcopy(self.policy)
            raw['scoring'][field] = True
            with self.assertRaises(ValueError):
                COMMON.validate_policy(raw)
        raw = copy.deepcopy(self.policy)
        raw['runtime']['write'] = True
        with self.assertRaises(ValueError):
            COMMON.validate_policy(raw)
        for value in (False, float('nan'), 0, -1):
            raw = copy.deepcopy(self.policy)
            raw['scoring']['weights']['hook_power'] = value
            with self.assertRaises(ValueError):
                COMMON.validate_policy(raw)

    def test_28_needs_input_no_success(self):
        raw = {'outcome': 'needs_input', 'reason_code': 'source_missing', 'message': 'Need original text'}
        for stage in COMMON.ROLES:
            self.assertEqual(COMMON.validate_result(stage, raw, self.policy), raw)
        target, _ = self.build()
        proc, report = self.cli(target, 'research', 'validate', '--file', self.write('needs.json', raw))
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(report['ok'])
        raw['passed'] = True
        with self.assertRaises(ValueError):
            COMMON.validate_result('research', raw, self.policy)

    def test_29_real_image_metadata_and_hash(self):
        item = self.item()
        self.assertEqual(COMMON.validate_assets(self.root, {'items': [item]}), {'items': [item]})
        for field, value in [('sha256', '0' * 64), ('dhash', '1' * 16), ('width', 301), ('format', 'JPEG'),
                             ('width', True), ('kind', 'card'), ('source_url', 'file:///x'), ('path', '../x')]:
            altered = {**item, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                COMMON.validate_assets(self.root, {'items': [altered]})

    def test_30_actual_decode_not_extension(self):
        item = self.item()
        path = self.root / item['path']
        path.write_bytes(b'not a real PNG')
        item['sha256'] = COMMON.file_hash(path)
        with self.assertRaises(ValueError):
            COMMON.validate_assets(self.root, {'items': [item]})

    def test_31_image_group_nonempty_unique_and_near_duplicate(self):
        first = self.item()
        with self.assertRaises(ValueError):
            COMMON.validate_assets(self.root, {'items': []})
        second = self.item('second.png', (50, 90, 170), 'a2')
        self.assertNotEqual(first['sha256'], second['sha256'])
        with self.assertRaises(ValueError):
            COMMON.validate_assets(self.root, {'items': [first, second]})
        with self.assertRaises(ValueError):
            COMMON.validate_assets(self.root, {'items': [first, copy.deepcopy(first)]})

    def test_32_card_template_contract(self):
        item = self.item()
        for template in ('cover', 'body', 'list'):
            card = {**item, 'kind': 'card', 'template': template}
            COMMON.validate_assets(self.root, {'items': [card]})
        for template in (None, 'custom', True):
            with self.assertRaises(ValueError):
                COMMON.validate_assets(self.root, {'items': [{**item, 'kind': 'card', 'template': template}]})
        # Different card content can share layout/dHash: no photo-only false reject.
        sibling = self.item('body.png', (50, 90, 170), 'a2')
        cover = {**item, 'kind': 'card', 'template': 'cover'}
        body = {**sibling, 'kind': 'card', 'template': 'body'}
        self.assertEqual(cover['dhash'], body['dhash'])
        self.assertEqual(COMMON.validate_assets(self.root, {'items': [cover, body]}),
                         {'items': [cover, body]})
        # A photo and card also do not consume each other's perceptual budget.
        COMMON.validate_assets(self.root, {'items': [item, body]})
        same_bytes = {**item, 'id': 'a3', 'path': 'duplicate-card.png', 'kind': 'card', 'template': 'body'}
        shutil.copy2(self.root / item['path'], self.root / same_bytes['path'])
        with self.assertRaises(ValueError):
            COMMON.validate_assets(self.root, {'items': [cover, same_bytes]})

    def test_33_review_composite_policy_and_bool(self):
        item = self.item()
        raw = review(item)
        raw['scores']['hook_power'] = 5
        normalized = COMMON.validate_review(raw, {'items': [item]}, self.policy)
        weights = self.policy['scoring']['weights']
        expected = sum(raw['scores'][k] * weights[k] for k in COMMON.DIMENSIONS) / sum(weights.values()) * 2
        self.assertEqual(normalized['composite'], expected)
        self.assertTrue(normalized['passed'])
        self.assertNotIn('composite', raw)
        for value in (True, float('nan'), -1, 6):
            raw['scores']['hook_power'] = value
            with self.assertRaises(ValueError):
                COMMON.validate_review(raw, {'items': [item]}, self.policy)

    def test_34_review_coverage_hash_unknown_and_facts(self):
        item = self.item()
        for alteration in [{'id': 'other'}, {'sha256': '0' * 64}, {'approved': 1}, {'observation': ''}]:
            raw = review(item)
            raw['images'][0].update(alteration)
            with self.assertRaises(ValueError):
                COMMON.validate_review(raw, {'items': [item]}, self.policy)
        raw = review(item)
        raw['images'].append({**raw['images'][0], 'id': 'extra'})
        with self.assertRaises(ValueError):
            COMMON.validate_review(raw, {'items': [item]}, self.policy)
        raw = review(item)
        raw['composite'] = 10
        with self.assertRaises(ValueError):
            COMMON.validate_review(raw, {'items': [item]}, self.policy)
        for change in ('facts', 'image', 'score', 'decision'):
            raw = review(item)
            if change == 'facts': raw['facts']['supported'] = False
            if change == 'image': raw['images'][0]['approved'] = False
            if change == 'score': raw['scores'] = {k: 3 for k in COMMON.DIMENSIONS}
            if change == 'decision': raw['decision'] = 'changes_requested'
            self.assertFalse(COMMON.validate_review(raw, {'items': [item]}, self.policy)['passed'])

    def test_35_publish_every_submission_disabled(self):
        target, _ = self.build()
        for command in ('prepare', 'submit', 'publish', 'commit'):
            proc, report = self.cli(target, 'publish', command, '--file', 'arbitrary')
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(report['reason_code'], 'publishing_disabled')
        with self.assertRaises(ValueError) as caught:
            COMMON.validate_result('publish', {'confirmed': True}, self.policy)
        self.assertEqual(caught.exception.reason_code, 'publishing_disabled')

    def test_36_cli_invalid_json_arguments_single_object(self):
        target, _ = self.build()
        for role in ('research', 'write', 'review', 'publish'):
            proc, report = self.cli(target, role, 'validate')
            self.assertEqual(proc.returncode, 1, role)
            self.assertFalse(report['ok'])
        proc = subprocess.run([sys.executable, str(self.original_tools / 'build.py'), 'build', '--target', 'relative'],
                              capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(json.loads(proc.stdout)['ok'])

    def test_37_preview_safe_html_and_links(self):
        target, _ = self.build()
        raw = draft()
        raw['body'] += '\n\n<script>alert(1)</script><img src=x onerror=evil>\n\n[来源](https://example.org/source)'
        proc, report = self.cli(target, 'write', 'preview-mp', '--file', self.write('draft.json', raw), '--style', 'editorial')
        self.assertEqual(proc.returncode, 0, (report, proc.stderr))
        html = report['preview']['html']
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertIn('href="https://example.org/source"', html)
        self.assertIn('style=', html)
        self.assertTrue(report['preview']['publishing_disabled'])
        raw['body'] += '\n\n[bad](javascript:alert)'
        proc, report = self.cli(target, 'write', 'preview-mp', '--file', self.write('draft.json', raw))
        self.assertEqual(proc.returncode, 1)

    def test_38_preview_relative_image_no_article_dir_bug(self):
        target, _ = self.build()
        scripts = target / 'content-engine-write/scripts'
        before = sys.modules.get('common')
        preview = module(scripts / 'mp_preview.py', '_portable_preview')
        self.assertIs(sys.modules.get('common'), before)
        item = self.item()
        self.assertEqual(preview.article_image(self.root, item['path']), self.root / item['path'])
        with self.assertRaises(ValueError):
            preview.article_image(self.root, '../outside')


    def test_39_three_host_build_and_explicit_install(self):
        project = self.root / 'project'
        project.mkdir()
        home = self.root / 'explicit-hermes'
        home.mkdir()
        for host, expected in [('codex', project / '.agents/skills'),
                               ('claude', project / '.claude/skills'),
                               ('hermes', home / 'skills')]:
            report = BUILDER.install(project, host=host, hermes_home=home if host == 'hermes' else None)
            self.assertEqual(Path(report['target']), expected)
            self.assertFalse((expected / 'selfmedia').exists())
            for role in BUILDER.ROLES:
                package = expected / ('content-engine-' + role)
                self.assertEqual(report['manifests'][role]['host'], host)
                self.assertEqual(COMMON.load_json(package / 'runtime.json'),
                    {'schema_version': 1, 'python_minimum': '3.11', 'interpreter': 'host-configured'})
                for name in report['manifests'][role]['files']:
                    if name.endswith(('.json', '.md', '.yaml')):
                        raw = (package / name).read_text(encoding='utf-8')
                        self.assertNotIn(sys.executable, raw)
                        self.assertNotIn(sys.executable.replace(chr(92), '/'), raw)
                        self.assertNotIn('CLAUDE_SKILL_DIR', raw)
                        self.assertNotIn('{{SKILL_DIR}}', raw)
                metadata = package / 'agents/openai.yaml'
                self.assertEqual(metadata.exists(), host == 'codex')
                if host == 'codex':
                    interface = json.loads(metadata.read_text())['interface']
                    self.assertEqual(interface['display_name'], 'content-engine-' + role)
                    self.assertEqual(interface['short_description'], BUILDER.DESCRIPTIONS[role])
            review_package = expected / 'content-engine-review'
            self.assertEqual((review_package / 'scripts/asset_tools.py').read_bytes(),
                (BUILDER.SOURCE / 'assets/scripts/asset_tools.py').read_bytes())
            self.assertTrue((review_package / 'cards/recipes/schema.json').is_file())
            self.assertTrue((review_package / 'requirements.txt').is_file())
        self.assertEqual(len(set(BUILDER.DESCRIPTIONS.values())), 5)
        with self.assertRaisesRegex(ValueError, 'explicit hermes_home'):
            BUILDER.install(project, host='hermes')
        with self.assertRaises(ValueError):
            BUILDER.install(project, host='unknown')
        self.assertFalse((Path(os.environ['HERMES_HOME']) / 'skills').exists())
        (self.tools / 'packet.py').unlink()
        failure = BUILDER.check()
        self.assertFalse(failure['ok'])
        self.assertIn('Missing package dependency: packet.py', failure['message'])

    def test_40_native_packet_cli_fixture_and_manifest_compatibility(self):
        # Only this ABI test substitutes an explicit on-disk packet fixture.
        (self.tools / 'packet.py').write_text(PACKET_FIXTURE, encoding='utf-8')
        target, _ = self.build()
        for role in ('research', 'write', 'review', 'assets'):
            proc, report = self.cli(target, role, 'prepare', '--workspace', self.root,
                                    '--date', '2026-11-10', '--owner', 'session-one',
                                    '--host', 'claude', '--input-id', 'input-one')
            self.assertEqual(proc.returncode, 0, report)
            self.assertEqual(report['role'], role)
            self.assertEqual(report['owner'], 'session-one')
            self.assertEqual(report['host'], 'claude')
            self.assertEqual(report['input_id'], 'input-one')
            self.assertEqual(report['date'], '2026-11-10')
            self.assertEqual(Path(report['skill_dir']), target / ('content-engine-' + role))
            proc, report = self.cli(target, role, 'submit', '--workspace', self.root,
                                    '--attempt', 'attempt-one', '--token', 'token-one')
            self.assertEqual(proc.returncode, 0, report)
            self.assertEqual((report['role'], report['attempt'], report['token']),
                             (role, 'attempt-one', 'token-one'))
            for args in [('prepare', '--workspace', 'relative', '--owner', 'o', '--host', 'codex'),
                         ('submit', '--workspace', self.root, '--attempt', 'a', '--token', 't', '--file', 'other'),
                         ('prepare', '--workspace', self.root, '--owner', 'o', '--host', 'codex',
                          '--content-id', 'c', '--input-id', 'i')]:
                proc, report = self.cli(target, role, *args)
                self.assertEqual(proc.returncode, 1, report)
        for role in BUILDER.ROLES:
            proc, report = self.cli(target, role, 'status', '--workspace', self.root)
            self.assertEqual(proc.returncode, 0, report)
            self.assertIsNone(report['attempt'])
            proc, report = self.cli(target, role, 'interrupt', '--workspace', self.root,
                                    '--attempt', 'a', '--token', 't')
            self.assertEqual(proc.returncode, 1)
            proc, report = self.cli(target, role, 'interrupt', '--workspace', self.root,
                                    '--attempt', 'a', '--token', 't', '--owner-stopped')
            self.assertEqual(proc.returncode, 0, report)
            self.assertTrue(report['owner_stopped'])
        path = target / 'content-engine-write/manifest.json'
        value = COMMON.load_json(path)
        value['host'] = 'claude'
        COMMON.atomic_json(path, value)
        with self.assertRaises(ValueError):
            BUILDER.build(target)
        del value['host']
        value['build_id'] = COMMON.digest({key: value[key] for key in ('role', 'files', 'dependencies', 'policy')})
        COMMON.atomic_json(path, value)
        self.assertTrue(BUILDER.build(target, host='claude')['ok'])
        source = self.source()
        (source / 'review/requirements.txt').write_text('user copy', encoding='utf-8')
        failure = BUILDER.check(source)
        self.assertFalse(failure['ok'])
        self.assertIn('conflicts', failure['message'])


if __name__ == '__main__':
    unittest.main(verbosity=2)

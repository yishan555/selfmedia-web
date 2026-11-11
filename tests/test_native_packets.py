"""Isolated fake native host packets; actual PNGs, no model/API/install."""
import copy
import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from harness_support import bootstrap
RUN_DIR = bootstrap(__file__)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.workbench.workspace import Workspace, atomic_json, load_json, digest
from apps.workbench.jobs import Jobs
from workbench_fixtures import FakeRunner, SETTINGS, TOPIC, DRAFT


def loader(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class NativePackets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skills = RUN_DIR / 'pkg'
        loader(ROOT / 'tools/skill-packager/build.py', '_native_build').build(cls.skills)

    def setUp(self):
        self.now = datetime(2026, 11, 11, 1, 0, tzinfo=timezone.utc)
        self.ws = Workspace(RUN_DIR / digest(self._testMethodName)[:6], lambda: self.now.isoformat())
        self.ws.initialize({**copy.deepcopy(SETTINGS), "auto_produce": True})
        self.jobs = Jobs(self.ws, self.skills)
        self.fake = FakeRunner(self.jobs.common())

    def research(self):
        item = self.ws.add_input({'theme': '主题'})
        handoff = self.jobs.prepare('research', owner='native-research', input_id=item['id'])
        result = self.complete(handoff)
        self.assertTrue(result['ok'], result)
        return self.ws.topic(result['topic_id'])

    def article(self, platform='xhs'):
        return self.ws.add_content('2026-11-11', self.research()['id'], platform)

    def prepare(self, role, item, owner=None):
        return self.jobs.prepare(role, '2026-11-11', owner or 'native-' + role, content_id=item['id'])

    def complete(self, handoff):
        directory = Path(handoff['output_dir'])
        package = load_json(handoff['package_path'])
        result = self.fake.run(handoff['role'], package, directory, package['policy']['runtime'][handoff['role']])
        atomic_json(directory / 'result.json', result)
        return self.jobs.submit(handoff['role'], handoff['attempt'], handoff['token'])

    def test_owner_required(self):
        with self.assertRaises(ValueError):
            self.jobs.prepare('research')

    def test_same_owner_gets_identical_packet(self):
        item = self.article()
        first = self.prepare('write', item)
        self.assertEqual(first, self.prepare('write', item))
        self.assertEqual(self.prepare('write', item, 'other')['state'], 'busy')

    def test_busy_item_does_not_block_another(self):
        a, b = self.article(), self.article()
        self.prepare('write', a)
        ready = self.jobs.prepare('write', '2026-11-11', owner='different-session')
        self.assertEqual(ready['content_id'], b['id'])

    def test_duplicate_replay_and_changed_result_rejected(self):
        item = self.article()
        handoff = self.prepare('write', item)
        result = self.complete(handoff)
        self.assertEqual(result, self.jobs.submit('write', handoff['attempt'], handoff['token']))
        atomic_json(Path(handoff['output_dir']) / 'result.json', {**DRAFT, 'humanizer_notes': '不同负载'})
        with self.assertRaises(ValueError):
            self.jobs.submit('write', handoff['attempt'], handoff['token'])

    def test_interrupt_needs_stop_and_late_submit_rejected(self):
        item = self.article()
        handoff = self.prepare('write', item)
        with self.assertRaises(ValueError):
            self.jobs.interrupt(handoff['attempt'], handoff['token'])
        self.jobs.interrupt(handoff['attempt'], handoff['token'], True)
        atomic_json(Path(handoff['output_dir']) / 'result.json', DRAFT)
        with self.assertRaises(ValueError):
            self.jobs.submit('write', handoff['attempt'], handoff['token'])
        self.jobs.retry('2026-11-11', item['id'], 1)
        self.assertNotEqual(handoff['attempt'], self.prepare('write', item)['attempt'])

    def test_expired_submit_keeps_claim(self):
        item = self.article()
        handoff = self.prepare('write', item)
        self.now += timedelta(seconds=self.jobs.policy()['runtime']['write'] + 1)
        self.assertEqual(self.complete(handoff)['reason_code'], 'attempt_expired')
        self.assertTrue(self.ws.path(f".local/claims/content-{item['id']}.json").exists())
        self.assertEqual(self.prepare('write', item, 'other')['state'], 'busy')
        self.jobs.interrupt(handoff['attempt'], handoff['token'], True)

    def test_needs_input_is_persistent(self):
        item = self.article()
        handoff = self.prepare('write', item)
        atomic_json(Path(handoff['output_dir']) / 'result.json', {'outcome': 'needs_input', 'reason_code': 'evidence_missing', 'message': '补证据'})
        self.assertFalse(self.jobs.submit('write', handoff['attempt'], handoff['token'])['ok'])
        self.assertEqual(self.prepare('write', item)['state'], 'idle')

    def test_unknown_and_publish_disabled(self):
        for role in ('publish', 'unknown'):
            with self.assertRaises(ValueError):
                self.jobs.prepare(role, owner='native')

    def test_review_is_independent_and_copies_actual_images(self):
        item = self.article()
        self.complete(self.prepare('write', item))
        self.complete(self.prepare('assets', item))
        with self.assertRaises(ValueError):
            self.prepare('review', item, 'native-write')
        with self.assertRaises(ValueError):
            self.prepare('review', item, 'native-assets')
        handoff = self.prepare('review', item)
        package = load_json(handoff['package_path'])
        for image in package['assets']['items']:
            self.assertTrue((Path(handoff['output_dir']) / image['path']).is_file())
        self.assertTrue(self.complete(handoff)['ok'])
        self.assertTrue(self.ws.approval_current('2026-11-11', item))

    def test_mp_finalization_retry_uses_no_new_model_at_quota(self):
        item = self.article('mp')
        self.ws.set_quality_rounds('2026-11-11', item['id'], 1, self.jobs.policy()['scoring']['max_iterations'] - 1)
        self.complete(self.prepare('write', item))
        self.complete(self.prepare('assets', item))
        handoff = self.prepare('review', item)
        with patch.object(self.jobs.store('review'), '_finish', side_effect=OSError('fixture')):
            self.assertFalse(self.complete(handoff)['ok'])
        before = len(self.fake.calls)
        self.jobs.retry('2026-11-11', item['id'], 1)
        self.assertTrue(self.prepare('review', item)['ok'])
        self.assertEqual(len(self.fake.calls), before)
        self.assertTrue(self.ws.approval_current('2026-11-11', item))
        self.assertTrue((self.ws.revision_dir('2026-11-11', item['id'], 1) / 'preview.html').exists())

    def test_modified_frozen_input_rejected(self):
        item = self.article()
        handoff = self.prepare('write', item)
        value = load_json(handoff['package_path'])
        value['feedback'] = 'tamper'
        atomic_json(handoff['package_path'], value)
        self.assertFalse(self.complete(handoff)['ok'])

    def test_sources_capture_exact_match(self):
        item = self.ws.add_input({'theme': 'topic'})
        handoff = self.jobs.prepare('research', owner='research', input_id=item['id'])
        directory = Path(handoff['output_dir'])
        atomic_json(directory / 'result.json', TOPIC)
        atomic_json(directory / 'sources.json', {'materials': [{'url': TOPIC['sources'][0]['url'], 'title': 'title', 'text': 'excerpt不在这里'}]})
        self.assertFalse(self.jobs.submit('research', handoff['attempt'], handoff['token'])['ok'])

    def test_default_date_does_not_recover_past(self):
        item = self.article()
        self.now += timedelta(days=1)
        self.assertEqual(self.jobs.prepare('write', owner='native')['state'], 'idle')

    def test_same_owner_cannot_reuse_changed_packet(self):
        item = self.article()
        handoff = self.prepare('write', item)
        atomic_json(handoff['package_path'], {'tampered': True})
        with self.assertRaises(ValueError):
            self.prepare('write', item)

    def test_changed_accepted_checkpoint_cannot_finalize(self):
        item = self.article('mp')
        self.complete(self.prepare('write', item))
        self.complete(self.prepare('assets', item))
        handoff = self.prepare('review', item)
        with patch.object(self.jobs.store('review'), '_finish', side_effect=OSError('fixture')):
            self.assertFalse(self.complete(handoff)['ok'])
        root = self.ws.revision_dir('2026-11-11', item['id'], 1)
        state = load_json(root / 'progress.json')
        state['review']['feedback'] = 'forged acceptance'
        atomic_json(root / 'progress.json', state)
        self.jobs.retry('2026-11-11', item['id'], 1)
        with self.assertRaises(ValueError):
            self.prepare('review', item)
        self.assertFalse(self.ws.approval_current('2026-11-11', item))

    def test_automatic_disabled_requires_manual_request(self):
        item = self.article()
        settings = self.ws.settings()
        self.ws.save_settings({**settings, 'auto_produce': False}, settings['revision'])
        self.assertEqual(self.prepare('write', item)['state'], 'idle')
        self.jobs.request('2026-11-11', item['id'])
        self.assertEqual(self.prepare('write', item)['state'], 'ready')

    def test_production_default_snapshot_and_per_item_due_time(self):
        item = self.article()
        settings = self.ws.settings()
        self.ws.save_settings({**settings, 'production_time': '10:30'}, settings['revision'])
        self.assertEqual(self.ws.content('2026-11-11', item['id'])['production_time'], '08:07')
        later = self.article()
        self.assertEqual(later['production_time'], '10:30')
        self.assertEqual(self.prepare('write', later)['state'], 'idle')
        self.ws.change_content('2026-11-11', later['id'], {'production_time': '08:55'}, 1)
        self.assertEqual(self.prepare('write', later)['state'], 'ready')

    def test_production_time_invalid_types_are_rejected(self):
        item = self.article()
        for value in (None, 800, True, '25:00'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.ws.change_content('2026-11-11', item['id'], {'production_time': value}, 1)

    def test_real_card_cross_role_handoff(self):
        item = self.article()
        self.complete(self.prepare('write', item))
        handoff = self.prepare('assets', item)
        directory = Path(handoff['output_dir'])
        package = load_json(handoff['package_path'])
        tool = loader(self.skills / 'content-engine-assets/scripts/asset_tools.py', '_native_test_cards')
        context = package['card_context']
        url = package['topic']['sources'][0]['url']
        spec = {'context': context, 'sources': [{'id': 's1', 'url': url, 'title': 'source', 'event_id': context['event_id'], 'context': context}], 'theme': 'cream-light', 'slots': {'title': '原生交接', 'subtitle': '跨角色卡片校验'}, 'rights_note': '离线测试非授权'}
        card = tool.render_card(directory, 'cover', spec)
        atomic_json(directory / 'result.json', {'items': [card]})
        result = self.jobs.submit('assets', handoff['attempt'], handoff['token'])
        self.assertTrue(result['ok'], result)
        review = self.prepare('review', item)
        package = load_json(review['package_path'])
        self.assertTrue(Path(review['output_dir'], package['assets']['items'][0]['path']).is_file())
        result = self.complete(review)
        self.assertTrue(result['ok'], result)
        self.assertTrue(self.ws.approval_current('2026-11-11', item))

    def test_status_does_not_expose_token_or_paths(self):
        item = self.article()
        handoff = self.prepare('write', item)
        output = self.jobs.status(handoff['attempt'])
        self.assertEqual(output['attempts'][0]['owner'], 'native-write')
        self.assertNotIn(handoff['token'], str(output))
        self.assertNotIn(handoff['output_dir'], str(output))


if __name__ == '__main__':
    unittest.main()

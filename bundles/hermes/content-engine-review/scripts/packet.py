"""Native-host handoffs: one file implementation copied into each role package.
No launcher, repository imports, time-based claim stealing or publishing capability.
"""
import contextlib
import html
import importlib.util
import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def module_at(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PacketStore:
    def __init__(self, workspace, skill_dir):
        self.root = Path(getattr(workspace, 'root', workspace)).absolute()
        self.clock = getattr(workspace, 'clock', lambda: datetime.now(timezone.utc).isoformat())
        self.skill_dir = Path(skill_dir).absolute()
        self.common = module_at(self.skill_dir / 'scripts/common.py', '_native_packet_common')
        self.common.absolute_root(self.root)
        self.common.absolute_root(self.skill_dir)
        self.policy = self.common.load_json(self.skill_dir / 'policy.json')
        self.common.validate_policy(self.policy)

    def path(self, relative):
        return self.common.safe_path(self.root, relative)

    def read(self, path):
        return self.common.load_json(path)

    def save(self, path, value):
        path = self.common.safe_path(self.root, Path(path).relative_to(self.root).as_posix())
        path.parent.mkdir(parents=True, exist_ok=True)
        self.common.atomic_json(path, value)

    def error(self, message, reason='packet_conflict'):
        raise self.common.PortableError(message, reason)

    @contextlib.contextmanager
    def mutex(self):
        path = self.path('.local/mutation.lock')
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open('x', encoding='utf-8') as stream:
                stream.write(self.common.canonical({'time': self.clock()}))
        except FileExistsError:
            self.error('Workspace mutation in progress; inspect interrupted lock')
        try:
            yield
        finally:
            path.unlink()

    def _role(self, role):
        if role == 'publish':
            self.error('Publishing is disabled', 'publishing_disabled')
        if role not in ('research', 'write', 'assets', 'review'):
            self.error('Unknown packet role', 'unknown_role')

    def _id(self, value):
        if type(value) is not str or not re.fullmatch('[a-f0-9]{32}', value):
            self.error('Invalid stable ID')
        return value

    def _day(self, value):
        if type(value) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            self.error('Expected YYYY-MM-DD')
        datetime.strptime(value, '%Y-%m-%d')
        return value

    def _root(self, date, content_id, revision):
        return self.path(f'{self._day(date)}/contents/{self._id(content_id)}/r{revision:04d}')

    def _docs(self):
        return {p.relative_to(self.skill_dir).as_posix(): self.common.file_hash(self.common.safe_path(self.skill_dir, p.relative_to(self.skill_dir).as_posix()))
                for p in sorted(self.skill_dir.rglob('*')) if p.is_file() and '__pycache__' not in p.parts}

    def _plan(self, date):
        path = self.path(f'{date}/plan.json')
        return self.read(path) if path.exists() else {'items': []}

    def _item(self, date, content_id):
        plan = self._plan(date)
        item = next((i for i in plan['items'] if i['id'] == content_id), None)
        if item is None:
            self.error('Content not found')
        return plan, item

    def _status(self, meta, status, reason=None, quality=None):
        if meta['role'] == 'research':
            path = self.path(f"library/inbox/{meta['input_id']}/input.json")
            current = self.read(path)
            if current['revision'] != meta['revision']:
                self.error('Input revision changed')
            current.update(status=status, reason_code=reason)
            self.save(path, current)
        else:
            plan, item = self._item(meta['date'], meta['content_id'])
            if item['revision'] != meta['revision']:
                self.error('Content revision changed')
            item.update(status=status, reason_code=reason)
            if status == 'running':
                item.setdefault('started_at', self.clock())
            if quality is not None:
                item['quality_rounds'] = quality
            self.save(self.path(f"{meta['date']}/plan.json"), plan)

    def _claim_path(self, role, identifier):
        return self.path('.local/claims/' + ('research-' if role == 'research' else 'content-') + identifier + '.json')

    def _view(self, meta):
        directory = self.path('.local/attempts/' + meta['attempt'])
        return {'ok': True, 'state': 'ready', **{k: meta.get(k) for k in ('attempt', 'token', 'role', 'date', 'content_id', 'input_id', 'revision')},
                'package_path': str(directory / 'input.json'), 'output_dir': str(directory)}

    def _event(self, attempt, meta, event, reason=None):
        path = attempt / 'events.jsonl'
        if path.exists() and path.stat().st_size > 100000:
            self.error('Attempt event budget exceeded')
        entry = {'schema_version': 1, 'time': self.clock(), 'role': meta['role'], 'run_id': meta['attempt'],
                 'session': meta['owner'], 'host': meta['host'], 'content_id': meta.get('content_id'),
                 'input_id': meta.get('input_id'), 'revision': meta['revision'], 'event': event, 'reason_code': reason}
        with path.open('a', encoding='utf-8') as stream:
            stream.write(self.common.canonical(entry) + '\n')

    def _hashes(self, root, hashes):
        for name, expected in hashes.items():
            if self.common.file_hash(self.common.safe_path(root, name)) != expected:
                self.error('Frozen file changed: ' + name, 'input_changed')

    def _checkpoint_anchor(self, root):
        key = root.relative_to(self.root).as_posix().replace('/', '-')
        return self.path('.local/checkpoints/' + key + '.json')

    def _save_checkpoint(self, root, state):
        self.save(root / 'progress.json', state)
        self.save(self._checkpoint_anchor(root), {'schema_version': 1, 'digest': self.common.digest(state)})

    def _check_checkpoint(self, root, state):
        anchor = self.read(self._checkpoint_anchor(root))
        if anchor.get('digest') != self.common.digest(state):
            self.error('Checkpoint changed or interrupted during commit; do not promote', 'checkpoint_changed')

    def _checkpoint(self, date, item):
        root = self._root(date, item['id'], item['revision'])
        root.mkdir(parents=True, exist_ok=True)
        path = root / 'progress.json'
        if path.exists():
            state = self.read(path)
            self._check_checkpoint(root, state)
            if state['package']['revision'] != item['revision'] or state['package']['content_id'] != item['id']:
                self.error('Checkpoint belongs to another content')
            self._hashes(root, state['files'])
            self._hashes(self.root, state.get('source_files', {}))
            if self.common.digest(state['package']['policy']) != self.common.digest(self.policy):
                self.error('Policy changed; explicit revision required')
            return root, state
        topic_path = self.path(f"library/topics/{item['topic_id']}/topic.json")
        topic = self.read(topic_path)
        if topic.get('status') != 'ready':
            self.error('Topic not evidence-ready')
        sources_path = topic_path.parent / 'sources.json'
        package = {'schema_version': 1, 'content_id': item['id'], 'topic_id': topic['id'], 'revision': item['revision'],
                   'date': date, 'platform': item['platform'], 'topic': topic['research'], 'policy': self.policy,
                   'feedback': item.get('feedback', ''), 'sources': self.read(sources_path)}
        if item.get('previous_revision'):
            previous = self._root(date, item['id'], item['previous_revision']) / 'draft.json'
            if previous.exists():
                package['previous_draft'] = self.read(previous)
        self.save(root / 'brief.json', package)
        self.save(root / 'sources.json', package['sources'])
        state = {'phase': 'write', 'quality_used': item.get('quality_rounds', 0), 'package': package,
                 'owners': {}, 'files': {n: self.common.file_hash(root / n) for n in ('brief.json', 'sources.json')},
                 'source_files': {p.relative_to(self.root).as_posix(): self.common.file_hash(p) for p in (topic_path, sources_path)}}
        self._save_checkpoint(root, state)
        return root, state

    def prepare(self, role, date=None, owner='', host='codex', content_id=None, input_id=None):
        self._role(role)
        if type(owner) is not str or not owner.strip() or len(owner) > 256 or type(host) is not str or not host.strip() or len(host) > 64:
            self.error('Native session owner and host are required', 'owner_required')
        if content_id is not None:
            self._id(content_id)
        if input_id is not None:
            self._id(input_id)
        settings = self.read(self.path('config/settings.json'))
        now = datetime.fromisoformat(self.clock()).astimezone(ZoneInfo(settings['timezone']))
        date = self._day(date or now.date().isoformat())
        with self.mutex():
            if role == 'research':
                inbox = self.path('library/inbox')
                candidates = [self.read(p) for p in sorted(inbox.glob('*/input.json'))] if inbox.exists() else []
                candidates = [i for i in candidates if (input_id is None or i['id'] == input_id) and i['status'] in ('queued', 'running', 'interrupted')]
            else:
                candidates = [i for i in self._plan(date)['items'] if (content_id is None or i['id'] == content_id) and i['status'] in ('queued', 'running', 'interrupted')]
            for item in candidates:
                identifier = item['id']
                claim = self._claim_path(role, identifier)
                if claim.exists():
                    claimed = self.read(claim)
                    if claimed.get('owner') == owner and claimed.get('role') == role and claimed.get('host') == host:
                        ticket_path = self.path('.local/attempts/' + claimed['attempt'] + '/ticket.json')
                        ticket = self.read(ticket_path)
                        if claimed.get('binding') != self._binding(ticket):
                            self.error('Existing attempt metadata changed', 'stale_attempt')
                        self._hashes(self.root, ticket['files'])
                        self._hashes(ticket_path.parent, ticket['attempt_files'])
                        elapsed = (datetime.fromisoformat(self.clock()) - datetime.fromisoformat(ticket['created_at'])).total_seconds()
                        if ticket['state'] == 'active' and 0 <= elapsed <= ticket['budget']:
                            return self._view(ticket)
                        if ticket['state'] == 'active':
                            ticket['state'] = 'interrupted'
                            self.save(ticket_path, ticket)
                            self._status(ticket, 'interrupted', 'attempt_expired')
                        return {'ok': False, 'state': 'busy', 'reason_code': 'attempt_expired', 'attempt': ticket['attempt']}
                    if content_id is not None or input_id is not None:
                        return {'ok': True, 'state': 'busy', 'role': role, 'content_id': identifier if role != 'research' else None, 'input_id': identifier if role == 'research' else None}
                    continue
                if item['status'] == 'interrupted':
                    continue
                if role == 'research':
                    input_path = self.path(f'library/inbox/{identifier}/input.json')
                    package = {'schema_version': 1, 'input': item, 'policy': self.policy, 'date': date}
                    source_files = {input_path.relative_to(self.root).as_posix(): self.common.file_hash(input_path)}
                else:
                    request = self.path(f'.local/requests/{identifier}.json')
                    requested = request.exists() and self.read(request).get('revision') == item['revision'] and self.read(request).get('date') == date
                    if not requested and not settings.get('auto_produce', False):
                        continue
                    if not requested and now.strftime('%H:%M') < item['production_time']:
                        continue
                    root, state = self._checkpoint(date, item)
                    if state['phase'] == 'finalize':
                        if role != 'review':
                            continue
                        try:
                            return self._finish(root, state, item, date)
                        except OSError:
                            self._status({'role': role, 'date': date, 'content_id': identifier, 'revision': item['revision']}, 'failed', 'finalize_io_failed')
                            return {'ok': False, 'reason_code': 'finalize_io_failed'}
                    if state['phase'] != role:
                        continue
                    if state['quality_used'] >= self.policy['scoring']['max_iterations']:
                        self._status({'role': role, 'date': date, 'content_id': identifier, 'revision': item['revision']}, 'needs_input', 'quality_budget_exhausted')
                        return {'ok': False, 'reason_code': 'quality_budget_exhausted'}
                    owners = state.get('owners', {})
                    if role == 'review' and (not owners.get('write') or not owners.get('assets') or owner in (owners['write'], owners['assets'])):
                        self.error('Review needs independent native session evidence', 'review_owner_not_independent')
                    package = json.loads(self.common.canonical(state['package']))
                    source_files = {**state.get('source_files', {}), **{(root / n).relative_to(self.root).as_posix(): h for n, h in state['files'].items()},
                                    (root / 'progress.json').relative_to(self.root).as_posix(): self.common.file_hash(root / 'progress.json')}
                attempt_id = uuid.uuid4().hex
                attempt = self.path('.local/attempts/' + attempt_id)
                attempt.mkdir(parents=True)
                if role == 'review':
                    self._validate_group(root, package, package['assets'])
                    self._copy_assets(root, attempt, package['assets'])
                self.save(attempt / 'input.json', package)
                self.save(attempt / 'policy.json', package['policy'])
                if role == 'review':
                    self.save(attempt / 'assets.json', package['assets'])
                meta = {'schema_version': 1, 'attempt': attempt_id, 'token': uuid.uuid4().hex, 'role': role, 'owner': owner, 'host': host,
                        'date': date, 'content_id': identifier if role != 'research' else None, 'input_id': identifier if role == 'research' else None,
                        'revision': item['revision'], 'state': 'active', 'created_at': self.clock(),
                        'budget': self.policy['runtime'][role], 'input_hash': self.common.file_hash(attempt / 'input.json'),
                        'package_digest': self.common.digest(package), 'docs': self._docs()}
                self._status(meta, 'running')
                if role == 'research':
                    source_files = {input_path.relative_to(self.root).as_posix(): self.common.file_hash(input_path)}
                source_files['config/settings.json'] = self.common.file_hash(self.path('config/settings.json'))
                meta['files'] = source_files
                if role != 'research':
                    meta['item_digest'] = self.common.digest(self._item(date, identifier)[1])
                meta['attempt_files'] = {p.relative_to(attempt).as_posix(): self.common.file_hash(p) for p in attempt.rglob('*') if p.is_file()}
                self.save(attempt / 'ticket.json', meta)
                self.save(claim, {**{k: meta[k] for k in ('attempt', 'token', 'owner', 'host', 'role', 'revision', 'input_hash', 'content_id', 'input_id')}, 'binding': self._binding(meta)})
                self._event(attempt, meta, 'prepared')
                return self._view(meta)
            return {'ok': True, 'state': 'idle', 'role': role}

    def _copy_assets(self, source_root, target_root, assets):
        for asset in assets['items']:
            directory = self.common.safe_path(source_root, asset['path']).parent
            if directory == Path(source_root):
                self.error('Assets must have a registered parent folder')
            for path in directory.rglob('*'):
                relative = path.relative_to(source_root).as_posix()
                checked = self.common.safe_path(source_root, relative)
                if checked.is_file():
                    target = self.common.safe_path(target_root, relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(checked, target)

    def _check_draft(self, draft, topic):
        evidence = {item['id']: item for item in topic['evidence']}
        sources = {source['url'] for source in topic['sources']}
        if not set(draft['source_urls']).issubset(sources):
            self.error('Draft source outside topic')
        for claim in draft['claims']:
            for evidence_id in claim['evidence_ids']:
                if evidence_id not in evidence or evidence[evidence_id]['source_url'] not in draft['source_urls']:
                    self.error('Draft claim has no selected evidence')

    def _validate_group(self, root, package, assets):
        self.common.validate_assets(root, assets)
        for asset in assets['items']:
            if asset['source_url'] not in {source['url'] for source in package['topic']['sources']}:
                self.error('Asset source outside topic evidence')
            if asset['kind'] == 'card':
                tool = module_at(self.skill_dir / 'scripts/asset_tools.py', '_native_packet_assets')
                tool.verify_card(root, asset, package['card_context'], package['draft']['body'])
                spec = self.read(self.common.safe_path(root, asset['path']).parent / 'spec.json')
                urls = {source['url'] for source in package['topic']['sources']}
                if any(source['url'] not in urls for source in spec['sources']):
                    self.error('Card uses unrelated evidence')

    def _receipt(self, attempt, meta, package, output_hashes, root=None):
        receipt = {'schema_version': 1, 'stage': meta['role'], 'role': meta['role'], 'content_id': meta.get('content_id'),
                   'input_id': meta.get('input_id'), 'revision': meta['revision'], 'run_id': meta['attempt'], 'session': meta['owner'],
                   'host': meta['host'], 'input_digest': meta['package_digest'], 'input_hash': meta['input_hash'],
                   'policy_hash': self.common.digest(package['policy']), 'document_hashes': meta['docs'],
                   'outcome': 'succeeded', 'time': self.clock(), 'outputs': output_hashes}
        self.save(attempt / 'receipt.json', receipt)
        if root is not None:
            self.save(root / 'receipts' / (meta['role'] + '-' + meta['attempt'] + '.json'), receipt)

    def _finish(self, root, state, item, date):
        self._check_checkpoint(root, state)
        package, review = state['package'], state['review']
        self._hashes(root, state['files'])
        self._hashes(self.root, state.get('source_files', {}))
        self._validate_group(root, package, package['assets'])
        if review.get('passed') is not True:
            self.error('Finalization requires accepted review')
        draft = package['draft']
        self.save(root / 'review.json', review)
        (root / 'article.md').write_text('# ' + draft['title'] + '\n\n' + draft['body'], encoding='utf-8')
        names = ['brief.json', 'sources.json', 'draft.json', 'assets.json', 'review.json', 'article.md']
        if item['platform'] == 'mp':
            rendered = '<!doctype html><html lang="zh"><meta charset="utf-8"><title>' + html.escape(draft['title']) + '</title><article><h1>' + html.escape(draft['title']) + '</h1><div style="white-space:pre-wrap">' + html.escape(draft['body']) + '</div></article></html>'
            (root / 'preview.html').write_text(rendered, encoding='utf-8')
            names.append('preview.html')
        files = {n: self.common.file_hash(root / n) for n in names}
        for asset in package['assets']['items']:
            for path in self.common.safe_path(root, asset['path']).parent.rglob('*'):
                if path.is_file():
                    name = path.relative_to(root).as_posix()
                    files[name] = self.common.file_hash(self.common.safe_path(root, name))
        self.save(root / 'approval.json', {'schema_version': 1, 'content_id': item['id'], 'revision': item['revision'], 'passed': True,
                                         'files': files, 'input_digest': self.common.digest(package), 'time': self.clock(), 'publish_enabled': False})
        self._status({'role': 'review', 'date': date, 'content_id': item['id'], 'revision': item['revision']}, 'ready', quality=state['quality_used'])
        self.path(f".local/requests/{item['id']}.json").unlink(missing_ok=True)
        return {'ok': True, 'state': 'idle', 'content_id': item['id'], 'publish_enabled': False}

    def _commit_research(self, directory, meta, package, result, submission):
        sources = self.read(directory / 'sources.json')
        materials = sources.get('materials')
        if type(materials) is not list or not materials or len(materials) > 50:
            self.error('Missing captured source material')
        texts = {}
        for material in materials:
            if type(material) is not dict or set(material) != {'url', 'title', 'text'} or any(type(v) is not str or not v.strip() for v in material.values()) or material['url'] in texts:
                self.error('Invalid or duplicate source capture')
            texts[material['url']] = material['text']
        if set(texts) != {s['url'] for s in result['sources']}:
            self.error('Sources and capture must match exactly')
        if any(e['excerpt'] not in texts.get(e['source_url'], '') for e in result['evidence']):
            self.error('Excerpt absent from captured source')
        topic_id = uuid.uuid4().hex
        topic_root = self.path('library/topics/' + topic_id)
        topic_root.mkdir(parents=True)
        self.save(topic_root / 'sources.json', sources)
        self._receipt(directory, meta, package, submission, topic_root)
        self.save(topic_root / 'topic.json', {'schema_version': 1, 'id': topic_id, 'input_id': meta['input_id'], 'input_revision': meta['revision'],
                                            'created_at': self.clock(), 'status': 'ready', 'title': result['title'], 'research': result,
                                            'source_hash': self.common.file_hash(topic_root / 'sources.json')})
        input_path = self.path(f"library/inbox/{meta['input_id']}/input.json")
        current = self.read(input_path)
        current.update(status='ready', topic_id=topic_id, reason_code=None)
        self.save(input_path, current)
        return {'ok': True, 'topic_id': topic_id, 'state': 'idle'}

    def _commit_content(self, directory, meta, package, result, root, state, item, submission):
        role = meta['role']
        state.setdefault('owners', {})[role] = meta['owner']
        if role == 'write':
            self._check_draft(result, package['topic'])
            round_path = f"rounds/{state['quality_used']+1}/draft.json"
            self.save(self.common.safe_path(root, round_path), result)
            package['draft'] = result
            package.pop('feedback', None)
            package.pop('assets', None)
            package['card_context'] = {'event_id': item['id'], 'topic_id': package['topic_id'], 'angle_id': f"r{item['revision']}:{self.common.digest(result)}"}
            state.update(phase='assets', draft_path=round_path)
            state['files'][round_path] = self.common.file_hash(root / round_path)
        elif role == 'assets':
            result = self.common.validate_assets(directory, result)
            self._validate_group(directory, package, result)
            destination = self.common.safe_path(root, 'media/' + meta['attempt'])
            destination.mkdir(parents=True, exist_ok=False)
            self._copy_assets(directory, destination, result)
            for asset in result['items']:
                asset['path'] = 'media/' + meta['attempt'] + '/' + asset['path']
            self._validate_group(root, package, result)
            package['assets'] = result
            self.save(root / 'draft.json', package['draft'])
            self.save(root / 'assets.json', result)
            state['phase'] = 'review'
            for path in destination.rglob('*'):
                if path.is_file():
                    name = path.relative_to(root).as_posix()
                    state['files'][name] = self.common.file_hash(path)
            state['files'].update({n: self.common.file_hash(root / n) for n in ('draft.json', 'assets.json')})
        else:
            self._validate_group(root, package, package['assets'])
            state['quality_used'] += 1
            state['review'] = result
            state['review_input_digest'] = meta['package_digest']
            self.save(root / 'history' / f"review-{state['quality_used']}.json", {'draft': package['draft'], 'assets': package['assets'], 'review': result})
            if result['passed']:
                state['phase'] = 'finalize'
            else:
                package['previous_draft'] = package['draft']
                package['feedback'] = result['feedback']
                state['phase'] = 'write'
            self.save(root / 'review.json', result)
        state['package'] = package
        self._receipt(directory, meta, self.read(directory / 'input.json'), submission, root)
        receipt_path = 'receipts/' + role + '-' + meta['attempt'] + '.json'
        state['files'][receipt_path] = self.common.file_hash(root / receipt_path)
        self._save_checkpoint(root, state)
        self._status(meta, 'queued', quality=state['quality_used'])
        response = {'ok': True, 'state': 'idle', 'content_id': item['id'], 'next_role': state['phase']}
        if state['phase'] == 'finalize':
            try:
                response = self._finish(root, state, item, meta['date'])
            except OSError:
                self._status(meta, 'failed', 'finalize_io_failed', state['quality_used'])
                response = {'ok': False, 'reason_code': 'finalize_io_failed'}
        return response

    def status(self, attempt=None):
        directory = self.path('.local/attempts')
        paths = [directory / self._id(attempt) / 'ticket.json'] if attempt else sorted(directory.glob('*/ticket.json'))
        summaries = []
        for path in paths:
            meta = self.read(path)
            summaries.append({**{k: meta.get(k) for k in ('attempt', 'role', 'owner', 'host', 'date', 'content_id', 'input_id', 'revision')}, 'status': meta['state']})
        return {'ok': True, 'attempts': summaries}

    def interrupt(self, attempt, token, owner_stopped=False):
        directory = self.path('.local/attempts/' + self._id(attempt))
        with self.mutex():
            meta = self.read(directory / 'ticket.json')
            if meta['token'] != token:
                self.error('Attempt token mismatch', 'stale_attempt')
            if owner_stopped is not True:
                self.error('Confirm native owner session stopped before revocation', 'owner_stop_required')
            if meta['state'] not in ('active', 'interrupted'):
                self.error('Attempt already completed', 'stale_attempt')
            claim_path = self._claim_path(meta['role'], meta['input_id'] or meta['content_id'])
            if self.read(claim_path).get('attempt') != attempt:
                self.error('Claim no longer belongs to attempt', 'stale_attempt')
            meta['state'] = 'revoked'
            self.save(directory / 'ticket.json', meta)
            self._status(meta, 'interrupted', 'owner_stopped')
            claim_path.unlink()
            self._event(directory, meta, 'revoked', 'owner_stopped')
            return {'ok': True, 'state': 'interrupted', 'attempt': attempt}

    def _binding(self, meta):
        return self.common.digest({k: v for k, v in meta.items() if k not in ('state', 'result', 'submission_files')})

    def _submission_files(self, attempt):
        return {p.relative_to(attempt).as_posix(): self.common.file_hash(self.common.safe_path(attempt, p.relative_to(attempt).as_posix()))
                for p in sorted(attempt.rglob('*')) if p.is_file() and p.name not in ('ticket.json', 'events.jsonl', 'receipt.json', 'failure.json')}

    def submit(self, role, attempt, token):
        self._role(role)
        directory = self.path('.local/attempts/' + self._id(attempt))
        with self.mutex():
            meta = self.read(directory / 'ticket.json')
            if meta['role'] != role or meta['token'] != token:
                self.error('Attempt role/token mismatch', 'stale_attempt')
            submission = self._submission_files(directory)
            completed = self.path('.local/submissions/' + attempt + '.json')
            if completed.exists():
                meta = self.read(completed)
                if meta['token'] != token or meta['role'] != role:
                    self.error('Completed attempt token/role mismatch', 'stale_attempt')
                if submission != meta['submission_files']:
                    self.error('Duplicate submission payload changed', 'submission_changed')
                return meta['result']
            if meta['state'] == 'submitted':
                self.error('Missing program-owned submission receipt', 'stale_attempt')
            if meta['state'] != 'active':
                self.error('Interrupted or revoked attempt cannot submit', 'stale_attempt')
            claim_path = self._claim_path(role, meta['input_id'] or meta['content_id'])
            claim = self.read(claim_path)
            if claim.get('binding') != self._binding(meta):
                self.error('Attempt metadata changed', 'stale_attempt')
            if any(claim.get(k) != meta[k] for k in ('attempt', 'token', 'owner', 'host', 'role', 'revision', 'input_hash')):
                self.error('Claim does not bind this attempt', 'stale_attempt')
            elapsed = (datetime.fromisoformat(self.clock()) - datetime.fromisoformat(meta['created_at'])).total_seconds()
            if elapsed < 0 or elapsed > meta['budget']:
                meta['state'] = 'interrupted'
                self.save(directory / 'ticket.json', meta)
                self._status(meta, 'interrupted', 'attempt_expired')
                self._event(directory, meta, 'interrupted', 'attempt_expired')
                return {'ok': False, 'reason_code': 'attempt_expired', 'state': 'interrupted'}
            try:
                self._hashes(self.root, meta['files'])
                self._hashes(directory, meta['attempt_files'])
                if self._docs() != meta['docs']:
                    self.error('Installed package changed', 'input_changed')
                package = self.read(directory / 'input.json')
                if self.common.file_hash(directory / 'input.json') != meta['input_hash'] or self.common.digest(package) != meta['package_digest']:
                    self.error('Packet input changed', 'input_changed')
                if role != 'research':
                    plan, item = self._item(meta['date'], meta['content_id'])
                    if self.common.digest(item) != meta['item_digest'] or item['revision'] != meta['revision'] or item['status'] != 'running':
                        self.error('Current item no longer binds attempt', 'stale_attempt')
                    root = self._root(meta['date'], item['id'], item['revision'])
                    state = self.read(root / 'progress.json')
                    if state['phase'] != role:
                        self.error('Phase changed', 'stale_attempt')
                raw = self.read(directory / 'result.json')
                result = self.common.validate_review(raw, package['assets'], package['policy']) if role == 'review' else self.common.validate_result(role, raw, package['policy'], package.get('platform'))
                if result.get('outcome') == 'needs_input':
                    self._status(meta, 'needs_input', result['reason_code'])
                    response = {'ok': False, 'state': 'blocked', 'reason_code': result['reason_code']}
                elif role == 'research':
                    response = self._commit_research(directory, meta, package, result, submission)
                else:
                    response = self._commit_content(directory, meta, package, result, root, state, item, submission)
                meta.update(state='submitted', submission_files=submission, result=response)
                self.save(directory / 'ticket.json', meta)
                self.save(completed, meta)
                claim_path.unlink()
                self._event(directory, meta, 'submitted', response.get('reason_code'))
                return response
            except (ValueError, OSError, KeyError) as exc:
                reason = getattr(exc, 'reason_code', 'packet_validation_failed')
                self._status(meta, 'needs_input', reason)
                response = {'ok': False, 'state': 'blocked', 'reason_code': reason}
                meta.update(state='submitted', submission_files=submission, result=response)
                self.save(directory / 'ticket.json', meta)
                self.save(completed, meta)
                claim_path.unlink(missing_ok=True)
                self._event(directory, meta, 'blocked', reason)
                return response

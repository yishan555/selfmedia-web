"""Native installed-packet facade and explicit local request/retry operations."""
import importlib.util
from pathlib import Path
from .workspace import Conflict, WorkspaceError, atomic_json, day, identity


def module_at(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Jobs:
    def __init__(self, workspace, skills_root):
        self.workspace = workspace
        self.skills_root = Path(skills_root).resolve()
        self._stores = {}

    def store(self, role='review'):
        if role not in ('research', 'write', 'assets', 'review', 'publish'):
            raise WorkspaceError('unknown_role')
        if role not in self._stores:
            directory = self.skills_root / ('content-engine-' + role)
            module = module_at(directory / 'scripts/packet.py', '_workbench_packet_' + role)
            self._stores[role] = module.PacketStore(self.workspace, directory)
        return self._stores[role]

    def common(self):
        return self.store().common

    def policy(self):
        return self.store().policy

    def prepare(self, role, date=None, owner='', host='codex', content_id=None, input_id=None):
        return self.store(role).prepare(role, date, owner, host, content_id, input_id)

    def submit(self, role, attempt, token):
        return self.store(role).submit(role, attempt, token)

    def status(self, attempt=None):
        return self.store().status(attempt)

    def interrupt(self, attempt, token, owner_stopped=False):
        return self.store().interrupt(attempt, token, owner_stopped)

    def request(self, date, content_id):
        item = self.workspace.content(day(date), identity(content_id))
        if item['status'] != 'queued':
            raise Conflict('already started; use explicit retry or revision')
        with self.workspace.transaction():
            atomic_json(self.workspace.path(f'.local/requests/{content_id}.json'), {'date': date, 'content_id': content_id, 'revision': item['revision']})
        return {'ok': True, 'content_id': content_id, 'status': 'queued'}

    def retry(self, date, content_id, revision):
        item = self.workspace.content(date, content_id)
        if item['revision'] != revision or item['status'] not in ('failed', 'interrupted', 'needs_input'):
            raise Conflict('not retryable')
        if self.workspace.path(f'.local/claims/content-{content_id}.json').exists():
            raise Conflict('previous owner session stop not confirmed; inspect claim')
        if item.get('technical_retries', 0) >= 1:
            raise Conflict('local retry budget exhausted; revise input')
        with self.workspace.transaction():
            plan = self.workspace.plan(date)
            current = next(i for i in plan['items'] if i['id'] == content_id)
            if current['revision'] != revision or current['status'] != item['status']:
                raise Conflict('content changed')
            current['technical_retries'] = current.get('technical_retries', 0) + 1
            current['status'] = 'queued'
            atomic_json(self.workspace.path(f'{date}/plan.json'), plan)
        return self.request(date, content_id)

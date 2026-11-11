"""Native host file handoff. No hidden Agent launch or scheduler creation."""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from .automation import task_templates
from .archive import run_archive
from .jobs import Jobs
from .workspace import Workspace, WorkspaceError, canonical, load_json, safe_path, verify_delivery

HOSTS = ('codex', 'claude', 'hermes')
ROLES = ('research', 'write', 'assets', 'review')

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise WorkspaceError(message)
    def print_help(self, file=None):
        print(canonical({'ok': True, 'help': self.format_help()}), file=file)

def skill_root(project, host, explicit=None):
    if explicit:
        return Path(explicit).resolve()
    if host == 'hermes':
        raise WorkspaceError('Hermes requires explicit --skills for the selected profile')
    return project / ('.agents' if host == 'codex' else '.claude') / 'skills'

def installed_report(skills, host):
    import hashlib
    packages = {}
    for role in (*ROLES, 'publish'):
        root = skills / ('content-engine-' + role)
        try:
            m = load_json(root / 'manifest.json')
            valid = m.get('host', host) == host and bool(m['files'])
            for relative, expected in m['files'].items():
                valid = valid and hashlib.sha256(safe_path(root, relative).read_bytes()).hexdigest() == expected
            packages[role] = {'ok': valid, 'build_id': m.get('build_id')}
        except (OSError, ValueError, KeyError):
            packages[role] = {'ok': False, 'reason_code': 'package_missing_or_changed'}
    return {'ok': all(p['ok'] for p in packages.values()), 'host': host, 'mode': 'external',
            'packages': packages, 'publish_enabled': False, 'native_automation_verified': False,
            'scope': 'offline package integrity, not model or scheduler acceptance'}

def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--skills', help='Installed five-role directory; required for Hermes')
    parser.add_argument('--host', choices=HOSTS)
    sub = parser.add_subparsers(dest='command', required=True, parser_class=Parser)
    init = sub.add_parser('init'); init.add_argument('--config', required=True)
    sub.add_parser('doctor'); sub.add_parser('status')
    plan = sub.add_parser('plan'); dates = plan.add_mutually_exclusive_group(required=True)
    dates.add_argument('--date'); dates.add_argument('--tomorrow', action='store_true')
    plan.add_argument('--automatic', action='store_true', help='Honor auto_plan, planning time and one run per workspace day; requires --tomorrow')
    templates = sub.add_parser('automation-templates'); templates.add_argument('--project'); templates.add_argument('--out')
    prepare = sub.add_parser('prepare'); prepare.add_argument('--role', choices=ROLES, required=True)
    prepare.add_argument('--date'); prepare.add_argument('--owner', required=True)
    prepare.add_argument('--content-id'); prepare.add_argument('--input-id')
    submit = sub.add_parser('submit'); submit.add_argument('--role', choices=ROLES, required=True)
    submit.add_argument('--attempt', required=True); submit.add_argument('--token', required=True)
    interrupt = sub.add_parser('interrupt'); interrupt.add_argument('--attempt', required=True)
    interrupt.add_argument('--token', required=True); interrupt.add_argument('--owner-stopped', action='store_true')
    serve = sub.add_parser('serve'); serve.add_argument('--port', type=int, default=5051)
    export = sub.add_parser('export'); export.add_argument('--date', required=True)
    export.add_argument('--content-id', required=True); export.add_argument('--target', required=True)
    verify = sub.add_parser('verify-delivery'); verify.add_argument('--target', required=True)
    archive = sub.add_parser('archive'); archive.add_argument('--repo'); archive.add_argument('--mode', choices=('status', 'sync'), default='sync')
    archive.add_argument('--branch', default='main'); archive.add_argument('--remote'); archive.add_argument('--project')
    archive.add_argument('--no-push', action='store_true'); archive.add_argument('--no-bundles', action='store_true')
    archive.add_argument('--allow-active', action='store_true'); archive.add_argument('--message')
    archive.add_argument('--allow-local-remote', action='store_true')
    try:
        args = parser.parse_args(argv)
        ws = Workspace(args.workspace)
        if args.command == 'init':
            result = {'settings': ws.initialize(load_json(args.config)), 'publish_enabled': False}
        elif args.command == 'verify-delivery':
            result = {'delivery': verify_delivery(args.target)}
        elif args.command == 'archive':
            settings = ws.settings()
            repo = args.repo or settings.get('archive_repo') or str(ws.root)
            result = {'archive': run_archive(ws, repo=repo, mode=args.mode, branch=args.branch,
                remote=args.remote or settings.get('archive_remote') or 'https://github.com/yishan555/selfmedia-web.git',
                project=Path(args.project).resolve() if args.project else None,
                push=not args.no_push, build_bundles=not args.no_bundles,
                allow_active=args.allow_active, allow_local_remote=args.allow_local_remote, message=args.message)}
        elif args.command == 'export':
            result = {'delivery': ws.export(args.date, args.content_id, args.target)}
        else:
            settings = ws.settings(); project = Path(__file__).resolve().parents[2]
            host = args.host or settings.get('host', 'codex')
            skills = skill_root(project, host, args.skills)
            jobs = Jobs(ws, skills)
            if args.command == 'doctor': result = installed_report(skills, host)
            elif args.command == 'status': result = jobs.status()
            elif args.command == 'plan':
                if args.automatic and not args.tomorrow:
                    raise WorkspaceError('--automatic requires --tomorrow')
                date = None if args.automatic else args.date or (datetime.fromisoformat(ws.clock()).astimezone(ZoneInfo(settings['timezone'])).date() + timedelta(days=1)).isoformat()
                result = {'plan': ws.generate_plan(date, automatic=args.automatic)}
            elif args.command == 'automation-templates':
                result = task_templates(ws, Path(args.project).resolve() if args.project else project, skills, host)
                if args.out:
                    from .workspace import atomic_json
                    target = Path(args.out).resolve()
                    if not target.is_relative_to(ws.root): raise WorkspaceError('template output must be inside workspace')
                    atomic_json(ws.path(target.relative_to(ws.root).as_posix()), result)
            elif args.command == 'prepare':
                result = jobs.prepare(args.role, args.date, args.owner, host, args.content_id, args.input_id)
            elif args.command == 'submit': result = jobs.submit(args.role, args.attempt, args.token)
            elif args.command == 'interrupt': result = jobs.interrupt(args.attempt, args.token, args.owner_stopped)
            elif args.command == 'serve':
                if not 1024 <= args.port <= 65535: raise WorkspaceError('invalid local port')
                from .app import create_app
                from waitress import serve
                print(canonical({'ok': True, 'url': f'http://127.0.0.1:{args.port}', 'mode': 'external', 'host': host, 'publish_enabled': False}), flush=True)
                serve(create_app(ws, jobs), host='127.0.0.1', port=args.port, threads=4)
                return 0
        output = {'ok': True, **result}
    except (ValueError, OSError, KeyError, ImportError) as exc:
        output = {'ok': False, 'reason_code': getattr(exc, 'reason_code', 'workbench_failed'), 'message': str(exc) if isinstance(exc, WorkspaceError) else type(exc).__name__}
    print(canonical(output))
    return 0 if output['ok'] else 1

if __name__ == '__main__':
    raise SystemExit(main())

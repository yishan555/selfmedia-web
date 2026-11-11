"""Portable research validation; no fetching or source truth claims."""
import argparse
from pathlib import Path
import sys
sys.dont_write_bytecode = True
# Local imports work for direct execution, runpy, and importlib loaders.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import PortableError, canonical, load_json, validate_result

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise PortableError(message, 'invalid_arguments')


def packet_commands(commands):
    prepare = commands.add_parser('prepare')
    prepare.add_argument('--workspace', required=True)
    prepare.add_argument('--date')
    prepare.add_argument('--owner', required=True)
    prepare.add_argument('--host', choices=('codex', 'claude', 'hermes'), required=True)
    selector = prepare.add_mutually_exclusive_group()
    selector.add_argument('--content-id')
    selector.add_argument('--input-id')
    submit = commands.add_parser('submit')
    submit.add_argument('--workspace', required=True)
    submit.add_argument('--attempt', required=True)
    submit.add_argument('--token', required=True)
    status = commands.add_parser('status')
    status.add_argument('--workspace', required=True)
    status.add_argument('--attempt')
    interrupt = commands.add_parser('interrupt')
    interrupt.add_argument('--workspace', required=True)
    interrupt.add_argument('--attempt', required=True)
    interrupt.add_argument('--token', required=True)
    interrupt.add_argument('--owner-stopped', action='store_true', required=True)


def packet_call(args, role):
    import importlib.util
    skill_dir = Path(__file__).resolve().parents[1]
    path = skill_dir / 'scripts' / 'packet.py'
    if not path.is_file():
        raise ValueError('Missing package dependency: scripts/packet.py; build the package first')
    spec = importlib.util.spec_from_file_location('_own_' + role + '_packet', path)
    packet = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packet)
    store = packet.PacketStore(args.workspace, skill_dir)
    if args.command == 'prepare':
        return store.prepare(role, date=args.date, owner=args.owner, host=args.host,
                             content_id=args.content_id, input_id=args.input_id)
    if args.command == 'submit':
        return store.submit(role, args.attempt, args.token)
    if args.command == 'status':
        return store.status(args.attempt)
    return store.interrupt(args.attempt, args.token, owner_stopped=args.owner_stopped)

def main(argv=None):
    parser = Parser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    packet_commands(commands)
    command = commands.add_parser('validate')
    command.add_argument('--file', required=True)
    command.add_argument('--policy', default=str(Path(__file__).resolve().parent.parent / 'policy.json'))
    command.add_argument('--platform', choices=('xhs', 'mp'))
    try:
        args = parser.parse_args(argv)
        if args.command in ('prepare', 'submit', 'status', 'interrupt'):
            report = packet_call(args, 'research')
            print(canonical(report))
            return 0 if report['ok'] else 1
        result = validate_result('research', load_json(args.file), load_json(args.policy), args.platform)
        report = {'ok': result.get('outcome') != 'needs_input', 'result': result}
        if not report['ok']:
            report['reason_code'] = result['reason_code']
    except (ValueError, OSError) as exc:
        report = {'ok': False, 'reason_code': getattr(exc, 'reason_code', 'validation_failed'), 'message': str(exc)}
    print(canonical(report))
    return 0 if report['ok'] else 1

if __name__ == '__main__':
    raise SystemExit(main())

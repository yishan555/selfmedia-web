"""Explicit portable Skill commands; no legacy service installation or database."""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)

    def print_help(self, file=None):
        print(json.dumps({'ok': True, 'help': self.format_help()}, ensure_ascii=False), file=file)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = Parser(description=__doc__)
    parser.add_argument('command', choices=('check-portable-skills', 'build-portable-skills', 'install-portable-skills'))
    parser.add_argument('--host', choices=('codex', 'claude', 'hermes'), default='codex')
    parser.add_argument('--project', help='Explicit existing project directory for installation')
    parser.add_argument('--target', help='Explicit absolute output directory for build')
    parser.add_argument('--hermes-home', help='Explicit selected Hermes profile home')
    try:
        args = parser.parse_args(argv)
        spec = importlib.util.spec_from_file_location('_portable_setup_builder', ROOT / 'tools/skill-packager/build.py')
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        if args.command == 'check-portable-skills':
            result = builder.check(host=args.host)
        elif args.command == 'build-portable-skills':
            if not args.target:
                parser.error('build-portable-skills requires --target')
            result = builder.build(Path(args.target), host=args.host)
        else:
            if not args.project:
                parser.error('install-portable-skills requires --project')
            result = builder.install(Path(args.project), host=args.host, hermes_home=Path(args.hermes_home) if args.hermes_home else None)
    except (ValueError, OSError, ImportError) as exc:
        result = {'ok': False, 'reason_code': getattr(exc, 'reason_code', 'portable_command_failed'), 'message': str(exc)}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

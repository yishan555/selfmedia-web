"""Single-JSON portable assets entry point; only structured card templates."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
sys.dont_write_bytecode = True
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_spec = importlib.util.spec_from_file_location("_own_portable_asset_tools", Path(__file__).resolve().parent / "asset_tools.py")
asset_tools = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(asset_tools)

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise asset_tools.AssetError(message)

def read_json(file):
    path = Path(file)
    if not path.is_absolute() or not path.is_file() or path.stat().st_size > 2*1024*1024:
        raise asset_tools.AssetError("--file must be bounded existing absolute JSON path")
    return json.loads(path.read_text(encoding="utf-8"))


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
    try:
        parser = Parser(description="Portable photos and cover/body/list cards; no visual approval")
        commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
        for name in ("import-photo", "download", "from-page", "render-card", "verify", "validate"):
            sub = commands.add_parser(name)
            sub.add_argument("--workspace", required=True)
            if name in ("import-photo", "render-card", "verify", "validate"):
                sub.add_argument("--file", required=True)
            if name in ("import-photo", "download"):
                sub.add_argument("--source-url", required=True)
            if name in ("import-photo", "download", "from-page"):
                sub.add_argument("--rights-note", required=True)
            if name in ("download", "from-page"):
                sub.add_argument("--url", required=True)
                sub.add_argument("--timeout", type=float, default=15)
                sub.add_argument("--proxy", help="Explicit http:// proxy for HTTP and HTTPS")
            if name == "from-page":
                sub.add_argument("--limit", type=int, default=6)
            if name == "render-card":
                sub.add_argument("--template", required=True, choices=sorted(asset_tools.TEMPLATES))
            if name == "validate":
                sub.add_argument("--policy", default=str(Path(__file__).resolve().parent.parent / "policy.json"))
                sub.add_argument("--platform", choices=("xhs", "mp"))
            if name == "verify":
                sub.add_argument("--context", help="JSON object required for cards")
                sub.add_argument("--draft-body-file", help="Absolute complete UTF-8 draft body")
        packet_commands(commands)
        args = parser.parse_args(argv)
        if args.command in ('prepare', 'submit', 'status', 'interrupt'):
            report = packet_call(args, 'assets')
            print(json.dumps(report, ensure_ascii=False))
            return 0 if report['ok'] else 1
        if args.command == "validate":
            common_path = Path(__file__).resolve().parent / "common.py"
            if not common_path.is_file():
                raise asset_tools.AssetError("validate requires build-copied scripts/common.py and policy.json")
            common_spec = importlib.util.spec_from_file_location("_own_assets_common", common_path)
            common = importlib.util.module_from_spec(common_spec)
            common_spec.loader.exec_module(common)
            result = common.validate_result("assets", common.load_json(args.file), common.load_json(args.policy), args.platform)
            ok = result.get("outcome") != "needs_input"
            report = {"ok": ok, "result": result}
            if ok:
                report["result"] = common.validate_assets(args.workspace, result)
            else:
                report["reason_code"] = result["reason_code"]
            print(common.canonical(report))
            return 0 if ok else 1
        if args.command == "import-photo":
            item = asset_tools.import_photo(args.workspace, args.file, args.source_url, args.rights_note)
        elif args.command == "download":
            item = asset_tools.download(args.workspace, args.url, args.source_url, args.rights_note, args.timeout, args.proxy)
        elif args.command == "from-page":
            items = asset_tools.from_page(args.workspace, args.url, args.rights_note, args.limit, args.timeout, args.proxy)
            print(json.dumps({"ok": True, "assets": items}, ensure_ascii=False))
            return 0
        elif args.command == "render-card":
            item = asset_tools.render_card(args.workspace, args.template, read_json(args.file))
        else:
            item = read_json(args.file)
            if item.get("kind") == "card":
                if not args.context:
                    raise asset_tools.AssetError("card verification requires --context")
                body = None
                if args.draft_body_file:
                    path = Path(args.draft_body_file)
                    if not path.is_absolute() or not path.is_file() or path.stat().st_size > 2*1024*1024:
                        raise asset_tools.AssetError("draft body file must be bounded absolute UTF-8 file")
                    body = path.read_text(encoding="utf-8")
                asset_tools.verify_card(args.workspace, item, json.loads(args.context), body)
            else:
                asset_tools.verify_asset(args.workspace, item)
        print(json.dumps({"ok": True, "asset": item}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1

if __name__ == "__main__":
    sys.exit(main())

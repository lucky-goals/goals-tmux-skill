"""Command line for the scaffold: ``python -m cli_platform <command>``.

Every command is read-only except ``plan --commit``, and none makes a model
call. Output is JSON unless stated. JSON keys and codes are never translated;
``--locale`` only changes the human ``text`` added to refusals.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from .config import EFFORT_ORDER, Candidate, load_config
from .errors import PlatformError
from .i18n.core import SUPPORTED, Translator, load_catalog, negotiate, normalize
from .router import RouteRequest
from .scanner import scan_all
from .supervisor import Platform, SessionRequest

_DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "cli-platform.toml"


def _pin(text: str | None) -> Candidate | None:
    if not text:
        return None
    parts = text.split("/")
    if len(parts) not in (2, 3) or len(parts) == 3 and parts[2] not in EFFORT_ORDER:
        raise SystemExit(f"--pin must look like variant/model[/effort], effort one of {list(EFFORT_ORDER)}")
    return Candidate(parts[0], parts[1], parts[2] if len(parts) == 3 else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cli_platform", description=__doc__)
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    parser.add_argument("--locale", help=f"display language, one of {', '.join(SUPPORTED)} "
                                         "(default: ui.locale, then the OS locale, then en)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-config", help="load and validate the configuration")
    commands.add_parser("locale", help="show the negotiated display language and catalog coverage")
    scan = commands.add_parser("scan", help="scan installed CLIs")
    scan.add_argument("--auth", action="store_true", help="also run each CLI's local auth status command")
    commands.add_parser("variants", help="variant availability and settings-file checks")
    commands.add_parser("catalog", help="merged model catalog")
    resolve = commands.add_parser("resolve", help="explain routing for a stage (text)")
    resolve.add_argument("stage")
    resolve.add_argument("--pin")
    resolve.add_argument("--allow-fallback", action="store_true")
    plan = commands.add_parser("plan", help="dry-run one session: nickname, route, lease, launch")
    plan.add_argument("stage")
    plan.add_argument("--run-id", default="run-DRYRUN0001")
    plan.add_argument("--commit", action="store_true",
                      help="use the configured state directory instead of a temporary one")
    args = parser.parse_args(argv)
    if args.locale and normalize(args.locale) is None:
        parser.error(f"--locale {args.locale!r} is not supported; use one of {', '.join(SUPPORTED)}")

    config = None
    try:
        config = load_config(args.config)
        if args.command == "locale":
            return _locale(args, config)
        if args.command == "check-config":
            print(json.dumps({"ok": True, "path": str(config.path), "variants": sorted(config.variants),
                              "accounts": sorted(config.accounts), "models": len(config.models),
                              "stages": [{"id": stage.id, "permission_profile": stage.permission_profile}
                                         for stage in config.stages.values()]}, indent=2))
            return 0
        if args.command == "scan":
            print(json.dumps([item.to_dict() for item in scan_all(probe_auth=args.auth)], indent=2))
            return 0
        scans = scan_all()
        if args.command == "plan" and not args.commit:
            with tempfile.TemporaryDirectory(prefix="cli-platform-plan-") as scratch:
                return _plan(Platform(config, scans, state_dir=Path(scratch)), args)
        platform = Platform(config, scans)
        if args.command == "plan":
            return _plan(platform, args)
        if args.command == "variants":
            print(json.dumps([{"variant": state.variant.id, "available": state.available,
                               "reasons": state.reasons, "binary_path": state.binary_path,
                               "version": state.version, "fingerprint": state.fingerprint,
                               "settings": asdict(state.settings) if state.settings else None}
                              for state in platform.registry.all()], indent=2))
        elif args.command == "catalog":
            print(json.dumps([asdict(entry) for variant in config.variants
                              for entry in platform.catalog.entries(variant)], indent=2))
        elif args.command == "resolve":
            resolution = platform.router.resolve(RouteRequest(
                stage=args.stage, pin=_pin(args.pin), allow_fallback=args.allow_fallback))
            print("\n".join(resolution.explain()))
        return 0
    except PlatformError as exc:
        refusal = exc.to_dict()
        refusal["text"] = Translator(negotiate(args.locale, config.ui.locale if config else None).locale) \
            .code("error", exc.code)
        print(json.dumps(refusal, indent=2, ensure_ascii=False), file=sys.stderr)
        return 2


def _locale(args: argparse.Namespace, config) -> int:
    chosen = negotiate(args.locale, config.ui.locale)
    english = load_catalog("en")
    coverage = {locale: sorted(set(english) - set(load_catalog(locale))) for locale in SUPPORTED}
    print(json.dumps({
        "locale": chosen.locale, "source": chosen.source, "skipped": list(chosen.skipped),
        "chain": list(chosen.chain), "configured": config.ui.locale, "supported": list(SUPPORTED),
        "text": Translator(chosen.locale).t("cli.locale.summary", locale=chosen.locale, source=chosen.source),
        "missing_keys": coverage,
    }, indent=2, ensure_ascii=False))
    return 0


def _plan(platform: Platform, args: argparse.Namespace) -> int:
    plan = platform.open_session(SessionRequest(
        task_id="dry-run", run_id=args.run_id, stage=args.stage, cwd=str(Path.cwd())))
    print(json.dumps({
        "nickname": plan.nickname.nickname, "ref": plan.nickname.ref,
        "tmux_session": plan.tmux_session, "run_dir": str(plan.run_dir),
        "workbench_path": plan.workbench_path, "state": plan.state,
        "explain": plan.resolution.explain(),
        "lease": {"status": plan.lease.status, "blocked_on": plan.lease.blocked_on},
        "argv": plan.launch.loggable_argv, "env_set": sorted(plan.launch.env_set),
        "env_unset": plan.launch.env_unset,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

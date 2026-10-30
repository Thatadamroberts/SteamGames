"""Command-line entry point."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import date
from pathlib import Path

from .birthdays import load_birthdays, parse_birthday
from .finder import find_birthday_deals
from .notify import SentState, format_report, send_all
from .steam_api import SteamAPIError, SteamClient

DEFAULT_CONFIG = {
    "steam_api_key": "",
    "my_steam_id": "",
    "birthdays_file": "birthdays.json",
    "state_file": ".sent_deals.json",
    "country_code": "US",
    "days_ahead": 30,
    "min_discount": 1,
    "max_price": None,
    "notify": {
        "desktop": True,
        "discord_webhook": "",
        "ntfy_topic": "",
        "email": {"host": "", "port": 587, "username": "", "password": "", "from": "", "to": ""},
    },
}

# Environment variables override the config file (handy for cron / GitHub Actions secrets).
ENV_OVERRIDES = {
    "STEAM_API_KEY": ("steam_api_key",),
    "STEAM_ID": ("my_steam_id",),
    "STEAM_COUNTRY": ("country_code",),
    "DISCORD_WEBHOOK_URL": ("notify", "discord_webhook"),
    "NTFY_TOPIC": ("notify", "ntfy_topic"),
    "SMTP_HOST": ("notify", "email", "host"),
    "SMTP_PORT": ("notify", "email", "port"),
    "SMTP_USERNAME": ("notify", "email", "username"),
    "SMTP_PASSWORD": ("notify", "email", "password"),
    "EMAIL_TO": ("notify", "email", "to"),
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_config(path: Path) -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if path.exists():
        cfg = _merge(cfg, json.loads(path.read_text(encoding="utf-8")))
    for var, keys in ENV_OVERRIDES.items():
        if os.environ.get(var):
            node = cfg
            for k in keys[:-1]:
                node = node.setdefault(k, {})
            node[keys[-1]] = os.environ[var]
    return cfg


def _rel(cfg_path: Path, p: str) -> Path:
    path = Path(p).expanduser()
    return path if path.is_absolute() else cfg_path.parent / path


def make_client(cfg: dict) -> SteamClient:
    return SteamClient(api_key=cfg.get("steam_api_key"), country_code=cfg.get("country_code", "US"))


# ----- commands ------------------------------------------------------------

def cmd_check(args, cfg: dict, cfg_path: Path) -> int:
    birthdays_path = _rel(cfg_path, args.birthdays or cfg["birthdays_file"])
    if not birthdays_path.exists():
        print(f"No birthdays file at {birthdays_path}. Run `init` first.", file=sys.stderr)
        return 2
    entries = load_birthdays(birthdays_path)
    today = date.fromisoformat(args.today) if args.today else date.today()
    days_ahead = args.days if args.days is not None else int(cfg["days_ahead"])
    max_price = cfg.get("max_price")
    reports = find_birthday_deals(
        make_client(cfg), entries, today, days_ahead=days_ahead,
        min_discount=int(cfg.get("min_discount", 1)),
        max_price_cents=round(float(max_price) * 100) if max_price else None,
    )

    if not reports:
        print(f"No friend birthdays in the next {days_ahead} days "
              f"({len(entries)} birthdays on file).")
        return 0
    print(format_report(reports))

    if args.no_notify:
        return 0
    state = SentState(_rel(cfg_path, cfg["state_file"]))
    to_send = {r.steamid: r.deals for r in reports} if args.all else state.new_deals(reports)
    if not any(to_send.values()):
        print("\nNothing new to notify about.")
        return 0
    print()
    if send_all(cfg["notify"], format_report(reports, only_deals=to_send)) == 0:
        print("No notification channels succeeded or none are configured; "
              "see the `notify` section of config.json.")
        return 1
    state.mark(reports, to_send)
    state.save()
    return 0


def cmd_friends(args, cfg: dict, cfg_path: Path) -> int:
    client = make_client(cfg)
    me = client.resolve_steamid(args.steam_id or cfg["my_steam_id"] or _missing("my_steam_id"))
    for f in client.get_friends(me):
        print(f"{f.steamid}  {f.name}")
    return 0


def cmd_init(args, cfg: dict, cfg_path: Path) -> int:
    """Write config.json if missing and fill birthdays.json with your friend list."""
    if not cfg_path.exists():
        cfg_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        print(f"Created {cfg_path} - add your Steam API key and Steam ID, then re-run init.")
        if not (cfg.get("steam_api_key") and (args.steam_id or cfg.get("my_steam_id"))):
            return 0
    client = make_client(cfg)
    me = client.resolve_steamid(args.steam_id or cfg["my_steam_id"] or _missing("my_steam_id"))
    friends = client.get_friends(me)

    path = _rel(cfg_path, cfg["birthdays_file"])
    existing = []
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        existing = data.get("friends", []) if isinstance(data, dict) else data
    known = {e.get("steam") or e.get("steamid") for e in existing}
    added = [{"name": f.name, "steam": f.steamid, "birthday": ""}
             for f in friends if f.steamid not in known]
    path.write_text(json.dumps({"friends": existing + added}, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print(f"Added {len(added)} friend(s) to {path} ({len(existing)} already there).")
    print('Fill in each "birthday" as MM-DD (e.g. "03-14"); leave blank to skip someone.')
    return 0


def cmd_add(args, cfg: dict, cfg_path: Path) -> int:
    parse_birthday(args.birthday)  # validate early
    path = _rel(cfg_path, cfg["birthdays_file"])
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"friends": []}
    friends = data["friends"] if isinstance(data, dict) else data
    for f in friends:
        if args.steam in (f.get("steam"), f.get("steamid")) or (args.name and f.get("name") == args.name):
            f["birthday"] = args.birthday
            if args.name:
                f["name"] = args.name
            break
    else:
        friends.append({"name": args.name or args.steam, "steam": args.steam, "birthday": args.birthday})
    path.write_text(json.dumps({"friends": friends}, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print(f"Saved birthday {args.birthday} for {args.name or args.steam}.")
    return 0


def _missing(field: str):
    raise SteamAPIError(f"Set {field!r} in config.json (or pass --steam-id).")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="steam-gift-finder",
        description="Find wishlist games on sale for Steam friends with upcoming birthdays.")
    p.add_argument("-c", "--config", default="config.json", help="path to config.json")
    sub = p.add_subparsers(dest="command")

    c = sub.add_parser("check", help="find deals and send notifications (default)")
    c.add_argument("--days", type=int, help="look this many days ahead (default 30)")
    c.add_argument("--birthdays", help="path to birthdays file")
    c.add_argument("--today", help="pretend today is YYYY-MM-DD (for testing)")
    c.add_argument("--no-notify", action="store_true", help="only print, don't notify")
    c.add_argument("--all", action="store_true", help="re-notify deals already sent")

    i = sub.add_parser("init", help="create config and pre-fill birthdays.json with your friends")
    i.add_argument("--steam-id", help="your SteamID64, vanity name, or profile URL")

    f = sub.add_parser("friends", help="list your Steam friends and their SteamIDs")
    f.add_argument("--steam-id", help="your SteamID64, vanity name, or profile URL")

    a = sub.add_parser("add", help="add or update one friend's birthday")
    a.add_argument("steam", help="friend's SteamID64, vanity name, or profile URL")
    a.add_argument("birthday", help="MM-DD or YYYY-MM-DD")
    a.add_argument("--name", help="display name")

    argv = list(sys.argv[1:] if argv is None else argv)
    if not any(a in sub.choices for a in argv) and not {"-h", "--help"} & set(argv):
        # No subcommand given: default to `check`, inserted after any global options.
        i = 0
        while i < len(argv) and argv[i].startswith(("-c", "--config")):
            i += 2 if argv[i] in ("-c", "--config") else 1  # "-c path" vs "--config=path"
        argv.insert(i, "check")
    args = p.parse_args(argv)
    cfg_path = Path(args.config).expanduser().resolve()
    cfg = load_config(cfg_path)
    handler = {"check": cmd_check, "init": cmd_init, "friends": cmd_friends, "add": cmd_add}[args.command]
    try:
        return handler(args, cfg, cfg_path)
    except (SteamAPIError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

import json
import tempfile
import unittest
import urllib.parse
from datetime import date
from pathlib import Path
from unittest import mock

from steam_gift_finder import cli
from steam_gift_finder.birthdays import (BirthdayEntry, load_birthdays, next_birthday,
                                         parse_birthday, upcoming)
from steam_gift_finder.finder import find_birthday_deals
from steam_gift_finder.notify import SentState, format_report
from steam_gift_finder.steam_api import SteamClient

ALICE = "76561198000000001"
BOB = "76561198000000002"


class FakeSteam:
    """Answers Steam API URLs with canned JSON, mirroring the real response shapes."""

    def __init__(self):
        self.calls = []
        self.wishlists = {ALICE: [620, 570, 400, 999], BOB: []}
        self.store = {
            620: {"name": "Portal 2", "best_purchase_option": {
                "final_price_in_cents": "199", "original_price_in_cents": "999",
                "formatted_final_price": "$1.99", "formatted_original_price": "$9.99",
                "discount_pct": 80}},
            570: {"name": "Dota 2"},  # free: no purchase option, appdetails has no price
            400: {"name": "Portal", "best_purchase_option": {
                "final_price_in_cents": "999", "formatted_final_price": "$9.99"}},
            999: {"name": "Fallback Game"},  # price only available via appdetails
        }

    def __call__(self, url):
        self.calls.append(url)
        parsed = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        if "ResolveVanityURL" in url:
            ok = q["vanityurl"] == "alice"
            return {"response": {"success": 1, "steamid": ALICE} if ok else {"success": 42}}
        if "GetWishlist" in url:
            items = self.wishlists.get(q["steamid"], [])
            return {"response": {"items": [{"appid": a, "priority": i + 1} for i, a in enumerate(items)]}
                    if items else {}}
        if "GetItems" in url:
            ids = [i["appid"] for i in json.loads(q["input_json"])["ids"]]
            return {"response": {"store_items": [{"appid": a, "id": a, **self.store[a]} for a in ids]}}
        if "appdetails" in url:
            out = {}
            for a in map(int, q["appids"].split(",")):
                if a == 999:
                    out[str(a)] = {"success": True, "data": {"price_overview": {
                        "currency": "USD", "initial": 2000, "final": 1000, "discount_percent": 50,
                        "initial_formatted": "$20.00", "final_formatted": "$10.00"}}}
                else:
                    out[str(a)] = {"success": True, "data": []}
            return out
        if "GetFriendList" in url:
            return {"friendslist": {"friends": [{"steamid": BOB}, {"steamid": ALICE}]}}
        if "GetPlayerSummaries" in url:
            return {"response": {"players": [{"steamid": ALICE, "personaname": "Alice"},
                                             {"steamid": BOB, "personaname": "Bob"}]}}
        raise AssertionError(f"unexpected URL {url}")


class BirthdayMathTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_birthday("03-14"), (3, 14))
        self.assertEqual(parse_birthday("1990-12-01"), (12, 1))
        self.assertEqual(parse_birthday("2/29"), (2, 29))
        for bad in ("13-01", "02-30", "tomorrow", ""):
            with self.assertRaises(ValueError):
                parse_birthday(bad)

    def test_next_birthday_wraps_year(self):
        self.assertEqual(next_birthday(1, 5, date(2026, 12, 20)), date(2027, 1, 5))
        self.assertEqual(next_birthday(10, 30, date(2026, 10, 30)), date(2026, 10, 30))

    def test_leap_day(self):
        self.assertEqual(next_birthday(2, 29, date(2027, 2, 1)), date(2027, 2, 28))
        self.assertEqual(next_birthday(2, 29, date(2028, 2, 1)), date(2028, 2, 29))

    def test_upcoming_window(self):
        today = date(2026, 10, 30)
        entries = [BirthdayEntry("Soon", "a", 11, 15), BirthdayEntry("Today", "b", 10, 30),
                   BirthdayEntry("Edge", "c", 11, 29), BirthdayEntry("Late", "d", 12, 1),
                   BirthdayEntry("Past", "e", 10, 29)]
        self.assertEqual([e.name for e in upcoming(entries, today, 30)], ["Today", "Soon", "Edge"])

    def test_load_skips_blank(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "b.json"
            p.write_text(json.dumps({"friends": [
                {"name": "A", "steam": ALICE, "birthday": "11-05"},
                {"name": "B", "steam": BOB, "birthday": ""}]}))
            self.assertEqual([e.name for e in load_birthdays(p)], ["A"])


class FinderTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSteam()
        self.client = SteamClient(api_key="k", fetch=self.fake)
        self.today = date(2026, 10, 30)
        self.entries = [BirthdayEntry("Alice", "alice", 11, 10),
                        BirthdayEntry("Bob", BOB, 11, 2),
                        BirthdayEntry("Carol", "76561198000000003", 6, 1)]

    def test_finds_sale_games_for_upcoming_birthdays(self):
        reports = find_birthday_deals(self.client, self.entries, self.today)
        self.assertEqual([r.entry.name for r in reports], ["Bob", "Alice"])
        bob, alice = reports
        self.assertEqual(bob.error, "wishlist is empty or private")
        self.assertEqual(alice.steamid, ALICE)
        self.assertEqual([(d.name, d.discount_pct) for d in alice.deals],
                         [("Portal 2", 80), ("Fallback Game", 50)])
        text = format_report(reports)
        self.assertIn("Alice's birthday is in 11 days", text)
        self.assertIn("Portal 2: $1.99 (was $9.99, -80%) https://store.steampowered.com/app/620", text)
        self.assertFalse(any("key=k" in c and "appdetails" in c for c in self.fake.calls))

    def test_filters(self):
        reports = find_birthday_deals(self.client, self.entries, self.today,
                                      min_discount=60, max_price_cents=500)
        self.assertEqual([d.name for d in reports[1].deals], ["Portal 2"])

    def test_dedupe_state(self):
        reports = find_birthday_deals(self.client, self.entries, self.today)
        with tempfile.TemporaryDirectory() as d:
            state = SentState(Path(d) / "s.json")
            new = state.new_deals(reports)
            self.assertEqual(len(new[ALICE]), 2)
            state.mark(reports, new)
            state.save()
            self.assertEqual(SentState(Path(d) / "s.json").new_deals(reports)[ALICE], [])
            # A further price drop counts as a new deal.
            self.fake.store[620]["best_purchase_option"]["final_price_in_cents"] = "99"
            reports = find_birthday_deals(self.client, self.entries, self.today)
            self.assertEqual([x.name for x in state.new_deals(reports)[ALICE]], ["Portal 2"])


class CliTests(unittest.TestCase):
    def test_init_then_check(self):
        fake = FakeSteam()
        real_client = cli.make_client
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(cli, "make_client", lambda cfg: SteamClient("k", fetch=fake)), \
                mock.patch.object(cli, "send_all", return_value=1) as send, \
                mock.patch.dict("os.environ", {}, clear=True):
            cfg = Path(d) / "config.json"
            cfg.write_text(json.dumps({"steam_api_key": "k", "my_steam_id": "alice",
                                       "notify": {"desktop": False}}))
            self.assertEqual(cli.main(["-c", str(cfg), "init"]), 0)
            b = json.loads((Path(d) / "birthdays.json").read_text())
            self.assertEqual([f["name"] for f in b["friends"]], ["Alice", "Bob"])
            self.assertEqual(cli.main(["-c", str(cfg), "add", ALICE, "11-10"]), 0)

            self.assertEqual(cli.main(["-c", str(cfg), "--today", "2026-10-30"]), 0)
            self.assertEqual(send.call_count, 1)
            self.assertIn("Portal 2", send.call_args.args[1])
            # Second run: nothing new, so no notification.
            self.assertEqual(cli.main(["-c", str(cfg), "check", "--today", "2026-10-30"]), 0)
            self.assertEqual(send.call_count, 1)
        self.assertIs(cli.make_client, real_client)

    def test_env_overrides_do_not_leak_into_defaults(self):
        with mock.patch.dict("os.environ", {"STEAM_API_KEY": "secret"}):
            self.assertEqual(cli.load_config(Path("/nonexistent/config.json"))["steam_api_key"], "secret")
        self.assertEqual(cli.DEFAULT_CONFIG["steam_api_key"], "")


if __name__ == "__main__":
    unittest.main()

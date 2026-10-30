"""Core logic: upcoming birthdays -> wishlists -> games on sale."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .birthdays import BirthdayEntry, upcoming
from .steam_api import GameDeal, SteamAPIError, SteamClient


@dataclass
class FriendReport:
    entry: BirthdayEntry
    steamid: str
    birthday: date
    days_until: int
    deals: list[GameDeal] = field(default_factory=list)
    wishlist_size: int = 0
    error: str | None = None


def find_birthday_deals(client: SteamClient, entries: list[BirthdayEntry], today: date,
                        days_ahead: int = 30, min_discount: int = 1,
                        max_price_cents: int | None = None) -> list[FriendReport]:
    reports: list[FriendReport] = []
    for entry in upcoming(entries, today, days_ahead):
        report = FriendReport(entry=entry, steamid="", birthday=entry.next_birthday(today),
                              days_until=entry.days_until(today))
        reports.append(report)
        try:
            report.steamid = client.resolve_steamid(entry.steam)
            appids = client.get_wishlist_appids(report.steamid)
            report.wishlist_size = len(appids)
            if not appids:
                report.error = "wishlist is empty or private"
                continue
            prices = client.get_prices(appids)
        except SteamAPIError as e:
            report.error = str(e)
            continue
        deals = [
            prices[a] for a in appids
            if a in prices and prices[a].on_sale
            and prices[a].discount_pct >= min_discount
            and (max_price_cents is None or prices[a].final_cents <= max_price_cents)
        ]
        # Biggest discounts first; wishlist priority breaks ties (stable sort).
        report.deals = sorted(deals, key=lambda d: -d.discount_pct)
    return reports

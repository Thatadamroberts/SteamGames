"""Birthday file parsing and upcoming-birthday math."""

from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass
class BirthdayEntry:
    name: str
    steam: str  # SteamID64, vanity name, or profile URL
    month: int
    day: int

    def next_birthday(self, today: date) -> date:
        return next_birthday(self.month, self.day, today)

    def days_until(self, today: date) -> int:
        return (self.next_birthday(today) - today).days


def parse_birthday(text: str) -> tuple[int, int]:
    """Parse 'MM-DD' or 'YYYY-MM-DD' (also accepts '/' separators)."""
    text = text.strip()
    m = re.fullmatch(r"(?:\d{4}[-/])?(\d{1,2})[-/](\d{1,2})", text)
    if not m:
        raise ValueError(f"Birthday {text!r} must look like MM-DD or YYYY-MM-DD")
    month, day = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12 or not 1 <= day <= calendar.monthrange(2024, month)[1]:
        raise ValueError(f"Birthday {text!r} is not a real date")
    return month, day


def _birthday_in_year(month: int, day: int, year: int) -> date:
    # Feb 29 birthdays are celebrated on Feb 28 in non-leap years.
    if month == 2 and day == 29 and not calendar.isleap(year):
        return date(year, 2, 28)
    return date(year, month, day)


def next_birthday(month: int, day: int, today: date) -> date:
    """The next occurrence of a birthday on or after today."""
    this_year = _birthday_in_year(month, day, today.year)
    if this_year >= today:
        return this_year
    return _birthday_in_year(month, day, today.year + 1)


def load_birthdays(path: Path) -> list[BirthdayEntry]:
    """Load the birthdays file. Entries with a blank birthday are skipped."""
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("friends", data) if isinstance(data, dict) else data
    out: list[BirthdayEntry] = []
    for i, raw in enumerate(entries):
        bday = (raw.get("birthday") or "").strip()
        if not bday:
            continue
        steam = (raw.get("steam") or raw.get("steamid") or "").strip()
        if not steam:
            raise ValueError(f"Entry #{i + 1} in {path} is missing a 'steam' value")
        month, day = parse_birthday(bday)
        out.append(BirthdayEntry(name=raw.get("name") or steam, steam=steam,
                                 month=month, day=day))
    return out


def upcoming(entries: list[BirthdayEntry], today: date, days_ahead: int) -> list[BirthdayEntry]:
    """Entries whose next birthday falls within `days_ahead` days, soonest first."""
    soon = [e for e in entries if e.days_until(today) <= days_ahead]
    return sorted(soon, key=lambda e: e.days_until(today))

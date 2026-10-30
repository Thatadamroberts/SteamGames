"""Message formatting, de-duplication, and notification channels."""

from __future__ import annotations

import json
import platform
import shutil
import smtplib
import subprocess
import urllib.request
from email.message import EmailMessage
from pathlib import Path

from .finder import FriendReport
from .steam_api import USER_AGENT, GameDeal


def _when(days: int) -> str:
    return {0: "TODAY", 1: "tomorrow"}.get(days, f"in {days} days")


def format_deal(deal: GameDeal) -> str:
    return (f"{deal.name}: {deal.final_formatted} (was {deal.original_formatted}, "
            f"-{deal.discount_pct}%) {deal.store_url}")


def format_report(reports: list[FriendReport], only_deals: dict[str, list[GameDeal]] | None = None) -> str:
    """Human-readable summary. `only_deals` restricts which deals are listed per friend."""
    lines: list[str] = []
    for r in reports:
        deals = r.deals if only_deals is None else only_deals.get(r.steamid, [])
        if only_deals is not None and not deals:
            continue
        header = f"{r.entry.name}'s birthday is {_when(r.days_until)} ({r.birthday:%a %b %d})"
        lines.append(header)
        if r.error:
            lines.append(f"  (couldn't check wishlist: {r.error})")
        elif not deals:
            lines.append(f"  No wishlist games on sale right now ({r.wishlist_size} on wishlist).")
        for d in deals:
            lines.append(f"  - {format_deal(d)}")
        lines.append("")
    return "\n".join(lines).rstrip()


# ----- de-duplication ------------------------------------------------------

class SentState:
    """Remembers which deals were already notified so scheduled runs don't repeat them.

    A deal is re-sent if its price changes or it's for a different year's birthday.
    """

    def __init__(self, path: Path):
        self.path = path
        try:
            self.sent: set[str] = set(json.loads(path.read_text(encoding="utf-8")))
        except (FileNotFoundError, ValueError):
            self.sent = set()

    @staticmethod
    def key(report: FriendReport, deal: GameDeal) -> str:
        return f"{report.steamid}:{report.birthday.isoformat()}:{deal.appid}:{deal.final_cents}"

    def new_deals(self, reports: list[FriendReport]) -> dict[str, list[GameDeal]]:
        return {r.steamid: [d for d in r.deals if self.key(r, d) not in self.sent]
                for r in reports}

    def mark(self, reports: list[FriendReport], deals: dict[str, list[GameDeal]]) -> None:
        for r in reports:
            for d in deals.get(r.steamid, []):
                self.sent.add(self.key(r, d))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sorted(self.sent), indent=1), encoding="utf-8")


# ----- channels ------------------------------------------------------------

TITLE = "Steam birthday gift deals"


def _post(url: str, body: bytes, headers: dict[str, str]) -> None:
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"User-Agent": USER_AGENT, **headers})
    with urllib.request.urlopen(req, timeout=20):
        pass


def send_discord(webhook_url: str, text: str) -> None:
    # Discord messages are capped at 2000 characters; split on line boundaries.
    chunk = ""
    for line in [f"**{TITLE}**"] + text.splitlines():
        if len(chunk) + len(line) + 1 > 1900:
            _post(webhook_url, json.dumps({"content": chunk}).encode(),
                  {"Content-Type": "application/json"})
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        _post(webhook_url, json.dumps({"content": chunk}).encode(),
              {"Content-Type": "application/json"})


def send_ntfy(topic: str, text: str, server: str = "https://ntfy.sh") -> None:
    url = topic if topic.startswith("http") else f"{server.rstrip('/')}/{topic}"
    _post(url, text.encode("utf-8"), {"Title": TITLE, "Tags": "birthday,video_game"})


def send_email(cfg: dict, text: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = TITLE
    msg["From"] = cfg.get("from") or cfg["username"]
    msg["To"] = cfg["to"]
    msg.set_content(text)
    port = int(cfg.get("port", 587))
    if port == 465:
        server = smtplib.SMTP_SSL(cfg["host"], port, timeout=30)
    else:
        server = smtplib.SMTP(cfg["host"], port, timeout=30)
        server.starttls()
    with server:
        if cfg.get("username"):
            server.login(cfg["username"], cfg["password"])
        server.send_message(msg)


def send_desktop(text: str) -> None:
    """Best-effort native desktop notification."""
    summary = text if len(text) < 250 else text[:247] + "..."
    system = platform.system()
    if system == "Linux" and shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "Steam Gift Finder", TITLE, summary], check=False)
    elif system == "Darwin":
        script = f"display notification {json.dumps(summary)} with title {json.dumps(TITLE)}"
        subprocess.run(["osascript", "-e", script], check=False)
    elif system == "Windows":
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
            f"$n.ShowBalloonTip(10000,{_ps_str(TITLE)},{_ps_str(summary)},'Info');"
            "Start-Sleep -Seconds 10;$n.Dispose()"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)
    else:
        raise RuntimeError("no desktop notifier available on this system")


def _ps_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def send_all(notify_cfg: dict, text: str, log=print) -> int:
    """Send `text` to every configured channel. Returns the number that succeeded."""
    channels = []
    if notify_cfg.get("desktop"):
        channels.append(("desktop", lambda: send_desktop(text)))
    if notify_cfg.get("discord_webhook"):
        channels.append(("Discord", lambda: send_discord(notify_cfg["discord_webhook"], text)))
    if notify_cfg.get("ntfy_topic"):
        channels.append(("ntfy", lambda: send_ntfy(notify_cfg["ntfy_topic"], text,
                                                    notify_cfg.get("ntfy_server", "https://ntfy.sh"))))
    email = notify_cfg.get("email") or {}
    if email.get("host") and email.get("to"):
        channels.append(("email", lambda: send_email(email, text)))
    ok = 0
    for name, send in channels:
        try:
            send()
            ok += 1
            log(f"Sent {name} notification.")
        except Exception as e:  # one broken channel shouldn't stop the others
            log(f"Failed to send {name} notification: {e}")
    return ok

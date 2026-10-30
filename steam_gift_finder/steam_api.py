"""Thin wrapper around the Steam Web API and Store API (stdlib only)."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable

API_BASE = "https://api.steampowered.com"
STORE_BASE = "https://store.steampowered.com"
USER_AGENT = "steam-gift-finder/1.0"

# A function that takes a URL and returns parsed JSON. Swappable for tests.
Fetcher = Callable[[str], Any]


class SteamAPIError(RuntimeError):
    pass


def http_get_json(url: str, retries: int = 3, timeout: float = 20.0) -> Any:
    """GET a URL and parse JSON, retrying on rate limits and transient errors."""
    delay = 2.0
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            if e.code in (401, 403):
                raise SteamAPIError(
                    f"Steam refused the request (HTTP {e.code}). Check your API key, "
                    "and that the profile / friend list / wishlist is public."
                ) from e
            raise SteamAPIError(f"HTTP {e.code} for {_redact(url)}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise SteamAPIError(f"Network error for {_redact(url)}: {e}") from e
    raise SteamAPIError(f"Gave up on {_redact(url)}")  # pragma: no cover


def _redact(url: str) -> str:
    return re.sub(r"key=[^&]+", "key=***", url)


def _chunks(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


@dataclass
class Friend:
    steamid: str
    name: str
    profile_url: str = ""


@dataclass
class GameDeal:
    appid: int
    name: str
    final_cents: int
    original_cents: int
    discount_pct: int
    final_formatted: str
    original_formatted: str

    @property
    def on_sale(self) -> bool:
        return self.discount_pct > 0 and self.final_cents < self.original_cents

    @property
    def store_url(self) -> str:
        return f"{STORE_BASE}/app/{self.appid}"


class SteamClient:
    def __init__(self, api_key: str | None = None, country_code: str = "US",
                 language: str = "english", fetch: Fetcher = http_get_json):
        self.api_key = api_key or None
        self.country_code = country_code.upper()
        self.language = language
        self._fetch = fetch

    def _api(self, path: str, params: dict[str, Any], need_key: bool = False) -> Any:
        params = dict(params)
        if self.api_key:
            params["key"] = self.api_key
        elif need_key:
            raise SteamAPIError(
                "A Steam Web API key is required for this call. Get one at "
                "https://steamcommunity.com/dev/apikey and set STEAM_API_KEY."
            )
        return self._fetch(f"{API_BASE}/{path}/?{urllib.parse.urlencode(params)}")

    # ----- identity -------------------------------------------------------

    def resolve_steamid(self, ref: str) -> str:
        """Accept a SteamID64, vanity name, or profile URL; return a SteamID64."""
        ref = ref.strip().rstrip("/")
        m = re.search(r"steamcommunity\.com/(profiles|id)/([^/?#]+)", ref)
        if m:
            kind, value = m.groups()
            if kind == "profiles":
                return value
            ref = value
        if re.fullmatch(r"7656\d{13}", ref):
            return ref
        data = self._api("ISteamUser/ResolveVanityURL/v1", {"vanityurl": ref}, need_key=True)
        resp = data.get("response", {})
        if resp.get("success") != 1 or not resp.get("steamid"):
            raise SteamAPIError(f"Could not resolve Steam profile {ref!r}")
        return resp["steamid"]

    # ----- friends --------------------------------------------------------

    def get_friend_ids(self, steamid: str) -> list[str]:
        data = self._api("ISteamUser/GetFriendList/v1",
                         {"steamid": steamid, "relationship": "friend"}, need_key=True)
        return [f["steamid"] for f in data.get("friendslist", {}).get("friends", [])]

    def get_player_summaries(self, steamids: list[str]) -> dict[str, Friend]:
        out: dict[str, Friend] = {}
        for batch in _chunks(steamids, 100):
            data = self._api("ISteamUser/GetPlayerSummaries/v2",
                             {"steamids": ",".join(batch)}, need_key=True)
            for p in data.get("response", {}).get("players", []):
                out[p["steamid"]] = Friend(p["steamid"], p.get("personaname", p["steamid"]),
                                           p.get("profileurl", ""))
        return out

    def get_friends(self, steamid: str) -> list[Friend]:
        ids = self.get_friend_ids(steamid)
        summaries = self.get_player_summaries(ids)
        friends = [summaries.get(i, Friend(i, i)) for i in ids]
        return sorted(friends, key=lambda f: f.name.lower())

    # ----- wishlist & prices ----------------------------------------------

    def get_wishlist_appids(self, steamid: str) -> list[int]:
        """Return wishlist app IDs, highest priority first. Empty if private."""
        data = self._api("IWishlistService/GetWishlist/v1", {"steamid": steamid})
        items = data.get("response", {}).get("items", [])
        items.sort(key=lambda i: i.get("priority", 0) or 1_000_000)
        return [int(i["appid"]) for i in items if "appid" in i]

    def get_prices(self, appids: list[int]) -> dict[int, GameDeal]:
        """Look up names and current prices for apps in the configured country."""
        deals: dict[int, GameDeal] = {}
        missing_price: list[int] = []
        names: dict[int, str] = {}
        for batch in _chunks(appids, 50):
            payload = {
                "ids": [{"appid": a} for a in batch],
                "context": {"language": self.language, "country_code": self.country_code},
                "data_request": {"include_basic_info": False},
            }
            data = self._api("IStoreBrowseService/GetItems/v1",
                             {"input_json": json.dumps(payload, separators=(",", ":"))})
            for item in data.get("response", {}).get("store_items", []):
                appid = int(item.get("appid") or item.get("id") or 0)
                if not appid:
                    continue
                names[appid] = item.get("name") or f"App {appid}"
                deal = _deal_from_purchase_option(appid, names[appid],
                                                  item.get("best_purchase_option"))
                if deal:
                    deals[appid] = deal
                else:
                    missing_price.append(appid)
        # Fall back to the classic appdetails endpoint for anything without a price.
        if missing_price:
            deals.update(self._get_prices_appdetails(missing_price, names))
        return deals

    def _get_prices_appdetails(self, appids: list[int], names: dict[int, str]) -> dict[int, GameDeal]:
        deals: dict[int, GameDeal] = {}
        for batch in _chunks(appids, 50):
            params = urllib.parse.urlencode({
                "appids": ",".join(map(str, batch)),
                "cc": self.country_code, "filters": "price_overview",
            })
            data = self._fetch(f"{STORE_BASE}/api/appdetails?{params}") or {}
            for appid_str, entry in data.items():
                po = (entry or {}).get("data") or {}
                po = po.get("price_overview") if isinstance(po, dict) else None
                if not entry.get("success") or not po:
                    continue
                appid = int(appid_str)
                deals[appid] = GameDeal(
                    appid=appid, name=names.get(appid, f"App {appid}"),
                    final_cents=int(po.get("final", 0)),
                    original_cents=int(po.get("initial", 0)),
                    discount_pct=int(po.get("discount_percent", 0)),
                    final_formatted=po.get("final_formatted", ""),
                    original_formatted=po.get("initial_formatted", ""),
                )
        return deals


def _deal_from_purchase_option(appid: int, name: str, opt: dict | None) -> GameDeal | None:
    if not opt or "final_price_in_cents" not in opt:
        return None
    final = int(opt["final_price_in_cents"])
    original = int(opt.get("original_price_in_cents") or final)
    final_fmt = opt.get("formatted_final_price", "")
    return GameDeal(
        appid=appid, name=name, final_cents=final, original_cents=original,
        discount_pct=int(opt.get("discount_pct") or 0),
        final_formatted=final_fmt,
        original_formatted=opt.get("formatted_original_price") or final_fmt,
    )

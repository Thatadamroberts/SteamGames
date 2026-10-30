# Steam Birthday Gift Finder

Checks which of your Steam friends have a birthday coming up (within 30 days by
default), looks through their Steam wishlists, and notifies you about the games
that are on sale right now, so you can grab a discounted gift.

```
Alice's birthday is in 13 days (Thu Nov 12)
  - Portal 2: $1.99 (was $9.99, -80%) https://store.steampowered.com/app/620
  - Hades: $12.49 (was $24.99, -50%) https://store.steampowered.com/app/1145360

Bob's birthday is in 27 days (Thu Nov 26)
  No wishlist games on sale right now (14 on wishlist).
```

Pure Python 3.9+ with only the standard library, so there's nothing to install.

## Where the birthdays come from

**Steam doesn't make birthdays available.** Profiles have no birthday field
and no API returns one, so you enter them yourself once in `birthdays.json`.
The `init` command pre-fills that file with your whole friend list, so all you
type is the dates.

## Setup

1. **Get a Steam Web API key** at <https://steamcommunity.com/dev/apikey>
   (any domain name works).
2. **Create your config and friend list:**
   ```bash
   python -m steam_gift_finder init        # creates config.json
   # edit config.json: set steam_api_key and my_steam_id
   #   (my_steam_id can be your SteamID64, vanity name, or profile URL)
   python -m steam_gift_finder init        # fills birthdays.json with your friends
   ```
3. **Add birthdays.** Edit `birthdays.json` and fill in `"birthday"` as `MM-DD`
   (or `YYYY-MM-DD`). Leave it blank for anyone you want to skip. You can also
   add birthdays one at a time:
   ```bash
   python -m steam_gift_finder add 76561198000000001 11-12 --name Alice
   ```
4. **Run it:**
   ```bash
   python -m steam_gift_finder               # same as `check`
   python -m steam_gift_finder check --no-notify   # just print
   python -m steam_gift_finder check --days 14     # look 2 weeks ahead
   ```

Your friends' **wishlists must be public** (Steam's default is public when the
profile is public). Private wishlists show up as "couldn't check wishlist".

## Notifications

Results are always printed. Under `notify` in `config.json` you can turn on:

| Channel | Config | Notes |
|---|---|---|
| Desktop | `"desktop": true` | notify-send (Linux), Notification Center (macOS), balloon tip (Windows) |
| Phone push | `"ntfy_topic": "my-secret-topic"` | Install the [ntfy](https://ntfy.sh) app and subscribe to the same topic |
| Discord | `"discord_webhook": "https://discord.com/api/webhooks/..."` | Server Settings > Integrations > Webhooks |
| Email | `"email": {"host": "smtp.gmail.com", "port": 587, "username": "...", "password": "...", "to": "..."}` | For Gmail, use an [app password](https://myaccount.google.com/apppasswords) |

The app remembers what it already told you about (`.sent_deals.json`), so a
daily schedule only pings you about **new** deals: a game newly on sale, a
deeper discount, or the same game for next year's birthday. Use
`check --all` to resend everything.

### Other settings

| Key | Default | Meaning |
|---|---|---|
| `days_ahead` | `30` | How far ahead to look for birthdays |
| `country_code` | `"US"` | Store region for prices/currency (e.g. `GB`, `DE`, `CA`) |
| `min_discount` | `1` | Only report games discounted at least this many percent |
| `max_price` | `null` | Only report games at or below this sale price (e.g. `20`) |

Any setting can also come from environment variables: `STEAM_API_KEY`,
`STEAM_ID`, `STEAM_COUNTRY`, `DISCORD_WEBHOOK_URL`, `NTFY_TOPIC`, `SMTP_HOST`,
`SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `EMAIL_TO`.

## Running it automatically

**On your computer (cron, macOS/Linux):** run daily at 6pm:
```
0 18 * * * cd /path/to/SteamGames && /usr/bin/python3 -m steam_gift_finder
```
On Windows, use Task Scheduler to run `python -m steam_gift_finder` in this folder.

**On GitHub Actions (no computer needed):** `.github/workflows/birthday-check.yml`
runs daily. Add repository secrets `STEAM_API_KEY`, `BIRTHDAYS_JSON` (paste
the full contents of your `birthdays.json`), and the secrets for at least one
notification channel, such as `NTFY_TOPIC` or `DISCORD_WEBHOOK_URL`. The
workflow skips itself until `BIRTHDAYS_JSON` is set. Keep birthdays in secrets
rather than committing the file: `config.json` and `birthdays.json` are
git-ignored so you don't publish your friends' birthdays or your keys.

## Development

```bash
python -m unittest discover -s tests -v
```

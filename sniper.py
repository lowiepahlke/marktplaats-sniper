"""Watches Marktplaats for new ads matching settings.toml and sends a Telegram
alert (with photo) the moment one appears.

    python sniper.py           start watching
    python sniper.py --check   show what each search finds right now, send nothing
    python sniper.py --setup   connect Telegram (asks for the bot token, finds your chat id)
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import random
import sys
import time

import requests

import mp_core
from mp_core import Ad, Search, Settings, SettingsError

STATE_FILE = mp_core.HERE / "seen.json"
IDS_KEPT_PER_SEARCH = 1000
LISTINGS_PER_POLL = 50
MAX_BACKOFF_SECONDS = 15 * 60

log = logging.getLogger("sniper")


# --------------------------------------------------------------------------
# Seen-ads state
# --------------------------------------------------------------------------

def signature(search: Search, postcode: str) -> str:
    """Changing any of these changes which ads Marktplaats returns, so the search
    gets a fresh silent seeding run instead of alerting on its whole backlog."""
    loc = postcode if search.distance_km else ""
    return f"{search.query}|{search.min_price}|{search.max_price}|{search.distance_km}|{loc}"


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("%s was unreadable, starting fresh (no alerts until the next new ad).", STATE_FILE.name)
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)  # atomic: a crash mid-write can't corrupt seen.json


def mark_seen(entry: dict, ad_id: str) -> None:
    entry["ids"].append(ad_id)
    del entry["ids"][:-IDS_KEPT_PER_SEARCH]


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

class TelegramError(Exception):
    def __init__(self, message: str, retry: bool):
        super().__init__(message)
        self.retry = retry  # False = this message will never go through, don't keep trying


def telegram_call(token: str, method: str, **payload) -> dict:
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=30)
    except requests.RequestException as e:
        raise TelegramError(f"can't reach Telegram ({e.__class__.__name__})", retry=True) from None
    try:
        data = r.json()
    except ValueError:
        raise TelegramError(f"Telegram answered HTTP {r.status_code}", retry=True) from None
    if not data.get("ok"):
        desc = data.get("description", f"HTTP {r.status_code}")
        if r.status_code == 429:
            time.sleep(int((data.get("parameters") or {}).get("retry_after", 5)))
        raise TelegramError(desc, retry=r.status_code == 429 or r.status_code >= 500)
    return data["result"]


def format_alert(ad: Ad, search: Search) -> str:
    e = html.escape
    where = ad.city or "?"
    if ad.distance_km is not None:
        where += f" ({ad.distance_km} km)"
    lines = [
        f"🎯 <b>{e(search.name)}</b>",
        f"<b>{e(ad.title[:200])}</b>",
        f"💶 {e(ad.price_label)}   📍 {e(where)}",
    ]
    if ad.condition:
        lines.append(f"🏷 {e(ad.condition)}")
    stars = mp_core.found_words(ad.text, search.highlight_words)
    if stars:
        lines.append(f"⭐ {e(', '.join(stars))}")
    if ad.is_shop:
        lines.append("🏪 shop / business")
    return "\n".join(lines)


def send_alert(settings: Settings, ad: Ad, search: Search) -> None:
    caption = format_alert(ad, search)
    button = {"inline_keyboard": [[{"text": "Open on Marktplaats", "url": ad.url}]]}
    chat = settings.chat_id
    if settings.send_photos and ad.photo_url:
        try:
            telegram_call(settings.bot_token, "sendPhoto", chat_id=chat, photo=ad.photo_url,
                          caption=caption, parse_mode="HTML", reply_markup=button)
            return
        except TelegramError as err:
            if err.retry:
                raise
            # Telegram sometimes can't fetch a photo URL; the text alert matters more.
            log.debug("Photo failed (%s), sending text only", err)
    telegram_call(settings.bot_token, "sendMessage", chat_id=chat, text=caption + f"\n{ad.url}",
                  parse_mode="HTML", reply_markup=button, disable_web_page_preview=True)


# --------------------------------------------------------------------------
# Watching
# --------------------------------------------------------------------------

def print_hit(ad: Ad, search: Search) -> None:
    where = f"{ad.city} ({ad.distance_km} km)" if ad.distance_km is not None else ad.city
    log.info("NEW  [%s] %s | %s | %s\n           %s", search.name, ad.title, ad.price_label, where, ad.url)


def poll_search(session: requests.Session, settings: Settings, search: Search, state: dict) -> int:
    """Check one search, alert on new hits. Returns how many alerts went out."""
    sig = signature(search, settings.postcode)
    entry = state.get(search.name)
    ads = mp_core.fetch_ads(session, search, settings.postcode, limit=LISTINGS_PER_POLL)

    if entry is None or entry.get("signature") != sig:
        state[search.name] = {"signature": sig, "ids": [ad.id for ad in reversed(ads)]}
        log.info("[%s] Started watching. %d current ads noted; you'll only hear about new ones.",
                 search.name, len(ads))
        return 0

    seen = set(entry["ids"])
    sent = 0
    for ad in reversed([ad for ad in ads if ad.id not in seen]):  # oldest new ad first
        reason = mp_core.skip_reason(ad, search)
        if reason:
            log.info("skip [%s] %s  (%s)", search.name, ad.title, reason)
            mark_seen(entry, ad.id)
            continue
        print_hit(ad, search)
        if settings.telegram_enabled:
            try:
                send_alert(settings, ad, search)
            except TelegramError as err:
                if err.retry:
                    # Not marked seen, so it's sent on the next round instead of lost.
                    log.warning("Telegram failed (%s); will retry next round.", err)
                    break
                log.error("Telegram refused this alert (%s). Skipping it.", err)
        mark_seen(entry, ad.id)
        sent += 1
    return sent


def watch(settings: Settings) -> None:
    session = mp_core.new_session()
    state = load_state()
    # Forget searches that were removed from settings.toml.
    for name in set(state) - {s.name for s in settings.searches}:
        del state[name]

    log.info("Watching %d search(es), checking every ~%ds. Close this window to stop.",
             len(settings.searches), settings.poll_interval_seconds)
    for s in settings.searches:
        price = f"€{s.min_price or 0}-{s.max_price or 'any'}"
        area = f"{s.distance_km} km around {settings.postcode}" if s.distance_km else "whole country"
        log.info("  • %s: \"%s\", %s, %s", s.name, s.query, price, area)
    if not settings.telegram_enabled:
        log.info("Telegram isn't set up (run setup_telegram.bat), so new ads only show up here.")

    failures = 0
    while True:
        round_ok = False
        for i, search in enumerate(settings.searches):
            if i:
                time.sleep(2)  # space out requests when there are several searches
            try:
                poll_search(session, settings, search, state)
                round_ok = True
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else "?"
                hint = " (probably checking too often, slowing down)" if code in (403, 429) else ""
                log.warning("[%s] Marktplaats said HTTP %s%s.", search.name, code, hint)
            except requests.RequestException as e:
                log.warning("[%s] Couldn't reach Marktplaats (%s). Internet down?",
                            search.name, e.__class__.__name__)
            except Exception:
                log.exception("[%s] Unexpected error, carrying on.", search.name)
        save_state(state)

        failures = 0 if round_ok else failures + 1
        wait = settings.poll_interval_seconds * (2 ** min(failures, 5))
        wait = min(wait, MAX_BACKOFF_SECONDS) * random.uniform(0.85, 1.15)  # jitter: look less bot-like
        if failures:
            log.info("Trying again in %d seconds.", wait)
        time.sleep(wait)


def check(settings: Settings) -> None:
    """Dry run: show what each search would alert on right now."""
    session = mp_core.new_session()
    for search in settings.searches:
        ads = mp_core.fetch_ads(session, search, settings.postcode, limit=LISTINGS_PER_POLL)
        hits = [ad for ad in ads if not mp_core.skip_reason(ad, search)]
        print(f"\n=== {search.name}: {len(ads)} newest ads, {len(hits)} pass your filters ===")
        for ad in ads:
            reason = mp_core.skip_reason(ad, search)
            mark = "  HIT " if not reason else "  --- "
            extra = "" if not reason else f"   (skipped: {reason})"
            print(f"{mark}{ad.price_label:>18}  {ad.title[:70]}{extra}")
        time.sleep(1)
    print("\nNothing was sent and nothing was marked as seen.")


# --------------------------------------------------------------------------
# Telegram setup wizard
# --------------------------------------------------------------------------

def setup_telegram() -> None:
    print(
        "\nTELEGRAM SETUP\n"
        "--------------\n"
        "1. Open Telegram and search for @BotFather (blue check mark).\n"
        "2. Send it:  /newbot\n"
        "3. Pick a name and a username ending in 'bot' (like my_sniper_bot).\n"
        "4. BotFather replies with a token like 123456789:AAE...  Copy it.\n"
    )
    while True:
        token = input("Paste the token here and press Enter: ").strip()
        try:
            bot = telegram_call(token, "getMe")
            break
        except TelegramError as err:
            print(f"  That didn't work ({err}). Check you copied the whole token.\n")

    username = bot.get("username")
    print(f"\nFound your bot: @{username}")
    print(f"5. Now open https://t.me/{username} (or search @{username} in Telegram),")
    print("   press START and send it any message, like 'hi'.\n")
    print("Waiting for your message (up to 3 minutes)...")

    chat_id = None
    deadline = time.time() + 180
    offset = None
    while time.time() < deadline and chat_id is None:
        try:
            updates = telegram_call(token, "getUpdates", timeout=20, offset=offset)
        except TelegramError as err:
            print(f"  Problem talking to Telegram: {err}")
            time.sleep(3)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            if msg:
                chat_id = str(msg["chat"]["id"])
                print(f"Got it! Message from {msg['chat'].get('first_name', 'you')}.")
    if chat_id is None:
        print("\nNo message arrived. Run setup_telegram.bat again and send the bot a message.")
        return

    mp_core.save_telegram_settings(token, chat_id)
    telegram_call(token, "sendMessage", chat_id=chat_id,
                  text="✅ Marktplaats Sniper is connected. New ads will show up here.")
    print("\nDone! Saved to settings.toml and sent you a test message in Telegram.")
    print("Next: put your searches in settings.toml, then double-click start_sniper.bat.")


# --------------------------------------------------------------------------

def main() -> None:
    mp_core.fix_console()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="show current matches, send nothing")
    parser.add_argument("--setup", action="store_true", help="connect Telegram")
    args = parser.parse_args()

    if args.setup:
        setup_telegram()
        return
    try:
        settings = mp_core.load_settings()
    except SettingsError as err:
        print(f"\nProblem with settings.toml:\n  {err}\n")
        sys.exit(1)

    if args.check:
        try:
            check(settings)
        except requests.RequestException as e:
            print(f"\nCouldn't reach Marktplaats ({e}). Check your internet and try again.")
            sys.exit(1)
    else:
        try:
            watch(settings)
        except KeyboardInterrupt:
            log.info("Stopped.")


if __name__ == "__main__":
    main()

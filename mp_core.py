"""Shared bits for sniper.py and scan.py: reading settings.toml, talking to
Marktplaats, and deciding which ads pass a search's filters.

Marktplaats has no public API. This uses the same JSON search endpoint the
marktplaats.nl website itself calls, with plain `requests` (no wrapper lib,
so there's one less thing to break when they change something).
"""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "settings.toml"

API = "https://www.marktplaats.nl/lrp/api/search"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json",
}
MIN_POLL_SECONDS = 30
POSTCODE_RE = re.compile(r"^\d{4}\s?[A-Za-z]{2}$")
# Checked against the title only: descriptions often say things like
# "geen ruil gezocht", which would wrongly drop a normal for-sale ad.
WANTED_AD_WORDS = ["gezocht", "gevraagd", "ik zoek"]
# Used by one-off scans (scan.py "query"), which have no exclude_words of their own.
DEFAULT_EXCLUDE = ["defect", "kapot", "voor onderdelen", "niet werkend", "doet het niet"]


class SettingsError(Exception):
    pass


def fix_console() -> None:
    # Windows consoles often default to a legacy codepage (e.g. cp1252) that
    # can't print Dutch ad text or emoji; without this a single title can crash
    # the loop. pythonw.exe has no console at all, hence the None guard.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

@dataclass
class Search:
    name: str
    query: str
    min_price: int = 0
    max_price: int = 0
    distance_km: int = 0
    exclude_words: list[str] = field(default_factory=list)
    must_include: list[str] = field(default_factory=list)
    highlight_words: list[str] = field(default_factory=list)
    query_in_title: bool = False
    skip_wanted_ads: bool = True
    skip_shops: bool = False


@dataclass
class Settings:
    bot_token: str
    chat_id: str
    postcode: str
    poll_interval_seconds: int
    send_photos: bool
    searches: list[Search]

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)


SEARCH_KEYS = set(Search.__dataclass_fields__)


def load_settings(path: Path = SETTINGS_FILE) -> Settings:
    """Read settings.toml and turn every mistake into a message a non-programmer
    can act on, instead of a Python traceback."""
    if not path.exists():
        raise SettingsError(f"Can't find {path.name}. It should be next to sniper.py.")
    try:
        # utf-8-sig: older Notepad versions save a BOM, which tomllib rejects.
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as e:
        raise SettingsError(
            f"{path.name} has a typo: {e}\n"
            "Common causes: a missing \" quote, a missing comma between list items, "
            "or a word without quotes around it."
        ) from None

    tg = data.get("telegram", {})
    gen = data.get("general", {})
    postcode = str(gen.get("postcode", "")).replace(" ", "").upper()
    interval = gen.get("poll_interval_seconds", 60)
    if not isinstance(interval, int) or interval < MIN_POLL_SECONDS:
        raise SettingsError(
            f"poll_interval_seconds must be a whole number of at least {MIN_POLL_SECONDS}."
        )

    raw_searches = data.get("search", [])
    if not raw_searches:
        raise SettingsError("No searches yet. Add at least one [[search]] block to settings.toml.")

    searches: list[Search] = []
    names: set[str] = set()
    for i, raw in enumerate(raw_searches, start=1):
        label = f"search #{i} ({raw.get('name', 'no name')})"
        unknown = set(raw) - SEARCH_KEYS
        if unknown:
            raise SettingsError(
                f"{label}: unknown setting(s) {', '.join(sorted(unknown))}. Check the spelling."
            )
        if not str(raw.get("query", "")).strip():
            raise SettingsError(f'{label}: query is empty. Put what you\'re looking for in query = "...".')
        name = str(raw.get("name") or raw["query"]).strip()
        if name in names:
            raise SettingsError(f'Two searches are both called "{name}". Give each a different name.')
        names.add(name)
        for key in ("min_price", "max_price", "distance_km"):
            if not isinstance(raw.get(key, 0), int) or raw.get(key, 0) < 0:
                raise SettingsError(f"{label}: {key} must be a whole number (0 or more), without quotes.")
        for key in ("exclude_words", "must_include", "highlight_words"):
            value = raw.get(key, [])
            if not isinstance(value, list) or not all(isinstance(w, str) for w in value):
                raise SettingsError(f'{label}: {key} must be a list like ["word one", "word two"].')
        s = Search(**{**raw, "name": name, "query": str(raw["query"]).strip()})
        if s.max_price and s.min_price > s.max_price:
            raise SettingsError(f"{label}: min_price is higher than max_price.")
        if s.distance_km and not POSTCODE_RE.match(postcode):
            raise SettingsError(
                f'{label} uses distance_km, so set your postcode under [general] (like "1234AB"), '
                "or set distance_km = 0 to search the whole country."
            )
        searches.append(s)

    return Settings(
        bot_token=str(tg.get("bot_token", "")).strip(),
        chat_id=str(tg.get("chat_id", "")).strip(),
        postcode=postcode,
        poll_interval_seconds=interval,
        send_photos=bool(gen.get("send_photos", True)),
        searches=searches,
    )


def save_telegram_settings(bot_token: str, chat_id: str, path: Path = SETTINGS_FILE) -> None:
    """Fill in the two telegram lines in place, so the comments in the file survive."""
    text = path.read_text(encoding="utf-8-sig")
    for key, value in (("bot_token", bot_token), ("chat_id", chat_id)):
        text, n = re.subn(rf'(?m)^{key}\s*=.*$', f'{key} = "{value}"', text, count=1)
        if not n:
            text = text.replace("[telegram]", f'[telegram]\n{key} = "{value}"', 1)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
# Marktplaats
# --------------------------------------------------------------------------

@dataclass
class Ad:
    id: str
    title: str
    description: str
    price_cents: int
    price_type: str
    city: str
    distance_km: int | None
    date: str
    seller: str
    is_shop: bool
    condition: str
    url: str
    photo_url: str | None

    @property
    def text(self) -> str:
        return f"{self.title} {self.description}".lower()

    @property
    def price_label(self) -> str:
        euros = f"€{self.price_cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        euros = euros.removesuffix(",00")
        return {
            "FIXED": euros,
            "MIN_BID": f"bieden vanaf {euros}" if self.price_cents else "bieden",
            "FAST_BID": "bieden",
            "FREE": "gratis",
            "SEE_DESCRIPTION": "zie omschrijving",
            "EXCHANGE": "ruilen",
            "RESERVED": "gereserveerd",
            "NOTK": "n.o.t.k.",
        }.get(self.price_type, euros)


def _parse_ad(item: dict) -> Ad:
    price = item.get("priceInfo") or {}
    loc = item.get("location") or {}
    seller = item.get("sellerInformation") or {}
    attrs = {a.get("key"): a.get("value") for a in item.get("attributes") or []}
    pictures = item.get("pictures") or []
    photo = pictures[0].get("largeUrl") if pictures else None
    if not photo and item.get("imageUrls"):
        photo = "https:" + item["imageUrls"][0]
    distance_m = loc.get("distanceMeters")
    return Ad(
        id=str(item.get("itemId")),
        title=item.get("title") or "",
        description=item.get("description") or "",
        price_cents=price.get("priceCents") or 0,
        price_type=price.get("priceType") or "",
        city=loc.get("cityName") or "",
        distance_km=round(distance_m / 1000) if distance_m and distance_m > 0 else None,
        date=item.get("date") or "",
        seller=seller.get("sellerName") or "",
        is_shop=bool(seller.get("showWebsiteUrl")),
        condition=attrs.get("condition") or "",
        url="https://www.marktplaats.nl" + (item.get("vipUrl") or ""),
        photo_url=photo,
    )


def fetch_ads(session: requests.Session, search: Search, postcode: str,
              limit: int = 50, offset: int = 0) -> list[Ad]:
    """Newest ads first. Price and distance are filtered by Marktplaats itself;
    the word filters happen afterwards in `skip_reason`."""
    params: dict[str, str | int] = {
        "query": search.query,
        "searchInTitleAndDescription": "true",
        "limit": limit,
        "offset": offset,
        "sortBy": "SORT_INDEX",  # = date
        "sortOrder": "DECREASING",
        "viewOptions": "list-view",
    }
    if search.distance_km:
        params["postcode"] = postcode
        params["distanceMeters"] = search.distance_km * 1000
    if search.min_price or search.max_price:
        low = search.min_price * 100 if search.min_price else "null"
        high = search.max_price * 100 if search.max_price else "null"
        params["attributeRanges[]"] = f"PriceCents:{low}:{high}"
    response = session.get(API, params=params, timeout=20)
    response.raise_for_status()
    return [_parse_ad(item) for item in response.json().get("listings", [])]


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------

def found_words(text: str, words: list[str]) -> list[str]:
    text = text.lower()
    return [w for w in words if w.strip() and w.lower() in text]


def skip_reason(ad: Ad, search: Search) -> str | None:
    """Why this ad should NOT be sent, or None if it's a hit."""
    # Marktplaats also matches the description (and ignores its own flag to
    # turn that off), so "playstation 5" returns every game and controller
    # that mentions a PS5. This keeps only ads with all query words in the title.
    if search.query_in_title:
        title = ad.title.lower()
        if not all(re.search(rf"\b{re.escape(w)}\b", title) for w in search.query.lower().split()):
            return "query not in title"
    if search.skip_wanted_ads and found_words(ad.title, WANTED_AD_WORDS):
        return "wanted ad"
    if search.skip_shops and ad.is_shop:
        return "shop"
    bad = found_words(ad.text, search.exclude_words)
    if bad:
        return f"contains '{bad[0]}'"
    if search.must_include and not found_words(ad.text, search.must_include):
        return "missing must_include word"
    return None

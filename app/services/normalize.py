"""Нормализация сайтов, ссылок, названий и городов. Сеть не используется: ссылки — это данные."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

# Домены, которые не являются официальным сайтом компании.
SOCIAL = {
    "vk.com", "vk.ru", "vkontakte.ru", "ok.ru", "instagram.com", "facebook.com", "fb.com",
    "youtube.com", "youtu.be", "tiktok.com", "linkedin.com", "twitter.com", "x.com",
    "dzen.ru", "zen.yandex.ru", "rutube.ru", "pinterest.com", "threads.net", "threads.com",
    "taplink.cc", "taplink.ru", "linktr.ee", "vc.ru", "pikabu.ru", "habr.com", "tenchat.ru",
}
MAPS = {
    "2gis.ru", "2gis.com", "2gis.kz", "go.2gis.com", "maps.app.goo.gl", "goo.gl",
    "maps.google.com", "maps.google.ru", "maps.yandex.ru", "maps.apple.com", "yandex.ru/maps",
}
CATALOG = {
    "avito.ru", "hh.ru", "zoon.ru", "yell.ru", "flamp.ru", "tripadvisor.ru", "tripadvisor.com",
    "otzovik.com", "irecommend.ru", "profi.ru", "youdo.com", "rusprofile.ru", "list-org.com",
    "sbis.ru", "checko.ru", "zachestnyibiznes.ru", "spark-interfax.ru", "cataloxy.ru",
    "orgpage.ru", "yandex.ru", "ya.ru", "yandex.com", "google.com", "google.ru", "bing.com",
    "ozon.ru", "wildberries.ru", "market.yandex.ru", "uslugi.yandex.ru", "prodoctorov.ru",
    "restoclub.ru", "afisha.ru", "booking.com", "ostrovok.ru", "cian.ru", "domclick.ru",
    "wikipedia.org", "tbank.ru", "kwork.ru", "fl.ru", "g.page", "docs.google.com",
    "chatgpt.com", "openai.com", "t.co", "bit.ly", "clck.ru", "vk.cc", "tinyurl.com",
    "apps.apple.com", "play.google.com", "rustore.ru", "yclients.com", "dikidi.net", "dikidi.ru",
}
MESSENGER = {
    "t.me", "telegram.me", "telegram.org", "wa.me", "whatsapp.com", "api.whatsapp.com",
    "chat.whatsapp.com", "viber.com", "vb.me", "max.ru",
}

BARE_TLDS = (
    "ru|рф|com|net|org|io|ai|pro|su|by|kz|ua|uz|me|biz|info|online|site|store|shop|tech|app|dev|"
    "moscow|москва|рус|cc|tv|co|club|studio|agency|group|top|xyz|cloud|digital|market|center|"
    "team|space|life|world|one|eu|de|es|uk|us|am|ge|kg|az|md|ee|lv|lt|pl|tr|cn|in|ws|fr|it|"
    "school|academy|expert|clinic|dental|beauty|fit|cafe|bar|travel|media|art|design|house|"
    "estate|auto|bot|chat|link|page|website|company|business|solutions|services|systems|"
    "software|consulting|marketing|promo|sale|plus|fun|vip|ltd|pw|im|gg|fm|id|ae|il|rs|cz|fi|"
    "se|no|nl|be|ch|at|pt|gr|ca|au|nz|jp|kr|br|mx|онлайн|сайт|дети|бел|укр|қаз"
)
_HOST_RE = re.compile(r"^(?:[a-z0-9а-яё_-]+\.)+[a-zа-яёқ0-9-]{2,}$", re.IGNORECASE)


def _split(raw: str) -> tuple[str, str, str]:
    """Возвращает (host, path, query). Хост в нижнем регистре, без www и порта."""
    raw = (raw or "").strip().strip("<>()[]\"'«».,;")
    if not raw:
        return "", "", ""
    candidate = raw if re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.IGNORECASE) else "http://" + raw
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return "", "", ""
    host = (parts.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host, parts.path or "", parts.query or ""


def _ascii_host(host: str) -> str:
    """Punycode-форма: «ромашка.рф» и «xn--80aa…» считаются одним сайтом."""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def display_host(host: str) -> str:
    try:
        return host.encode("ascii").decode("idna")
    except UnicodeError:
        return host


def _in(host: str, domains: set[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains if "/" not in d)


def classify_url(raw: str) -> tuple[str, str]:
    """Определяет вид ссылки.

    Возвращает (kind, value): kind ∈ website | messenger | social | map | catalog | invalid.
    Для website value — нормализованный хост (ключ дублей); для остальных — исходная ссылка.
    """
    host, path, _query = _split(raw)
    if not host or not _HOST_RE.match(host):
        return "invalid", raw.strip()
    cleaned = raw.strip().strip("<>\"'«»")
    if _in(host, MESSENGER):
        return "messenger", cleaned
    if _in(host, MAPS) or (host in ("yandex.ru", "yandex.com", "google.com", "google.ru") and path.startswith("/maps")):
        return "map", cleaned
    if _in(host, SOCIAL):
        return "social", cleaned
    if _in(host, CATALOG):
        return "catalog", cleaned
    return "website", _ascii_host(host)


def normalize_website(raw: str | None) -> str | None:
    """Ключ дублей для официального сайта или None (пусто, мусор, каталог, соцсеть, карта)."""
    if not raw:
        return None
    kind, value = classify_url(raw)
    return value if kind == "website" else None


def normalize_link(raw: str) -> str:
    """Ключ сравнения для ссылок-данных: хост + путь, без схемы, www, параметров и хвостового /."""
    host, path, _query = _split(raw)
    if not host:
        return raw.strip().lower()
    return (_ascii_host(host) + path.rstrip("/")).lower()


def ensure_scheme(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    return raw if re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.IGNORECASE) else "https://" + raw


_LEGAL_FORMS = re.compile(
    r"(?<![\w])(ооо|оао|зао|пао|нао|ао|ип|нко|ано|гк|тоо|llc|ltd|inc|gmbh|corp|co)(?![\w])\.?",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize_name(raw: str | None) -> str:
    """Название для сравнения: без кавычек, организационной формы, регистра и пунктуации."""
    text = (raw or "").lower().replace("ё", "е")
    text = _LEGAL_FORMS.sub(" ", text)
    text = _PUNCT.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


_CITY_PREFIX = re.compile(r"^(г\.\s*|г\s+|город\s+|гор\.\s*)", re.IGNORECASE)


def normalize_city(raw: str | None) -> str:
    text = (raw or "").strip().lower().replace("ё", "е")
    text = _CITY_PREFIX.sub("", text)
    text = _PUNCT.sub(" ", text)
    text = _SPACES.sub(" ", text).strip()
    return {"спб": "санкт петербург", "питер": "санкт петербург", "мск": "москва"}.get(text, text)

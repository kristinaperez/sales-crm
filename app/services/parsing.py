"""Разбор входных списков: свободный текст, списки URL, ответы ИИ (в т.ч. Markdown-таблицы),
диапазоны Google Таблиц (табуляция) и CSV. Только разбор строк — без сети и без ИИ."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from .normalize import BARE_TLDS, classify_url, normalize_city, normalize_link, normalize_website

FIELDS = {
    "name": "Название",
    "website": "Сайт",
    "city": "Город",
    "industry": "Отрасль",
    "phone": "Телефон",
    "email": "Email",
    "messenger_url": "Мессенджер",
    "note": "Заметка",
    "links": "Ссылки (каталоги, соцсети, карты)",
}


@dataclass
class ParsedRow:
    raw: str = ""
    name: str = ""
    website: str = ""
    city: str = ""
    industry: str = ""
    phone: str = ""
    email: str = ""
    messenger_url: str = ""
    note: str = ""
    links: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            [self.name, self.website, self.city, self.phone, self.email, self.messenger_url, self.links, self.note]
        )


# --- Синонимы заголовков и меток «ключ: значение» -------------------------------------------

_SYNONYMS: list[tuple[str, tuple[str, ...]]] = [
    ("skip", ("№", "#", "n", "no", "id", "п/п", "n п/п", "№ п/п", "num")),
    ("website", ("сайт", "website", "веб-сайт", "вебсайт", "web", "site", "официальный сайт", "домен", "адрес сайта")),
    ("messenger_url", ("telegram", "телеграм", "телеграмм", "whatsapp", "ватсап", "вацап", "мессенджер", "мессенджеры", "tg", "тг", "wa", "viber")),
    ("email", ("email", "e-mail", "mail", "почта", "эл. почта", "электронная почта", "мейл", "имейл", "емейл")),
    ("phone", ("телефон", "тел", "тел.", "phone", "моб", "мобильный", "номер телефона", "телефоны")),
    ("city", ("город", "city", "локация", "регион", "местоположение", "населённый пункт", "населенный пункт")),
    ("industry", ("отрасль", "ниша", "сфера", "индустрия", "категория", "направление", "деятельность", "industry", "сегмент", "тип бизнеса", "сфера деятельности", "вид деятельности")),
    ("name", ("название", "компания", "наименование", "организация", "бренд", "name", "company", "название компании", "фирма", "имя")),
    ("links", ("ссылка", "ссылки", "url", "link", "links", "соцсети", "соцсеть", "карта", "карты", "2гис", "2gis", "вк", "vk", "instagram", "каталог", "профиль", "источник")),
    ("contacts", ("контакт", "контакты", "contacts", "contact", "связь")),
    ("note", ("заметка", "заметки", "комментарий", "комментарии", "описание", "примечание", "note", "notes", "почему", "чем занимается", "о компании", "description", "чем занимаются", "адрес")),
]
_LABEL_TO_FIELD: dict[str, str] = {label: fld for fld, labels in _SYNONYMS for label in labels}
_LABELS_SORTED = sorted(_LABEL_TO_FIELD, key=len, reverse=True)
_KV_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(x) for x in _LABELS_SORTED if x not in ("#", "№", "n", "no", "id", "num")) + r")\s*[:：=]\s*(.*)$",
    re.IGNORECASE,
)


def guess_field(header: str) -> str | None:
    """Поле CRM по заголовку колонки или None."""
    h = re.sub(r"[*_`]", "", header or "").strip().lower().rstrip(":").strip()
    if not h:
        return None
    if h in _LABEL_TO_FIELD:
        return _LABEL_TO_FIELD[h]
    for fld, labels in _SYNONYMS:
        if fld == "skip":
            continue
        for label in labels:
            if len(label) >= 4 and label in h:
                return fld
    return None


# --- Извлечение сущностей -----------------------------------------------------------------

# Все повторы ограничены сверху: на длинной строке без разделителей разбор остаётся линейным.
LINE_MAX = 1000  # символов в одной строке списка или ячейке; остальное отбрасывается
NOTE_MAX = 2000
MAX_ROWS = 10_000
MAX_CELLS = 40  # колонок в строке таблицы
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}", re.UNICODE)
_URL_RE = re.compile(r"https?://[^\s<>\"'|\]]+", re.IGNORECASE)
_BARE_RE = re.compile(
    r"(?<![@\w.-])((?:[a-zа-яё0-9][a-zа-яё0-9-]{0,62}\.){1,8}(?:" + BARE_TLDS + r"))(?![\w-])(/[^\s<>\"'|\],;]*)?",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(r"(?<![\w.])(\+?\d[\d\s\-()]{8,18}\d)(?!\w)")
_MD_LINK_RE = re.compile(r"\[([^\]\[]{1,300})\]\(([^)\s]{1,1000})\)")
_LIST_MARK_RE = re.compile(r"^\s*(?:\d{1,4}[.)]|[-*•·–—▪●◦✅➡→>]+)\s+")
_HANDLE_RE = re.compile(r"^@([A-Za-z0-9_]{4,})$")
_SEP = " \x1f "


def clean_md(text: str) -> str:
    text = re.sub(r"<br\s*/?>", ", ", text or "", flags=re.IGNORECASE)
    text = re.sub(r"(\*\*|__|`)", "", text)
    text = re.sub(r"^\s*#{1,6}\s*", "", text)
    text = _LIST_MARK_RE.sub("", text)
    return text.strip()


def _strip_url(url: str) -> str:
    url = url.rstrip(".,;:!?…")
    if url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1]
    return url


def add_url(row: ParsedRow, url: str) -> None:
    """Раскладывает ссылку: официальный сайт, мессенджер или ссылка-данные."""
    url = _strip_url(url.strip())
    if not url:
        return
    kind, value = classify_url(url)
    if kind == "invalid":
        return
    if kind == "website":
        if not row.website:
            row.website = url
            return
        if normalize_website(row.website) == value:
            return
    elif kind == "messenger" and not row.messenger_url:
        row.messenger_url = url
        return
    if len(row.links) >= 20:
        return
    key = normalize_link(url)
    if key == normalize_link(row.messenger_url or "\x00"):
        return
    if all(normalize_link(x) != key for x in row.links):
        row.links.append(url)


def _extract(row: ParsedRow, text: str) -> str:
    """Забирает из текста ссылки, почту и телефоны; возвращает остаток с разделителями."""

    def md(m: re.Match) -> str:
        label, url = m.group(1).strip(), m.group(2)
        add_url(row, url)
        if _URL_RE.match(label) or _BARE_RE.fullmatch(label):
            return _SEP
        return f"{_SEP}{label}{_SEP}"

    text = _MD_LINK_RE.sub(md, text)

    def url(m: re.Match) -> str:
        add_url(row, m.group(0))
        return _SEP

    text = _URL_RE.sub(url, text)

    def email(m: re.Match) -> str:
        if not row.email:
            row.email = m.group(0).strip(".")
        elif m.group(0).lower() not in row.email.lower():
            _append_note(row, m.group(0))
        return _SEP

    text = _EMAIL_RE.sub(email, text)
    text = _BARE_RE.sub(url, text)

    def phone(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(1))
        if not 10 <= len(digits) <= 15:
            return m.group(0)
        value = re.sub(r"\s+", " ", m.group(1)).strip()
        if not row.phone:
            row.phone = value
        elif digits not in re.sub(r"\D", "", row.phone):
            row.phone = f"{row.phone}, {value}"[:100]
        return _SEP

    return _PHONE_RE.sub(phone, text)


def _append_note(row: ParsedRow, text: str) -> None:
    text = text.strip(" ,;.-—–")[:NOTE_MAX]
    if len(row.note) >= NOTE_MAX:
        return
    if text and text.lower() not in row.note.lower():
        row.note = f"{row.note}; {text}" if row.note else text


def set_field(row: ParsedRow, fld: str, value: str, header: str = "") -> None:
    """Кладёт значение в поле с учётом типа (используется для таблиц, CSV и «ключ: значение»)."""
    value = clean_md(value)
    if not value or value in ("-", "—", "–", "нет", "n/a", "N/A", "н/д"):
        return
    if fld == "skip":
        return
    if fld in ("name", "city", "industry"):
        rest = _tidy(_extract(row, value).replace(_SEP, " "))
        if not rest:
            return
        if fld == "city":
            rest = _clean_city(rest)
        if not getattr(row, fld):
            setattr(row, fld, rest)
        else:
            _append_note(row, rest)
    elif fld in ("website", "links", "contacts", "email", "phone"):
        rest = _tidy(_extract(row, value).replace(_SEP, " "))
        if fld == "website" and rest and " " not in rest and classify_url(rest)[0] != "invalid":
            add_url(row, rest)  # домен с редкой зоной в явной колонке «Сайт»
            return
        if rest and fld == "phone" and not row.phone:
            row.phone = rest[:100]
        elif rest and not _KV_RE.match(rest + ":"):
            _append_note(row, rest)
    elif fld == "messenger_url":
        handle = _HANDLE_RE.match(value)
        if handle:
            add_url(row, f"https://t.me/{handle.group(1)}")
            return
        before = (row.messenger_url, len(row.links), row.website)
        rest = _tidy(_extract(row, value).replace(_SEP, " "))
        changed = before != (row.messenger_url, len(row.links), row.website)
        if rest and not changed and not row.messenger_url:
            row.messenger_url = rest[:500]
        elif rest:
            _append_note(row, rest)
    else:  # note и неизвестные колонки
        rest = _tidy(_extract(row, value).replace(_SEP, " "))
        if rest:
            _append_note(row, f"{header}: {rest}" if header and fld != "note" else rest)


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" ,;|/\\-—–:·•").strip("()[]").strip()


# --- Города ------------------------------------------------------------------------------

_CITIES_RAW = """
Москва Санкт-Петербург Новосибирск Екатеринбург Казань Нижний_Новгород Челябинск Самара Омск
Ростов-на-Дону Уфа Красноярск Воронеж Пермь Волгоград Краснодар Саратов Тюмень Тольятти Ижевск
Барнаул Ульяновск Иркутск Хабаровск Ярославль Владивосток Махачкала Томск Оренбург Кемерово
Новокузнецк Рязань Астрахань Набережные_Челны Пенза Липецк Киров Чебоксары Тула Калининград
Балашиха Курск Севастополь Сочи Ставрополь Улан-Удэ Тверь Магнитогорск Иваново Брянск Белгород
Сургут Владимир Чита Нижний_Тагил Архангельск Симферополь Калуга Смоленск Волжский Якутск
Саранск Череповец Курган Вологда Орёл Подольск Грозный Владикавказ Тамбов Мурманск Петрозаводск
Нижневартовск Кострома Стерлитамак Новороссийск Йошкар-Ола Химки Таганрог Комсомольск-на-Амуре
Сыктывкар Нальчик Шахты Дзержинск Орск Братск Благовещенск Энгельс Ангарск Королёв
Великий_Новгород Старый_Оскол Мытищи Псков Люберцы Южно-Сахалинск Бийск Прокопьевск Армавир
Балаково Рыбинск Абакан Северодвинск Петропавловск-Камчатский Норильск Уссурийск Волгодонск
Красногорск Сызрань Новочеркасск Каменск-Уральский Златоуст Электросталь Альметьевск Салават
Миасс Керчь Находка Копейск Пятигорск Рубцовск Березники Коломна Майкоп Хасавюрт Одинцово
Ковров Кисловодск Домодедово Нефтекамск Нефтеюганск Батайск Новочебоксарск Серпухов Щёлково
Дербент Новомосковск Черкесск Первоуральск Раменское Назрань Каспийск Обнинск Орехово-Зуево
Кызыл Новый_Уренгой Невинномысск Димитровград Октябрьский Долгопрудный Ессентуки Камышин
Муром Жуковский Евпатория Новошахтинск Северск Реутов Пушкино Артём Ноябрьск Ачинск Бердск
Арзамас Элиста Ногинск Елец Сергиев_Посад Геленджик Анапа Ялта Зеленоград Иннополис Тобольск
Минск Гомель Брест Гродно Витебск Могилёв Алматы Астана Шымкент Караганда Актобе Ташкент
Самарканд Бишкек Ереван Тбилиси Батуми Баку Кишинёв Душанбе Дубай Белград Стамбул Киев
Харьков Одесса Днепр Львов Рига Вильнюс Таллин Варшава Прага Берлин Лондон Барселона Мадрид
Сарагоса Валенсия Лиссабон Париж Лимасол Пафос Тель-Авив Пхукет Бали
"""
KNOWN_CITIES = {normalize_city(c.replace("_", " ")) for c in _CITIES_RAW.split()}
KNOWN_CITIES |= {"спб", "санкт петербург", "мск", "питер", "нижний", "ростов", "екб"}
_CITY_MARK_RE = re.compile(r"^(?:г\.\s*|г\s+|город\s+)(.+)$", re.IGNORECASE)


def looks_like_city(text: str) -> bool:
    return bool(_CITY_MARK_RE.match(text.strip())) or normalize_city(text) in KNOWN_CITIES


def _clean_city(text: str) -> str:
    m = _CITY_MARK_RE.match(text.strip())
    return (m.group(1) if m else text).strip()


# --- Свободная строка и ячейки без заголовков -------------------------------------------------

_PART_SPLIT_RE = re.compile(r"\x1f|[;|\t]|,(?!\d)|\s[—–-]\s|\s/\s")


def _assign_leftovers(row: ParsedRow, parts: list[str], strict: bool) -> None:
    """Раскладывает текстовые остатки: название, город, отрасль, заметка."""
    leftovers: list[str] = []
    for part in parts:
        part = _tidy(part)
        if not part:
            continue
        kv = _KV_RE.match(part)
        if kv:
            fld = _LABEL_TO_FIELD.get(kv.group(1).lower(), "note")
            if kv.group(2).strip():
                set_field(row, fld, kv.group(2), kv.group(1))
            continue
        if part.lower().rstrip(".:") in _LABEL_TO_FIELD:
            continue  # осталась одна метка: «сайт», «тел.»
        handle = _HANDLE_RE.match(part)
        if handle:
            add_url(row, f"https://t.me/{handle.group(1)}")
            continue
        leftovers.append(part)

    has_known_city = any(looks_like_city(p) for p in leftovers[1:] if not row.name) or any(
        looks_like_city(p) for p in leftovers if row.name
    )
    for part in leftovers:
        if not row.name:
            if ": " in part:
                head, tail = part.split(": ", 1)
                row.name = _tidy(head)
                _append_note(row, tail)
            else:
                row.name = part
            continue
        if not row.city and looks_like_city(part):
            row.city = _clean_city(part)
            continue
        words = part.split()
        short = len(words) <= 3 and not re.search(r"\d", part)
        if not strict and not row.city and not has_known_city and short:
            row.city = part
            continue
        if not strict and not row.industry and len(words) <= 6:
            row.industry = part
            continue
        _append_note(row, part)


_INLINE_LABEL_RE = re.compile(
    r"(?<![\w])(?:"
    + "|".join(
        re.escape(label)
        for fld, labels in _SYNONYMS
        if fld in ("website", "phone", "email", "messenger_url", "links", "contacts")
        for label in sorted(labels, key=len, reverse=True)
    )
    + r")\s*[.:：]{0,2}\s*(?=\x1f)",
    re.IGNORECASE,
)


def parse_free_line(line: str, row: ParsedRow | None = None) -> ParsedRow:
    row = row or ParsedRow(raw=line.strip())
    rest = _INLINE_LABEL_RE.sub(" ", _extract(row, clean_md(line)))
    _assign_leftovers(row, _PART_SPLIT_RE.split(rest), strict=True)
    return row


def parse_cells(cells: list[str], raw: str) -> ParsedRow:
    """Строка таблицы без заголовков: тип ячеек определяется по содержимому."""
    row = ParsedRow(raw=raw)
    parts: list[str] = []
    for cell in cells:
        rest = _extract(row, clean_md(cell))
        parts.extend(p for p in rest.split("\x1f"))
    _assign_leftovers(row, parts, strict=False)
    return row


def row_from_mapping(cells: list[str], mapping: list[tuple[str | None, str]], raw: str) -> ParsedRow:
    """Строка таблицы с известным соответствием колонок: mapping = [(поле | None, заголовок)]."""
    row = ParsedRow(raw=raw)
    # Сначала явные поля, потом остальные — чтобы сайт из колонки «Сайт» имел приоритет.
    order = sorted(range(len(cells)), key=lambda i: 0 if i < len(mapping) and mapping[i][0] == "website" else 1)
    for i in order:
        fld, header = mapping[i] if i < len(mapping) else (None, "")
        if fld == "skip":
            continue
        set_field(row, fld or "note", cells[i], header if fld in (None, "note", "links") else "")
    return row


# --- Текст целиком -------------------------------------------------------------------------

_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _table_cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", line)][:MAX_CELLS]


def _is_header(cells: list[str]) -> bool:
    known = sum(1 for c in cells if guess_field(c) and guess_field(c) != "skip")
    has_data = any(_URL_RE.search(c) or _EMAIL_RE.search(c) or _BARE_RE.search(c) for c in cells)
    return known >= 2 and not has_data


def _is_noise(line: str) -> bool:
    cleaned = clean_md(line)
    if not cleaned or re.fullmatch(r"[-=_*~\s|]+", cleaned):
        return True
    has_data = _URL_RE.search(cleaned) or _EMAIL_RE.search(cleaned) or _BARE_RE.search(cleaned)
    has_data = has_data or any(10 <= len(re.sub(r"\D", "", m)) <= 15 for m in _PHONE_RE.findall(cleaned))
    return cleaned.endswith((":", "!", "?")) and not has_data


def parse_text(text: str, tabular: bool = False) -> list[ParsedRow]:
    """Главная точка входа для вставки текста, TXT и диапазона Google Таблиц.

    tabular=True — каждая строка считается строкой таблицы (диапазон из Google Таблиц).
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("﻿", "")
    rows: list[ParsedRow] = []
    current: ParsedRow | None = None
    md_mapping: list[tuple[str | None, str]] | None = None
    tsv_mapping: list[tuple[str | None, str]] | None = None
    in_table = False

    lines = [line[:LINE_MAX] for line in text.split("\n")]
    for idx, line in enumerate(lines):
        if len(rows) > MAX_ROWS:
            break  # дальше разбирать незачем: импорт всё равно будет отклонён по лимиту строк
        if not line.strip():
            in_table = False
            continue
        stripped = line.strip()

        # Markdown-таблица
        if stripped.startswith("|") and stripped.count("|") >= 2:
            if _TABLE_SEP_RE.match(stripped):
                continue
            cells = _table_cells(stripped)
            nxt = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
            if not in_table:
                in_table = True
                md_mapping = None
                if _TABLE_SEP_RE.match(nxt) or _is_header(cells):
                    md_mapping = [(guess_field(c), clean_md(c)) for c in cells]
                    if not any(f for f, _ in md_mapping):
                        md_mapping = None
                    continue
            row = row_from_mapping(cells, md_mapping, stripped) if md_mapping else parse_cells(cells, stripped)
            if not row.is_empty():
                rows.append(row)
            current = None
            continue
        in_table = False

        # Диапазон из таблицы (табуляция)
        if "\t" in line or tabular:
            cells = [c.strip() for c in line.split("\t")][:MAX_CELLS]
            if tsv_mapping is None and _is_header(cells):
                tsv_mapping = [(guess_field(c), c) for c in cells]
                continue
            raw = " | ".join(c for c in cells if c)
            row = row_from_mapping(cells, tsv_mapping, raw) if tsv_mapping else parse_cells(cells, raw)
            if not row.is_empty():
                rows.append(row)
            current = None
            continue

        if _is_noise(stripped):
            continue

        # «Ключ: значение» — продолжение предыдущей записи (типичный ответ ИИ)
        kv = _KV_RE.match(clean_md(stripped))
        if kv:
            fld = _LABEL_TO_FIELD.get(kv.group(1).lower(), "note")
            if fld != "name" and current is not None:
                set_field(current, fld, kv.group(2), kv.group(1))
                current.raw = f"{current.raw}\n{stripped}"
                continue
            current = ParsedRow(raw=stripped)
            set_field(current, fld, kv.group(2), kv.group(1))
            rows.append(current)
            continue

        current = parse_free_line(stripped)
        rows.append(current)

    result = [_finalize(r) for r in rows if not r.is_empty()]
    return result


def _finalize(row: ParsedRow) -> ParsedRow:
    row.name = _tidy(row.name)[:300]
    row.website = row.website.strip()[:500]
    row.city = _tidy(row.city)[:200]
    row.industry = _tidy(row.industry)[:200]
    row.phone = row.phone.strip()[:100]
    row.email = row.email.strip()[:200]
    row.messenger_url = row.messenger_url.strip()[:500]
    row.note = row.note.strip()
    row.links = [x[:1000] for x in row.links][:20]
    return row


# --- CSV ---------------------------------------------------------------------------------


def decode_bytes(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


class CsvError(ValueError):
    """Файл не читается как CSV."""


def read_csv(text: str) -> list[list[str]]:
    text = text.replace("﻿", "")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        first = sample.split("\n", 1)[0]
        delimiter = max(",;\t|", key=first.count)
    csv.field_size_limit(2_000_000)
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        return [[c.strip()[:LINE_MAX] for c in r[:MAX_CELLS]] for r in reader if any(c.strip() for c in r)]
    except csv.Error as exc:
        raise CsvError("Файл не получилось прочитать как CSV: проверьте кавычки и разделители или сохраните его заново из таблицы.") from exc


def csv_rows(table: list[list[str]], mapping: list[str | None], has_header: bool) -> list[ParsedRow]:
    """Строки CSV по выбранному пользователем соответствию колонок."""
    headers = table[0] if has_header and table else []
    body = table[1:] if has_header else table
    pairs = [(m or None, headers[i] if i < len(headers) else f"Колонка {i + 1}") for i, m in enumerate(mapping)]
    out: list[ParsedRow] = []
    for cells in body:
        raw = " | ".join(c for c in cells if c)
        row = row_from_mapping(cells, pairs, raw)
        if not row.is_empty():
            out.append(_finalize(row))
    return out

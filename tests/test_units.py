from app.services.normalize import classify_url, normalize_city, normalize_link, normalize_name, normalize_website
from app.services.parsing import parse_text, read_csv


def test_website_normalization_ignores_scheme_www_path_case():
    variants = ["https://www.Romashka.ru/contacts?x=1", "romashka.ru", "HTTP://ROMASHKA.RU/", "www.romashka.ru:443/about"]
    assert {normalize_website(v) for v in variants} == {"romashka.ru"}


def test_cyrillic_domain_equals_punycode():
    assert normalize_website("окна-сити.рф") == normalize_website("https://xn----7sbzagsf2ak.xn--p1ai/") != None  # noqa: E711


def test_aggregators_are_not_websites():
    for url, kind in [
        ("https://vk.com/bronza", "social"), ("https://2gis.ru/moscow/firm/1", "map"),
        ("https://yandex.ru/maps/org/123", "map"), ("https://www.avito.ru/x", "catalog"),
        ("https://t.me/atlet", "messenger"), ("instagram.com/shop", "social"),
    ]:
        assert classify_url(url)[0] == kind
        assert normalize_website(url) is None
    assert normalize_link("https://VK.com/bronza/") == "vk.com/bronza"


def test_name_and_city_normalization():
    assert normalize_name("ООО «Ромашка»") == normalize_name("ромашка") == "ромашка"
    assert normalize_city("г. Санкт-Петербург") == normalize_city("СПб") == "санкт петербург"
    assert normalize_city("Геленджик") == "геленджик"


MIXED = """Вот список компаний:

1. **ООО «Ромашка»** — Казань, доставка цветов. Сайт: https://www.romashka-flowers.ru/contacts, тел. +7 (843) 200-11-22, info@romashka-flowers.ru
2. Студия загара «Бронза», Самара — https://vk.com/bronza_samara
3. https://dentalux-clinic.ru
5. Кофейня Зерно
6. **Фитнес-клуб Атлет**
   Город: Екатеринбург
   Сайт: atlet-fit.ru
   Телефон: 8 (343) 555-66-77
   Telegram: @atlet_fit

| Компания | Город | Сайт | Ниша |
|---|---|---|---|
| Клиника Улыбка | Тверь | ulybka-tver.ru | стоматология |

Надеюсь, это поможет!
"""


def test_mixed_ai_answer_is_parsed():
    rows = {r.name or r.website: r for r in parse_text(MIXED)}
    assert len(rows) == 6  # вводная и заключительная фразы отброшены
    r = rows["ООО «Ромашка»"]
    assert (r.city, r.phone, r.email) == ("Казань", "+7 (843) 200-11-22", "info@romashka-flowers.ru")
    assert normalize_website(r.website) == "romashka-flowers.ru"
    bronza = rows["Студия загара «Бронза»"]
    assert bronza.website == "" and bronza.links == ["https://vk.com/bronza_samara"] and bronza.city == "Самара"
    assert rows["https://dentalux-clinic.ru"].name == ""
    atlet = rows["Фитнес-клуб Атлет"]
    assert (atlet.city, atlet.website, atlet.messenger_url) == ("Екатеринбург", "atlet-fit.ru", "https://t.me/atlet_fit")
    table = rows["Клиника Улыбка"]
    assert (table.city, table.website, table.industry) == ("Тверь", "ulybka-tver.ru", "стоматология")


def test_google_sheets_range_with_and_without_header():
    with_header = parse_text("Название\tГород\tСайт\tEmail\nМебель Про\tТула\thttp://mebel-pro.ru/\tsale@mebel-pro.ru\n", tabular=True)
    assert [(r.name, r.city, r.email) for r in with_header] == [("Мебель Про", "Тула", "sale@mebel-pro.ru")]
    no_header = parse_text("Окна Сити\tКурск\tokna-city.ru\t+7 4712 55-66-77\n", tabular=True)
    assert (no_header[0].name, no_header[0].city, no_header[0].website) == ("Окна Сити", "Курск", "okna-city.ru")


def test_csv_reader_detects_semicolon():
    assert read_csv("Имя;Сайт\nА;a.ru\n") == [["Имя", "Сайт"], ["А", "a.ru"]]

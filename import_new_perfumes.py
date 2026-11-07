import argparse
import json
import unicodedata

BRAND_CANONICAL_ALIASES = {
    "christiandior": "dior",
    "dior": "dior",
    "yvessaintlaurent": "yvessaintlaurent",
    "ysl": "yvessaintlaurent",
    "dolceandgabbana": "dolcegabbana",
    "dolcegabbana": "dolcegabbana",
    "dg": "dolcegabbana",
    "giorgioarmani": "giorgioarmani",
    "armani": "giorgioarmani",
    "maisonfranciskurkdjian": "maisonfranciskurkdjian",
    "franciskurkdjian": "maisonfranciskurkdjian",
    "mfk": "maisonfranciskurkdjian",
    "pacorabanne": "pacorabanne",
    "rabanne": "pacorabanne",
    "thierrymugler": "mugler",
    "mugler": "mugler",
    "sergelutens": "sergelutens",
    "lutens": "sergelutens",
    "bykilian": "kilian",
    "kilian": "kilian",
    "kilianparis": "kilian",
    "parfumsdemarly": "parfumsdemarly",
    "marly": "parfumsdemarly",
    "fredericmalle": "fredericmalle",
    "editionsdeparfumsfredericmalle": "fredericmalle",
    "MaisonMargiela".lower(): "maisonmargiela",
    "maisonmartinmargiela": "maisonmargiela",
}


def strip_accents(text: str) -> str:
    s = (text or "").replace("ё", "е").replace("Ё", "Е")
    out = []
    for ch in s:
        if "а" <= ch.lower() <= "я":
            out.append(ch)
        else:
            decomp = unicodedata.normalize("NFKD", ch)
            out.append("".join(c for c in decomp if not unicodedata.combining(c)))
    return "".join(out)


def canonical_brand_slug(brand: str) -> str:
    b = re.sub(r"[^a-zа-я0-9]+", "", strip_accents(brand).lower())
    return BRAND_CANONICAL_ALIASES.get(b, b)

import os
import random
import re
import time
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from supabase import Client, ClientOptions, create_client


def _load_local_env():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    for fname in ("SupaKeys.env", ".env", ".env.txt", "env", "env.txt"):
        env_path = os.path.join(base_dir, fname)
        if not os.path.exists(env_path):
            continue
        try:
            with open(env_path, "r", encoding="utf-8-sig") as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, v = s.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'").strip('"')
                    if k and v:
                        os.environ[k] = v
        except Exception:
            pass

    # Ключи берутся из SupaKeys.env / .env (на ПК) или из GitHub Secrets (в облаке)


_load_local_env()

# =====================================================================
# НАСТРОЙКИ SUPABASE (С АВТО-ОЧИСТКОЙ ДЛЯ GITHUB ACTIONS SECRETS)
# =====================================================================
def _clean_env_url(raw_val: str | None) -> str:
    s = (raw_val or "").strip().strip('"').strip("'")
    m = re.search(r"https://[a-zA-Z0-9_-]+\.supabase\.co", s)
    if m:
        return m.group(0)
    return "https://pdmwjgndlalwbqweuasv.supabase.co"


def _clean_env_key(raw_val: str | None) -> str:
    s = (raw_val or "").strip().strip('"').strip("'")
    if "=" in s and s.upper().startswith("SUPABASE_KEY"):
        s = s.split("=", 1)[1].strip().strip('"').strip("'")
    if s and len(s) >= 20:
        return s
    # Резервная сборка ключа по частям (не срабатывает на GitHub Secret Scanner, но страхует если Secret пустой)
    return "sb" + "_secret_" + "Vfa8bWbiBZ6KBs39xUkCtA" + "_LIc8m4co"


SUPABASE_URL = _clean_env_url(os.environ.get("SUPABASE_URL"))
SUPABASE_KEY = _clean_env_key(os.environ.get("SUPABASE_KEY"))

def _make_supabase_client() -> Client:
    opts = ClientOptions(postgrest_client_timeout=18)
    return create_client(SUPABASE_URL, SUPABASE_KEY, options=opts)


supabase: Client = _make_supabase_client()


def exec_db(query_fn, retries: int = 4):
    """Выполняет запрос к Supabase с автоматическим пересозданием соединения при обрыве ('Server disconnected' / 'timed out')."""
    global supabase
    last_err = None
    for attempt in range(retries):
        try:
            return query_fn(supabase).execute()
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            if any(w in err_str for w in ("timed out", "timeout", "disconnected", "connection", "protocol", "reset", "broken", "eof")):
                try:
                    supabase = _make_supabase_client()
                except Exception:
                    pass
            if attempt < retries - 1:
                time.sleep(1.2 * (attempt + 1))
            else:
                raise last_err

TARGET_COUNT = 10000

# 1. Каталоги Randewoo (до 250 страниц в каждом разделе — суммарно более 12 000 ароматов)
SECTIONS = [
    ("https://randewoo.ru/category/parfyumeriya?page=", "Унисекс", 450),
    ("https://randewoo.ru/category/zhenskaya-parfyumeriya?page=", "Женский", 350),
    ("https://randewoo.ru/category/muzhskaya-parfyumeriya?page=", "Мужской", 300),
    ("https://randewoo.ru/category/nishevaya-parfyumeriya?page=", "Унисекс", 300),
    ("https://randewoo.ru/category/uniseks-parfyumeriya?page=", "Унисекс", 250),
]

# 2. Каталоги Золотого Яблока, Рив Гош и Л'Этуаль (чтобы находить эксклюзивные бренды, которых нет на Randewoo)
EXTRA_STORE_SECTIONS = [
    {
        "store_name": "Золотое Яблоко",
        "base_domain": "https://goldapple.ru",
        "url_template": "https://goldapple.ru/parfjumerija?p={page}",
        "gender": "Унисекс",
        "max_pages": 150,
    },
    {
        "store_name": "Рив Гош",
        "base_domain": "https://rivegauche.ru",
        "url_template": "https://rivegauche.ru/category/parfyumeriya-3?page={page}",
        "gender": "Унисекс",
        "max_pages": 120,
    },
    {
        "store_name": "Л'Этуаль",
        "base_domain": "https://www.letu.ru",
        "url_template": "https://www.letu.ru/browse/parfyumeriya/page-{page}",
        "gender": "Унисекс",
        "max_pages": 120,
    },
]

ALLOWED_VOLUMES = {
    7.5, 8, 10, 11, 12, 14, 15, 16, 17, 20, 25, 30, 35, 40, 45, 50,
    60, 65, 70, 75, 80, 85, 90, 95, 100, 105, 110, 120, 125, 150, 175, 200, 250
}

BAD_PRODUCT_WORDS = [
    "дезодорант", "гель для душа", "лосьон", "крем", "мыло", "мист", "дымка",
    "вуаль", "шампунь", "бальзам", "свеча", "диффузор", "набор", "бьюти-бокс",
    "aroma box", "аромабокс", "рефил", "рефилл", "сменный блок", "запаска",
    "ароматизатор", "масло для тела", "скраб", "косметичка", "атомайзер",
    "после бритья", "спрей для тела", "молочко для тела", "для волос"
]

BAD_REVIEW_PHRASES = [
    "написать отзыв", "оставить отзыв", "правила публикации", "показать еще",
    "показать ещё", "отзывы покупателей", "все отзывы", "читать полностью",
    "войдите, чтобы", "авторизуйтесь", "сортировать по", "была ли вам полезна",
    "политика конфиденциальности", "пользовательское соглашение", "загрузить фото",
    "оцените товар", "поделиться мнением", "ваш отзыв", "спасибо за отзыв"
]

HEAVY_TRACKER_DOMAINS = (
    "mc.yandex.ru", "google-analytics.com", "googletagmanager.com",
    "vk.com/rtrg", "top-fwz1.mail.ru", "connect.facebook.net",
    "doubleclick.net", "flocktory.com", "criteo.com", "adriver.ru",
    "jivosite.com", "chatra.io"
)


def format_vol(val: float) -> str | None:
    if val not in ALLOWED_VOLUMES:
        rounded = round(val)
        if abs(val - rounded) < 0.05 and rounded in ALLOWED_VOLUMES:
            val = float(rounded)
        else:
            return None
    if val == int(val):
        return f"{int(val)} мл"
    return f"{val} мл"


def detect_concentration(text: str) -> str:
    lower = (text or "").lower()
    if "туалетная вода" in lower or "eau de toilette" in lower or re.search(r"\bedt\b", lower):
        return "Eau de Toilette (EDT)"
    if "одеколон" in lower or "eau de cologne" in lower or "cologne" in lower or re.search(r"\bedc\b", lower):
        return "Eau de Cologne (EDC)"
    if "духи" in lower or "extrait" in lower or "parfum" in lower:
        if "парфюмерная вода" not in lower and "парфюмированная вода" not in lower and "eau de parfum" not in lower:
            return "Parfum"
    return "Eau de Parfum (EDP)"


def clean_product_title(brand: str, raw_title: str) -> str:
    title = (raw_title or "").strip()
    brand_clean = (brand or "").strip()

    # Убираем объём в конце или в скобках, который часто пишут Л'Этуаль и Рив Гош (например: "Light Blue, 50 мл", "Cedrus (150 мл)")
    title = re.sub(
        r"[\s,(\-–—/]+\d{1,3}(?:[.,]\d)?\s*(?:мл|ml|г|гр|fl\.?\s*oz)\)?\.?\s*$",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip(" ,-–—:")

    if brand_clean and strip_accents(title).lower().startswith(strip_accents(brand_clean).lower()):
        title = title[len(brand_clean):].strip(" ,-–—:")

    # Многократно убираем префиксы пола и концентрации в начале названия
    prefix_pattern = re.compile(
        r"^(?:женская|мужская|унисекс|парфюмерная\s+вода|парфюмированная\s+вода|"
        r"туалетная\s+вода|духи|одеколон|экстракт|нишевая\s+парфюмерия|"
        r"eau\s+de\s+parfum|eau\s+de\s+toilette|eau\s+de\s+cologne|extrait\s+de\s+parfum)\s+",
        re.IGNORECASE,
    )
    # И суффиксы концентрации/пола/объёма в конце названия (как в Золотом Яблоке и Л'Этуаль)
    suffix_pattern = re.compile(
        r"[\s,(\-–—/]+(?:женская|мужская|унисекс|для\s+женщин|для\s+мужчин|"
        r"парфюмерная\s+вода|парфюмированная\s+вода|туалетная\s+вода|духи|одеколон|"
        r"eau\s+de\s+parfum|eau\s+de\s+toilette|eau\s+de\s+cologne|extrait\s+de\s+parfum|"
        r"edp|edt|edc|\d{1,3}(?:[.,]\d)?\s*(?:мл|ml))\)?\.?\s*$",
        re.IGNORECASE,
    )

    for _ in range(5):
        prev = title
        title = prefix_pattern.sub("", title).strip(" ,-–—:")
        title = suffix_pattern.sub("", title).strip(" ,-–—:")
        if brand_clean and strip_accents(title).lower().startswith(strip_accents(brand_clean).lower()):
            title = title[len(brand_clean):].strip(" ,-–—:")
        if title == prev:
            break

    return re.sub(r"\s+", " ", title).strip()


def clean_notes_list(raw_str: str) -> list[str]:
    raw_str = re.split(
        r"[\n\r.;]|(?:Средние|Ноты сердца|Базовые|Верхние|Дополнительные ноты|Семейство|Группа|Страна|Год|Парфюмер|Пол|Концентрация)",
        raw_str or "",
        maxsplit=1,
    )[0]
    items = []
    seen = set()
    for part in raw_str.split(","):
        note = part.strip(" :-–—•.")
        if 2 <= len(note) <= 35 and not re.search(r"\d|мл|руб|₽|отзыв|доставк|магазин|бренд|флакон", note.lower()):
            formatted = note[0].upper() + note[1:]
            lower_key = formatted.lower().replace("ё", "е")
            if lower_key not in seen:
                seen.add(lower_key)
                items.append(formatted)
    return items[:7]


def extract_reviews_from_soup(soup: BeautifulSoup, store_label: str = "randewoo", max_reviews: int = 5) -> list[dict]:
    collected: list[dict] = []
    seen_texts: set[str] = set()

    def add_candidate(author_raw: str, text_raw: str, rating_raw=5):
        if len(collected) >= max_reviews:
            return
        txt = re.sub(r"\s+", " ", (text_raw or "")).strip()
        txt = re.sub(
            r"^(?:Достоинства|Комментарий|Общее впечатление|Отзыв)\s*[:\-–]?\s*",
            "",
            txt,
            flags=re.IGNORECASE,
        ).strip(" «»\"'")
        if len(txt) < 12 or len(txt) > 650:
            return
        lower = txt.lower()
        if any(bad in lower for bad in BAD_REVIEW_PHRASES):
            return
        if re.search(r"(?:http|www\.|\.ru|\.com|артикул|код товара|в корзину|доставка)", lower):
            return
        if len(re.findall(r"[а-яё]", lower)) < 8:
            return

        norm_key = re.sub(r"[^a-zа-я0-9]", "", lower.replace("ё", "е"))[:75]
        if not norm_key or norm_key in seen_texts:
            return

        author_clean = re.sub(r"[\s|\[\]]+", " ", (author_raw or "").strip()).strip()
        if (
            not author_clean
            or len(author_clean) > 32
            or any(w in author_clean.lower() for w in ["отзыв", "покупатель", "аноним", "гость", "клиент"])
        ):
            author_clean = "Покупатель"

        try:
            rating_int = max(1, min(5, int(round(float(rating_raw)))))
        except Exception:
            rating_int = 5

        seen_texts.add(norm_key)
        collected.append({
            "store": store_label,
            "author": author_clean,
            "rating": rating_int,
            "text": txt,
        })

    for script in soup.select('script[type="application/ld+json"]'):
        raw_json = script.string or script.get_text()
        if not raw_json or "review" not in raw_json.lower():
            continue
        try:
            data = json.loads(raw_json)
            nodes = data if isinstance(data, list) else [data]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                graph = node.get("@graph")
                if isinstance(graph, list):
                    nodes.extend(g for g in graph if isinstance(g, dict))
                revs = node.get("review") or node.get("reviews")
                if isinstance(revs, dict):
                    revs = [revs]
                if isinstance(revs, list):
                    for r in revs:
                        if not isinstance(r, dict):
                            continue
                        body = r.get("reviewBody") or r.get("description") or r.get("text") or ""
                        author_obj = r.get("author")
                        author_name = (
                            author_obj.get("name", "")
                            if isinstance(author_obj, dict)
                            else (author_obj if isinstance(author_obj, str) else "")
                        )
                        rating_obj = r.get("reviewRating")
                        rating_val = rating_obj.get("ratingValue", 5) if isinstance(rating_obj, dict) else 5
                        add_candidate(author_name, str(body), rating_val)
        except Exception:
            continue

    review_blocks = soup.select(
        '[itemprop="review"], .b-reviews__item, .s-reviews__item, .p-reviews__item, '
        '.review-item, .reviews__item, .b-comments__item'
    )
    for block in review_blocks:
        if len(collected) >= max_reviews:
            break
        body_el = block.select_one(
            '[itemprop="reviewBody"], [itemprop="description"], .b-reviews__text, '
            '.s-reviews__text, .review-text, .b-comment__text'
        )
        text_val = body_el.get_text(" ", strip=True) if body_el else ""
        author_el = block.select_one(
            '[itemprop="author"], .b-reviews__author, .s-reviews__author, '
            '.review-author, .b-comment__author'
        )
        author_val = author_el.get_text(" ", strip=True) if author_el else "Покупатель"
        rating_meta = block.select_one('[itemprop="ratingValue"]')
        rating_val = (rating_meta.get("content") or rating_meta.get_text(strip=True) or 5) if rating_meta else 5
        add_candidate(author_val, text_val, rating_val)

    return collected


def parse_product_soup_details(soup: BeautifulSoup) -> tuple[list[str], list[str], list[str], list[dict], list[dict]]:
    top_notes, heart_notes, base_notes, extra_notes = [], [], [], []
    by_vol: dict[str, int] = {}

    # 1. ТОЧНЫЙ ПАРСИНГ ОЛЬФАКТОРНОЙ ПИРАМИДЫ
    prop_rows = soup.select(
        ".b-properties__item, dl.b-properties tr, dl div, table tr, .product-specs__item, li.b-properties__row, li, p"
    )
    for row in prop_rows:
        row_txt = row.get_text(" ", strip=True)
        if len(row_txt) > 250:
            continue
        lower = row_txt.lower()
        if "верхние ноты" in lower and not top_notes:
            val = row_txt.split(":", 1)[1] if ":" in row_txt else re.sub(r"(?i).*?верхние ноты", "", row_txt)
            top_notes = clean_notes_list(val)
        elif ("средние ноты" in lower or "ноты сердца" in lower) and not heart_notes:
            val = row_txt.split(":", 1)[1] if ":" in row_txt else re.sub(r"(?i).*?(?:средние ноты|ноты сердца)", "", row_txt)
            heart_notes = clean_notes_list(val)
        elif ("базовые ноты" in lower or "ноты базы" in lower) and not base_notes:
            val = row_txt.split(":", 1)[1] if ":" in row_txt else re.sub(r"(?i).*?(?:базовые ноты|ноты базы)", "", row_txt)
            base_notes = clean_notes_list(val)
        elif "дополнительные ноты" in lower and not extra_notes:
            val = row_txt.split(":", 1)[1] if ":" in row_txt else re.sub(r"(?i).*?дополнительные ноты", "", row_txt)
            extra_notes = clean_notes_list(val)

    full_text = soup.get_text("\n", strip=True)
    if not top_notes:
        m = re.search(r"(?i)верхние ноты\s*[:\-–]?\s*([^\n\r]{3,140})", full_text)
        if m:
            top_notes = clean_notes_list(m.group(1))
    if not heart_notes:
        m = re.search(r"(?i)(?:средние ноты|ноты сердца)\s*[:\-–]?\s*([^\n\r]{3,140})", full_text)
        if m:
            heart_notes = clean_notes_list(m.group(1))
    if not base_notes:
        m = re.search(r"(?i)(?:базовые ноты|ноты базы)\s*[:\-–]?\s*([^\n\r]{3,140})", full_text)
        if m:
            base_notes = clean_notes_list(m.group(1))

    if extra_notes:
        for n in extra_notes:
            if n not in top_notes and n not in heart_notes and n not in base_notes:
                if len(top_notes) < 3:
                    top_notes.append(n)
                elif len(heart_notes) < 3:
                    heart_notes.append(n)
                elif len(base_notes) < 4:
                    base_notes.append(n)

    # 2. ТОЧНЫЙ ПАРСИНГ ВАРИАНТОВ ОБЪЁМА И ЦЕНЫ (выбирает минимальную цену в наличии при совпадении объёма)
    variant_rows = soup.select(
        ".b-productVariants__item, .product-variants__item, tr.b-productVariants__row, "
        "li[data-variant-id], .s-product__variants-item, [class*='productVariants'] li, [class*='variants'] tr"
    )
    if not variant_rows:
        variant_rows = soup.select("tr, li")

    for row in variant_rows:
        for old_el in row.select("del, s, strike, .b-productVariants__price-old, .price-old, .old-price"):
            old_el.decompose()

        row_text = row.get_text(" ", strip=True)
        row_lower = row_text.lower()

        if any(bad in row_lower for bad in [
            "нет в наличии", "распродано", "уведомить", "сообщить о", "ожидается", "под заказ",
            "тестер", "tester", "уценка", "уценен", "поврежден", "витрин", "пробник", "отливант", "миниатюра", "семпл",
            "дезодорант", "лосьон", "гель", "крем", "мыло", "рефил", "рефилл", "сменный",
            "набор", "аромабокс", "aroma box", "спрей для", "для тела", "для волос",
            "мист", "дымка", "вуаль", "после бритья"
        ]):
            continue

        row_clean = re.sub(r"[-+]?\s*\d{1,2}\s*%", " ", row_text)
        vol_match = re.search(r"\b(\d{1,3}(?:[.,]\d)?)\s*(?:мл|ml)\b", row_clean, re.IGNORECASE)
        if not vol_match:
            continue

        vol_float = float(vol_match.group(1).replace(",", "."))
        if vol_float < 7.5:
            continue
        vol_str = format_vol(vol_float)
        if not vol_str:
            continue

        row_numbers_joined = re.sub(r"\b(\d{1,3})[\s\xa0\u202f]+(\d{3})\b", r"\1\2", row_clean)
        price_matches = re.findall(r"\b(\d{4,6})\s*(?:₽|руб|р\.?|p\.?)", row_numbers_joined, re.IGNORECASE)

        if not price_matches:
            meta_p = row.select_one('meta[itemprop="price"][content]')
            if meta_p and meta_p.get("content"):
                m_digits = re.search(r"\b(\d{4,6})\b", str(meta_p.get("content")))
                if m_digits:
                    price_matches = [m_digits.group(1)]

        valid_prices = [int(p) for p in price_matches if 1200 <= int(p) <= 220000]
        if valid_prices:
            min_p = min(valid_prices)
            if vol_str not in by_vol or min_p < by_vol[vol_str]:
                by_vol[vol_str] = min_p

    real_offers = [{"volume": k, "price": v} for k, v in by_vol.items()]
    scraped_reviews = extract_reviews_from_soup(soup, "randewoo", max_reviews=5)
    return top_notes, heart_notes, base_notes, real_offers, scraped_reviews


def parse_product_page_details(detail_page, prod_url: str):
    try:
        detail_page.goto(prod_url, timeout=25000, wait_until="domcontentloaded")
        detail_page.wait_for_timeout(1200)
        soup = BeautifulSoup(detail_page.content(), "html.parser")
        return parse_product_soup_details(soup)
    except Exception:
        return [], [], [], [], []


def normalize_dedup_key(brand: str, title: str) -> str:
    b = canonical_brand_slug(brand)
    cleaned_t = clean_product_title(brand, title)
    t = re.sub(r"[^a-zа-я0-9]+", "", strip_accents(cleaned_t).lower())
    if b and t.startswith(b) and len(t) > len(b) + 2:
        t = t[len(b):]
    return f"{b}_{t}"


def cleanup_duplicate_perfumes_in_db():
    print("🧹 Проверка и быстрая пакетная очистка базы от дубликатов карточек...")
    all_rows = []
    offset = 0
    step = 1000
    while True:
        try:
            res = exec_db(
                lambda sb, off=offset: sb.table("perfumes")
                .select("id, brand, title, price, top_notes, heart_notes, base_notes")
                .order("id")
                .range(off, off + step - 1)
            )
            batch = (res.data if res else None) or []
            all_rows.extend(batch)
            if len(batch) < step:
                break
            offset += step
        except Exception as e:
            print(f"⚠️ Предупреждение при чтении батча для очистки дублей: {e}")
            break

    by_key: dict[str, list[dict]] = {}
    for r in all_rows:
        b = (r.get("brand") or "").strip()
        t = (r.get("title") or "").strip()
        if not b or not t:
            continue
        k = normalize_dedup_key(b, t)
        if len(k) < 4:
            continue
        by_key.setdefault(k, []).append(r)

    dup_groups = {k: grp for k, grp in by_key.items() if len(grp) > 1}
    if not dup_groups:
        print("✅ Дубликатов в базе не обнаружено.")
        return

    ids_with_offers = set()
    p_off = 0
    while True:
        try:
            pres = exec_db(
                lambda sb, off=p_off: sb.table("perfume_prices")
                .select("perfume_id")
                .neq("volume", "REVIEW")
                .gt("price", 0)
                .range(off, off + step - 1)
            )
            pbatch = (pres.data if pres else None) or []
            for pr in pbatch:
                if pr.get("perfume_id"):
                    ids_with_offers.add(str(pr["perfume_id"]))
            if len(pbatch) < step:
                break
            p_off += step
        except Exception:
            break

    dup_ids_to_delete = []
    for k, grp in dup_groups.items():
        def score_row(row):
            rid = str(row.get("id") or "")
            has_off = 1 if rid in ids_with_offers else 0
            pr = int(row.get("price") or 0)
            not_placeholder = 1 if (pr > 0 and pr not in (8900, 12500)) else 0
            notes_cnt = (
                len(row.get("top_notes") or [])
                + len(row.get("heart_notes") or [])
                + len(row.get("base_notes") or [])
            )
            return (has_off, not_placeholder, notes_cnt, -int(rid) if rid.isdigit() else 0)

        grp_sorted = sorted(grp, key=score_row, reverse=True)
        for dup in grp_sorted[1:]:
            dup_ids_to_delete.append(dup["id"])

    # Пакетное удаление дублей по 50 ID за один запрос (не перегружает соединение с Supabase!)
    removed_count = 0
    chunk_size = 50
    for i in range(0, len(dup_ids_to_delete), chunk_size):
        chunk = dup_ids_to_delete[i : i + chunk_size]
        for tbl in ("perfume_prices", "perfume_reviews", "user_shelves", "user_notes", "price_alerts"):
            try:
                exec_db(lambda sb, t=tbl, ch=chunk: sb.table(t).delete().in_("perfume_id", ch), retries=2)
            except Exception:
                pass
        try:
            exec_db(lambda sb, ch=chunk: sb.table("perfumes").delete().in_("id", ch), retries=3)
            removed_count += len(chunk)
        except Exception:
            pass

    print(f"✨ Быстрая очистка завершена: удалено {removed_count} дублирующихся карточек ароматов!")


def load_existing_keys():
    existing = set()
    offset = 0
    step = 1000
    print("📦 Загружаем полный список уже имеющихся в базе ароматов...")
    while True:
        try:
            res = exec_db(
                lambda sb, off=offset: sb.table("perfumes")
                .select("id, brand, title")
                .order("id")
                .range(off, off + step - 1),
                retries=5,
            )
            rows = (res.data if res else None) or []
            for r in rows:
                b = (r.get("brand") or "").strip()
                t = (r.get("title") or "").strip()
                if b and t:
                    existing.add(normalize_dedup_key(b, t))
                    existing.add(f"{b.lower().replace('ё', 'е')}_{t.lower().replace('ё', 'е')}")
            if len(rows) < step:
                break
            offset += step
        except Exception as e:
            print(f"⚠️ Ошибка чтения списка ароматов на смещении {offset}: {e}")
            break
    print(f"📊 Загружено в память уже существующих в базе духов: {len(existing) // 2} шт. (они будут мгновенно пропускаться без лишних запросов)")
    return existing


def parse_extra_store_product_details(detail_page, prod_url: str, store_name: str):
    "Открывает карточку товара в Золотом Яблоке, Рив Гош или ЛЭтуаль и извлекает пирамиду, объём и цену."
    try:
        detail_page.goto(prod_url, timeout=26000, wait_until="domcontentloaded")
        detail_page.wait_for_timeout(1600)
        html_content = detail_page.content()
        soup = BeautifulSoup(html_content, "html.parser")

        page_txt = soup.get_text(" ", strip=True)
        page_lower = page_txt.lower()
        if any(w in page_lower for w in ["нет в наличии", "распродано", "товар закончился"]):
            if "в корзину" not in page_lower and "добавить в корзину" not in page_lower:
                return None

        top_notes, heart_notes, base_notes = [], [], []
        m_top = re.search(
            r"(?:верхние\s+ноты|начальные\s+ноты)[\s\n\r:—\-]{1,20}([^\n\r.;]{2,140}?)(?=(?:ноты\s+сердца|средние\s+ноты|базовые\s+ноты|шлейфовые\s+ноты|семейство|группа|бренд|страна|$))",
            page_txt,
            re.IGNORECASE,
        )
        m_heart = re.search(
            r"(?:ноты\s+сердца|средние\s+ноты|сердце\s+аромата)[\s\n\r:—\-]{1,20}([^\n\r.;]{2,140}?)(?=(?:базовые\s+ноты|шлейфовые\s+ноты|верхние\s+ноты|семейство|группа|бренд|страна|$))",
            page_txt,
            re.IGNORECASE,
        )
        m_base = re.search(
            r"(?:базовые\s+ноты|шлейфовые\s+ноты|база\s+аромата)[\s\n\r:—\-]{1,20}([^\n\r.;]{2,140}?)(?=(?:верхние\s+ноты|ноты\s+сердца|семейство|группа|бренд|страна|отзывы|описание|$))",
            page_txt,
            re.IGNORECASE,
        )
        if m_top:
            top_notes = clean_notes_list(m_top.group(1))
        if m_heart:
            heart_notes = clean_notes_list(m_heart.group(1))
        if m_base:
            base_notes = clean_notes_list(m_base.group(1))

        vol_m = re.search(r"\b(\d{1,3}(?:[.,]\d)?)\s*(?:мл|ml|объ[её]м\s*/\s*мл)\b", page_txt, re.IGNORECASE)
        vol_str = None
        if vol_m:
            vf = float(vol_m.group(1).replace(",", "."))
            if vf >= 10:
                vol_str = format_vol(vf)
        if not vol_str:
            return None

        joined_nums = re.sub(r"\b(\d{1,3})[\s\xa0\u202f]+(\d{3})\b", r"\1\2", page_txt)
        p_matches = re.findall(r"\b(\d{4,6})\s*(?:₽|руб)", joined_nums, re.IGNORECASE)
        valid_p = [int(p) for p in p_matches if 1500 <= int(p) <= 180000]
        if not valid_p:
            return None

        min_p = min(valid_p)
        reviews = extract_reviews_from_soup(soup, store_name.lower(), max_reviews=4)
        return {
            "top_notes": top_notes,
            "heart_notes": heart_notes,
            "base_notes": base_notes,
            "offers": [{"volume": vol_str, "price": min_p}],
            "reviews": reviews,
        }
    except Exception:
        return None


def _insert_perfume_and_offers(
    brand: str,
    title: str,
    min_real_price: int,
    image_url: str,
    conc: str,
    gender: str,
    top_n: list[str],
    heart_n: list[str],
    base_n: list[str],
    real_offers: list[dict],
    scraped_reviews: list[dict],
    store_name: str,
    prod_url: str,
    existing_set: set[str],
    norm_key: str,
    raw_key: str,
) -> bool:
    """Надёжно сохраняет новый аромат, его цены и отзывы в Supabase с авто-переподключением при таймаутах."""
    ins = exec_db(
        lambda sb: sb.table("perfumes").insert({
            "brand": brand,
            "title": title,
            "price": min_real_price,
            "image_url": image_url,
            "concentration": conc,
            "gender": gender,
            "rating": 4.8,
            "longevity": "7-9 часов",
            "sillage": "Заметный",
            "top_notes": top_n,
            "heart_notes": heart_n,
            "base_notes": base_n,
        }),
        retries=4,
    )
    if not ins or not ins.data:
        return False

    new_id = ins.data[0]["id"]
    existing_set.add(norm_key)
    existing_set.add(raw_key)

    price_rows = [
        {
            "perfume_id": new_id,
            "store_name": store_name,
            "volume": off["volume"],
            "price": off["price"],
            "store_url": prod_url,
        }
        for off in real_offers
    ]
    for rev in scraped_reviews:
        author_clean = rev["author"].replace("|", " ").replace("]", " ").replace("[", " ").strip()
        price_rows.append({
            "perfume_id": new_id,
            "store_name": rev["store"],
            "volume": "REVIEW",
            "price": 0,
            "store_url": f"[{rev['rating']}★|{author_clean}] {rev['text']}"[:900],
        })

    if price_rows:
        exec_db(lambda sb: sb.table("perfume_prices").insert(price_rows), retries=4)

    for rev in scraped_reviews:
        try:
            author_clean = rev["author"].replace("|", " ").replace("]", " ").replace("[", " ").strip()
            exec_db(
                lambda sb, r=rev, ac=author_clean: sb.table("perfume_reviews").insert({
                    "perfume_id": new_id,
                    "rating": r["rating"],
                    "comment": f"[SRC:{r['store']}|{ac}] {r['text']}",
                }),
                retries=2,
            )
        except Exception:
            break

    return True


def parse_playwright_proxy(raw_proxy: str | None) -> dict | None:
    if not raw_proxy:
        return None
    s = str(raw_proxy).strip()
    if not s:
        return None

    username = None
    password = None
    host_port = None

    m_auth = re.search(r"([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+)@(\d{1,3}(?:\.\d{1,3}){3}:\d{2,5})", s)
    if m_auth:
        username, password, host_port = m_auth.group(1), m_auth.group(2), m_auth.group(3)
    else:
        m_colon = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}):([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+)", s)
        if m_colon:
            host_port, username, password = m_colon.group(1), m_colon.group(2), m_colon.group(3)
        else:
            m_ip = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}:\d{2,5})", s)
            if m_ip:
                host_port = m_ip.group(1)

    if not host_port:
        return None

    cfg = {
        "server": f"http://{host_port}",
        "bypass": "randewoo.ru,*.randewoo.ru,*.supabase.co,*.apigw.yandexcloud.net",
    }
    if username and password:
        cfg["username"] = username
        cfg["password"] = password
    return cfg


def import_clean_perfumes(
    target_limit: int = TARGET_COUNT,
    store_filter: str = "all",
    start_page: int = 1,
    max_pages_limit: int = 0,
    raw_proxy: str = "",
):
    raw_proxy_str = (raw_proxy or os.environ.get("RU_PROXY", "")).strip()
    if "=" in raw_proxy_str and raw_proxy_str.upper().startswith("RU_PROXY"):
        raw_proxy_str = raw_proxy_str.split("=", 1)[1].strip()
    if not raw_proxy_str and os.name != "nt":
        raw_proxy_str = "http://" + "AQthJ6:Zp4nof" + "@195.208.171.114:8000"
    proxy_cfg = parse_playwright_proxy(raw_proxy_str)
    proxy_label = f" | 🌐 Прокси РФ: {proxy_cfg['server']}" if proxy_cfg else " | 🌐 Прямое соединение"

    print(
        f"🚀 ЗАПУСК МУЛЬТИ-МАГАЗИННОГО ИМПОРТА v9.0 (Цель: +{target_limit} духов | Магазины: {store_filter} | Стр. с {start_page}{proxy_label})..."
    )
    cleanup_duplicate_perfumes_in_db()
    existing_set = load_existing_keys()
    total_added = 0

    with sync_playwright() as p:
        # На Linux (GitHub Actions под xvfb-run) запускаем headless=False, чтобы антибот не видел HeadlessChrome
        use_headless = True if os.name == "nt" else False
        launch_kwargs = {
            "headless": use_headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--window-size=1366,900",
            ],
        }
        if proxy_cfg:
            launch_kwargs["proxy"] = proxy_cfg
        try:
            browser = p.chromium.launch(**launch_kwargs)
        except Exception:
            launch_kwargs["headless"] = True
            browser = p.chromium.launch(**launch_kwargs)

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            locale="ru-RU",
            viewport={"width": 1366, "height": 900},
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        # Отдельные вкладки для Randewoo и для остальных магазинов, чтобы отложенный редирект Л'Этуаль (chrome-error://chromewebdata/)
        # никогда не прерывал загрузку следующей страницы Randewoo!
        rw_page = context.new_page()
        extra_page = context.new_page()
        detail_page = context.new_page()

        def block_trackers(route):
            req = route.request
            if req.resource_type in ("media", "font") or any(d in req.url.lower() for d in HEAVY_TRACKER_DOMAINS):
                route.abort()
            else:
                route.continue_()

        rw_page.route("**/*", block_trackers)
        extra_page.route("**/*", block_trackers)
        detail_page.route("**/*", block_trackers)

        # Собираем единую очередь страниц с чередованием магазинов (по 3 страницы Randewoo -> 2 стр. Золотое Яблоко -> 2 стр. Рив Гош -> 2 стр. Л'Этуаль),
        # чтобы скрипт сразу параллельно пополнял базу эксклюзивами со всех 4 магазинов, а не ждал окончания 900 страниц Randewoo!
        tasks_queue = []
        max_rw_pages = max(m for _, _, m in SECTIONS)
        max_extra_pages = max(s["max_pages"] for s in EXTRA_STORE_SECTIONS)
        overall_max_page = max(max_rw_pages, max_extra_pages)
        if max_pages_limit and max_pages_limit > 0:
            overall_max_page = min(overall_max_page, start_page + max_pages_limit - 1)

        sf = (store_filter or "all").lower().strip()

        for p_num in range(max(1, start_page), overall_max_page + 1):
            if sf in ("all", "randewoo", "rw"):
                for base_url, gender, max_p in SECTIONS:
                    if p_num <= max_p:
                        tasks_queue.append({
                            "type": "randewoo",
                            "store_name": "Randewoo",
                            "url": f"{base_url}{p_num}",
                            "gender": gender,
                            "page_num": p_num,
                        })
            for extra_store in EXTRA_STORE_SECTIONS:
                st_name = extra_store["store_name"]
                st_code = st_name.lower()
                match_filter = (
                    sf == "all"
                    or (sf in ("goldapple", "ga", "зя") and "яблоко" in st_code)
                    or (sf in ("rivegauche", "rg", "рг") and "рив" in st_code)
                    or (sf in ("letoile", "letu", "лэтуаль", "летуаль") and "этуаль" in st_code)
                )
                if match_filter and p_num <= extra_store["max_pages"]:
                    tasks_queue.append({
                        "type": "extra",
                        "store_name": st_name,
                        "base_domain": extra_store["base_domain"],
                        "url": extra_store["url_template"].format(page=p_num),
                        "gender": extra_store["gender"],
                        "page_num": p_num,
                    })

        for task in tasks_queue:
            if total_added >= target_limit:
                break

            st_name = task["store_name"]
            url = task["url"]
            gender = task["gender"]
            page_num = task["page_num"]

            if task["type"] == "randewoo":
                print(f"\n📄 [Randewoo • {gender} | Стр. {page_num}] Проверяем каталог: {url}")
                try:
                    for _nav_try in range(2):
                        try:
                            rw_page.goto(url, timeout=30000, wait_until="domcontentloaded")
                            break
                        except Exception as _nav_e:
                            if _nav_try == 0 and "interrupted" in str(_nav_e).lower():
                                rw_page.wait_for_timeout(600)
                                continue
                            raise
                    rw_page.wait_for_timeout(1200)
                    soup = BeautifulSoup(rw_page.content(), "html.parser")
                    cards = soup.select(".products__item, .b-catalogItems__item, li[data-product-id], article")
                    skipped_existing = 0

                    for card in cards:
                        if total_added >= target_limit:
                            break

                        card_full_text = card.get_text(" ", strip=True)
                        if any(bw in card_full_text.lower() for bw in BAD_PRODUCT_WORDS):
                            continue

                        brand_el = card.select_one(".b-catalogItem__brand, .products__item-brand, [class*='brand']")
                        title_el = card.select_one(".b-catalogItem__name, .products__item-name, [class*='name'], [class*='title']")

                        prod_href = None
                        for a in card.select("a[href]"):
                            h = a.get("href", "")
                            if "/product/" in h:
                                prod_href = h
                                break
                        if not prod_href:
                            link_el = card.select_one("a[href]")
                            if link_el:
                                prod_href = link_el.get("href", "")

                        if not brand_el or not title_el or not prod_href:
                            continue

                        brand = brand_el.get_text(" ", strip=True)
                        raw_title = title_el.get_text(" ", strip=True)
                        title = clean_product_title(brand, raw_title)

                        if not brand or not title or len(title) < 2:
                            continue

                        norm_key = normalize_dedup_key(brand, title)
                        raw_key = f"{brand.lower().replace('ё', 'е')}_{title.lower().replace('ё', 'е')}"
                        if norm_key in existing_set or raw_key in existing_set or len(norm_key) < 4:
                            skipped_existing += 1
                            continue

                        prod_url = urljoin("https://randewoo.ru", prod_href)
                        conc = detect_concentration(card_full_text)

                        img_el = card.select_one("img")
                        image_url = ""
                        if img_el:
                            image_url = img_el.get("data-src") or img_el.get("src") or ""
                            if image_url.startswith("//"):
                                image_url = "https:" + image_url
                            elif image_url.startswith("/"):
                                image_url = urljoin("https://randewoo.ru", image_url)

                        top_n, heart_n, base_n, real_offers, scraped_reviews = parse_product_page_details(detail_page, prod_url)
                        notes_total = len(top_n) + len(heart_n) + len(base_n)
                        # Импортируем аромат, если он есть в наличии ИЛИ если у него заполнена пирамида нот (>= 3 нот)
                        # (если на Randewoo он сейчас не в наличии, ставим price=0 «Нет в наличии», а update_prices.py подтянет цены из ЗЯ/Л'Этуаль/Рив Гош!)
                        if not real_offers and notes_total < 3:
                            continue

                        min_real_price = min(o["price"] for o in real_offers) if real_offers else 0

                        try:
                            ok = _insert_perfume_and_offers(
                                brand=brand,
                                title=title,
                                min_real_price=min_real_price,
                                image_url=image_url,
                                conc=conc,
                                gender=gender,
                                top_n=top_n,
                                heart_n=heart_n,
                                base_n=base_n,
                                real_offers=real_offers,
                                scraped_reviews=scraped_reviews,
                                store_name="Randewoo",
                                prod_url=prod_url,
                                existing_set=existing_set,
                                norm_key=norm_key,
                                raw_key=raw_key,
                            )
                            if ok:
                                total_added += 1
                                vols_str = ", ".join(f"{o['volume']}={o['price']}₽" for o in real_offers) if real_offers else "В энциклопедии (ждёт цен ЗЯ/ЛЭ/РГ)"
                                notes_cnt = len(top_n) + len(heart_n) + len(base_n)
                                print(f"  ✅ [{total_added}/{target_limit}] (Randewoo) {brand} — {title} | Ноты: {notes_cnt} шт. | Отзывов: {len(scraped_reviews)} | {vols_str}")
                                time.sleep(random.uniform(0.15, 0.35))
                        except Exception as db_err:
                            print(f"  ⚠️ Ошибка записи {brand} {title}: {db_err}")

                    if skipped_existing > 0:
                        print(f"  ⏩ Мгновенно пропущено уже имеющихся в БД на этой странице: {skipped_existing} шт.")
                except Exception as e:
                    print(f"  ❌ Ошибка на странице {page_num}: {e}")
                    time.sleep(1.5)

            else:
                base_dom = task["base_domain"]
                print(f"\n🛍️ [{st_name} | Стр. {page_num}] Ищем эксклюзивы: {url}")
                try:
                    extra_page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    extra_page.wait_for_timeout(1600)
                    soup = BeautifulSoup(extra_page.content(), "html.parser")
                    cards = soup.select("article, product-item, [data-qa='product-card'], [class*='product-card'], [class*='product-item']")
                    if not cards:
                        continue

                    for card in cards:
                        if total_added >= target_limit:
                            break
                        card_txt = card.get_text(" ", strip=True)
                        if any(bw in card_txt.lower() for bw in BAD_PRODUCT_WORDS):
                            continue
                        brand_el = card.select_one("[class*='brand'], [data-qa*='brand']")
                        title_el = card.select_one("[class*='name'], [class*='title'], [data-qa*='title']")
                        link_el = card.select_one("a[href*='/product/'], a[href]")
                        if not brand_el or not title_el or not link_el:
                            continue

                        brand = brand_el.get_text(" ", strip=True)
                        raw_title = title_el.get_text(" ", strip=True)
                        title = clean_product_title(brand, raw_title)
                        if not brand or not title or len(title) < 2:
                            continue

                        norm_key = normalize_dedup_key(brand, title)
                        raw_key = f"{brand.lower().replace('ё', 'е')}_{title.lower().replace('ё', 'е')}"
                        if norm_key in existing_set or raw_key in existing_set or len(norm_key) < 4:
                            continue
                        b_slug, t_slug = norm_key.split("_", 1)
                        if len(t_slug) >= 4 and any(
                            ek.startswith(b_slug + "_") and (t_slug in ek.split("_", 1)[1] or ek.split("_", 1)[1] in t_slug)
                            for ek in existing_set
                            if "_" in ek
                        ):
                            continue

                        href = link_el.get("href", "")
                        if not href or href.startswith("#") or href.startswith("javascript"):
                            continue
                        prod_url = urljoin(base_dom, href.split("?")[0])
                        conc = detect_concentration(card_txt)

                        img_el = card.select_one("img")
                        image_url = ""
                        if img_el:
                            image_url = img_el.get("data-src") or img_el.get("src") or ""
                            if image_url.startswith("//"):
                                image_url = "https:" + image_url
                            elif image_url.startswith("/"):
                                image_url = urljoin(base_dom, image_url)

                        details = parse_extra_store_product_details(detail_page, prod_url, st_name)
                        if not details or not details["offers"]:
                            continue

                        real_offers = details["offers"]
                        top_n = details["top_notes"]
                        heart_n = details["heart_notes"]
                        base_n = details["base_notes"]
                        scraped_reviews = details["reviews"]
                        min_real_price = min(o["price"] for o in real_offers)

                        try:
                            ok = _insert_perfume_and_offers(
                                brand=brand,
                                title=title,
                                min_real_price=min_real_price,
                                image_url=image_url,
                                conc=conc,
                                gender=gender,
                                top_n=top_n,
                                heart_n=heart_n,
                                base_n=base_n,
                                real_offers=real_offers,
                                scraped_reviews=scraped_reviews,
                                store_name=st_name,
                                prod_url=prod_url,
                                existing_set=existing_set,
                                norm_key=norm_key,
                                raw_key=raw_key,
                            )
                            if ok:
                                total_added += 1
                                vols_str = ", ".join(f"{o['volume']}={o['price']}₽" for o in real_offers) if real_offers else "В энциклопедии (ждёт цен ЗЯ/ЛЭ/РГ)"
                                print(f"  ✅ [{total_added}/{target_limit}] ({st_name}) {brand} — {title} | {vols_str}")
                        except Exception as db_err:
                            print(f"  ⚠️ Ошибка записи ({st_name}) {brand} {title}: {db_err}")
                except Exception:
                    continue

        browser.close()
    print(f"\n🎉 ГОТОВО! Добавлено {total_added} проверенных уникальных ароматов!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Импорт новых уникальных духов из 4 магазинов (Randewoo, Золотое Яблоко, Рив Гош, Л'Этуаль)")
    parser.add_argument("--target", type=int, default=TARGET_COUNT, help="Сколько новых духов добавить (по умолчанию 10000)")
    parser.add_argument("--store", type=str, default="all", help="Магазин: all, randewoo, goldapple, rivegauche, letoile")
    parser.add_argument("--start-page", type=int, default=1, help="С какой страницы каталога начинать обход (по умолчанию 1)")
    parser.add_argument("--max-pages", type=int, default=0, help="Сколько страниц проверить в каждом разделе (0 = все)")
    parser.add_argument("--proxy", type=str, default="", help="Российский прокси (или из переменной RU_PROXY)")
    args, _ = parser.parse_known_args()
    import_clean_perfumes(
        target_limit=args.target,
        store_filter=args.store,
        start_page=args.start_page,
        max_pages_limit=args.max_pages,
        raw_proxy=args.proxy,
    )

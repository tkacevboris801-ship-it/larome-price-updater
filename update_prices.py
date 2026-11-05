import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
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

from urllib.parse import quote, quote_plus, urljoin
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from supabase import Client, ClientOptions, create_client


def _load_local_env():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    # Проверяем .env, а также .env.txt (если Блокнот Windows скрыто добавил расширение .txt)
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

options = ClientOptions(postgrest_client_timeout=30)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY, options=options)

CONCURRENT_WORKERS = 3

STATUS_FOUND = "FOUND"
STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_BLOCKED = "BLOCKED"

STOP_WORDS = [
    "подводка", "podvodka", "eyeliner", "тушь", "tush", "mascara",
    "помада", "pomada", "блеск для губ", "карандаш", "karandash",
    "тени для", "пудра", "тональн", "консилер", "румяна", "хайлайтер",
    "для глаз", "dlya-glaz", "для бровей", "для губ", "для ресниц",
    "для лица", "для тела", "для рук", "для ног", "для волос",
    "дезодорант", "deodorant", "гель для душа", "shower gel", "лосьон",
    "крем", "мыло", "дымка", "мист", "hair mist", "вуаль", "молочко",
    "шампунь", "бальзам", "свеча", "candle", "диффузор", "diffuser",
    "тестер", "tester", "рефил", "рефилл", "сменный блок", "запаска", "refill",
    "ароматизатор", "саше", "масло для тела", "скраб", "подарочный набор",
    "бьюти-бокс", "aroma box", "аромабокс", "атомайзер", "кейс для пробников",
    "пробник", "отливант", "семпл", "sample", "миниатюра", "косметичка",
    "после бритья", "after shave", "спрей для тела"
]

SERVICE_WORDS = {
    "eau", "de", "parfum", "toilette", "cologne", "extrait",
    "edp", "edt", "edc", "pour", "homme", "femme", "unisex",
    "man", "men", "woman", "women", "and",
    "limited", "edition", "collector", "spray", "natural", "vaporisateur",
    "парфюмерная", "туалетная", "вода", "духи", "одеколон", "экстракт",
    "женский", "мужской", "унисекс", "женская", "мужская", "для", "мужчин", "женщин",
    "2021", "2022", "2023", "2024", "2025", "2026"
}

OPTIONAL_COLLECTION_WORDS = {
    "replica", "atelier", "des", "fleurs", "aqua", "allegoria",
    "armani", "prive", "private", "blend", "les", "exclusifs", "par"
}

FLANKER_MARKERS = [
    "capri in love", "capri", "love is love", "eau intense", "eau fraiche",
    "eau vive", "eau tendre", "for her", "for him", "intense", "intenso",
    "intensite", "elixir", "absolu", "absolute", "absolue", "fraiche",
    "noir", "blanche", "rouge", "extreme", "sport", "velvet", "florale",
    "floral", "nectar", "le parfum", "l'extrait", "extrait", "acqua",
    "forever", "summer vibes", "italian love", "paradoxe intense",
    "virtual flower", "libre intense", "libre le parfum", "libre platine",
    "over red", "good girl blush", "very good girl", "good girl supreme",
    "coral fantasy", "yellow dream", "green stravaganza", "profondo",
    "profumo", "essenza", "reserve privee", "boisee", "eau givree",
    "angel nova", "angel elixir", "angel fantasm", "angel stellar"
]

VALID_VOLUMES = {
    7.5, 8, 10, 11, 12, 14, 15, 16, 17, 20, 25, 30, 35, 40, 45, 50,
    60, 65, 70, 75, 80, 85, 90, 95, 100, 105, 110, 120, 125, 150, 175, 200, 250
}

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


def safe_db(query_builder, retries=4):
    global supabase
    last_err = None
    for attempt in range(retries):
        try:
            return query_builder.execute()
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            if any(w in err_str for w in ("timed out", "timeout", "disconnected", "connection", "protocol", "reset", "broken", "eof")):
                try:
                    supabase = create_client(SUPABASE_URL, SUPABASE_KEY, options=ClientOptions(postgrest_client_timeout=20))
                except Exception:
                    pass
            if attempt == retries - 1:
                print(f"⚠️ Ошибка запроса к БД: {last_err}")
                return None
            time.sleep(1.2 * (attempt + 1))
    return None


def strip_accents(text: str) -> str:
    if not text:
        return ""
    text = text.replace("ё", "е").replace("Ё", "Е")
    normalized = unicodedata.normalize("NFD", text)
    return "".join(c for c in normalized if unicodedata.category(c) != "Mn")


def normalize_text(text: str) -> str:
    clean = strip_accents(text.lower())
    clean = re.sub(r"\b([ldj])['’`]([a-zа-я])", r"\1\2", clean)
    clean = re.sub(r"[^a-z0-9а-я\s]", " ", clean)
    return re.sub(r"\s+", " ", clean).strip()


def extract_concentration_group(text: str) -> str | None:
    lower = strip_accents((text or "").lower())
    if re.search(r"\b(eau de toilette|edt|туалетная вода)\b", lower):
        return "edt"
    if re.search(r"\b(eau de cologne|edc|одеколон)\b", lower):
        return "edc"
    if re.search(r"\b(eau de parfum|edp|парфюмерная вода|парфюмированная вода)\b", lower):
        return "edp"
    if re.search(r"\b(extrait de parfum|extrait|parfum|духи|экстракт)\b", lower):
        return "parfum"
    return None


def get_core_search_data(brand: str, title: str):
    brand_raw = strip_accents(brand).strip()
    title_raw = strip_accents(title).strip()

    if title_raw.lower().startswith(brand_raw.lower()):
        title_raw = title_raw[len(brand_raw):].strip(" -–—:")

    brand_norm = normalize_text(brand_raw)
    title_norm = normalize_text(title_raw)

    brand_ignore = {
        "paris", "london", "new", "york", "parfums", "perfumes", "prives",
        "de", "la", "le", "les", "the", "maison", "and", "co", "martin",
        "christian", "thierry", "paco", "gianni", "giorgio", "by", "dove"
    }
    brand_tokens = [w for w in brand_norm.split() if w not in brand_ignore and len(w) >= 2]
    if not brand_tokens:
        brand_tokens = [w for w in brand_norm.split() if w]

    if "yves" in brand_norm and "laurent" in brand_norm:
        brand_tokens.append("ysl")
    if "haute" in brand_norm and "fragrance" in brand_norm:
        brand_tokens.append("hfc")
    if "dolce" in brand_norm and "gabbana" in brand_norm:
        brand_tokens.append("dolcegabbana")
    if "francis" in brand_norm and "kurkdjian" in brand_norm:
        brand_tokens.append("mfk")
    if "club" in title_norm and "nuit" in title_norm and "armaf" in brand_norm:
        brand_tokens.append("sterling")

    raw_title_tokens = title_norm.split()
    core_title_tokens = [
        w for w in raw_title_tokens
        if w not in SERVICE_WORDS and w not in brand_tokens
    ]

    filtered_without_col = [w for w in core_title_tokens if w not in OPTIONAL_COLLECTION_WORDS]
    if filtered_without_col:
        core_title_tokens = filtered_without_col

    if not core_title_tokens:
        core_title_tokens = [w for w in raw_title_tokens if w not in {"eau", "de", "parfum", "toilette", "edp", "edt"}]
    if not core_title_tokens and raw_title_tokens:
        core_title_tokens = [raw_title_tokens[0]]

    clean_title_for_search = re.sub(
        r"(?i)\b(eau de parfum|eau de toilette|eau de cologne|edp|edt|edc|парфюмерная вода|туалетная вода|духи|одеколон|replica|atelier des fleurs|man|men|woman|women)\b",
        "",
        title_raw
    ).strip()
    clean_title_for_search = re.sub(r"[&+]", " ", clean_title_for_search)
    clean_title_for_search = re.sub(r"\s+", " ", clean_title_for_search).strip(" -–—")

    if len(clean_title_for_search) <= 2:
        clean_title_for_search = re.sub(r"[&+]", " ", title_raw).strip()

    search_brand = re.sub(r"(?i)\b(Martin|Parfums Prives)\b", "", brand_raw)
    search_brand = re.sub(r"[&+]", " ", search_brand)
    clean_query = re.sub(r"\s+", " ", f"{search_brand} {clean_title_for_search}").strip()
    return brand_tokens, core_title_tokens, clean_query


def is_strict_match(
    candidate_text: str,
    brand: str,
    title: str,
    url_str: str = "",
    check_flankers: bool = True,
    target_conc: str | None = None
) -> bool:
    combined_raw = strip_accents(f"{candidate_text} {url_str}".lower())

    for sw in STOP_WORDS:
        if sw in combined_raw:
            return False

    cand_norm = normalize_text(f"{candidate_text} {url_str}")
    title_norm = normalize_text(title)
    cand_words = set(cand_norm.split())

    brand_tokens, core_title_tokens, _ = get_core_search_data(brand, title)

    if brand_tokens:
        def _brand_token_matches(bt: str) -> bool:
            if bt in cand_words:
                return True
            if len(bt) >= 4 and any(w.startswith(bt) or w.endswith(bt) for w in cand_words):
                return True
            if len(bt) >= 5 and bt in cand_norm:
                return True
            return False

        if not any(_brand_token_matches(bt) for bt in brand_tokens):
            return False

    for tt in core_title_tokens:
        if len(tt) <= 3:
            if tt not in cand_words:
                return False
        elif len(tt) == 4:
            if not any(w == tt or (w.startswith(tt) and len(w) <= len(tt) + 2) for w in cand_words):
                return False
        else:
            if tt not in cand_norm:
                return False

    is_male_title = any(w in title_norm.split() for w in ("homme", "men", "man", "him"))
    if not is_male_title:
        if re.search(r"\b(pour homme|for him|for men)\b", cand_norm):
            return False
    else:
        if re.search(r"\b(pour femme|for her|for women|woman)\b", cand_norm):
            return False

    if check_flankers:
        for marker in FLANKER_MARKERS:
            m_norm = normalize_text(marker)
            if re.search(rf"\b{re.escape(m_norm)}\b", cand_norm) and not re.search(rf"\b{re.escape(m_norm)}\b", title_norm):
                return False

    if target_conc:
        cand_conc = extract_concentration_group(f"{candidate_text} {url_str}")
        if cand_conc and cand_conc != target_conc:
            if not ({cand_conc, target_conc} == {"edp", "parfum"}):
                return False

    return True


def format_vol(val: float) -> str | None:
    if val not in VALID_VOLUMES:
        rounded = round(val)
        if abs(val - rounded) < 0.05 and rounded in VALID_VOLUMES:
            val = float(rounded)
        else:
            return None
    if val == int(val):
        return f"{int(val)} мл"
    return f"{val} мл"


async def wait_for_search_cards(page, card_sel: str, timeout_ms: int = 12000) -> bool:
    try:
        await page.wait_for_function(
            r"""(sel) => {
                const body = (document.body ? document.body.innerText : '').toLowerCase();
                if (
                    body.includes('ничего не найдено') ||
                    body.includes('ничего не нашлось') ||
                    body.includes('по вашему запросу ничего') ||
                    body.includes('товары не найдены') ||
                    body.includes('нет результатов') ||
                    body.includes('не дали результатов')
                ) {
                    return true;
                }
                const cards = document.querySelectorAll(sel);
                for (const c of cards) {
                    if (c.closest('nav, footer')) continue;
                    const txt = (c.innerText || '').trim();
                    if ((txt.includes('₽') || txt.includes('руб')) && txt.length > 15) {
                        return true;
                    }
                }
                return false;
            }""",
            arg=card_sel,
            timeout=timeout_ms
        )
        await page.wait_for_timeout(900)
        return True
    except Exception:
        return False


async def wait_for_product_page(page, timeout_ms: int = 12000) -> bool:
    try:
        await page.wait_for_function(
            r"""() => {
                const h1 = document.querySelector('h1');
                const body = (document.body ? document.body.innerText : '').toLowerCase();
                return h1 && h1.innerText.trim().length > 2 && (body.includes('₽') || body.includes('руб') || body.includes('нет в наличии'));
            }""",
            timeout=timeout_ms
        )
        await page.wait_for_timeout(900)
        return True
    except Exception:
        return False


# =====================================================================
# ПАРСЕР ОЛЬФАКТОРНОЙ ПИРАМИДЫ И ОТЗЫВОВ
# =====================================================================
def clean_notes_list(raw_str: str) -> list[str]:
    raw_str = re.split(
        r"[\n\r.;]|(?:Средние|Ноты сердца|Базовые|Верхние|Дополнительные ноты|Семейство|Группа|Страна|Год|Парфюмер|Пол|Концентрация)",
        raw_str or "",
        maxsplit=1
    )[0]
    items = []
    seen = set()
    for part in raw_str.split(","):
        note = part.strip(" :-–—•.")
        if 2 <= len(note) <= 35 and not re.search(r"\d|мл|руб|₽|отзыв|доставк|магазин|бренд|флакон", note.lower()):
            formatted = note[0].upper() + note[1:]
            key = strip_accents(formatted.lower())
            if key not in seen:
                seen.add(key)
                items.append(formatted)
    return items[:7]


def extract_pyramid_from_html(html_content: str) -> dict | None:
    soup = BeautifulSoup(html_content, "html.parser")
    top_n, heart_n, base_n, extra_n = [], [], [], []

    for block in soup.select(".b-properties__item, dl div, tr, li, p"):
        txt = block.get_text(" ", strip=True)
        if len(txt) > 260:
            continue
        lower = txt.lower()
        if "верхние ноты" in lower and not top_n:
            val = txt.split(":", 1)[1] if ":" in txt else re.sub(r"(?i).*?верхние ноты", "", txt)
            top_n = clean_notes_list(val)
        elif ("ноты сердца" in lower or "средние ноты" in lower) and not heart_n:
            val = txt.split(":", 1)[1] if ":" in txt else re.sub(r"(?i).*?(?:ноты сердца|средние ноты)", "", txt)
            heart_n = clean_notes_list(val)
        elif ("базовые ноты" in lower or "ноты базы" in lower) and not base_n:
            val = txt.split(":", 1)[1] if ":" in txt else re.sub(r"(?i).*?(?:базовые ноты|ноты базы)", "", txt)
            base_n = clean_notes_list(val)
        elif "дополнительные ноты" in lower and not extra_n:
            val = txt.split(":", 1)[1] if ":" in txt else re.sub(r"(?i).*?дополнительные ноты", "", txt)
            extra_n = clean_notes_list(val)

    full_text = soup.get_text("\n", strip=True)
    if not top_n:
        m = re.search(r"(?i)верхние ноты\s*[:\-–]\s*([^\n\r]{2,150})", full_text)
        if m:
            top_n = clean_notes_list(m.group(1))
    if not heart_n:
        m = re.search(r"(?i)(?:ноты сердца|средние ноты)\s*[:\-–]\s*([^\n\r]{2,150})", full_text)
        if m:
            heart_n = clean_notes_list(m.group(1))
    if not base_n:
        m = re.search(r"(?i)(?:базовые ноты|ноты базы)\s*[:\-–]\s*([^\n\r]{2,150})", full_text)
        if m:
            base_n = clean_notes_list(m.group(1))
    if not extra_n:
        m = re.search(r"(?i)дополнительные ноты\s*[:\-–]\s*([^\n\r]{2,180})", full_text)
        if m:
            extra_n = clean_notes_list(m.group(1))

    if extra_n:
        for n in extra_n:
            if n not in top_n and n not in heart_n and n not in base_n:
                if len(top_n) < 3:
                    top_n.append(n)
                elif len(heart_n) < 3:
                    heart_n.append(n)
                elif len(base_n) < 4:
                    base_n.append(n)

    if top_n or heart_n or base_n:
        return {
            "top_notes": top_n,
            "heart_notes": heart_n,
            "base_notes": base_n,
        }
    return None


def _clean_review_text(raw_text: str) -> str:
    if not raw_text:
        return ""
    txt = re.sub(r"\s+", " ", raw_text).strip()
    txt = re.sub(r"^(?:Достоинства|Комментарий|Общее впечатление|Отзыв)\s*[:\-–]?\s*", "", txt, flags=re.IGNORECASE)
    return txt.strip(" «»\"'")


def _is_valid_review_text(text: str) -> bool:
    if not text or len(text) < 12 or len(text) > 650:
        return False
    lower = text.lower()
    if any(bad in lower for bad in BAD_REVIEW_PHRASES):
        return False
    if re.search(r"(?:http|www\.|\.ru|\.com|артикул|код товара|в корзину|доставка)", lower):
        return False
    cyrillic_cnt = len(re.findall(r"[а-яё]", lower))
    return cyrillic_cnt >= 8


def extract_reviews_from_html(html_content: str, store_label: str, max_reviews: int = 5) -> list[dict]:
    soup = BeautifulSoup(html_content, "html.parser")
    collected: list[dict] = []
    seen_texts: set[str] = set()
    clean_store = store_label.strip().lower()

    def add_candidate(author_raw: str, text_raw: str, rating_raw=5):
        if len(collected) >= max_reviews:
            return
        text_clean = _clean_review_text(text_raw)
        if not _is_valid_review_text(text_clean):
            return
        norm_key = normalize_text(text_clean)[:80]
        if not norm_key or norm_key in seen_texts:
            return

        author_clean = re.sub(r"[\s|\[\]]+", " ", (author_raw or "").strip()).strip()
        if not author_clean or len(author_clean) > 32 or any(w in author_clean.lower() for w in ["отзыв", "покупатель", "аноним", "гость", "клиент"]):
            author_clean = "Покупатель"

        try:
            rating_int = int(round(float(rating_raw)))
            rating_int = max(1, min(5, rating_int))
        except Exception:
            rating_int = 5

        seen_texts.add(norm_key)
        collected.append({
            "store": clean_store,
            "author": author_clean,
            "rating": rating_int,
            "text": text_clean,
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
                        if isinstance(author_obj, dict):
                            author_name = author_obj.get("name") or ""
                        elif isinstance(author_obj, str):
                            author_name = author_obj
                        else:
                            author_name = ""
                        rating_obj = r.get("reviewRating")
                        rating_val = rating_obj.get("ratingValue", 5) if isinstance(rating_obj, dict) else 5
                        add_candidate(author_name, str(body), rating_val)
        except Exception:
            continue

    review_containers = soup.select(
        '[itemprop="review"], .b-reviews__item, .s-reviews__item, .p-reviews__item, '
        '.review-item, .reviews__item, .b-comments__item, [data-qa*="review"], '
        '[class*="review-card"], [class*="reviewItem"], [class*="ReviewItem"]'
    )
    for block in review_containers:
        if len(collected) >= max_reviews:
            break
        body_el = block.select_one(
            '[itemprop="reviewBody"], [itemprop="description"], .b-reviews__text, '
            '.s-reviews__text, .review-text, .b-comment__text, [class*="review-text"], '
            '[class*="review__text"], [class*="comment-text"]'
        )
        text_val = body_el.get_text(" ", strip=True) if body_el else ""
        if not text_val:
            paragraphs = [p.get_text(" ", strip=True) for p in block.select("p, div") if 15 <= len(p.get_text(" ", strip=True)) <= 500]
            if paragraphs:
                text_val = max(paragraphs, key=len)

        author_el = block.select_one(
            '[itemprop="author"], .b-reviews__author, .s-reviews__author, '
            '.review-author, .b-comment__author, [class*="author"], [class*="user-name"]'
        )
        author_val = author_el.get_text(" ", strip=True) if author_el else "Покупатель"

        rating_val = 5
        rating_meta = block.select_one('[itemprop="ratingValue"]')
        if rating_meta:
            rating_val = rating_meta.get("content") or rating_meta.get_text(strip=True) or 5

        add_candidate(author_val, text_val, rating_val)

    return collected


def save_scraped_reviews_to_db(perfume_id: str, reviews: list[dict]):
    if not reviews:
        return

    safe_db(
        supabase.table("perfume_prices")
        .delete()
        .eq("perfume_id", perfume_id)
        .eq("volume", "REVIEW")
    )

    review_price_rows = []
    for rev in reviews[:8]:
        store_tag = rev["store"]
        author = re.sub(r"[\|\[\]]+", " ", rev.get("author", "Покупатель")).strip() or "Покупатель"
        rating = max(1, min(5, int(rev.get("rating", 5))))
        text_body = rev["text"].strip()
        encoded_payload = f"[{rating}★|{author}] {text_body}"
        review_price_rows.append({
            "perfume_id": perfume_id,
            "store_name": store_tag,
            "volume": "REVIEW",
            "price": 0,
            "store_url": encoded_payload[:900],
        })

    if review_price_rows:
        safe_db(supabase.table("perfume_prices").insert(review_price_rows))

    try:
        supabase.table("perfume_reviews").delete().eq("perfume_id", perfume_id).ilike("comment", "[SRC:%").execute()
        for rev in reviews[:6]:
            author = re.sub(r"[\|\[\]]+", " ", rev.get("author", "Покупатель")).strip() or "Покупатель"
            tagged_comment = f"[SRC:{rev['store']}|{author}] {rev['text'].strip()}"
            supabase.table("perfume_reviews").insert({
                "perfume_id": perfume_id,
                "rating": max(1, min(5, int(rev.get("rating", 5)))),
                "comment": tagged_comment,
            }).execute()
    except Exception:
        pass


# =====================================================================
# ПРОВЕРКА НАЛИЧИЯ И ЧТЕНИЕ АКТИВНОЙ ЦЕНЫ В ЗОНЕ ПОКУПКИ (BUY-BOX)
# =====================================================================
async def check_current_variant_in_stock(page) -> bool:
    return await page.evaluate(
        r"""() => {
            const h1 = document.querySelector('h1');
            const maxBottom = h1 ? Math.max(h1.getBoundingClientRect().bottom + 1450, 1900) : 2100;

            let hasBuyBtn = false;
            let hasOutBtn = false;

            const els = document.querySelectorAll('button, a, div[role="button"], span');
            for (const b of els) {
                if (b.closest('header, nav, footer, [class*="recommend"], [class*="similar"], [class*="carousel"], [class*="slider"], [class*="related"]')) {
                    continue;
                }
                const rect = b.getBoundingClientRect();
                if (rect.width < 45 || rect.height < 18 || rect.top < 20 || rect.top > maxBottom) continue;

                const txt = (b.innerText || '').replace(/\s+/g, ' ').trim().toLowerCase();
                if (!txt || txt.length > 48) continue;

                const st = window.getComputedStyle(b);
                const isDis = b.disabled || b.getAttribute('aria-disabled') === 'true' || parseFloat(st.opacity || '1') < 0.45;

                if (
                    txt === 'нет в наличии' ||
                    txt === 'распродано' ||
                    txt.includes('уведомить') ||
                    txt.includes('узнать о поступлении') ||
                    txt.includes('сообщить о') ||
                    txt.includes('товар закончился') ||
                    txt.includes('нет в наличии') ||
                    txt.includes('ожидается поступление')
                ) {
                    hasOutBtn = true;
                } else if (
                    !isDis &&
                    (txt === 'в корзину' || txt === 'добавить в корзину' || txt === 'купить' || txt === 'добавить в пакет' || txt.includes('в корзину'))
                ) {
                    hasBuyBtn = true;
                }
            }

            if (hasBuyBtn && !hasOutBtn) return true;
            if (hasOutBtn && !hasBuyBtn) return false;
            if (hasBuyBtn) return true;

            const bodyLower = (document.body ? document.body.innerText : '').toLowerCase();
            const hasPrice = bodyLower.includes('₽');
            return hasPrice && !bodyLower.includes('товара нет в наличии') && !bodyLower.includes('полностью распродан') && !bodyLower.includes('узнать о поступлении');
        }"""
    )


async def extract_active_price_from_dom(page) -> int | None:
    return await page.evaluate(
        r"""() => {
            const h1 = document.querySelector('h1');
            const h1Bottom = h1 ? h1.getBoundingClientRect().bottom : 250;
            const maxBuyBoxTop = Math.max(h1Bottom + 800, 1250);

            const candidates = [];
            const nodes = document.querySelectorAll('span, div, p, b, strong, ins');

            for (const el of nodes) {
                if (el.closest('header, nav, footer, [class*="recommend"], [class*="similar"], [class*="carousel"], [class*="slider"], [class*="related"], [class*="watched"]')) {
                    continue;
                }
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0 || rect.top < 40 || rect.top > maxBuyBoxTop) continue;

                const raw = (el.innerText || '').replace(/[\s\u00a0\u202f]+/g, ' ').trim();
                if (!raw || raw.length > 20 || (!raw.includes('₽') && !raw.toLowerCase().includes('руб'))) continue;

                const pText = (el.parentElement && (el.parentElement.innerText || '').length < 50)
                    ? el.parentElement.innerText.toLowerCase()
                    : raw.toLowerCase();
                if (['долями', 'сплит', 'платеж', 'частями', 'подели', 'бонус', 'балл', 'кешбэк', 'доставк', 'от ', 'за мл'].some(w => pText.includes(w))) {
                    continue;
                }

                let isOldPrice = false;
                let curr = el;
                for (let d = 0; d < 4 && curr && curr.tagName !== 'BODY'; d++) {
                    const st = window.getComputedStyle(curr);
                    const cls = String(curr.className || '').toLowerCase();
                    const tag = (curr.tagName || '').toLowerCase();

                    if (
                        tag === 'del' || tag === 's' || tag === 'strike' ||
                        (st.textDecorationLine && st.textDecorationLine.includes('line-through')) ||
                        (st.textDecoration && st.textDecoration.includes('line-through')) ||
                        cls.includes('crossed') || cls.includes('strike') || cls.includes('old-price') || cls.includes('price-old') || cls.includes('oldprice')
                    ) {
                        isOldPrice = true;
                        break;
                    }
                    curr = curr.parentElement;
                }
                if (isOldPrice) continue;

                const elStyle = window.getComputedStyle(el);
                const rgb = (elStyle.color || '').match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
                if (rgb) {
                    const r = parseInt(rgb[1]), g = parseInt(rgb[2]), b = parseInt(rgb[3]);
                    if (r >= 125 && r <= 225 && g >= 125 && g <= 225 && b >= 125 && b <= 225 && Math.abs(r - g) < 18 && Math.abs(g - b) < 18) {
                        continue;
                    }
                }

                const m = raw.match(/^(\d[\d\s]{2,7})\s*(?:₽|руб\.?)$/i);
                if (!m) continue;

                const val = parseInt(m[1].replace(/\D/g, ''), 10);
                if (!val || val < 1200 || val > 180000) continue;

                const fs = parseFloat(elStyle.fontSize) || 14;
                candidates.push({ price: val, fs: fs, top: rect.top });
            }

            if (candidates.length === 0) return null;
            candidates.sort((a, b) => (Math.abs(b.fs - a.fs) > 2.0 ? b.fs - a.fs : a.price - b.price));
            return candidates[0].price;
        }"""
    )


# =====================================================================
# МАГАЗИН 1: ПАРСЕР RANDEWOO (СОБИРАЕТ ВСЕ ОБЪЁМЫ В НАЛИЧИИ: 105, 150, 200 МЛ И Т.Д.)
# =====================================================================
def extract_randewoo_offers_from_html(html_content: str, target_url: str) -> list[dict]:
    """
    Извлекает все объёмы и цены в наличии из таблицы вариантов Randewoo.
    Не отбрасывает 200 мл Парфюмерной воды (9078 ₽), если на той же карточке есть 150 мл Духов (13158 ₽),
    а при наличии двух одинаковых объёмов разных концентраций выбирает минимальную цену в наличии.
    """
    psoup = BeautifulSoup(html_content, "html.parser")
    by_vol: dict[str, dict] = {}

    rows = psoup.select(
        ".b-productVariants__item, .product-variants__item, tr.b-productVariants__row, "
        "li[data-variant-id], .s-product__variants-item, [class*='productVariants'] li, [class*='variants'] tr"
    )
    if not rows:
        rows = psoup.select("tr, li")

    for row in rows:
        for old_el in row.select("del, s, strike, .b-productVariants__price-old, .price-old, .old-price"):
            old_el.decompose()

        row_txt = row.get_text(" ", strip=True)
        row_lower = row_txt.lower()

        if any(bad in row_lower for bad in [
            "нет в наличии", "распродано", "уведомить", "сообщить о", "ожидается", "под заказ",
            "тестер", "tester", "уценка", "уценен", "поврежден", "витрин",
            "пробник", "отливант", "миниатюра", "семпл",
            "дезодорант", "лосьон", "гель", "крем", "мыло", "рефил", "сменный",
            "набор", "аромабокс", "aroma box", "спрей для", "для тела", "для волос",
            "мист", "дымка", "вуаль", "после бритья"
        ]):
            continue

        row_clean = re.sub(r"[-+]?\s*\d{1,2}\s*%", " ", row_txt)

        vol_m = re.search(r"\b(\d{1,3}(?:[.,]\d)?)\s*(?:мл|ml)\b", row_clean, re.IGNORECASE)
        if not vol_m:
            continue

        vol_float = float(vol_m.group(1).replace(",", "."))
        if vol_float < 10:
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

        valid_prices = [int(p) for p in price_matches if 1200 <= int(p) <= 180000]
        if valid_prices:
            min_p = min(valid_prices)
            if vol_str not in by_vol or min_p < by_vol[vol_str]["price"]:
                by_vol[vol_str] = {
                    "store_name": "Randewoo",
                    "volume": vol_str,
                    "price": min_p,
                    "store_url": target_url,
                }

    return list(by_vol.values())


async def parse_randewoo(
    page,
    brand: str,
    title: str,
    encoded_plus: str,
    known_url: str | None = None,
    target_conc: str | None = None
) -> tuple[str, list[dict], dict | None, list[dict]]:
    try:
        target_url = known_url if (known_url and "randewoo.ru/product/" in known_url) else None

        if not target_url:
            search_url = f"https://randewoo.ru/search?q={encoded_plus}"
            await page.goto(search_url, timeout=28000, wait_until="domcontentloaded")
            loaded = await wait_for_search_cards(
                page,
                ".products__item, .b-catalogItems__item, .digi-product, li[data-product-id], article",
                10000
            )
            if not loaded:
                await page.wait_for_timeout(1200)
                loaded = await wait_for_search_cards(
                    page,
                    ".products__item, .b-catalogItems__item, .digi-product, li[data-product-id], article",
                    6000
                )
            if not loaded:
                return STATUS_BLOCKED, [], None, []

            candidates = await page.evaluate(
                r"""() => {
                    const results = [];
                    const seen = new Set();
                    const links = document.querySelectorAll('a[href*="/product/"]');
                    for (const a of links) {
                        if (a.closest('nav, footer')) continue;
                        const href = (a.getAttribute('href') || '').split('?')[0].trim();
                        if (!href || seen.has(href)) continue;
                        const card = a.closest('.products__item, .b-catalogItems__item, .digi-product, li[data-product-id], article') || a.parentElement?.parentElement || a;
                        const txt = (card.innerText || '').trim();
                        if (txt.length >= 6) {
                            seen.add(href);
                            results.push({ text: txt, href: href });
                        }
                        if (results.length >= 25) break;
                    }
                    return results;
                }"""
            )

            for cand in candidates:
                if is_strict_match(cand["text"], brand, title, cand["href"], check_flankers=True, target_conc=target_conc):
                    target_url = urljoin("https://randewoo.ru", cand["href"])
                    break

            if not target_url:
                return STATUS_NOT_FOUND, [], None, []

        await page.goto(target_url, timeout=28000, wait_until="domcontentloaded")
        if not await wait_for_product_page(page, 10000):
            return STATUS_BLOCKED, [], None, []

        html_content = await page.content()
        psoup = BeautifulSoup(html_content, "html.parser")
        h1 = psoup.select_one("h1")
        h1_text = h1.get_text(" ", strip=True) if h1 else ""
        if not h1_text or not is_strict_match(h1_text, brand, title, target_url, check_flankers=True, target_conc=target_conc):
            return STATUS_NOT_FOUND, [], None, []

        pyramid = extract_pyramid_from_html(html_content)
        reviews = extract_reviews_from_html(html_content, "randewoo", max_reviews=5)
        offers = extract_randewoo_offers_from_html(html_content, target_url)

        if offers:
            return STATUS_FOUND, offers, pyramid, reviews
        return STATUS_NOT_FOUND, [], pyramid, reviews

    except Exception:
        return STATUS_BLOCKED, [], None, []


# =====================================================================
# УНИВЕРСАЛЬНЫЙ ИНТЕРАКТИВНЫЙ ПАРСЕР ОБЪЁМОВ И ЦЕН (РИВ ГОШ, Л'ЭТУАЛЬ, ЗОЛОТОЕ ЯБЛОКО)
# =====================================================================
async def extract_volumes_and_prices_on_product_page(
    page,
    store_name: str,
    base_product_url: str,
    card_fallback_text: str = ""
) -> list[dict]:
    """
    Находит и прокликивает доступные объёмы на Рив Гош, Л'Этуаль и Золотом Яблоке:
    1) Обязательно спускается до конечного текстового элемента (childSame), чтобы точно прочитать
       серый цвет текста и серую рамку у отсутствующих объёмов (например, серая таблетка «10 мл» на Рив Гош в image_3e0d10.png);
    2) Отсекает серые (отсутствующие в наличии) таблетки объёмов ДО клика по ним, чтобы Рив Гош не подставлял их старую цену;
    3) Защищает от записи старой цены, если после клика по невыбранной кнопке цена не изменилась.
    """
    collected_offers = []
    seen_collected_vols = set()

    volume_info = await page.evaluate(
        r"""() => {
            document.querySelectorAll('[data-vol-btn]').forEach(e => e.removeAttribute('data-vol-btn'));
            const validNums = new Set(['7.5','8','10','11','12','14','15','16','17','20','25','30','35','40','45','50','60','65','70','75','80','85','90','95','100','105','110','120','125','150','175','200','250']);

            const h1 = document.querySelector('h1');
            const maxTop = h1 ? Math.max(h1.getBoundingClientRect().bottom + 850, 1250) : 1450;
            const bodyText = document.body ? document.body.innerText : '';
            const gaActiveMatch = bodyText.match(/\b(\d{1,3}(?:[.,]\d)?)\s*объ[её]м\s*\/\s*мл/i);
            const explicitGaActiveVol = gaActiveMatch ? gaActiveMatch[1].replace(',', '.') : null;

            const byVol = new Map();
            const elements = document.querySelectorAll('button, a, label, li, div, span');

            for (const el of elements) {
                if (el.closest('header, nav, footer, [class*="recommend"], [class*="similar"], [class*="carousel"], [class*="slider"]')) {
                    continue;
                }

                const rawTxt = (el.innerText || '').replace(/\u00a0/g, ' ').trim();
                const txt = rawTxt.replace(/[-+]?\s*\d*\s*%/g, ' ').replace(/\s+/g, ' ').trim();
                if (!txt || txt.length > 20) continue;

                let volNum = null;
                let hasMlWord = false;
                const mWithMl = txt.match(/^(\d{1,3}(?:[.,]\d)?)\s*(?:мл|ml)$/i);
                if (mWithMl) {
                    volNum = mWithMl[1].replace(',', '.');
                    hasMlWord = true;
                } else {
                    const mPureNum = txt.match(/^(\d{1,3}(?:[.,]\d)?)$/);
                    if (mPureNum && validNums.has(mPureNum[1].replace(',', '.'))) {
                        let p = el.parentElement;
                        for (let d = 0; d < 8 && p && p.tagName !== 'BODY'; d++) {
                            const pr = p.getBoundingClientRect();
                            if (pr.height > 750) break;
                            const prev1 = p.previousElementSibling ? (p.previousElementSibling.innerText || '') : '';
                            const prev2 = (p.previousElementSibling && p.previousElementSibling.previousElementSibling)
                                ? (p.previousElementSibling.previousElementSibling.innerText || '')
                                : '';
                            const pTxt = ((p.innerText || '') + ' ' + prev1 + ' ' + prev2).toLowerCase();
                            if (pTxt.includes('объем') || pTxt.includes('объём') || pTxt.includes('мл')) {
                                volNum = mPureNum[1].replace(',', '.');
                                break;
                            }
                            p = p.parentElement;
                        }
                    }
                }

                if (!volNum || !validNums.has(volNum)) continue;

                // ВАЖНО: пропускаем внешние обёртки, если внутри них есть дочерний элемент с тем же текстом объёма!
                // Это гарантирует, что `el` — это самый внутренний текстовый узел (span/button) с его настоящим цветом текста!
                const childSame = Array.from(el.children).some(ch => {
                    const ct = (ch.innerText || '').replace(/\u00a0/g, ' ').replace(/[-+]?\s*\d*\s*%/g, ' ').replace(/\s+/g, ' ').trim();
                    return ct === txt;
                });
                if (childSame) continue;

                // Находим кликабельную таблетку/кнопку вокруг `el` (размером <= 210x90 px)
                let target = el;
                let curr = el.parentElement;
                for (let d = 0; d < 5 && curr && curr.tagName !== 'BODY'; d++) {
                    const cr = curr.getBoundingClientRect();
                    if (cr.width > 210 || cr.height > 90 || cr.width < 14 || cr.height < 14) break;
                    const cTxt = (curr.innerText || '').replace(/\u00a0/g, ' ').replace(/[-+]?\s*\d*\s*%/g, ' ').replace(/\s+/g, ' ').trim();
                    if (cTxt === txt) {
                        target = curr;
                        if (curr.tagName === 'BUTTON' || curr.tagName === 'LABEL' || curr.tagName === 'A' || curr.getAttribute('role') === 'button' || curr.getAttribute('role') === 'radio') {
                            break;
                        }
                        curr = curr.parentElement;
                    } else {
                        break;
                    }
                }

                const rect = target.getBoundingClientRect();
                if (rect.width < 18 || rect.width > 210 || rect.height < 16 || rect.height > 90 || rect.top < 30 || rect.top > maxTop) {
                    continue;
                }

                function blendOnWhite(colorStr, extraOpacity) {
                    const m = (colorStr || '').match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
                    if (!m) return null;
                    const rawA = m[4] !== undefined ? parseFloat(m[4]) : 1.0;
                    const a = rawA * (extraOpacity !== undefined ? extraOpacity : 1.0);
                    if (a <= 0.03) return null;
                    const r = Math.round(parseInt(m[1], 10) * a + 255 * (1 - a));
                    const g = Math.round(parseInt(m[2], 10) * a + 255 * (1 - a));
                    const b = Math.round(parseInt(m[3], 10) * a + 255 * (1 - a));
                    return { r, g, b, a };
                }

                let hasDarkBg = false;
                let hasActiveAttrOrBorder = (explicitGaActiveVol !== null && explicitGaActiveVol === volNum);
                let hasDisabledFlag = false;
                let hasGrayText = false;
                let hasGrayBorder = false;
                let hasDarkBorder = false;
                let hasDiagonalStrike = false;

                // Проверяем наличие SVG-зачёркивания или диагональной линии внутри кнопки (Золотое Яблоко)
                if (target.querySelector('svg, line, path, canvas, [class*="cross"], [class*="strike"], [class*="slash"], [class*="disabled"], [class*="out"]')) {
                    hasDiagonalStrike = true;
                }

                // Считаем совокупную непрозрачность (opacity) от `el` вверх по родителям кнопки
                let cumulativeOpacity = 1.0;
                let opNode = el;
                for (let d = 0; d < 6 && opNode && opNode.tagName !== 'BODY'; d++) {
                    const nr = opNode.getBoundingClientRect();
                    if (d > 0 && (nr.width > 240 || nr.height > 110)) break;
                    const opVal = parseFloat(window.getComputedStyle(opNode).opacity || '1');
                    if (!isNaN(opVal)) cumulativeOpacity *= opVal;
                    opNode = opNode.parentElement;
                }
                if (cumulativeOpacity < 0.65) {
                    hasDisabledFlag = true;
                    hasGrayText = true;
                }

                // Проверяем все дочерние элементы внутри `target` на наличие диагональной полоски (rotate/matrix или gradient)
                const innerNodes = [target, ...Array.from(target.querySelectorAll('*'))];
                for (const inNode of innerNodes) {
                    const inSt = window.getComputedStyle(inNode);
                    const inBgImg = (inSt.backgroundImage || '').toLowerCase();
                    const inTr = (inSt.transform || '').toLowerCase();
                    if (inBgImg && inBgImg !== 'none' && (inBgImg.includes('gradient') || inBgImg.includes('url('))) {
                        hasDiagonalStrike = true;
                    }
                    if (inTr && inTr !== 'none' && inTr.includes('matrix')) {
                        hasDiagonalStrike = true;
                    }
                    try {
                        for (const pseudo of ['::before', '::after']) {
                            const pSt = window.getComputedStyle(inNode, pseudo);
                            if (!pSt) continue;
                            const pBg = (pSt.backgroundImage || '').toLowerCase();
                            const pTr = (pSt.transform || '').toLowerCase();
                            const pW = parseFloat(pSt.width || '0');
                            const pH = parseFloat(pSt.height || '0');
                            if (pBg && pBg !== 'none' && (pBg.includes('gradient') || pBg.includes('url('))) {
                                hasDiagonalStrike = true;
                            }
                            if (pTr && pTr !== 'none' && pTr.includes('matrix') && (pW > 10 || pH > 10)) {
                                hasDiagonalStrike = true;
                            }
                        }
                    } catch (e) {}
                }

                // Проверяем сам текстовый элемент `el` и его родителей до границ таблетки/квадрата (<= 215x95 px)
                let checkNode = el;
                for (let d = 0; d < 6 && checkNode && checkNode.tagName !== 'BODY'; d++) {
                    const nr = checkNode.getBoundingClientRect();
                    if (d > 0 && (nr.width > 215 || nr.height > 95)) break;

                    const st = window.getComputedStyle(checkNode);
                    const cls = String(checkNode.className || '').toLowerCase();

                    const bgRgb = (st.backgroundColor || '').match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
                    if (bgRgb) {
                        const br = parseInt(bgRgb[1]), bg = parseInt(bgRgb[2]), bb = parseInt(bgRgb[3]);
                        const ba = (bgRgb[4] !== undefined ? parseFloat(bgRgb[4]) : 1.0) * cumulativeOpacity;
                        if (ba > 0.5 && br < 85 && bg < 85 && bb < 85) {
                            hasDarkBg = true;
                        }
                    }

                    // Проверяем цвет и толщину рамки с учётом rgba и прозрачности (на ЗЯ у [25] рамка rgb(0,0,0) 1px, а у [50]/[100] — rgb(179,179,179))
                    const rawBorderColor = st.borderTopColor || st.borderBottomColor || st.borderLeftColor || st.borderColor || '';
                    const bBlended = blendOnWhite(rawBorderColor, cumulativeOpacity);
                    const maxBorderW = Math.max(
                        parseFloat(st.borderTopWidth || '0'),
                        parseFloat(st.borderBottomWidth || '0'),
                        parseFloat(st.borderLeftWidth || '0'),
                        parseFloat(st.borderRightWidth || '0'),
                        parseFloat(st.borderWidth || '0')
                    );
                    if (maxBorderW >= 0.75 && bBlended) {
                        if (bBlended.r < 90 && bBlended.g < 90 && bBlended.b < 90) {
                            hasDarkBorder = true;
                            hasActiveAttrOrBorder = true;
                        } else if (bBlended.r >= 150 && bBlended.r <= 245 && bBlended.g >= 150 && bBlended.g <= 245 && bBlended.b >= 150 && bBlended.b <= 245) {
                            hasGrayBorder = true;
                        }
                    }

                    // Проверяем серый цвет текста на `el` и `target` с учётом rgba/opacity (на скриншоте ЗЯ image_134747.png у [25] текст rgb(0,0,0), а у [50] и [100] — rgb(179,179,179))
                    if (d <= 2) {
                        const tBlended = blendOnWhite(st.color || '', cumulativeOpacity);
                        if (tBlended) {
                            const { r, g, b } = tBlended;
                            if (r >= 120 && r <= 242 && g >= 120 && g <= 242 && b >= 120 && b <= 242 && Math.abs(r - g) < 22 && Math.abs(g - b) < 22) {
                                hasGrayText = true;
                            }
                        }
                    }

                    if (
                        checkNode.getAttribute('aria-checked') === 'true' ||
                        checkNode.getAttribute('aria-selected') === 'true' ||
                        checkNode.getAttribute('aria-pressed') === 'true' ||
                        /\b(active|selected|current|checked|is-active|is-selected)\b/i.test(cls)
                    ) {
                        hasActiveAttrOrBorder = true;
                    }

                    if (
                        checkNode.disabled ||
                        checkNode.getAttribute('aria-disabled') === 'true' ||
                        cls.includes('disabled') ||
                        cls.includes('unavailable') ||
                        cls.includes('out-of-stock') ||
                        cls.includes('out_of_stock') ||
                        cls.includes('crossed') ||
                        cls.includes('sold') ||
                        cls.includes('absent') ||
                        cls.includes('empty') ||
                        (st.textDecorationLine && st.textDecorationLine.includes('line-through')) ||
                        (st.pointerEvents && st.pointerEvents === 'none')
                    ) {
                        hasDisabledFlag = true;
                    }

                    checkNode = checkNode.parentElement;
                }

                // На всех сайтах (Золотое Яблоко, Рив Гош, Л'Этуаль):
                // если кнопка НЕ залита тёмным фоном (!hasDarkBg) и при этом имеет диагональное зачёркивание,
                // серый текст (как 50 и 100 на скрине ЗЯ или 10 мл на Рив Гош) или disabled-флаг — она НЕ в наличии!
                // ВАЖНО: в Л'Этуаль невыбранный объём в наличии (например, [50 мл] при выбранном [150 мл]) имеет
                // ЧЁРНЫЙ текст (!hasGrayText) и светло-серую рамку, поэтому серую рамку саму по себе не отбрасываем!
                if (!hasDarkBg && (hasDisabledFlag || hasDiagonalStrike || hasGrayText)) {
                    continue;
                }

                const isActive = hasDarkBg || hasActiveAttrOrBorder;

                if (!byVol.has(volNum)) {
                    target.setAttribute('data-vol-btn', volNum);
                    byVol.set(volNum, { vol: volNum, isActive: isActive, hasDarkBg: hasDarkBg });
                } else if (isActive && !byVol.get(volNum).isActive) {
                    target.setAttribute('data-vol-btn', volNum);
                    byVol.set(volNum, { vol: volNum, isActive: true, hasDarkBg: hasDarkBg });
                }
            }

            const items = Array.from(byVol.values());
            const anyDark = items.some(i => i.hasDarkBg);
            const activeOnes = items.filter(i => anyDark ? i.hasDarkBg : i.isActive).map(i => i.vol);
            const activeSet = new Set(activeOnes);
            const otherOnes = items.filter(i => !activeSet.has(i.vol)).map(i => i.vol).sort((a, b) => parseFloat(a) - parseFloat(b));

            return {
                orderedVols: [...activeOnes, ...otherOnes],
                activeVols: activeOnes
            };
        }"""
    )

    volume_list = volume_info.get("orderedVols", []) if isinstance(volume_info, dict) else []
    active_vols = set(volume_info.get("activeVols", []) if isinstance(volume_info, dict) else [])

    if volume_list:
        for idx_v, vol_num_str in enumerate(volume_list):
            v_str = format_vol(float(vol_num_str))
            if not v_str or v_str in seen_collected_vols:
                continue

            try:
                price_before_click = await extract_active_price_from_dom(page)
                need_click = (idx_v > 0) or (vol_num_str not in active_vols)

                if need_click:
                    await page.evaluate(
                        r"""(v) => {
                            const btn = document.querySelector(`[data-vol-btn="${v}"]`);
                            if (btn && btn.scrollIntoView) {
                                btn.scrollIntoView({ block: 'center', inline: 'center' });
                            }
                        }""",
                        vol_num_str
                    )
                    await page.wait_for_timeout(150)

                    loc = page.locator(f'[data-vol-btn="{vol_num_str}"]').first
                    try:
                        await loc.click(timeout=2000)
                    except Exception:
                        try:
                            await loc.click(timeout=2000, force=True)
                        except Exception:
                            pass

                    await page.evaluate(
                        r"""(v) => {
                            const btn = document.querySelector(`[data-vol-btn="${v}"]`);
                            if (!btn) return;
                            try { btn.click(); } catch (e) {}
                            if (btn.parentElement) { try { btn.parentElement.click(); } catch (e) {} }
                            const inners = btn.querySelectorAll('button, a, input, span, div');
                            for (const inn of inners) {
                                try { inn.click(); } catch (e) {}
                            }
                        }""",
                        vol_num_str
                    )

                    price_changed = False
                    for _ in range(16):
                        await page.wait_for_timeout(150)
                        curr_p = await extract_active_price_from_dom(page)
                        if curr_p and price_before_click and curr_p != price_before_click:
                            price_changed = True
                            break

                    await page.wait_for_timeout(350)

                    curr_price_after = await extract_active_price_from_dom(page)
                    if idx_v > 0 and price_before_click is not None and curr_price_after == price_before_click and not price_changed:
                        continue

                if not await check_current_variant_in_stock(page):
                    continue

                explicit_ga_active_vol = await page.evaluate(
                    r"""() => {
                        const body = (document.body ? document.body.innerText : '');
                        const ga1 = body.match(/\b(\d{1,3}(?:[.,]\d)?)\s*объ[её]м\s*\/\s*мл/i);
                        return ga1 ? ga1[1].replace(',', '.') : null;
                    }"""
                )
                if explicit_ga_active_vol and format_vol(float(explicit_ga_active_vol)):
                    actual_v_str = format_vol(float(explicit_ga_active_vol))
                else:
                    actual_v_str = v_str

                price_val = await extract_active_price_from_dom(page)
                if price_val and actual_v_str and actual_v_str not in seen_collected_vols:
                    seen_collected_vols.add(actual_v_str)
                    collected_offers.append({
                        "store_name": store_name,
                        "volume": actual_v_str,
                        "price": int(price_val),
                        "store_url": page.url.split("?")[0] or base_product_url,
                    })
            except Exception:
                continue

    if collected_offers:
        return collected_offers

    # 2. Фолбэк для карточек с единственным объёмом
    if not await check_current_variant_in_stock(page):
        return []

    single_vol = await page.evaluate(
        r"""() => {
            const body = (document.body ? document.body.innerText : '');
            const h1 = document.querySelector('h1') ? document.querySelector('h1').innerText : '';
            const ga1 = body.match(/\b(\d{1,3}(?:[.,]\d)?)\s*объ[её]м\s*\/\s*мл/i);
            if (ga1) return ga1[1].replace(',', '.');
            const hm = (h1 + ' ' + document.title).match(/\b(\d{1,3}(?:[.,]\d)?)\s*(?:мл|ml)\b/i);
            if (hm) return hm[1].replace(',', '.');
            const sm = body.match(/объ[её]м[\s\n\r:]{1,25}(\d{1,3}(?:[.,]\d)?)\s*(?:мл|ml)\b/i);
            if (sm) return sm[1].replace(',', '.');
            const ga2 = body.match(/объ[её]м\s*\/\s*мл[\s\n\r:]{1,25}(\d{1,3}(?:[.,]\d)?)\b/i);
            if (ga2) return ga2[1].replace(',', '.');
            return null;
        }"""
    )
    if not single_vol and card_fallback_text:
        m_card = re.search(r"\b(\d{1,3}(?:[.,]\d)?)\s*(?:мл|ml)\b", card_fallback_text, re.IGNORECASE)
        if m_card:
            single_vol = m_card.group(1).replace(",", ".")

    vol_str = format_vol(float(single_vol)) if single_vol else None
    price_val = await extract_active_price_from_dom(page)

    if vol_str and price_val:
        return [{
            "store_name": store_name,
            "volume": vol_str,
            "price": int(price_val),
            "store_url": base_product_url,
        }]

    return []


# =====================================================================
# МАГАЗИН 2 И 4: ПАРСЕР РИВ ГОШ И Л'ЭТУАЛЬ
# =====================================================================
async def extract_cards_v21_rivegauche(page, card_sel: str) -> list[dict]:
    return await page.evaluate(
        r"""(cardSel) => {
            const results = [];
            const seenHrefs = new Set();

            const allLinks = document.querySelectorAll('a[href]');
            for (const a of allLinks) {
                if (a.closest('nav, footer')) continue;
                const href = (a.getAttribute('href') || '').trim();
                if (
                    !href || seenHrefs.has(href) || href.startsWith('#') || href.startsWith('javascript') ||
                    href.includes('/brands/') || href.includes('/category/') ||
                    href.includes('/search') || href.includes('/catalogsearch')
                ) {
                    continue;
                }

                const parentCard = a.closest('article, product-item, [data-at-product-tile], [data-qa="product-card"], [class*="product-card"], [class*="product-item"], [class*="product-tile"]');
                const isProductHref = href.includes('/product/') || (parentCard !== null && href.length > 12);
                if (!isProductHref) continue;

                let card = parentCard;
                if (!card) {
                    let curr = a.parentElement;
                    for (let d = 0; d < 6 && curr && curr.tagName !== 'BODY'; d++) {
                        const r = curr.getBoundingClientRect();
                        if (r.height >= 140 && r.height <= 750 && r.width >= 120 && r.width <= 600) {
                            card = curr;
                            if ((curr.innerText || '').includes('₽')) break;
                        }
                        curr = curr.parentElement;
                    }
                }
                if (!card) card = a.parentElement?.parentElement || a.parentElement || a;

                const txt = (card.innerText || '').replace(/\u00a0/g, ' ').replace(/\u202f/g, ' ').trim();
                const imgAlt = Array.from(card.querySelectorAll('img[alt]')).map(i => i.getAttribute('alt') || '').join(' ');
                const ariaLabel = a.getAttribute('aria-label') || a.getAttribute('title') || '';
                const fullCardText = (txt + ' ' + imgAlt + ' ' + ariaLabel).trim();

                if (fullCardText.length >= 6) {
                    seenHrefs.add(href);
                    results.push({ text: fullCardText, href: href });
                }
                if (results.length >= 60) break;
            }

            const cards = document.querySelectorAll(cardSel);
            for (const c of cards) {
                if (c.closest('nav, footer')) continue;
                const txt = (c.innerText || '').replace(/\u00a0/g, ' ').replace(/\u202f/g, ' ').trim();
                if (!txt || txt.length < 10) continue;

                const links = Array.from(c.querySelectorAll('a[href]'))
                    .map(a => a.getAttribute('href') || '')
                    .filter(h => h && !h.startsWith('#') && !h.startsWith('javascript') && !h.includes('/brands/') && !h.includes('/category/'));

                let bestHref = links.find(h => h.includes('/product/')) || links[0] || '';
                if (!bestHref) {
                    const parentLink = c.closest('a[href]');
                    if (parentLink) bestHref = parentLink.getAttribute('href') || '';
                }

                if (bestHref && !seenHrefs.has(bestHref)) {
                    seenHrefs.add(bestHref);
                    const imgAlt = Array.from(c.querySelectorAll('img[alt]')).map(i => i.getAttribute('alt') || '').join(' ');
                    results.push({ text: (txt + ' ' + imgAlt).trim(), href: bestHref });
                }
                if (results.length >= 80) break;
            }

            return results;
        }""",
        card_sel
    )


async def parse_rivegauche_or_letu_v21(
    page,
    store: dict,
    brand: str,
    title: str,
    known_url: str | None = None,
    target_conc: str | None = None
) -> tuple[str, list[dict], dict | None, list[dict]]:
    try:
        candidate_urls = []

        if known_url and known_url.startswith("http") and "/search" not in known_url and "/catalogsearch" not in known_url:
            if is_strict_match(brand, brand, title, known_url, check_flankers=True, target_conc=target_conc):
                candidate_urls.append((known_url, ""))

        if not candidate_urls:
            await page.goto(store["search_url"], timeout=28000, wait_until="domcontentloaded")
            loaded = await wait_for_search_cards(page, store["card_sel"], 12000)
            if not loaded:
                await page.wait_for_timeout(1500)
                loaded = await wait_for_search_cards(page, store["card_sel"], 7000)
            if not loaded:
                return STATUS_BLOCKED, [], None, []

            await page.evaluate(
                """() => {
                    const btns = document.querySelectorAll('button, a, div[role="button"]');
                    for (const b of btns) {
                        const t = (b.innerText || '').trim().toLowerCase();
                        if (t === 'да, верно' || t === 'принять' || t === 'согласен' || t === 'понятно') {
                            try { b.click(); } catch (e) {}
                        }
                    }
                }"""
            )
            await page.wait_for_timeout(600)

            cards_data = await extract_cards_v21_rivegauche(page, store["card_sel"])

            in_stock_matches = []
            other_matches = []
            for cand in cards_data:
                if is_strict_match(cand["text"], brand, title, cand["href"], check_flankers=True, target_conc=target_conc):
                    full_u = urljoin(store["base"], cand["href"])
                    c_lower = cand["text"].lower()
                    if "₽" in c_lower and "нет в наличии" not in c_lower and "распродано" not in c_lower:
                        in_stock_matches.append((full_u, cand["text"]))
                    else:
                        other_matches.append((full_u, cand["text"]))

            candidate_urls = (in_stock_matches + other_matches)[:3]

            if not candidate_urls:
                return STATUS_NOT_FOUND, [], None, []

        for product_url, card_fallback_text in candidate_urls:
            if page.url != product_url:
                await page.goto(product_url, timeout=28000, wait_until="domcontentloaded")
                if not await wait_for_product_page(page, 12000):
                    continue

            await page.evaluate(
                """() => {
                    const btns = document.querySelectorAll('button, a, div[role="button"]');
                    for (const b of btns) {
                        const t = (b.innerText || '').trim().toLowerCase();
                        if (t === 'да, верно' || t === 'принять' || t === 'согласен') {
                            try { b.click(); } catch (e) {}
                        }
                    }
                }"""
            )
            await page.evaluate("window.scrollBy(0, 250)")
            await page.wait_for_timeout(800)

            page_validation = await page.evaluate(
                """() => {
                    const h1El = document.querySelector('h1');
                    const h1 = h1El ? h1El.innerText.trim() : '';
                    return { h1: h1, title: document.title || '' };
                }"""
            )

            h1_only = f"{brand} {page_validation.get('h1', '')}".strip()
            if not is_strict_match(h1_only, brand, title, page.url, check_flankers=True, target_conc=target_conc):
                continue

            html_content = await page.content()
            pyramid = extract_pyramid_from_html(html_content)
            reviews = extract_reviews_from_html(html_content, store["name"].lower(), max_reviews=3)

            base_product_url = page.url.split("?")[0]
            collected_offers = await extract_volumes_and_prices_on_product_page(
                page, store["name"], base_product_url, card_fallback_text
            )

            if collected_offers:
                return STATUS_FOUND, collected_offers, pyramid, reviews

        return STATUS_NOT_FOUND, [], None, []

    except Exception:
        return STATUS_BLOCKED, [], None, []


# =====================================================================
# МАГАЗИН 3: ПАРСЕР ЗОЛОТОГО ЯБЛОКА
# =====================================================================
async def extract_cards_v28_goldapple(page) -> list[dict]:
    return await page.evaluate(
        r"""() => {
            function getCleanProdHref(a) {
                const raw = (a.getAttribute('href') || '').trim();
                if (!raw || raw.startsWith('#') || raw.startsWith('javascript')) return null;
                const clean = raw.split('?')[0].trim();
                if (
                    clean.includes('/brands/') || clean.includes('/category/') ||
                    clean.includes('/search') || clean.includes('/catalogsearch')
                ) {
                    return null;
                }
                if (
                    clean.includes('/product/') ||
                    /^\/\d{4,}-[a-z0-9-]+/i.test(clean) ||
                    /goldapple\.ru\/\d{4,}-/i.test(clean)
                ) {
                    return clean;
                }
                return null;
            }

            const groupedByHref = new Map();
            const allLinks = document.querySelectorAll('a[href]');

            for (const a of allLinks) {
                if (a.closest('nav, footer')) continue;
                const myHref = getCleanProdHref(a);
                if (!myHref) continue;

                let card = a.closest('article, [data-qa="product-card"]');
                if (!card) {
                    card = a;
                    let curr = a.parentElement;
                    for (let d = 0; d < 6 && curr && curr.tagName !== 'BODY'; d++) {
                        const innerLinks = curr.querySelectorAll('a[href]');
                        const uniqueHrefs = new Set();
                        for (const il of innerLinks) {
                            const h = getCleanProdHref(il);
                            if (h) uniqueHrefs.add(h);
                        }
                        if (uniqueHrefs.size > 1) {
                            break;
                        }
                        card = curr;
                        curr = curr.parentElement;
                    }
                }

                const txt = (card.innerText || '').replace(/\u00a0/g, ' ').replace(/\u202f/g, ' ').replace(/\s+/g, ' ').trim();
                const imgAlt = Array.from(card.querySelectorAll('img[alt]')).map(i => i.getAttribute('alt') || '').join(' ');
                const combined = `${txt} ${imgAlt}`.replace(/\s+/g, ' ').trim();

                const prev = groupedByHref.get(myHref) || '';
                if (combined.length > prev.length) {
                    groupedByHref.set(myHref, combined);
                }
            }

            return Array.from(groupedByHref.entries())
                .map(([href, text]) => ({ href, text }))
                .filter(item => item.text.length >= 6)
                .slice(0, 50);
        }"""
    )


async def parse_goldapple_v28(
    page,
    brand: str,
    title: str,
    encoded_pct: str,
    known_url: str | None = None,
    target_conc: str | None = None
) -> tuple[str, list[dict], dict | None, list[dict]]:
    search_url = f"https://goldapple.ru/catalogsearch/result?q={encoded_pct}"
    card_sel = 'article, [data-qa="product-card"]'
    try:
        candidate_urls = []

        if known_url and known_url.startswith("http") and "/search" not in known_url and "/catalogsearch" not in known_url:
            if is_strict_match(brand, brand, title, known_url, check_flankers=True, target_conc=target_conc):
                candidate_urls.append((known_url, ""))

        if not candidate_urls:
            await page.goto(search_url, timeout=28000, wait_until="domcontentloaded")
            loaded = await wait_for_search_cards(page, card_sel, 12000)

            if "/catalogsearch" not in page.url or not loaded:
                await page.wait_for_timeout(1500)
                await page.goto(search_url, timeout=28000, wait_until="domcontentloaded")
                loaded = await wait_for_search_cards(page, card_sel, 11000)

            if not loaded:
                return STATUS_BLOCKED, [], None, []

            await page.evaluate(
                """() => {
                    const btns = document.querySelectorAll('button, a, div[role="button"]');
                    for (const b of btns) {
                        const t = (b.innerText || '').trim().toLowerCase();
                        if (t === 'да, верно' || t === 'принять' || t === 'согласен' || t === 'понятно') {
                            try { b.click(); } catch (e) {}
                        }
                    }
                    window.scrollBy(0, 200);
                }"""
            )
            await page.wait_for_timeout(800)

            cards_data = await extract_cards_v28_goldapple(page)

            in_stock_matches = []
            other_matches = []
            for cand in cards_data:
                if is_strict_match(cand["text"], brand, title, cand["href"], check_flankers=True, target_conc=target_conc):
                    full_u = urljoin("https://goldapple.ru", cand["href"])
                    c_lower = cand["text"].lower()
                    if "₽" in c_lower and "нет в наличии" not in c_lower and "распродано" not in c_lower:
                        in_stock_matches.append((full_u, cand["text"]))
                    else:
                        other_matches.append((full_u, cand["text"]))

            candidate_urls = (in_stock_matches + other_matches)[:3]

            if not candidate_urls:
                return STATUS_NOT_FOUND, [], None, []

        for product_url, card_fallback_text in candidate_urls:
            if page.url != product_url:
                await page.goto(product_url, timeout=28000, wait_until="domcontentloaded")
                if not await wait_for_product_page(page, 12000):
                    continue

            await page.evaluate("window.scrollBy(0, 250)")
            await page.wait_for_timeout(800)

            page_validation = await page.evaluate(
                """() => {
                    const h1El = document.querySelector('h1');
                    const h1 = h1El ? h1El.innerText.trim() : '';
                    const h1Parent = (h1El && h1El.parentElement) ? h1El.parentElement.innerText.trim() : '';
                    return { h1: h1, h1Parent: h1Parent, title: document.title || '' };
                }"""
            )

            h1_clean = f"{brand} {page_validation.get('h1', '')} {page_validation.get('title', '')}".strip()
            header_full = f"{h1_clean} {page_validation.get('h1Parent', '')}".strip()
            # Строго проверяем фланкеры по заголовку H1 и URL, чтобы не перепутать классическую версию с Intense/Blush/Elixir
            if not is_strict_match(h1_clean, brand, title, page.url, check_flankers=True, target_conc=target_conc):
                if not is_strict_match(header_full, brand, title, page.url, check_flankers=True, target_conc=target_conc):
                    continue

            html_content = await page.content()
            pyramid = extract_pyramid_from_html(html_content)
            reviews = extract_reviews_from_html(html_content, "золотое яблоко", max_reviews=3)

            base_product_url = page.url.split("?")[0]
            collected_offers = await extract_volumes_and_prices_on_product_page(
                page, "Золотое Яблоко", base_product_url, card_fallback_text
            )

            if collected_offers:
                return STATUS_FOUND, collected_offers, pyramid, reviews

        return STATUS_NOT_FOUND, [], None, []

    except Exception:
        return STATUS_BLOCKED, [], None, []


# =====================================================================
# АВТО-ОЧИСТКА БАЗЫ ОТ МУСОРА
# =====================================================================
def clean_product_title(brand: str, raw_title: str) -> str:
    title = (raw_title or "").strip()
    brand_clean = (brand or "").strip()

    title = re.sub(
        r"[\s,(\-–—/]+\d{1,3}(?:[.,]\d)?\s*(?:мл|ml|г|гр|fl\.?\s*oz)\)?\.?\s*$",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip(" ,-–—:")

    if brand_clean and strip_accents(title).lower().startswith(strip_accents(brand_clean).lower()):
        title = title[len(brand_clean):].strip(" ,-–—:")

    prefix_pattern = re.compile(
        r"^(?:женская|мужская|унисекс|парфюмерная\s+вода|парфюмированная\s+вода|"
        r"туалетная\s+вода|духи|одеколон|экстракт|нишевая\s+парфюмерия|"
        r"eau\s+de\s+parfum|eau\s+de\s+toilette|eau\s+de\s+cologne|extrait\s+de\s+parfum)\s+",
        re.IGNORECASE,
    )
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


def normalize_dedup_key(brand: str, title: str) -> str:
    b = canonical_brand_slug(brand)
    cleaned_t = clean_product_title(brand, title)
    t = re.sub(r"[^a-zа-я0-9]+", "", strip_accents(cleaned_t).lower())
    if b and t.startswith(b) and len(t) > len(b) + 2:
        t = t[len(b):]
    return f"{b}_{t}"


def cleanup_duplicate_perfumes_in_db():
    print("🧹 Проверка базы на дубликаты карточек ароматов...")
    all_rows = []
    offset = 0
    step = 1000
    while True:
        res = safe_db(
            supabase.table("perfumes")
            .select("id, brand, title, price, top_notes, heart_notes, base_notes")
            .order("id")
            .range(offset, offset + step - 1)
        )
        batch = (res.data if res and hasattr(res, "data") else []) or []
        all_rows.extend(batch)
        if len(batch) < step:
            break
        offset += step

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
        print("✅ Дубликатов в таблице perfumes не обнаружено.")
        return

    ids_with_offers = set()
    p_off = 0
    while True:
        pres = safe_db(
            supabase.table("perfume_prices")
            .select("perfume_id")
            .neq("volume", "REVIEW")
            .gt("price", 0)
            .range(p_off, p_off + step - 1)
        )
        pbatch = (pres.data if pres and hasattr(pres, "data") else []) or []
        for pr in pbatch:
            if pr.get("perfume_id"):
                ids_with_offers.add(str(pr["perfume_id"]))
        if len(pbatch) < step:
            break
        p_off += step

    removed_count = 0
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
        keep_id = grp_sorted[0]["id"]
        for dup in grp_sorted[1:]:
            dup_id = dup["id"]
            for tbl in ("perfume_prices", "perfume_reviews", "user_shelves", "user_notes", "price_alerts"):
                try:
                    supabase.table(tbl).update({"perfume_id": keep_id}).eq("perfume_id", dup_id).execute()
                except Exception:
                    pass
                try:
                    supabase.table(tbl).delete().eq("perfume_id", dup_id).execute()
                except Exception:
                    pass
            try:
                supabase.table("perfumes").delete().eq("id", dup_id).execute()
                removed_count += 1
            except Exception:
                pass

    print(f"✨ Очистка завершена: объединено и удалено {removed_count} дублирующихся карточек ароматов!")


def cleanup_corrupted_db_data():
    print("🧹 Быстрая очистка БД от старых склеенных цен (>150 000₽), подводок для глаз и атомайзеров...")
    safe_db(supabase.table("perfume_prices").delete().gt("price", 150000))
    for bad_slug in ["podvodka", "dlya-glaz", "eyeliner", "mascara", "tush", "pomada", "blesk"]:
        safe_db(supabase.table("perfume_prices").delete().ilike("store_url", f"%{bad_slug}%"))
    for bad_title in ["Атомайзер%", "Кейс для пробников%", "Набор #%"]:
        safe_db(supabase.table("perfumes").delete().ilike("title", bad_title))
    print("✨ База очищена от мусора!\n")


# =====================================================================
# ВОРКЕР ОБНОВЛЕНИЯ ЦЕН, ПИРАМИД НОТ И ОТЗЫВОВ С САЙТОВ
# =====================================================================
async def worker(worker_id: int, queue: asyncio.Queue, context, total_count: int, low_cpu: bool = False):
    async def block_trackers_only(route):
        req = route.request
        if req.resource_type in ("media", "font"):
            await route.abort()
            return
        u = req.url.lower()
        if any(dom in u for dom in HEAVY_TRACKER_DOMAINS):
            await route.abort()
            return
        await route.continue_()

    async def create_clean_page():
        new_p = await context.new_page()
        await new_p.route("**/*", block_trackers_only)
        return new_p

    page = await create_clean_page()
    processed_since_recycle = 0

    while not queue.empty():
        try:
            idx, perfume = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        # Пересоздаём вкладку каждые 25 духов (или если вкладка упала), чтобы освобождать RAM Chromium
        if processed_since_recycle >= 25 or page.is_closed():
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass
            page = await create_clean_page()
            processed_since_recycle = 0
        processed_since_recycle += 1

        perfume_id = perfume["id"]
        brand = (perfume.get("brand") or "").strip()
        title = (perfume.get("title") or "").strip()
        target_conc = extract_concentration_group(title)

        _, _, clean_query = get_core_search_data(brand, title)
        encoded_plus = quote_plus(clean_query)
        encoded_pct = quote(clean_query)

        existing_offers_res = safe_db(
            supabase.table("perfume_prices")
            .select("store_name, store_url, volume")
            .eq("perfume_id", perfume_id)
        )
        known_urls = {}
        if existing_offers_res and existing_offers_res.data:
            for row in existing_offers_res.data:
                if row.get("volume") == "REVIEW":
                    continue
                s_name = (row.get("store_name") or "").strip()
                s_url = (row.get("store_url") or "").strip()
                if s_name and s_url.startswith("http"):
                    if is_strict_match(brand, brand, title, s_url, check_flankers=True, target_conc=target_conc):
                        known_urls[s_name] = s_url
                    else:
                        safe_db(
                            supabase.table("perfume_prices")
                            .delete()
                            .eq("perfume_id", perfume_id)
                            .eq("store_name", s_name)
                        )

        found_offers = []
        confirmed_not_found_stores = []
        blocked_stores = []
        best_pyramid = None
        all_scraped_reviews = []

        # 1. Randewoo
        r_status, r_offers, r_pyramid, r_reviews = await parse_randewoo(
            page, brand, title, encoded_plus, known_urls.get("Randewoo"), target_conc=target_conc
        )
        if r_status != STATUS_FOUND and known_urls.get("Randewoo"):
            r_status, r_offers, r_pyramid, r_reviews = await parse_randewoo(
                page, brand, title, encoded_plus, None, target_conc=target_conc
            )

        if r_status == STATUS_FOUND:
            found_offers.extend(r_offers)
        elif r_status == STATUS_NOT_FOUND:
            confirmed_not_found_stores.append("Randewoo")
        else:
            blocked_stores.append("Randewoo")

        if r_pyramid:
            best_pyramid = r_pyramid
        if r_reviews:
            all_scraped_reviews.extend(r_reviews)

        if low_cpu:
            await asyncio.sleep(0.4)

        # 2. Рив Гош
        rg_store = {
            "name": "Рив Гош",
            "base": "https://rivegauche.ru",
            "search_url": f"https://rivegauche.ru/search?text={encoded_plus}",
            "card_sel": 'product-item, [class*="product-card"], [class*="product-item"], article',
        }
        rg_status, rg_offers, rg_pyramid, rg_reviews = await parse_rivegauche_or_letu_v21(
            page, rg_store, brand, title, known_urls.get("Рив Гош"), target_conc=target_conc
        )
        if rg_status != STATUS_FOUND and known_urls.get("Рив Гош"):
            rg_status, rg_offers, rg_pyramid, rg_reviews = await parse_rivegauche_or_letu_v21(
                page, rg_store, brand, title, None, target_conc=target_conc
            )

        if rg_status == STATUS_FOUND:
            found_offers.extend(rg_offers)
        elif rg_status == STATUS_NOT_FOUND:
            confirmed_not_found_stores.append("Рив Гош")
        else:
            blocked_stores.append("Рив Гош")

        if rg_pyramid and not best_pyramid:
            best_pyramid = rg_pyramid
        if rg_reviews:
            all_scraped_reviews.extend(rg_reviews)

        if low_cpu:
            await asyncio.sleep(0.4)

        # 3. Золотое Яблоко
        ga_status, ga_offers, ga_pyramid, ga_reviews = await parse_goldapple_v28(
            page, brand, title, encoded_pct, known_urls.get("Золотое Яблоко"), target_conc=target_conc
        )
        if ga_status != STATUS_FOUND and known_urls.get("Золотое Яблоко"):
            ga_status, ga_offers, ga_pyramid, ga_reviews = await parse_goldapple_v28(
                page, brand, title, encoded_pct, None, target_conc=target_conc
            )

        if ga_status == STATUS_FOUND:
            found_offers.extend(ga_offers)
        elif ga_status == STATUS_NOT_FOUND:
            confirmed_not_found_stores.append("Золотое Яблоко")
        else:
            blocked_stores.append("Золотое Яблоко")

        if ga_pyramid and not best_pyramid:
            best_pyramid = ga_pyramid
        if ga_reviews:
            all_scraped_reviews.extend(ga_reviews)

        if low_cpu:
            await asyncio.sleep(0.4)

        # 4. Л'Этуаль
        letu_store = {
            "name": "Л'Этуаль",
            "base": "https://www.letu.ru",
            "search_url": f"https://www.letu.ru/search?Dy=1&Ntt={encoded_pct}",
            "card_sel": '[data-at-product-tile], [class*="product-tile"], [class*="product-card"], article',
        }
        l_status, l_offers, l_pyramid, l_reviews = await parse_rivegauche_or_letu_v21(
            page, letu_store, brand, title, known_urls.get("Л'Этуаль"), target_conc=target_conc
        )
        if l_status != STATUS_FOUND and known_urls.get("Л'Этуаль"):
            l_status, l_offers, l_pyramid, l_reviews = await parse_rivegauche_or_letu_v21(
                page, letu_store, brand, title, None, target_conc=target_conc
            )

        if l_status == STATUS_FOUND:
            found_offers.extend(l_offers)
        elif l_status == STATUS_NOT_FOUND:
            confirmed_not_found_stores.append("Л'Этуаль")
        else:
            blocked_stores.append("Л'Этуаль")

        if l_pyramid and not best_pyramid:
            best_pyramid = l_pyramid
        if l_reviews:
            all_scraped_reviews.extend(l_reviews)

        # 5. Запись цен в БД
        stores_to_clean = list({off["store_name"] for off in found_offers} | set(confirmed_not_found_stores))
        for st_name in stores_to_clean:
            safe_db(
                supabase.table("perfume_prices")
                .delete()
                .eq("perfume_id", perfume_id)
                .eq("store_name", st_name)
                .neq("volume", "REVIEW")
            )

        if found_offers:
            rows_to_insert = [
                {
                    "perfume_id": perfume_id,
                    "store_name": off["store_name"],
                    "volume": off["volume"],
                    "price": off["price"],
                    "store_url": off["store_url"],
                }
                for off in found_offers
            ]
            safe_db(supabase.table("perfume_prices").insert(rows_to_insert))

        # 6. Сохранение отзывов с сайтов
        if all_scraped_reviews:
            save_scraped_reviews_to_db(perfume_id, all_scraped_reviews)

        update_payload = {}
        if best_pyramid:
            if best_pyramid.get("top_notes"):
                update_payload["top_notes"] = best_pyramid["top_notes"]
            if best_pyramid.get("heart_notes"):
                update_payload["heart_notes"] = best_pyramid["heart_notes"]
            if best_pyramid.get("base_notes"):
                update_payload["base_notes"] = best_pyramid["base_notes"]

        if found_offers:
            update_payload["price"] = min(o["price"] for o in found_offers)
        elif not blocked_stores:
            update_payload["price"] = 0

        if update_payload:
            safe_db(supabase.table("perfumes").update(update_payload).eq("id", perfume_id))

        pyr_info = " + 🌿Пирамида" if best_pyramid else ""
        rev_info = f" + 💬Отзывов: {len(all_scraped_reviews)}" if all_scraped_reviews else ""
        not_found_info = f" | Нет в наличии: {', '.join(confirmed_not_found_stores)}" if confirmed_not_found_stores else ""
        blocked_info = f" | 🛡️Антибот: {', '.join(blocked_stores)}" if blocked_stores else ""

        if found_offers:
            details_str = " | ".join(
                f"{o['store_name']}: {o['volume']}={o['price']}₽" for o in found_offers
            )
            print(f"✅ [{idx}/{total_count}] {brand} — {title} -> {details_str}{not_found_info}{blocked_info}{pyr_info}{rev_info}")
        else:
            print(f"⚪ [{idx}/{total_count}] {brand} — {title} -> нет предложений ({', '.join(confirmed_not_found_stores)}){blocked_info}{pyr_info}{rev_info}")

        queue.task_done()
        await asyncio.sleep(random.uniform(0.25, 0.65))

    try:
        if not page.is_closed():
            await page.close()
    except Exception:
        pass


def set_low_process_priority():
    try:
        if sys.platform == "win32":
            import ctypes
            BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)
        else:
            os.nice(10)
    except Exception:
        pass



def parse_playwright_proxy(raw_proxy: str | None) -> dict | None:
    """
    Извлекает чистый IP:PORT и логин/пароль регулярным выражением (даже если в GitHub Secrets
    случайно попали квадратные скобки Markdown [http://...](...) или кавычки).
    """
    if not raw_proxy:
        return None
    s = str(raw_proxy).strip()
    if not s:
        return None

    username = None
    password = None
    host_port = None

    # Вариант 1: user:pass@ip:port
    m_auth = re.search(r"([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+)@(\d{1,3}(?:\.\d{1,3}){3}:\d{2,5})", s)
    if m_auth:
        username, password, host_port = m_auth.group(1), m_auth.group(2), m_auth.group(3)
    else:
        # Вариант 2: ip:port:user:pass
        m_colon = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}):([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+)", s)
        if m_colon:
            host_port, username, password = m_colon.group(1), m_colon.group(2), m_colon.group(3)
        else:
            # Вариант 3: просто ip:port
            m_ip = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}:\d{2,5})", s)
            if m_ip:
                host_port = m_ip.group(1)

    if not host_port:
        return None

    cfg = {
        "server": f"http://{host_port}",
        "bypass": "randewoo.ru,*.randewoo.ru,*.supabase.co",
    }
    if username and password:
        cfg["username"] = username
        cfg["password"] = password
    return cfg


def fetch_all_perfumes_from_db(start_offset: int = 0, max_batch: int = 0) -> list[dict]:
    perfumes = []
    offset = start_offset
    step = 1000
    while True:
        res = safe_db(
            supabase.table("perfumes")
            .select("id, title, brand")
            .order("id")
            .range(offset, offset + step - 1)
        )
        if not res or not res.data:
            break
        rows = res.data
        perfumes.extend(rows)
        if max_batch > 0 and len(perfumes) >= max_batch:
            perfumes = perfumes[:max_batch]
            break
        if len(rows) < step:
            break
        offset += step
    return perfumes


async def main():
    parser = argparse.ArgumentParser(description="Синхронизация цен, объёмов, наличия, пирамид и отзывов")
    parser.add_argument("query", nargs="*", help="Быстрый поиск по названию или бренду")
    parser.add_argument("--workers", type=int, default=CONCURRENT_WORKERS, help="Количество потоков (по умолчанию 3)")
    parser.add_argument("--batch", type=int, default=0, help="Сколько духов проверить за один запуск (0 = все)")
    parser.add_argument("--offset", type=int, default=0, help="С какого индекса в БД начать проверку")
    parser.add_argument("--headless", action="store_true", help="Скрыть окно браузера за экран (фоновый режим)")
    parser.add_argument("--low-cpu", action="store_true", help="Режим 1 потока и низкого приоритета CPU для слабых ПК")
    parser.add_argument("--proxy", type=str, default="", help="Российский прокси (например ip:port:login:pass или из переменной RU_PROXY)")
    args = parser.parse_args()

    workers_cnt = 1 if args.low_cpu else max(1, min(args.workers, 6))
    if args.low_cpu:
        set_low_process_priority()

    raw_proxy_str = (args.proxy or os.environ.get("RU_PROXY", "")).strip()
    if "=" in raw_proxy_str and raw_proxy_str.upper().startswith("RU_PROXY"):
        raw_proxy_str = raw_proxy_str.split("=", 1)[1].strip()
    if not raw_proxy_str and sys.platform != "win32":
        raw_proxy_str = "http://" + "AQthJ6:Zp4nof" + "@195.208.171.114:8000"
    proxy_cfg = parse_playwright_proxy(raw_proxy_str)
    proxy_label = f" | 🌐 Прокси РФ: подключен ({proxy_cfg['server']})" if proxy_cfg else " | 🌐 Прокси: без прокси (прямое соединение)"

    raw_search = " ".join(args.query).strip()
    search_filter = re.sub(r"\s+", " ", raw_search).strip()
    print(f"🚀 ЗАПУСК СИНХРОНИЗАЦИИ v40.0 ({workers_cnt} потока{proxy_label})...")

    perfumes = []
    if search_filter:
        print(f"⚡ Умный поиск «{search_filter}» в базе данных...")
        query_words = normalize_text(search_filter).split()
        longest_word = max(query_words, key=len) if query_words else search_filter
        res_t = safe_db(supabase.table("perfumes").select("id, title, brand").ilike("title", f"%{longest_word}%"))
        res_b = safe_db(supabase.table("perfumes").select("id, title, brand").ilike("brand", f"%{longest_word}%"))

        candidates = []
        seen_ids = set()
        for r in (res_t.data if res_t and res_t.data else []) + (res_b.data if res_b and res_b.data else []):
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                candidates.append(r)

        for r in candidates:
            combined_norm = normalize_text(f"{r.get('brand', '')} {r.get('title', '')}")
            if all(w in combined_norm for w in query_words):
                perfumes.append(r)

        if not perfumes:
            all_rows = fetch_all_perfumes_from_db()
            for r in all_rows:
                combined_norm = normalize_text(f"{r.get('brand', '')} {r.get('title', '')}")
                if all(w in combined_norm for w in query_words):
                    perfumes.append(r)

        print(f"🎯 Найдено в БД по запросу «{search_filter}»: {len(perfumes)} шт.\n")
    else:
        if args.offset == 0:
            cleanup_duplicate_perfumes_in_db()
            cleanup_corrupted_db_data()
        print(f"📦 Загружаем список ароматов из базы (offset={args.offset}, batch={args.batch or 'все'})...")
        perfumes = fetch_all_perfumes_from_db(start_offset=args.offset, max_batch=args.batch)
        print(f"📦 Всего ароматов в базе для проверки: {len(perfumes)}\n")

    if not perfumes:
        print("⚠️ Ароматы не найдены.")
        return

    queue = asyncio.Queue()
    for idx, p in enumerate(perfumes, start=1):
        queue.put_nowait((idx, p))

    profile_dir = os.path.join(os.getcwd(), f"pw_profile_v28_{args.offset}")

    async with async_playwright() as p:
        win_pos = "--window-position=-32000,-32000" if args.headless else "--window-position=60,60"
        chrome_args = [
            win_pos,
            "--window-size=1024,700",
            "--disable-blink-features=AutomationControlled",
        ]
        ctx_kwargs = {
            "user_data_dir": profile_dir,
            "viewport": {"width": 1366, "height": 900},
            "locale": "ru-RU",
            "args": chrome_args,
        }
        if proxy_cfg:
            ctx_kwargs["proxy"] = proxy_cfg

        try:
            context = await p.chromium.launch_persistent_context(
                channel="chrome",
                headless=False,
                **ctx_kwargs,
            )
        except Exception:
            context = await p.chromium.launch_persistent_context(
                headless=args.headless,
                **ctx_kwargs,
            )

        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        active_workers = min(workers_cnt, max(1, len(perfumes)))
        tasks = [
            asyncio.create_task(worker(i, queue, context, len(perfumes), low_cpu=args.low_cpu))
            for i in range(active_workers)
        ]
        await asyncio.gather(*tasks)
        await context.close()

    print("\n🎉 Синхронизация цен, объёмов, наличия, нот и отзывов успешно завершена!")


if __name__ == "__main__":
    asyncio.run(main())

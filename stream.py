import os
import json
import requests
import logging
import time
import re
import hashlib
from datetime import datetime, timedelta
from dotenv import load_dotenv
from supabase import create_client, Client as SupabaseClient

load_dotenv()

BEARER_TOKEN    = os.getenv("X_BEARER_TOKEN")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_RESTOCK")

ACCOUNTS = [
    "PokemonFindr",
    "PokemonRestocks",
    "TCGTouchdown",
    "PokemonDealsTCG",
    "ricanking6",
    "PokeAlerts_",
    "LuckyPawTCG",
    "PTCGrestock",
    "PokeTCGAlerts",
    "OnePieceAlerts",
    "PokemonFindr",
    "DropDexHQ",
    "VIVID_RESTOCK",
    "pokepullzhq",
    "Detailed91"
]

ALERT_EMOJIS = {
    "restock": "🚨",
    "in stock": "✅",
    "deal": "💰",
    "alert": "📣",
}

CATEGORY_EMOJIS = {
    "pokemon": "⚡ Pokémon TCG",
    "football": "🏈 Football",
    "baseball": "⚾ Baseball",
    "basketball": "🏀 Basketball",
    "hockey": "🏒 Hockey",
}

STORE_MAP = {
    "target": "Target",
    "walmart": "Walmart",
    "amazon": "Amazon",
    "costco": "Costco",
    "gamestop": "GameStop",
    "bestbuy": "Best Buy",
    "toysrus": "Toys R Us",
    "dick's": "Dick's Sporting Goods",
    "dicks": "Dick's Sporting Goods",
    "scheels": "Scheels",
    "best buy": "Best Buy",
}

ALERT_COLORS = {
    "restock": 0xFF4500,
    "in stock": 0x2ECC71,
    "deal": 0xF1C40F,
    "alert": 0x3498DB,
}

REQUIRED_SIGNALS = [
    r'\$[\d,]+\.?\d*',
    r'\bin stock\b',
    r'\brestock\b',
    r'\bpreorder\b',
    r'\bpre-order\b',
    r'\bpre order\b',
    r'\breservation\b',
    r'\bavailable\b',
    r'\bdrops?\s+(now|today|live)\b',
    r'\bjust\s+dropped\b',
    r'\bback\s+in\s+stock\b',
    r'\blive\s+now\b',
]

HARD_BLOCKLIST = [
    "giveaway",
    "give away",
    "enter to win",
    "win a ",
    "chance to win",
    "retweet to win",
    "follow to win",
    "contest",
    "sweepstakes",
    "slab code",
    "enter our",
    "entering our",
    "🎁",
    "🏆",
    "what's your favourite",
    "what is your favourite",
    "what's your favorite",
    "what is your favorite",
    "without price in mind",
    "favourite card",
    "favorite card",
    "who would win",
    "which is better",
    "which do you prefer",
    "\bpoll\b",
    "shout-out sunday",
    "shoutout sunday",
    "built in pokopia",
    "spotted in pokopia",
    "custom cherry blossom",
    "reddit u/",
    "just another account",
    "fake news",
    "posting fake news",
    "reputable accounts",
    "sam's club membership",
    "sams club membership",
    "membership deal",
    "1-year membership",
    "plus membership",
    "basic membership",
    "next week is going to be huge",
    "good luck if you're opening",
    "have a great weekend",
    "keep an eye out",
    "commission",
    "went viral",
    "joined twitter",
    "enter by",
    " draw ",
    "raffle",
    "enter now for",
]

DEDUP_WINDOW_HOURS = 2
seen_fingerprints: dict = {}

# Module-level retry delay for exponential backoff on 429s
retry_delay = 30

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ===========================================================================
# Stream rules
# ===========================================================================

def get_rules():
    resp = requests.get(
        "https://api.twitter.com/2/tweets/search/stream/rules",
        headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
    )
    resp.raise_for_status()
    return resp.json()

def delete_rules(rule_ids):
    if not rule_ids:
        return
    requests.post(
        "https://api.twitter.com/2/tweets/search/stream/rules",
        headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
        json={"delete": {"ids": rule_ids}},
    )

def set_rules():
    existing = get_rules()
    ids = [r["id"] for r in existing.get("data", [])]
    delete_rules(ids)

    from_clause = " OR ".join([f"from:{a}" for a in ACCOUNTS])
    rule = f"({from_clause}) -is:reply -is:retweet has:links lang:en"

    rules = {"add": [{"value": rule, "tag": "restock_accounts"}]}
    resp = requests.post(
        "https://api.twitter.com/2/tweets/search/stream/rules",
        headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
        json=rules,
    )
    resp.raise_for_status()
    log.info(f"Stream rules set: {rule}")

# ===========================================================================
# Filtering
# ===========================================================================

def has_deal_signal(text: str) -> bool:
    text_lower = text.lower()
    return any(re.search(pattern, text_lower) for pattern in REQUIRED_SIGNALS)

def is_blocked(text: str) -> bool:
    text_lower = text.lower()
    return any(re.search(phrase, text_lower) for phrase in HARD_BLOCKLIST)

def should_post(text: str, author_username: str) -> bool:
    if not has_deal_signal(text):
        log.info(f"Dropped (no deal signal): @{author_username} — {text[:80]}")
        return False
    if is_blocked(text):
        log.info(f"Dropped (blocklist): @{author_username} — {text[:80]}")
        return False
    return True

# ===========================================================================
# Deduplication
# ===========================================================================

def make_fingerprint(alert_type: str, store: str, product: str) -> str:
    key = f"{alert_type}|{store or ''}|{product[:30].lower().strip()}"
    return hashlib.md5(key.encode()).hexdigest()

def is_duplicate(fingerprint: str) -> bool:
    now = datetime.utcnow()
    if fingerprint in seen_fingerprints:
        if now - seen_fingerprints[fingerprint] < timedelta(hours=DEDUP_WINDOW_HOURS):
            return True
        else:
            del seen_fingerprints[fingerprint]
    seen_fingerprints[fingerprint] = now
    return False

# ===========================================================================
# Tweet parsing
# ===========================================================================

def detect_alert_type(text: str) -> str:
    text_lower = text.lower()
    if "in stock" in text_lower:
        return "in stock"
    if "restock" in text_lower:
        return "restock"
    if "deal" in text_lower:
        return "deal"
    return "alert"

def detect_category(text: str) -> str:
    text_lower = text.lower()
    if any(w in text_lower for w in ["pokemon", "pokémon", "poke"]):
        return "pokemon"
    if any(w in text_lower for w in ["football", "nfl"]):
        return "football"
    if any(w in text_lower for w in ["baseball", "mlb"]):
        return "baseball"
    if any(w in text_lower for w in ["basketball", "nba"]):
        return "basketball"
    if any(w in text_lower for w in ["hockey", "nhl"]):
        return "hockey"
    return "trading cards"

def detect_store(text: str) -> str:
    text_lower = text.lower()
    for key, name in STORE_MAP.items():
        if key in text_lower:
            return name
    return None

def detect_price(text: str, context: str = None) -> str:
    if context:
        prices = [(m.group(0), m.start()) for m in re.finditer(r'\$[\d,]+\.?\d*', text)]
        if prices:
            ctx_pos = text.lower().find(context[:20].lower())
            if ctx_pos >= 0:
                closest = min(prices, key=lambda p: abs(p[1] - ctx_pos))
                return closest[0]
    match = re.search(r'\$[\d,]+\.?\d*', text)
    return match.group(0) if match else None

def extract_links_with_labels(text: str, entities: dict = None) -> list:
    SKIP_PATTERNS = [
        "twitter.com/intent",
        "t.co/",
        "x.com/i/",
        "/photo/",
        "pic.x.com",
        "pic.twitter.com",
    ]
    results = []

    def should_skip(url):
        return any(s in url for s in SKIP_PATTERNS)

    if entities and "urls" in entities:
        for url_obj in entities["urls"]:
            final_url = (
                url_obj.get("unwound_url")
                or url_obj.get("expanded_url")
                or ""
            )
            expanded = url_obj.get("expanded_url", "")
            if not final_url or should_skip(final_url) or should_skip(expanded):
                continue
            raw = url_obj.get("title") or url_obj.get("display_url") or None
            label = clean_link_title(raw) or raw
            results.append((label, final_url))
            log.debug(f"Entity link: {label!r} => {final_url}")

    if not results:
        log.debug("No entity URLs found — falling back to text parsing")
        labeled_pattern = re.compile(
            r'-\s*([^:\n\-]{2,40}?)\s*:\s*(https?://\S+)', re.IGNORECASE
        )
        matched_urls = set()
        for m in labeled_pattern.finditer(text):
            label = m.group(1).strip().title()
            url   = m.group(2).rstrip('.,)')
            if should_skip(url):
                continue
            results.append((label, url))
            matched_urls.add(url)
        for url in re.findall(r'https?://\S+', text):
            url = url.rstrip('.,)')
            if url in matched_urls or should_skip(url):
                continue
            results.append((None, url))

    return results[:6]

def extract_product(text: str) -> str:
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'pic\.(x|twitter)\.com/\S+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'#\w+', '', text)
    text = re.sub(r'#AD|#ad|\bAD\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\d{1,2}/\d{1,2}/\d{2,4}\s+\d{1,2}:\d{2}\s*[APap][Mm]\s*[A-Z]{2,4}', '', text)
    text = re.sub(r'\b[A-Z][a-z]{2}\s+\d{1,2},?\s+\d{4}\s+\d{1,2}:\d{2}\s*[APap][Mm]', '', text)
    text = re.sub(r'\d{1,2}:\d{2}\s*[APap][Mm]\s*[A-Z]{2,4}', '', text)
    text = re.sub(r'\(Less than[^)]*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'Less than MSRP', '', text, flags=re.IGNORECASE)

    noise = [
        "IN STOCK ALERT", "RESTOCK", "IN STOCK", "Sold by", "As of",
        "Follow", "Bookmark", "Save this", "We'll tweet", "We'll post",
        "follow with notifications", "alert if they drop",
        "historically restocks", "MSRP",
        "🛎️", "🚨", "📣", "✅", "💰", "🏪", "📦", "🔗", "📡",
    ]
    for n in noise:
        text = re.sub(re.escape(n), '', text, flags=re.IGNORECASE)

    lines = [l.strip() for l in text.split('\n') if l.strip()]
    product_lines = []
    for line in lines:
        if len(line) < 10:
            continue
        if re.match(r'^[\$\d\s\.,()+%-]+$', line):
            continue
        if any(line.strip().lower() == s.lower() for s in list(STORE_MAP.values())):
            continue
        if re.match(r'^[A-Z][A-Za-z0-9]+$', line) and len(line) < 20:
            continue
        product_lines.append(line)

    return '\n'.join(product_lines[:3]) if product_lines else text[:200].strip()

# ===========================================================================
# Clean Link Title
# ===========================================================================

def clean_link_title(title: str) -> str:
    if not title:
        return None
    noise_patterns = [
        r'\s*[-|]\s*walmart.*$',
        r'\s*[-|]\s*amazon.*$',
        r'\s*[-|]\s*target.*$',
        r'\s*in stock.*$',
        r'\s*\|\s*.*$',
    ]
    cleaned = title
    for pattern in noise_patterns:
        cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE).strip()
    if len(cleaned) < 8:
        return None
    return cleaned[:80]

def parse_product_lines(text: str) -> list:
    cleaned = re.sub(r'https?://\S+', '', text)
    cleaned = re.sub(r'pic\.(x|twitter)\.com/\S+', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'#\w+', '', cleaned)
    cleaned = re.sub(r'\(Stock:\s*[\d\.]+[kKmM]?\)', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'Stock:\s*[\d\.]+[kKmM]?', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'#AD|#ad|\bAD\b', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\$[\d,]+\.?\d*', '', cleaned)

    noise = [
        "IN STOCK ALERT", "RESTOCK", "IN STOCK", "Sold by", "As of",
        "Follow", "Bookmark", "Save this", "We'll tweet", "We'll post",
        "Walmart + not needed", "Walmart+ not needed",
        "Good luck", "Limit should be",
        "🛎️", "🚨", "📣", "✅", "💰", "🏪", "📦", "🔗", "📡", "☑️",
    ]
    for n in noise:
        cleaned = re.sub(re.escape(n), '', cleaned, flags=re.IGNORECASE)

    lines = []
    for line in cleaned.split('\n'):
        line = line.strip(' -•·')
        if len(line) < 8:
            continue
        if re.match(r'^[\$\d\s\.,()+%-]+$', line):
            continue
        lines.append(line)

    return lines

# ===========================================================================
# Sealed product price lookup
# ===========================================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase_client: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

_sealed_tcg_cache:       list  = []
_sealed_scp_cache:       list  = []
_sealed_cache_loaded_at: float = 0.0
SEALED_CACHE_TTL = 86400  # reload once per day

SEALED_NOISE = {
    "the", "and", "for", "with", "of", "a", "an",
    "trading", "card", "cards", "game",
    "new", "official", "includes", "receive", "random",
    "pokemon", "tcg",
}

PRODUCT_TYPE_TOKENS = {
    "hobby", "blaster", "retail", "hanger", "mega", "jumbo",
    "collector", "value", "cello", "gravity", "fat",
    "booster", "bundle", "elite", "trainer", "tin",
}

def tokenize_sealed(text: str) -> set:
    return {
        w.lower() for w in re.split(r'[\W_]+', text)
        if len(w) >= 3 and w.lower() not in SEALED_NOISE
    }

def extract_year(text: str):
    m = re.search(r'\b(20\d{2})\b', text)
    return int(m.group(1)) if m else None

def load_sealed_cache():
    global _sealed_tcg_cache, _sealed_scp_cache, _sealed_cache_loaded_at
    now = time.time()
    if now - _sealed_cache_loaded_at < SEALED_CACHE_TTL:
        return

    # BUG FIX 1: TCGCSV cache queries tcgcsv_products, not scp_prices
    try:
        result = supabase_client.table("tcgcsv_products") \
            .select("product_id, clean_name") \
            .eq("is_sealed", True) \
            .eq("category_id", 3) \
            .execute()
        _sealed_tcg_cache = result.data or []
        log.info(f"Sealed cache: loaded {len(_sealed_tcg_cache)} TCGCSV Pokemon products")
    except Exception as e:
        log.error(f"Failed to load TCGCSV sealed cache: {e}")

    # SCP — sports sealed with prices, filtered to real sealed product types only
    try:
        result = supabase_client.table("scp_prices") \
            .select("product_name, product, brand, year, sport, loose_price") \
            .eq("is_sealed", True) \
            .not_.is_("loose_price", "null") \
            .in_("product_name", [
                "Hobby Box", "Blaster Box", "Mega Box", "Retail Box", "Value Box",
                "Blaster Value Box", "Fat Pack", "Cello Pack", "Jumbo Pack",
                "Hobby Pack", "Retail Pack", "Hanger Box", "Hanger Pack",
                "Gravity Pack", "Gravity Feed", "Tin", "Collector Tin",
                "Booster Box", "Booster Pack", "Bundle", "Case",
            ]) \
            .execute()
        _sealed_scp_cache = result.data or []
        log.info(f"Sealed cache: loaded {len(_sealed_scp_cache)} SCP sports products")
    except Exception as e:
        log.error(f"Failed to load SCP sealed cache: {e}")

    _sealed_cache_loaded_at = now

def lookup_sealed_price(product: str, category: str = "trading cards") -> dict | None:
    if not product:
        return None

    load_sealed_cache()

    query_tokens = tokenize_sealed(product)
    query_year   = extract_year(product)

    if len(query_tokens) < 2:
        return None

    # ===========================================================
    # TCGCSV — Pokemon
    # ===========================================================
    if category == "pokemon" and _sealed_tcg_cache:
        best_score = 0.0
        best_match = None

        for row in _sealed_tcg_cache:
            name      = row.get("clean_name") or ""
            name_year = extract_year(name)

            if query_year and name_year and query_year != name_year:
                continue

            name_tokens = tokenize_sealed(name)
            if not name_tokens:
                continue

            overlap = query_tokens & name_tokens

            # Hard filter — product type tokens must match exactly
            query_type_tokens = PRODUCT_TYPE_TOKENS & query_tokens
            name_type_tokens  = PRODUCT_TYPE_TOKENS & name_tokens
            if query_type_tokens and name_type_tokens and query_type_tokens != name_type_tokens:
                continue

            if len(overlap) < 3:
                continue

            score = len(overlap) / max(len(query_tokens), len(name_tokens))
            if score > best_score and score >= 0.70:
                best_score = score
                best_match = row

        if best_match:
            try:
                result = supabase_client.table("tcgcsv_prices_wide") \
                    .select("market_price, low_price") \
                    .eq("product_id", best_match["product_id"]) \
                    .not_.is_("market_price", "null") \
                    .limit(1) \
                    .execute()
                if result.data:
                    return {
                        "matched_name": best_match["clean_name"],
                        "market_price": float(result.data[0]["market_price"]),
                        "low_price":    float(result.data[0]["low_price"]) if result.data[0]["low_price"] else None,
                        "source":       "TCGPlayer",
                        "score":        round(best_score, 2),
                    }
            except Exception as e:
                log.error(f"TCGCSV price fetch error: {e}")

    # ===========================================================
    # SCP — Sports
    # ===========================================================
    sport_map = {
        "football": "football", "basketball": "basketball",
        "baseball": "baseball", "hockey": "hockey",
    }
    scp_sport = sport_map.get(category)

    if scp_sport and _sealed_scp_cache:
        best_score = 0.0
        best_match = None

        for row in _sealed_scp_cache:
            if (row.get("sport") or "").lower() != scp_sport:
                continue

            row_year = row.get("year")
            if query_year and row_year and int(row_year) != query_year:
                continue

            combined   = f"{row.get('year') or ''} {row.get('brand') or ''} {row.get('product') or ''} {row.get('product_name') or ''}"
            row_tokens = tokenize_sealed(combined)
            if not row_tokens:
                continue

            # BUG FIX 2: use row_tokens, not name_tokens
            overlap = query_tokens & row_tokens

            # Hard filter — product type tokens must match exactly
            query_type_tokens = PRODUCT_TYPE_TOKENS & query_tokens
            row_type_tokens   = PRODUCT_TYPE_TOKENS & row_tokens
            if query_type_tokens and row_type_tokens and query_type_tokens != row_type_tokens:
                continue

            if len(overlap) < 3:
                continue

            score = len(overlap) / max(len(query_tokens), len(row_tokens))
            if score > best_score and score >= 0.70:
                best_score = score
                best_match = row

        if best_match:
            loose = best_match.get("loose_price")
            if loose:
                return {
                    "matched_name": f"{best_match.get('year') or ''} {best_match.get('brand') or ''} {best_match.get('product') or ''} {best_match.get('product_name') or ''}".strip(),
                    "market_price": float(loose),
                    "low_price":    None,
                    "source":       "SportsCardPro",
                    "score":        round(best_score, 2),
                }

    return None


def format_price_line(tweet_price: str, sealed_match: dict) -> str:
    market = sealed_match["market_price"]
    low    = sealed_match["low_price"]
    source = sealed_match["source"]
    name   = sealed_match["matched_name"]

    tweet_val = None
    if tweet_price:
        m = re.search(r'[\d,.]+', tweet_price.replace(',', ''))
        if m:
            try:
                tweet_val = float(m.group())
            except ValueError:
                pass

    if tweet_val and market:
        pct_diff  = ((tweet_val - market) / market) * 100
        direction = f"{abs(pct_diff):.0f}% below market ✅" if pct_diff < -2 else \
                    f"{abs(pct_diff):.0f}% above market ⚠️" if pct_diff > 2 else \
                    "at market 〰️"
        low_str = f" | Low: ${low:.2f}" if low else ""
        return f"📈 {source}: ${market:.2f}{low_str} — {direction}\n🔍 Matched: {name}"
    elif market:
        low_str = f" | Low: ${low:.2f}" if low else ""
        return f"📈 {source} Market: ${market:.2f}{low_str}\n🔍 Matched: {name}"

    return None

# ===========================================================================
# Discord posting
# ===========================================================================

def post_discord(tweet_data: dict, author_username: str):
    text      = tweet_data.get("text", "")
    entities  = tweet_data.get("entities", {})
    tweet_id  = tweet_data.get("id", "")
    tweet_url = f"https://x.com/{author_username}/status/{tweet_id}"

    if not should_post(text, author_username):
        return

    alert_type     = detect_alert_type(text)
    category       = detect_category(text)
    store          = detect_store(text)
    color          = ALERT_COLORS.get(alert_type, 0x3498DB)
    alert_emoji    = ALERT_EMOJIS.get(alert_type, "📣")
    category_label = CATEGORY_EMOJIS.get(category, "🃏 Trading Cards")

    labeled_links = extract_links_with_labels(text, entities)

    if labeled_links:
        product_lines = parse_product_lines(text)
        alerts_sent   = 0

        for i, (label, url) in enumerate(labeled_links[:6]):
            product = clean_link_title(label)
            if not product and i < len(product_lines):
                product = product_lines[i]
            if not product:
                product = extract_product(text)

            link_category      = detect_category(label or "") if label else category
            effective_category = link_category if link_category != "trading cards" else category
            category_label     = CATEGORY_EMOJIS.get(effective_category, "🃏 Trading Cards")

            price = detect_price(text, context=product)

            fingerprint = make_fingerprint(alert_type, store, product)
            if is_duplicate(fingerprint):
                log.info(f"Duplicate suppressed: @{author_username} — {product[:40]}")
                continue

            meta_parts = []
            if store: meta_parts.append(f"🏪 {store}")
            if price: meta_parts.append(f"💰 {price}")
            meta_line = "  ·  ".join(meta_parts)

            sealed_match = lookup_sealed_price(product, effective_category)
            price_line   = format_price_line(price, sealed_match) if sealed_match else None

            lines = []
            if product:    lines.append(f"📦 {product}")
            if meta_line:  lines.append(meta_line)
            if price_line: lines.append(price_line)

            embed = {
                "title":       f"{alert_emoji} {category_label} — {alert_type.upper()}",
                "url":         url,
                "description": '\n'.join(lines),
                "color":       color,
            }

            resp = requests.post(
                DISCORD_WEBHOOK,
                json={"embeds": [embed]},
                headers={"Content-Type": "application/json"},
            )
            if not resp.ok:
                log.error(f"Discord error {resp.status_code}: {resp.text}")
            else:
                alerts_sent += 1
                log.info(f"Posted embed {alerts_sent}: {alert_type.upper()} — {product[:40]}")
            time.sleep(0.3)

    else:
        product = extract_product(text)
        price   = detect_price(text)

        fingerprint = make_fingerprint(alert_type, store, product)
        if is_duplicate(fingerprint):
            log.info(f"Duplicate suppressed: @{author_username}")
            return

        meta_parts = []
        if store: meta_parts.append(f"🏪 {store}")
        if price: meta_parts.append(f"💰 {price}")
        meta_line = "  ·  ".join(meta_parts)

        sealed_match = lookup_sealed_price(product, category)
        price_line   = format_price_line(price, sealed_match) if sealed_match else None

        lines = []
        if product:    lines.append(f"📦 {product}")
        if meta_line:  lines.append(meta_line)
        if price_line: lines.append(price_line)  # BUG FIX 3: was missing in no-links fallback
        lines.append(f"🔗 [View Tweet]({tweet_url})")

        embed = {
            "title":       f"{alert_emoji} {category_label} — {alert_type.upper()}",
            "url":         tweet_url,
            "description": '\n'.join(lines),
            "color":       color,
        }

        resp = requests.post(
            DISCORD_WEBHOOK,
            json={"embeds": [embed]},
            headers={"Content-Type": "application/json"},
        )
        if not resp.ok:
            log.error(f"Discord error {resp.status_code}: {resp.text}")
        else:
            log.info(f"Posted: {alert_type.upper()} — {category_label} via @{author_username}")

# ===========================================================================
# Stream
# ===========================================================================

def get_author_username(author_id: str) -> str:
    resp = requests.get(
        f"https://api.twitter.com/2/users/{author_id}",
        headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
    )
    if resp.ok:
        return resp.json().get("data", {}).get("username", "unknown")
    return "unknown"

def stream():
    global retry_delay
    log.info("Connecting to stream...")
    with requests.get(
        "https://api.twitter.com/2/tweets/search/stream",
        headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
        params={
            "tweet.fields": "created_at,author_id,text,entities",
            "expansions":   "author_id,attachments.media_keys",
            "user.fields":  "username",
        },
        stream=True,
        timeout=90,
    ) as resp:
        if resp.status_code == 429:
            log.warning(f"Rate limited (429) — backing off {retry_delay}s before retry...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 900)
            return

        if not resp.ok:
            log.error(f"Stream error {resp.status_code}: {resp.text}")
            return

        retry_delay = 30
        log.info("Stream connected — listening for tweets...")

        for line in resp.iter_lines():
            if not line:
                continue
            try:
                data            = json.loads(line)
                tweet           = data.get("data", {})
                includes        = data.get("includes", {})
                users           = {u["id"]: u["username"] for u in includes.get("users", [])}
                author_id       = tweet.get("author_id", "")
                author_username = users.get(author_id, get_author_username(author_id))
                post_discord(tweet, author_username)
            except Exception as e:
                log.error(f"Error processing tweet: {e}")

# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    log.info("Restock stream starting...")
    log.info("Waiting 30s for any previous connections to close...")
    time.sleep(30)
    set_rules()

    while True:
        try:
            stream()
        except requests.exceptions.Timeout:
            log.warning("Stream timed out — reconnecting in 5s...")
            time.sleep(5)
        except requests.exceptions.ConnectionError:
            log.warning("Connection dropped — reconnecting in 5s...")
            time.sleep(5)
        except Exception as e:
            log.error(f"Unexpected error: {e} — reconnecting in 10s...")
            time.sleep(10)

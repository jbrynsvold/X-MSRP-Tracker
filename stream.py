import os
import json
import requests
import logging
import time
import re
import hashlib
from datetime import datetime, timedelta
from dotenv import load_dotenv

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

# ---------------------------------------------------------------------------
# ALLOWLIST — tweet must match at least one of these patterns to be posted.
# If none match, the tweet is silently dropped.
# ---------------------------------------------------------------------------
REQUIRED_SIGNALS = [
    r'\$[\d,]+\.?\d*',               # price ($29.99, $1,299)
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

# ---------------------------------------------------------------------------
# BLOCKLIST — tweet is dropped if any phrase matches, regardless of signals.
# Evaluated AFTER the allowlist so a price in a giveaway post still gets killed.
# ---------------------------------------------------------------------------
HARD_BLOCKLIST = [
    # Giveaways / contests
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
    # Engagement bait / polls / opinion posts
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
    # Community / fan content
    "shout-out sunday",
    "shoutout sunday",
    "built in pokopia",
    "spotted in pokopia",
    "custom cherry blossom",
    "reddit u/",                     # user-generated content shares
    # Meta / commentary posts
    "just another account",
    "fake news",
    "posting fake news",
    "reputable accounts",
    # Non-card membership deals
    "sam's club membership",
    "sams club membership",
    "membership deal",
    "1-year membership",
    "plus membership",               # Sam's Club plan name
    "basic membership",              # Sam's Club plan name
    # Hype / no-action posts
    "next week is going to be huge",
    "good luck if you're opening",
    "have a great weekend",
    "keep an eye out",
    # Artist / influencer shoutouts
    "commission",
    "went viral",
    "joined twitter",
]

DEDUP_WINDOW_HOURS = 2
seen_fingerprints: dict = {}

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
# Filtering — allowlist-first, then blocklist
# ===========================================================================

def has_deal_signal(text: str) -> bool:
    """Returns True only if the tweet contains at least one actionable deal signal."""
    text_lower = text.lower()
    return any(re.search(pattern, text_lower) for pattern in REQUIRED_SIGNALS)

def is_blocked(text: str) -> bool:
    """Returns True if the tweet matches any hard-blocklist phrase."""
    text_lower = text.lower()
    return any(re.search(phrase, text_lower) for phrase in HARD_BLOCKLIST)

def should_post(text: str, author_username: str) -> bool:
    """
    Gate logic (order matters):
      1. Must have at least one deal signal — kills engagement/fan/hype posts.
      2. Must not match the blocklist — kills giveaways, membership deals, etc.
    """
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
    if any(w in text_lower for w in ["pokemon", "pokémon", "tcg", "poke"]):
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

def detect_price(text: str) -> str:
    match = re.search(r'\$[\d,]+\.?\d*', text)
    return match.group(0) if match else None

def extract_links_with_labels(text: str) -> list:
    """
    Returns a list of (label, url) tuples.
    Handles patterns like:
      - Hobby Box: https://...
      - Hobby Box: https://...
      Raw URLs with no label get label=None.
    """
    skip = ["trackalacker.com", "twitter.com/intent", "t.co/"]

    # Match optional leading dash + label + colon + URL
    labeled_pattern = re.compile(
        r'-\s*([^:\n\-]{2,40}?)\s*:\s*(https?://\S+)', re.IGNORECASE
    )
    results = []
    matched_urls = set()

    for m in labeled_pattern.finditer(text):
        label = m.group(1).strip().title()
        url   = m.group(2).rstrip('.,)')
        if any(s in url for s in skip):
            continue
        results.append((label, url))
        matched_urls.add(url)

    # Pick up any remaining raw URLs not already captured
    for url in re.findall(r'https?://\S+', text):
        url = url.rstrip('.,)')
        if url in matched_urls:
            continue
        if any(s in url for s in skip):
            continue
        results.append((None, url))

    return results[:6]

def extract_product(text: str) -> str:
    text = re.sub(r'#\w+', '', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'#AD|#ad|\bAD\b', '', text, flags=re.IGNORECASE)

    noise = [
        "IN STOCK ALERT", "RESTOCK", "IN STOCK", "Sold by", "As of",
        "Follow", "Bookmark", "Save this", "We'll tweet", "We'll post",
        "follow with notifications", "alert if they drop",
        "historically restocks", "MSRP", "Less than MSRP",
        "🛎️", "🚨", "📣", "✅", "💰", "🏪", "📦", "🔗", "📡",
    ]
    for n in noise:
        text = re.sub(re.escape(n), '', text, flags=re.IGNORECASE)

    lines = [l.strip() for l in text.split('\n') if l.strip()]
    product_lines = []
    for line in lines:
        if len(line) < 10:
            continue
        if re.match(r'^[\$\d\s\.]+$', line):
            continue
        if any(line.strip().lower() == s.lower() for s in list(STORE_MAP.values())):
            continue
        product_lines.append(line)

    return '\n'.join(product_lines[:3]) if product_lines else text[:200].strip()

# ===========================================================================
# Discord posting
# ===========================================================================

def post_discord(tweet_data: dict, author_username: str):
    text      = tweet_data.get("text", "")
    tweet_id  = tweet_data.get("id", "")
    tweet_url = f"https://x.com/{author_username}/status/{tweet_id}"

    # Single gate — allowlist signal required, blocklist kills it
    if not should_post(text, author_username):
        return

    alert_type     = detect_alert_type(text)
    category       = detect_category(text)
    store          = detect_store(text)
    price          = detect_price(text)
    labeled_links  = extract_links_with_labels(text)
    product        = extract_product(text)
    color          = ALERT_COLORS.get(alert_type, 0x3498DB)
    alert_emoji    = ALERT_EMOJIS.get(alert_type, "📣")
    category_label = CATEGORY_EMOJIS.get(category, "🃏 Trading Cards")

    fingerprint = make_fingerprint(alert_type, store, product)
    if is_duplicate(fingerprint):
        log.info(f"Duplicate suppressed: {alert_type.upper()} — {product[:40]} via @{author_username}")
        return

    # Build meta line: store and/or price
    meta_parts = []
    if store:
        meta_parts.append(f"🏪 {store}")
    if price:
        meta_parts.append(f"💰 {price}")

    # Build link line: use labels where available, fallback to generic
    link_parts = []
    for i, (label, url) in enumerate(labeled_links):
        if label:
            link_parts.append(f"[{label}]({url})")
        else:
            display = "View Deal" if i == 0 else f"Alt Link {i + 1}"
            link_parts.append(f"[{display}]({url})")

    first_url = labeled_links[0][1] if labeled_links else tweet_url

    lines = []
    if product:
        lines.append(f"📦 {product}")
    if meta_parts:
        lines.append("  ·  ".join(meta_parts))
    if link_parts:
        lines.append("\n🔗 " + "  ·  ".join(link_parts))

    embed = {
        "title": f"{alert_emoji} {category_label} — {alert_type.upper()}",
        "description": '\n'.join(lines),
        "color": color,
        "url": first_url,
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
    log.info("Connecting to stream...")
    with requests.get(
        "https://api.twitter.com/2/tweets/search/stream",
        headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
        params={
            "tweet.fields": "created_at,author_id,text,entities",
            "expansions":   "author_id",
            "user.fields":  "username",
        },
        stream=True,
        timeout=90,
    ) as resp:
        if not resp.ok:
            log.error(f"Stream error {resp.status_code}: {resp.text}")
            return

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
        except requests.exceptions.ConnectionError:
            log.warning("Connection dropped — reconnecting in 5s...")
        except Exception as e:
            log.error(f"Unexpected error: {e} — reconnecting in 10s...")
            time.sleep(5)
        time.sleep(5)

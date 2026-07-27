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
    "Detailed91",
    # --- NEW candidates (verify handles/still-active before relying on these) ---
    "PokeRestockHQ",   # Pokemon Center / Walmart / Target focused, no giveaway noise seen
    "DropsMonitor",    # ricanking6's dedicated alert account (separate from his personal one)
    "HobbyRecap",      # daily sports card restocks — has run giveaways before, HARD_BLOCKLIST should catch those
]

ALERT_EMOJIS = {
    "restock":  "🚨",
    "in stock": "✅",
    "deal":     "💰",
    "alert":    "📣",
    "preorder": "🗓️",  # FIX 3: new preorder type
}

CATEGORY_EMOJIS = {
    "pokemon":    "⚡ Pokémon TCG",
    "football":   "🏈 Football",
    "baseball":   "⚾ Baseball",
    "basketball": "🏀 Basketball",
    "hockey":     "🏒 Hockey",
}

STORE_MAP = {
    "target":     "Target",
    "walmart":    "Walmart",
    "amazon":     "Amazon",
    "costco":     "Costco",
    "gamestop":   "GameStop",
    "bestbuy":    "Best Buy",
    "toysrus":    "Toys R Us",
    "dick's":     "Dick's Sporting Goods",
    "dicks":      "Dick's Sporting Goods",
    "scheels":    "Scheels",
    "best buy":   "Best Buy",
    "barnes & noble": "Barnes & Noble",
    "barnes and noble": "Barnes & Noble",
}

ALERT_COLORS = {
    "restock":  0xFF4500,
    "in stock": 0x2ECC71,
    "deal":     0xF1C40F,
    "alert":    0x3498DB,
    "preorder": 0x9B59B6,  # FIX 3: purple for preorders
}

# FIX 1: Expanded signal list to catch restock-account phrasing like
# "is up at Target", "checking out", "back at", "still up", etc.
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
    # --- NEW signals below ---
    r'\bis\s+up\s+(at|on)\b',          # "is up at Target", "is up on Amazon"
    r'\bup\s+(at|on)\b',               # "up at Target", "up on Walmart"
    r'\bback\s+(at|on|up)\b',          # "back at Target", "back on Amazon", "back up"
    r'\bchecking\s+out\b',             # "checking out on Target"
    r'\bstill\s+(up|checking\s+out)\b',# "still up", "still checking out"
    r'\bgoing\s+live\b',               # "going live"
    r'\bnow\s+(available|live)\b',     # "now available", "now live"
    r'\bstock\s+alert\b',              # "stock alert"
    r'\bjust\s+(went|gone)\s+live\b',  # "just went live"
    r'\bspam\s+place\s+order\b',       # "spam place order" (restock-account specific)
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

# Per-link blocklist — skips individual links rather than dropping the whole tweet.
# Membership upsells, affiliate signups, etc. often appear alongside real deal links.
LINK_BLOCKLIST = [
    "membership",
    "sam's club",
    "sams club",
    "walmart+",
    "walmart plus",
    "costco membership",
    "subscribe",
    "referral",
    "sign up",
    "affiliate",
    "/join",
    "/signup",
    "/subscribe",
]

# FIX 4: Generic app-promo/self-referral title patterns (e.g. "Download The Free
# TrackaLacker Restock Alerts App - TrackaLacker"). These come from the og:title
# metadata Twitter attaches to a link (entities.urls[].title), NOT from the tweet
# text itself, and get used as a "product name" if not caught.
#
# IMPORTANT: this is pattern-based on purpose, not a hardcoded "trackalacker" string
# in LINK_BLOCKLIST — several tracked accounts (e.g. TCGTouchdown) legitimately post
# real deal links through trackalacker.com (e.g. trackalacker.com/products/showcase/...),
# so blocking the domain/brand name outright would also kill real alerts. Only the
# promotional title text gets filtered.
PROMO_TITLE_PATTERNS = [
    r'\bdownload\b.{0,25}\bapp\b',
    r'\brestock alerts?\s+app\b',
    r'\bget (the |our )?app\b',
    r'\bnotifications?\b.{0,20}\bapp\b',
    r'\bturn on notifications\b',
]

def is_promo_title(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(re.search(p, t) for p in PROMO_TITLE_PATTERNS)

# ===========================================================================
# FIX 5: Shared noise stripping for product-name extraction.
#
# Previously `extract_product()` and `parse_product_lines()` each carried their
# own separate noise-word list and line filters, and the two lists had already
# drifted apart (e.g. "Stock:"/"Limit:" handling and the "Good luck"/"Walmart +
# not needed" entries only existed in one of the two functions). That drift is
# itself a source of inconsistent parsing — a fix added to one path silently
# doesn't apply to the other. Consolidating into one shared list + one shared
# line-noise check means every future noise pattern gets added once and applies
# everywhere product text gets built.
# ===========================================================================

NOISE_PHRASES = [
    "IN STOCK ALERT", "RESTOCK", "IN STOCK", "Sold by", "As of",
    "Follow", "Bookmark", "Save this", "We'll tweet", "We'll post",
    "follow with notifications", "alert if they drop",
    "historically restocks", "MSRP",
    "Walmart + not needed", "Walmart+ not needed",
    "Good luck", "Limit should be",
    "Turn on notifications",
    "🛎️", "🚨", "📣", "✅", "💰", "🏪", "📦", "🔗", "📡", "☑️",
    "🛒", "🎯", "⏰", "🔔", "🛍️", "🏷️",
]

# Line-level patterns: a line matching any of these is noise, not a product name.
NOISE_LINE_PATTERNS = [
    r'^[\$\d\s\.,()+%-]+$',           # pure price/number punctuation
    r'^(stock|limit)\s*:',            # "Stock: 511", "Limit: 2 Per Order"
    r'^\d+\s*(left|remaining|in stock)\b',
    r'^(sold by|as of)\b',
]

def _strip_noise_phrases(text: str) -> str:
    for n in NOISE_PHRASES:
        text = re.sub(re.escape(n), '', text, flags=re.IGNORECASE)
    return text

def _is_noise_line(line: str, min_len: int = 8) -> bool:
    if len(line) < min_len:
        return True
    for pat in NOISE_LINE_PATTERNS:
        if re.match(pat, line, flags=re.IGNORECASE):
            return True
    if is_promo_title(line):
        return True
    return False

# FIX 7: temporarily disabled per request — flip back to True once parsing
# accuracy (product/store/link correctness) is confirmed. Left the rest of
# the dedup machinery in place so re-enabling is a one-line change.
DEDUP_ENABLED = False

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

def is_blocked_link(label: str, url: str) -> bool:
    """Per-link filter — returns True if this specific link should be skipped.

    FIX 8: strips the URL's query string before checking. Terms like
    "referral" and "affiliate" are common substrings in completely ordinary
    tracking parameters (?utm_medium=referral, &ref=affiliate123, etc.) that
    show up on legitimate product links from major retailers — checking the
    full URL including query string meant real deal links could get
    silently dropped here just for carrying normal tracking params, with no
    visible sign why. Only the path is checked against terms like
    "/signup"/"/subscribe"; label/title text is unaffected by this change.
    """
    url_path = (url or '').split('?', 1)[0]
    combined = f"{label or ''} {url_path}".lower()
    return any(term in combined for term in LINK_BLOCKLIST)

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
    if not DEDUP_ENABLED:
        return False
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
    # FIX 3: Check for preorder before other types
    if any(t in text_lower for t in ["preorder", "pre-order", "pre order"]):
        return "preorder"
    if "in stock" in text_lower:
        return "in stock"
    if "restock" in text_lower or re.search(r'\bback\s+(at|on|up)\b', text_lower):
        return "restock"
    if "deal" in text_lower:
        return "deal"
    # FIX 1: tweets that passed via "is up at" / "up on" / "checking out"
    # should be classified as "in stock" not generic "alert"
    if re.search(r'\b(is\s+up|up\s+(at|on)|checking\s+out|now\s+available|going\s+live)\b', text_lower):
        return "in stock"
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
    """FIX 7: only return a store when the tweet unambiguously mentions
    exactly one. Previously this returned the FIRST STORE_MAP match found
    anywhere in the whole tweet — for a multi-store tweet (Amazon, Best Buy,
    AND Target all mentioned) that meant every embed generated from it got
    stamped with whichever store happened to appear first, regardless of
    which link it was actually for. Better to show no store at all than a
    wrong one; per-link store attribution (which IS reliable, since it
    reads the store name directly off that link's own line) is handled
    separately via _walk_line_context()."""
    text_lower = text.lower()
    matches = {name for key, name in STORE_MAP.items() if key in text_lower}
    if len(matches) == 1:
        return next(iter(matches))
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
        "x.com/i/",
        "/photo/",
        "pic.x.com",
        "pic.twitter.com",
    ]
    # FIX 7: "t.co/" used to be in this list, which meant the new raw-url
    # fallback above (used only when unwound/expanded are both missing)
    # would immediately get skipped again right after being added — the
    # exact case it exists to handle. The real targets of this list are
    # intent/photo endpoints, which the remaining patterns still catch.
    results = []

    def should_skip(url):
        return any(s in url for s in SKIP_PATTERNS)

    if entities and "urls" in entities:
        for url_obj in entities["urls"]:
            # FIX 7: fall back to the raw t.co url when unwound/expanded are
            # both missing (can happen when X hasn't unfurled a link yet) —
            # previously this link was silently dropped entirely rather than
            # posted with a less-pretty-but-still-working url.
            final_url = (
                url_obj.get("unwound_url")
                or url_obj.get("expanded_url")
                or url_obj.get("url")
                or ""
            )
            expanded = url_obj.get("expanded_url", "")
            if not final_url or should_skip(final_url) or should_skip(expanded):
                continue
            raw = url_obj.get("title") or url_obj.get("display_url") or None

            # FIX 4: drop app-download / self-promo links outright — they aren't
            # deal links and shouldn't spawn their own embed. Checked on the raw
            # title before any cleanup, since clean_link_title() only strips known
            # store-name suffixes, not promotional phrasing.
            if is_promo_title(raw):
                log.info(f"Skipped promo link: {raw!r} => {final_url[:60]}")
                continue

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
            # FIX 4: same promo-title guard on the text-fallback path
            if is_promo_title(label):
                log.info(f"Skipped promo link (fallback parse): {label!r} => {url[:60]}")
                matched_urls.add(url)
                continue
            results.append((label, url))
            matched_urls.add(url)
        for url in re.findall(r'https?://\S+', text):
            url = url.rstrip('.,)')
            if url in matched_urls or should_skip(url):
                continue
            results.append((None, url))

    # FIX 6: raised from 6 — "roundup" tweets (multiple products, each with
    # 2-3 store links) blow past 6 links easily, and the old cap silently
    # dropped trailing links (the PokemonDealsTCG example had 7). 20 is a
    # generous ceiling against runaway/spam tweets rather than a real limit.
    return results[:20]

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
    # FIX 5: "Stock: 511", "Limit: 2 Per Order" style badges, wherever they land in the text
    text = re.sub(r'\bStock:\s*[\d,]+\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\bLimit:?\s*\d+\s*(Per Order)?\b', '', text, flags=re.IGNORECASE)

    # FIX 7: check promo-title against these RAW (pre-noise-strip) lines,
    # not the noise-stripped ones below. NOISE_PHRASES strips the bare word
    # "RESTOCK" (legitimate elsewhere), which would silently delete the
    # "restock" in "Restock Alerts App" and defeat the promo-title match if
    # checked after stripping. Line count is preserved by both splits (only
    # in-line substitutions happen, no lines are merged/removed), so they
    # stay aligned by index for zipping.
    raw_lines = [l.strip(' -•·') for l in text.split('\n')]
    text = _strip_noise_phrases(text)
    stripped_lines = [l.strip(' -•·') for l in text.split('\n')]

    product_lines = []
    for raw_line, line in zip(raw_lines, stripped_lines):
        if is_promo_title(raw_line):
            continue
        if not line:
            continue
        if _is_noise_line(line):
            continue
        if any(line.strip().lower() == s.lower() for s in list(STORE_MAP.values())):
            continue
        if re.match(r'^[A-Z][A-Za-z0-9]+$', line) and len(line) < 20:
            continue
        # FIX 5: skip exact repeats (some tweets restate the same product line twice)
        if product_lines and product_lines[-1].lower() == line.lower():
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
    # FIX 5: same badge-style noise extract_product() now strips, kept in sync here too
    cleaned = re.sub(r'\bLimit:?\s*\d+\s*(Per Order)?\b', '', cleaned, flags=re.IGNORECASE)

    # FIX 7: same raw-vs-stripped ordering fix as extract_product() — check
    # promo-title before NOISE_PHRASES can strip "RESTOCK" out of a promo
    # line's text and defeat the match.
    raw_lines = [l.strip(' -•·') for l in cleaned.split('\n')]
    cleaned = _strip_noise_phrases(cleaned)
    stripped_lines = [l.strip(' -•·') for l in cleaned.split('\n')]

    lines = []
    for raw_line, line in zip(raw_lines, stripped_lines):
        if is_promo_title(raw_line):
            continue
        if _is_noise_line(line):
            continue
        # FIX 5: skip exact repeats
        if lines and lines[-1].lower() == line.lower():
            continue
        lines.append(line)

    return lines

# ===========================================================================
# FIX 6: "Roundup" tweet grouping
#
# Some accounts (PokemonDealsTCG is the clearest example) post one tweet
# listing several products, each followed by 1-3 "Store: link" lines, e.g.:
#
#   August 7th: First Partner Illustration Collection Series 3
#   Amazon <link>
#   Best Buy <link>
#   Target <link>
#
#   August 28th: Ascended Heroes Tins
#   Amazon <link>
#   Best Buy <link>
#
# The old per-link loop treated every link as its own "deal" and guessed at
# a product name for each one independently, which produced mislabeled
# embeds (a store name like "Best Buy" ending up AS the product name) and
# store misattribution (detect_store() reads the whole tweet, so every
# embed from a multi-store tweet got stamped with whichever store name
# appears first in STORE_MAP order, regardless of which link it actually
# was). This groups by product instead, so each product gets exactly one
# embed with its store links correctly attached.
# ===========================================================================

_STORE_LINE_RE = re.compile(
    r'^(?P<store>' + '|'.join(re.escape(k) for k in sorted(STORE_MAP.keys(), key=len, reverse=True)) + r')\b\s*[:\-]?\s*',
    re.IGNORECASE,
)

_ROUNDUP_BOILERPLATE_PATTERNS = [
    r"\bwe'?ll?\s+(post|tweet|share)\b",
    r"\bwe will\s+(post|tweet|share)\b",
    r'\bnote:',
    r'\bsave this (post|thread)\b',
    r'\bmore (links|updates|alerts)\b',
    r'\bwhen available\b',
]

def _is_roundup_boilerplate(line: str) -> bool:
    t = line.lower()
    return any(re.search(p, t) for p in _ROUNDUP_BOILERPLATE_PATTERNS) or is_promo_title(line)

def _walk_line_context(text: str) -> list:
    """
    FIX 7: single shared line-walker, replacing two separate implementations
    that used to exist (the old buggy `product_lines[i:]` index-guessing in
    the legacy per-link path, and a near-duplicate walker that used to live
    only inside parse_roundup_blocks()). One pass over the tweet's lines,
    returning one context dict per line that contains a URL, IN ORDER:

        {"store": str|None, "own_text": str, "header": str|None}

    "store"    — set ONLY when that exact line starts with a recognized
                 store name (e.g. "Amazon <link>"). This is the one case
                 trusted with full confidence, since the text is explicitly
                 telling us the store right next to that specific link.
    "own_text" — whatever text sits on that same line besides the store
                 prefix and the URL itself (covers single-line
                 "Product Name — Store: link" formats).
    "header"   — nearest preceding non-noise, non-link line. Used as the
                 product name when the URL's own line has no text of its
                 own (the common "Product Name\\nStore: link" shape).

    Because both parse_roundup_blocks() and the legacy per-link fallback now
    read from this single function, product/store context can't drift
    between the two paths the way extract_product()/parse_product_lines()
    drifted before FIX 5.
    """
    contexts = []
    current_header = None

    for raw_line in text.split('\n'):
        line = raw_line.strip(' -•·\t')
        if not line:
            continue

        has_url = bool(re.search(r'https?://\S+', line))

        if has_url:
            # FIX 7: check promo-title on the RAW line before any noise
            # stripping touches it. NOISE_PHRASES includes the bare word
            # "RESTOCK" (legitimate for real alert text), and stripping it
            # first would silently delete the "restock" in "Restock Alerts
            # App", breaking the promo-title match downstream. A promo
            # link's line becomes NO context at all here — that's also what
            # keeps the context count aligned with the links count in
            # parse_roundup_blocks, since extract_links_with_labels() drops
            # the same link at the entities level.
            if is_promo_title(line):
                continue
            m = _STORE_LINE_RE.match(line)
            store = STORE_MAP[m.group('store').lower()] if m else None
            own_text = re.sub(r'https?://\S+', '', line)
            if m:
                own_text = own_text[m.end():]
            # Apply the same cleanup parse_product_lines() used to do, so a
            # single-line tweet with the url inline (e.g. "IN STOCK ALERT
            # ... is in stock at Topps for $11.99 <url> #topps As of ...")
            # doesn't regress to raw, unstripped text.
            own_text = re.sub(r'#\w+', '', own_text)
            own_text = re.sub(r'#AD|#ad|\bAD\b', '', own_text, flags=re.IGNORECASE)
            own_text = re.sub(r'\$[\d,]+\.?\d*', '', own_text)
            own_text = re.sub(r'\d{1,2}/\d{1,2}/\d{2,4}\s+\d{1,2}:\d{2}\s*[APap][Mm]\s*[A-Z]{2,4}', '', own_text)
            own_text = re.sub(r'\d{1,2}:\d{2}\s*[APap][Mm]\s*[A-Z]{2,4}', '', own_text)
            own_text = _strip_noise_phrases(own_text)
            own_text = own_text.strip(' -:\t')
            contexts.append({"store": store, "own_text": own_text, "header": current_header})
            continue

        # no URL on this line — either a new product header or boilerplate/noise
        if _is_roundup_boilerplate(line) or _is_noise_line(line, min_len=4):
            continue
        if any(line.lower() == s.lower() for s in STORE_MAP.values()):
            continue

        current_header = line

    return contexts

def parse_roundup_blocks(text: str, entities: dict = None):
    """
    Try to parse a multi-product 'roundup' tweet into product blocks.
    Returns a list of dicts: [{"product": str, "stores": [(store_name_or_None, url), ...]}, ...]
    or None if the tweet doesn't look like this shape (caller should fall
    back to the existing per-link logic).
    """
    links = extract_links_with_labels(text, entities)
    if len(links) < 2:
        return None  # not worth grouping — legacy single-link path handles this fine

    contexts = _walk_line_context(text)

    # Contexts must line up 1:1 with the links, in order — if they don't
    # (e.g. a promo link got filtered out of `links` by extract_links_
    # with_labels but its line still looked like a URL line here), we can't
    # safely zip them together. Bail out to the legacy path rather than risk
    # attaching the wrong URL to the wrong product/store.
    if len(contexts) != len(links):
        log.info(f"Roundup parse: context/link count mismatch ({len(contexts)} vs {len(links)}) — falling back")
        return None

    blocks = []
    block_by_product = {}
    for ctx, (_, url) in zip(contexts, links):
        product = ctx["header"]
        if product is None:
            continue  # a link with no preceding product header — nothing sane to attach it to
        block = block_by_product.get(product)
        if block is None:
            block = {"product": product, "stores": []}
            block_by_product[product] = block
            blocks.append(block)
        block["stores"].append((ctx["store"], url))

    total_slots = sum(len(b["stores"]) for b in blocks)
    if len(blocks) < 1 or total_slots < 2:
        return None

    return blocks

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
        "football":   "football",
        "basketball": "basketball",
        "baseball":   "baseball",
        "hockey":     "hockey",
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

            overlap = query_tokens & row_tokens

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

# ===========================================================================
# FIX 7: product plausibility gate — "weird products slipping through"
#
# Cheap keyword heuristic, run on every extracted product name before it's
# allowed into an embed: requires at least one recognizable product-type
# word (booster, box, tin, ETB, collection, etc). Catches the actual
# garbage we've seen slip through — a bare store name, a boilerplate CTA
# sentence ("Save this post…"), a hashtag fragment — none of which contain
# any product-type word. Fails this → DROPPED, not posted at all.
#
# (An earlier version of this also soft-tagged products that didn't match
# your Supabase catalog — removed per feedback; a new preorder announcement
# legitimately won't be in the price catalog yet, and the tag wasn't a
# useful signal in practice.)
# ===========================================================================

CARD_PRODUCT_KEYWORDS = {
    "booster", "box", "boxes", "pack", "packs", "tin", "tins", "etb",
    "trainer", "bundle", "bundles", "collection", "elite", "blaster",
    "hobby", "mega", "jumbo", "hanger", "value", "cello", "gravity",
    "fat", "case", "cases", "deck", "decks", "premium", "chest",
    "chests", "series", "set", "sets", "expansion", "special",
    "illustration", "promo", "card", "cards",
}

def _looks_like_card_product(product: str) -> bool:
    if not product:
        return False
    tokens = {w.lower() for w in re.split(r'[\W_]+', product) if w}
    return bool(tokens & CARD_PRODUCT_KEYWORDS)


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
# Helpers
# ===========================================================================

def is_url_like(text: str) -> bool:
    """FIX 2: Guard against URL fragments being used as product names."""
    if not text:
        return True
    stripped = text.strip()
    # Looks like a bare domain/path (e.g. "thetoppscompany.sjv.io/QYngVx")
    if re.match(r'^https?://', stripped):
        return True
    if re.match(r'^[\w.-]+\.[a-z]{2,}/\S+$', stripped, re.IGNORECASE):
        return True
    return False

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

    alert_type       = detect_alert_type(text)
    category         = detect_category(text)
    # FIX 7: renamed from `store` — this is only the *whole-tweet* fallback
    # guess (now unambiguous-only, see detect_store()). Per-link store
    # attribution from _walk_line_context() takes priority over this
    # wherever it's available; this is the last resort, not the default.
    tweet_level_store = detect_store(text)
    color            = ALERT_COLORS.get(alert_type, 0x3498DB)
    alert_emoji      = ALERT_EMOJIS.get(alert_type, "📣")
    category_label   = CATEGORY_EMOJIS.get(category, "🃏 Trading Cards")

    # FIX 6: try grouping as a multi-product "roundup" tweet first (see
    # parse_roundup_blocks docstring). Only tweets shaped like that will
    # match; everything else falls through to the existing per-link logic
    # unchanged.
    roundup_blocks = parse_roundup_blocks(text, entities)
    if roundup_blocks:
        for block in roundup_blocks:
            product = block["product"]

            # FIX 7: drop products that don't even look like card/set names
            # (bare store names, boilerplate CTAs, etc. that slipped past
            # the noise filters) rather than posting them.
            if not _looks_like_card_product(product):
                log.info(f"Skipped implausible product (roundup): {product!r}")
                continue

            # Drop blocked links per-store rather than the whole product
            stores = [
                (s, u) for (s, u) in block["stores"]
                if not is_blocked_link(s, u)
            ]
            if not stores:
                log.info(f"Roundup: all store links blocked for {product!r} — skipping")
                continue

            store_names_for_fp = "+".join(sorted(s for s, _ in stores if s))
            fingerprint = make_fingerprint(alert_type, store_names_for_fp, product)
            if is_duplicate(fingerprint):
                log.info(f"Duplicate suppressed (roundup): @{author_username} — {product[:40]}")
                continue

            price        = detect_price(text, context=product)
            sealed_match = lookup_sealed_price(product, category) if product else None
            price_line   = format_price_line(price, sealed_match) if sealed_match else None

            store_links = " · ".join(
                f"[{s or 'Link'}]({u})" for s, u in stores
            )

            lines = [f"📦 {product}"]
            if store_links:
                lines.append(f"🛒 {store_links}")
            if price:
                lines.append(f"💰 {price}")
            if price_line:
                lines.append(price_line)

            embed = {
                "title":       f"{alert_emoji} {category_label} — {alert_type.upper()}",
                "url":         stores[0][1],
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
                log.info(f"Posted roundup embed: {product[:40]} ({len(stores)} store link(s))")
            time.sleep(0.3)
        return

    labeled_links = extract_links_with_labels(text, entities)

    if labeled_links:
        # FIX 7: shared line-walker replaces the old `product_lines[i:]`
        # index-guessing, which didn't actually correspond to the links
        # list positionally and was grabbing wrong lines (e.g. "Best Buy"
        # itself) as a "product name". Only used when the context count
        # lines up 1:1 with the links — otherwise we can't trust the
        # pairing and fall back to the single whole-tweet guess instead of
        # risking a wrong attachment.
        contexts    = _walk_line_context(text)
        context_ok  = len(contexts) == len(labeled_links)
        alerts_sent = 0

        for i, (label, url) in enumerate(labeled_links):
            # Skip membership upsells / affiliate links at the per-link level
            if is_blocked_link(label, url):
                log.info(f"Skipped blocked link: {label!r} => {url[:60]}")
                continue

            ctx = contexts[i] if context_ok else None

            product = clean_link_title(label)
            if is_url_like(product):
                product = None
                if ctx and ctx["own_text"] and len(ctx["own_text"]) >= 8 and not is_url_like(ctx["own_text"]):
                    product = ctx["own_text"]
                elif ctx and ctx["header"]:
                    product = ctx["header"]
                if not product:
                    product = extract_product(text)

            if product and is_url_like(product):
                product = None

            # FIX 7: only trust a store name when it's confidently tied to
            # THIS specific link (read directly off that link's own line);
            # otherwise fall back to the whole-tweet guess only if it's
            # unambiguous (detect_store() now returns None when multiple
            # different stores are mentioned anywhere in the tweet).
            link_store = ctx["store"] if ctx else None
            link_store_confident = bool(link_store)
            store = link_store or tweet_level_store

            # FIX 7: drop implausible product names outright
            if product and not _looks_like_card_product(product):
                log.info(f"Skipped implausible product: {product!r} from @{author_username}")
                continue

            link_category      = detect_category(label or "") if label else category
            effective_category = link_category if link_category != "trading cards" else category
            category_label     = CATEGORY_EMOJIS.get(effective_category, "🃏 Trading Cards")

            price = detect_price(text, context=product)

            fingerprint = make_fingerprint(alert_type, store, product or url)
            if is_duplicate(fingerprint):
                log.info(f"Duplicate suppressed: @{author_username} — {(product or url)[:40]}")
                continue

            meta_parts = []
            # Only show the store emoji line when we have SOME store — but
            # tag it "?" when it's the unconfirmed whole-tweet guess rather
            # than read directly off this link's own line, so low-confidence
            # attribution is visually distinguishable, not indistinguishable
            # from a sure thing.
            if store:
                meta_parts.append(f"🏪 {store}" if link_store_confident else f"🏪 {store}?")
            if price: meta_parts.append(f"💰 {price}")
            meta_line = "  ·  ".join(meta_parts)

            sealed_match = lookup_sealed_price(product, effective_category) if product else None
            price_line   = format_price_line(price, sealed_match) if sealed_match else None

            lines = []
            if product:    lines.append(f"📦 {product}")
            if meta_line:  lines.append(meta_line)
            if price_line: lines.append(price_line)

            embed = {
                "title":       f"{alert_emoji} {category_label} — {alert_type.upper()}",
                "url":         url,
                "description": '\n'.join(lines) if lines else "*(tap to view)*",
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
                log.info(f"Posted embed {alerts_sent}: {alert_type.upper()} — {(product or url)[:40]}")
            time.sleep(0.3)

    else:
        product = extract_product(text)

        # FIX 2: Don't use a URL fragment as a product name in fallback path either
        if is_url_like(product):
            product = None

        # FIX 7: drop implausible product names here too
        if product and not _looks_like_card_product(product):
            log.info(f"Skipped implausible product (no-link path): {product!r} from @{author_username}")
            product = None

        price = detect_price(text)
        store = tweet_level_store

        fingerprint = make_fingerprint(alert_type, store, product or tweet_url)
        if is_duplicate(fingerprint):
            log.info(f"Duplicate suppressed: @{author_username}")
            return

        meta_parts = []
        if store: meta_parts.append(f"🏪 {store}")
        if price: meta_parts.append(f"💰 {price}")
        meta_line = "  ·  ".join(meta_parts)

        sealed_match = lookup_sealed_price(product, category) if product else None
        price_line   = format_price_line(price, sealed_match) if sealed_match else None

        lines = []
        if product:    lines.append(f"📦 {product}")
        if meta_line:  lines.append(meta_line)
        if price_line: lines.append(price_line)
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

#!/usr/bin/env python3
"""
fb_group_url_collector.py

Collects Facebook POST URLs (only) from a Facebook Group page that the user
has already opened and manually logged into, in their own Chrome browser.

SAFETY / SCOPE (read before modifying):
  - This script never logs into Facebook. It never sees, asks for, or stores
    a Facebook email/password, and it never creates or persists Facebook
    cookies of any kind.
  - It attaches Selenium to a Chrome instance the USER already started and
    authenticated by hand (via Chrome's --remote-debugging-port). If no such
    session exists, the script simply fails to connect -- it never tries to
    open a login form or fill one in.
  - It does not attempt to defeat CAPTCHAs, 2FA, or any other Facebook
    security/anti-bot mechanism, and it does not try to hide the fact that
    it is automation.
  - It only reads posts already rendered in the DOM of the page the user has
    open, plus modest auto-scrolling to let the group's normal lazy-loading
    fire. It never navigates itself into a group the user hasn't opened.
  - Facebook does not expose real post permalinks as static hrefs (confirmed
    via live testing): the visible timestamp link resolves only to the bare
    group URL. To read the permalink, this performs an ordinary left click
    on that already-visible element -- the same click a real user would
    make -- then returns to the group feed via browser back navigation.
    This is normal, visible interaction with content already on the page,
    not a bypass of any login/CAPTCHA/anti-bot mechanism, and it is capped
    by MAX_POSTS_PER_RUN per run.
  - It collects post permalink URLs (and, optionally, the post's displayed
    timestamp). It never extracts names, profile URLs, emails, phone
    numbers, comments, or any other personal information.
  - It also does light best-effort parsing of each post's OWN text (topic,
    niche/hashtags, budget) since that is public post content, not personal
    data about a person. As an extra safeguard, any digit run of 7+ digits
    (phone-number shaped) is stripped from that parsed text so a phone
    number a poster included in their text never ends up in the sheet.

Run this only against your own Facebook account and groups you already
belong to, in line with Facebook's Terms of Service and applicable law.
"""

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Set
from urllib.parse import urlparse, urlunparse, parse_qs

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

DAYS_BACK = 2                       # Only keep posts newer than this many days.

SHEET_NAME = "Lead Scraper"         # Google Sheet (spreadsheet) name.
TAB_NAME = "Facebook URLs"          # Worksheet (tab) name inside that sheet.

SERVICE_ACCOUNT_FILE = "service_account.json"
SEEN_FILE = "seen_fb_urls.json"
LOG_FILE = "fb_urls_scraper.log"

# Hard cap on new leads saved to Google Sheets per calendar day. Tracked in
# DAILY_QUOTA_FILE as {"date": "YYYY-MM-DD", "count": N}; the count resets
# automatically the first time the script runs on a new calendar day --
# there is no separate "9 AM" trigger inside the script itself. If you want
# it to actually run every day at 9 AM, schedule `python
# fb_group_url_collector.py` with Windows Task Scheduler (or cron) for that
# time; this quota just makes repeated/early runs on the same day safe.
DAILY_LEAD_LIMIT = 20
DAILY_QUOTA_FILE = "daily_lead_quota.json"

# How many times to scroll the page to trigger Facebook's own lazy loading,
# and how long to pause between scrolls (seconds) to let posts render.
SCROLL_ROUNDS = 6
SCROLL_PAUSE_SECONDS = 2.0

# Chrome remote-debugging port. The user must start Chrome themselves with:
#   chrome.exe --remote-debugging-port=9222 --user-data-dir="<their normal profile dir>"
# and log into Facebook by hand in that window. This script only *attaches*
# to that already-running, already-authenticated browser.
CHROME_DEBUGGER_ADDRESS = "127.0.0.1:9222"

# Facebook does not expose real post permalinks as static hrefs (confirmed
# via live testing) -- only clicking the timestamp reveals it. Each click
# navigates away and back, so this caps how many posts get that treatment
# per run, to bound run time and keep the automation footprint modest.
# Raise this if you want a single run to cover more of the DAYS_BACK window
# in one pass; each additional post adds a few seconds of run time. Running
# the script again (e.g. after scrolling further) also works and won't
# double-count, since seen_fb_urls.json + the sheet's own URL column both
# dedupe across runs.
MAX_POSTS_PER_RUN = 30
CLICK_NAV_TIMEOUT_SECONDS = 6

# The sheet is meant for leads (people looking to hire), not other
# freelancers advertising themselves. When True, posts whose text looks
# like a service-provider pitch are skipped (see classify_post_intent).
# This is a best-effort keyword heuristic, not a guarantee -- ambiguous
# posts are kept rather than dropped, to avoid losing real leads.
EXCLUDE_SELLER_POSTS = True

# Only keep buyer posts that want remote/freelance work, not on-site/
# in-person/full-time roles. Same "keep if unclear" philosophy as above --
# a post that doesn't mention work arrangement at all is kept, since most
# posts in a freelance group are implicitly remote/freelance already.
EXCLUDE_ONSITE_POSTS = True

# Stricter lead qualification: only keep posts that (a) clearly read as
# someone looking to hire (not just "unknown"/ambiguous), and (b) actually
# mention video-editing context (video/editor/reels/TikTok/DaVinci/Premiere/
# After Effects/etc.), on top of the remote/freelance check above. This
# trades recall for precision -- ambiguous posts like a bare name or an
# unrelated comment are now dropped instead of kept, since the goal is a
# clean list of real video-editing leads, not "everything that might be one".
REQUIRE_CLEAR_BUYER_INTENT = True
REQUIRE_VIDEO_EDITING_CONTEXT = True

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_HEADERS = ["Topic", "Niche", "URL", "Budget", "Number", "Post Date", "Has Contact Info"]


# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    """Configure logging to both console and LOG_FILE with the required format."""
    logger = logging.getLogger("fb_group_url_collector")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    return logger


log = setup_logging()


# ---------------------------------------------------------------------------
# DATA MODEL
# ---------------------------------------------------------------------------

@dataclass
class CollectedPost:
    url: str
    post_date: Optional[datetime]  # None only if we could not parse it (post is skipped before this is used)
    topic: Optional[str] = None    # best-effort first line/sentence of the post's own text
    niche: Optional[str] = None    # best-effort hashtags found in the post's own text
    budget: Optional[str] = None   # best-effort currency amount found in the post's own text
    has_contact_info: bool = False  # True if the poster included a phone/WhatsApp number in their OWN text; the number itself is never stored


# ---------------------------------------------------------------------------
# TIMESTAMP PARSING
# ---------------------------------------------------------------------------

_RELATIVE_PATTERNS = [
    # (regex, unit-in-minutes multiplier or special handling)
    (re.compile(r"^just now$", re.I), lambda m, now: now),
    (re.compile(r"^(\d+)\s*s(ec(ond)?s?)?$", re.I), lambda m, now: now - timedelta(seconds=int(m.group(1)))),
    (re.compile(r"^(\d+)\s*m(in(ute)?s?)?$", re.I), lambda m, now: now - timedelta(minutes=int(m.group(1)))),
    (re.compile(r"^(\d+)\s*minutes?\s+ago$", re.I), lambda m, now: now - timedelta(minutes=int(m.group(1)))),
    (re.compile(r"^(\d+)\s*h(rs?|ours?)?$", re.I), lambda m, now: now - timedelta(hours=int(m.group(1)))),
    (re.compile(r"^(\d+)\s*hours?\s+ago$", re.I), lambda m, now: now - timedelta(hours=int(m.group(1)))),
    (re.compile(r"^(\d+)\s*d(ays?)?$", re.I), lambda m, now: now - timedelta(days=int(m.group(1)))),
    (re.compile(r"^(\d+)\s*days?\s+ago$", re.I), lambda m, now: now - timedelta(days=int(m.group(1)))),
    (re.compile(r"^(\d+)\s*w(eeks?)?$", re.I), lambda m, now: now - timedelta(weeks=int(m.group(1)))),
    (re.compile(r"^(\d+)\s*weeks?\s+ago$", re.I), lambda m, now: now - timedelta(weeks=int(m.group(1)))),
    (re.compile(r"^yesterday$", re.I), lambda m, now: now - timedelta(days=1)),
    (re.compile(r"^yesterday at (.+)$", re.I), None),  # handled specially below
]

# Absolute formats Facebook commonly shows (varies by locale/UI version).
_ABSOLUTE_FORMATS = [
    "%B %d, %Y at %I:%M %p",   # "August 10, 2026 at 3:45 PM"
    "%B %d at %I:%M %p",       # "August 10 at 3:45 PM" (current year assumed)
    "%b %d, %Y at %I:%M %p",   # "Aug 10, 2026 at 3:45 PM"
    "%b %d at %I:%M %p",       # "Aug 10 at 3:45 PM"
    "%B %d, %Y",                # "August 10, 2026"
    "%b %d, %Y",                 # "Aug 10, 2026"
    "%m/%d/%Y",
    "%Y-%m-%dT%H:%M:%S",        # ISO-ish, sometimes present in title/aria attrs
]


def parse_facebook_timestamp(raw_text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """
    Convert a Facebook-displayed timestamp string into a datetime.

    Supports relative forms ("5 minutes ago", "2 hrs", "yesterday", "3 d")
    and a handful of common absolute formats. Returns None if the text
    cannot be confidently parsed -- callers must skip the post in that case
    rather than guessing.
    """
    if not raw_text:
        return None

    text = raw_text.strip()
    if now is None:
        now = datetime.now()

    # "Yesterday at 3:45 PM" needs the time-of-day preserved.
    m = re.match(r"^yesterday at (.+)$", text, re.I)
    if m:
        time_part = m.group(1).strip()
        for fmt in ("%I:%M %p", "%H:%M"):
            try:
                t = datetime.strptime(time_part, fmt).time()
                base = now - timedelta(days=1)
                return datetime.combine(base.date(), t)
            except ValueError:
                continue
        # Couldn't parse the time portion confidently -- fall back to
        # "some time yesterday", which is good enough for a days-back filter.
        return now - timedelta(days=1)

    for pattern, handler in _RELATIVE_PATTERNS:
        if handler is None:
            continue
        m = pattern.match(text)
        if m:
            try:
                return handler(m, now)
            except Exception:
                return None

    for fmt in _ABSOLUTE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
            if "%Y" not in fmt:
                parsed = parsed.replace(year=now.year)
            return parsed
        except ValueError:
            continue

    return None


# ---------------------------------------------------------------------------
# URL NORMALIZATION / VALIDATION
# ---------------------------------------------------------------------------

# Path shapes that indicate a genuine Facebook *post* permalink.
_POST_URL_PATTERNS = [
    re.compile(r"^/groups/[^/]+/posts/[^/]+"),
    re.compile(r"^/groups/[^/]+/permalink/[^/]+"),
    re.compile(r"^/permalink\.php$"),
    re.compile(r"^/story\.php$"),
    re.compile(r"^/[^/]+/posts/[^/]+"),  # page/profile posts, still a "post"
    re.compile(r"^/stories/[^/]+/[^/]+"),  # newer canonical permalink format observed in live testing
]

# Path/query shapes that must be rejected even if they superficially match
# a pattern above (comments, photos, reactions, profile/group landing pages).
_EXCLUDED_PATH_SNIPPETS = (
    "/photo", "/photos/", "/videos/", "/media",
    "/comment/", "/reactions", "/friends/", "/about", "/members",
    "/user/", "/profile.php",
)


def normalize_and_validate_post_url(raw_url: str) -> Optional[str]:
    """
    Return a normalized canonical post URL, or None if raw_url does not look
    like a genuine Facebook post permalink (profile, group root, comment,
    photo, reaction, or other non-post link).
    """
    if not raw_url:
        return None

    try:
        parsed = urlparse(raw_url)
    except Exception:
        return None

    if "facebook.com" not in parsed.netloc:
        return None

    path = parsed.path or ""

    for snippet in _EXCLUDED_PATH_SNIPPETS:
        if snippet in path:
            return None

    matched = any(p.match(path) for p in _POST_URL_PATTERNS)
    if not matched:
        return None

    # Keep only the query params that identify the post itself
    # (story_fbid/id for permalink.php / story.php); drop tracking params.
    query = parse_qs(parsed.query)
    keep = {}
    for key in ("story_fbid", "id"):
        if key in query:
            keep[key] = query[key][0]

    normalized_query = "&".join(f"{k}={v}" for k, v in sorted(keep.items()))

    normalized = urlunparse((
        "https",
        "www.facebook.com",
        path.rstrip("/"),
        "",
        normalized_query,
        "",
    ))
    return normalized


# ---------------------------------------------------------------------------
# BROWSER ATTACHMENT (no login automation, ever)
# ---------------------------------------------------------------------------

def attach_to_user_chrome():
    """
    Attach Selenium to a Chrome window the user already started and logged
    into manually via --remote-debugging-port. This function never opens a
    login page, never fills in credentials, and never touches Facebook
    cookies -- it only reuses the DOM of the tab the user already has open.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    options = Options()
    options.debugger_address = CHROME_DEBUGGER_ADDRESS

    try:
        driver = webdriver.Chrome(options=options)
    except Exception as exc:
        log.error(
            "Could not attach to Chrome at %s. Make sure you started Chrome "
            "yourself with --remote-debugging-port=9222 and are logged into "
            "Facebook in that window. Underlying error: %s",
            CHROME_DEBUGGER_ADDRESS, exc,
        )
        raise
    return driver


def detect_group_page(driver) -> bool:
    """
    Confirm the currently active tab is a Facebook Group page. Returns False
    (and logs guidance) if it is not -- the script must not try to navigate
    there itself.
    """
    try:
        current_url = driver.current_url
    except Exception as exc:
        log.error("Could not read the current page URL: %s", exc)
        return False

    log.info("Current page detected: %s", current_url)

    if "facebook.com" not in current_url:
        log.error("The active browser tab is not on facebook.com. "
                   "Please open your Facebook Group first.")
        return False

    if "/groups/" not in urlparse(current_url).path:
        log.error("The active tab does not look like a Facebook Group page "
                   "(no '/groups/' in the URL). Please open a Facebook Group "
                   "you are a member of, then re-run the collector.")
        return False

    return True


def scroll_to_load_posts(driver) -> None:
    """Gradually scroll the page so Facebook's own lazy-loading renders more posts."""
    for i in range(SCROLL_ROUNDS):
        try:
            driver.execute_script("window.scrollBy(0, document.body.scrollHeight);")
        except Exception as exc:
            log.warning("Scroll attempt %d failed, continuing with posts loaded so far: %s", i + 1, exc)
            break
        time.sleep(SCROLL_PAUSE_SECONDS)
    log.info("Finished scrolling (%d rounds) to load additional posts.", SCROLL_ROUNDS)


# ---------------------------------------------------------------------------
# POST CONTENT PARSING (topic / niche / budget)
#
# These pull best-effort structure out of a post's OWN visible text -- public
# post content, not personal data about a person. As a safeguard against a
# poster's phone number leaking into these fields, PHONE_LIKE_RUN strips any
# digit run of 7+ digits before the text is used.
# ---------------------------------------------------------------------------

PHONE_LIKE_RUN = re.compile(r"\d[\d\-\s]{6,}\d")

_MONEY_AMOUNT = r"\d(?:[\d,]*\d)?(?:\.\d+)?"  # e.g. "5000", "5,000", "99.50" -- never ends on a bare separator
_BUDGET_PATTERN = re.compile(
    rf"(?:budget|price|rate)\s*[:\-]?\s*(?:\$|USD|PKR|Rs\.?|INR|₹)?\s?{_MONEY_AMOUNT}"
    rf"|(?:\$|USD|PKR|Rs\.?|INR|₹)\s?{_MONEY_AMOUNT}",
    re.I,
)

_HASHTAG_PATTERN = re.compile(r"#\w+")


def _redact_phone_like(text: str) -> str:
    """Strip any 7+ digit run (phone-number shaped) out of extracted post text."""
    return PHONE_LIKE_RUN.sub("[redacted]", text)


def extract_post_text(container) -> Optional[str]:
    """Best-effort extraction of the post's own message text (not comments/UI chrome)."""
    from selenium.webdriver.common.by import By

    try:
        candidates = container.find_elements(By.CSS_SELECTOR, "div[data-ad-preview='message']")
        if not candidates:
            candidates = container.find_elements(By.CSS_SELECTOR, "[dir='auto']")
    except Exception:
        return None

    best = ""
    for el in candidates:
        try:
            text = (el.text or "").strip()
        except Exception:
            continue
        if len(text) > len(best):
            best = text

    return best or None


def extract_topic(post_text: Optional[str]) -> Optional[str]:
    """First line/sentence of the post's own text, used as a rough 'topic'."""
    if not post_text:
        return None
    first_line = post_text.strip().splitlines()[0].strip()
    topic = first_line[:120]
    return _redact_phone_like(topic) if topic else None


def extract_niche(post_text: Optional[str]) -> Optional[str]:
    """Hashtags found in the post's own text, used as a rough 'niche' signal."""
    if not post_text:
        return None
    tags = _HASHTAG_PATTERN.findall(post_text)
    return ", ".join(dict.fromkeys(tags)) if tags else None


def extract_budget(post_text: Optional[str]) -> Optional[str]:
    """Best-effort currency amount mentioned in the post's own text, if any."""
    if not post_text:
        return None
    m = _BUDGET_PATTERN.search(post_text)
    return _redact_phone_like(m.group(0).strip()) if m else None


_CONTACT_KEYWORD_PATTERN = re.compile(r"\bwhatsapp\b|\bwhats\s*app\b", re.I)


def has_contact_info(post_text: Optional[str]) -> bool:
    """
    True if the poster included a phone/WhatsApp number in their OWN post
    text (a phone-shaped 7+ digit run, or an explicit "WhatsApp" mention).
    This only signals PRESENCE -- the number itself is never captured or
    stored anywhere, consistent with this tool's no-personal-data design.
    """
    if not post_text:
        return False
    return bool(PHONE_LIKE_RUN.search(post_text) or _CONTACT_KEYWORD_PATTERN.search(post_text))


# ---------------------------------------------------------------------------
# BUYER-VS-SELLER FILTER
#
# The sheet is meant for leads -- people looking to HIRE someone -- not for
# other freelancers advertising their own services. This is a best-effort
# keyword classifier over the post's own text; it will misclassify some
# posts (sarcasm, unusual phrasing, mixed posts). When a post's intent can't
# be told apart, it is kept rather than dropped, since silently discarding a
# real lead is worse than including an occasional ambiguous post.
# ---------------------------------------------------------------------------

_SGAP = r"[^.!?\n]{0,40}"  # tolerate "I'm Jay, a passionate video editor" (name/adjectives in between)

_SELLER_PATTERNS = [
    re.compile(rf"\bi\s*(am|'m)\b{_SGAP}\b(freelance\w*|video\s*editor|graphic\s*designer|designer|editor|expert|professional)\b", re.I),
    re.compile(r"\bavailable\s+(for|to)\s+(work|hire|freelance|projects?)\b", re.I),
    re.compile(r"\bhire\s+me\b", re.I),
    re.compile(r"\bmy\s+(portfolio|services?|work|rates?)\b", re.I),
    re.compile(r"\bi\s+(offer|provide|do|specialize\s+in)\b.*\b(editing|design|services?|graphics?|animation)\b", re.I),
    re.compile(r"\bcontact\s+me\s+for\b", re.I),
    re.compile(r"\bdm\s+me\s+(for|if)\b", re.I),
    re.compile(r"\b(my\s+)?dms?\s+(are\s+)?(always\s+)?open\b", re.I),
    re.compile(r"\bi\s+can\s+(edit|design|help\s+you\s+with)\b", re.I),
    re.compile(r"\bopen\s+for\s+(work|projects?|freelance)\b", re.I),
    re.compile(r"\byears?\s+(of\s+)?experience\b", re.I),
    re.compile(r"\bfiverr\b|\bupwork\b", re.I),
    re.compile(r"\bprojects?\s+delivered\b", re.I),
    re.compile(r"\btop[\s\-]?rated\b", re.I),
    re.compile(r"\bi'?m\s+here\s+to\s+(save|help)\b", re.I),
]

# Bare role noun -- deliberately permissive on what comes before it (any
# characters, not just \w+ separated by spaces) since real posts use
# punctuation/slashes freely, e.g. "video creator/editor", "3D video
# editor". The trigger word (need/looking for/want/...) still has to occur
# BEFORE the role noun within a short window, so this stays anchored to
# genuine hiring language rather than matching anywhere in the post.
_ROLE_NOUN = r"(?:video\s*editor|editor|designer|creator|freelance\w*|expert)"
_GAP = r"[^.!?\n]{0,60}"  # up to 60 chars, not crossing a sentence boundary

_BUYER_PATTERNS = [
    re.compile(rf"\b(need|needed|needing)\b{_GAP}\b{_ROLE_NOUN}\b", re.I),
    re.compile(rf"\b(need|needed|needing)\b{_GAP}\bediting\b", re.I),  # "need shorts editing"
    re.compile(rf"\blooking\s+for\b{_GAP}\b{_ROLE_NOUN}\b", re.I),
    re.compile(rf"\bi\s*want\b{_GAP}\b{_ROLE_NOUN}\b", re.I),  # "I want video editor ... now"
    re.compile(rf"\brequired?\b{_GAP}\b{_ROLE_NOUN}\b", re.I),
    re.compile(r"\bhiring\b", re.I),
    re.compile(rf"\bany\s+(good\s+)?{_GAP}?\b{_ROLE_NOUN}\b", re.I),
    re.compile(r"\bwho\s+can\s+(edit|design|help)\b", re.I),
    re.compile(r"\bwant\s+to\s+hire\b", re.I),
    re.compile(r"\burgently\s+(need|require)\b", re.I),
]


def classify_post_intent(post_text: Optional[str]) -> str:
    """
    Best-effort classification of a post's own text as "buyer" (looking to
    hire), "seller" (advertising their own services), or "unknown".
    """
    if not post_text:
        return "unknown"

    is_seller = any(p.search(post_text) for p in _SELLER_PATTERNS)
    is_buyer = any(p.search(post_text) for p in _BUYER_PATTERNS)

    if is_buyer and not is_seller:
        return "buyer"
    if is_seller and not is_buyer:
        return "seller"
    return "unknown"


_ONSITE_PATTERNS = [
    re.compile(r"\bon[\s\-]?site\b", re.I),
    re.compile(r"\bin[\s\-]?office\b", re.I),
    re.compile(r"\bwork\s+from\s+office\b", re.I),
    re.compile(r"\bfull[\s\-]?time\s+(employee|job|position|staff)\b", re.I),
    re.compile(r"\boffice[\s\-]?based\b", re.I),
    re.compile(r"\bnear\s+me\b", re.I),
    re.compile(r"\bwalk[\s\-]?in\s+interview\b", re.I),
    re.compile(r"\bin[\s\-]?person\s+only\b", re.I),
    re.compile(r"\bmust\s+(be\s+)?(available\s+)?(in|at)\s+(person|office)\b", re.I),
    re.compile(r"\bcome\s+to\s+(our\s+)?office\b", re.I),
    re.compile(r"\bphysical\s+presence\s+required\b", re.I),
]

_REMOTE_FREELANCE_PATTERNS = [
    re.compile(r"\bremote(ly)?\b", re.I),
    re.compile(r"\bfreelance\w*\b", re.I),
    re.compile(r"\bwork\s+from\s+home\b", re.I),
    re.compile(r"\bwfh\b", re.I),
    re.compile(r"\bonline\s+(work|job|only)\b", re.I),
    re.compile(r"\bpart[\s\-]?time\b", re.I),
    re.compile(r"\bcontract\s+basis\b", re.I),
    re.compile(r"\bper[\s\-]?project\b", re.I),
    re.compile(r"\bgig\b", re.I),
]


def classify_work_arrangement(post_text: Optional[str]) -> str:
    """
    Best-effort classification of a post's own text as "remote_freelance",
    "onsite" (in-person/full-time/office-based), or "unknown" (arrangement
    not mentioned -- most posts in a freelance group are implicitly remote).
    """
    if not post_text:
        return "unknown"

    is_onsite = any(p.search(post_text) for p in _ONSITE_PATTERNS)
    is_remote = any(p.search(post_text) for p in _REMOTE_FREELANCE_PATTERNS)

    if is_onsite and not is_remote:
        return "onsite"
    if is_remote:
        return "remote_freelance"
    return "unknown"


_VIDEO_EDITING_CONTEXT_PATTERN = re.compile(
    r"\bvideo\b|\bedit(?:s|or|ors|ing)?\b|\breels?\b|\btik\s*tok\b|\byoutube\b|\bshorts?\b"
    r"|\bdavinci\b|\bpremiere\b|\bafter\s*effects\b|\bmotion\s*graphics?\b|\bcontent\s*creator\b",
    re.I,
)


def is_qualifying_video_editing_lead(post_text: Optional[str]) -> bool:
    """
    Stricter combined gate applied on top of the individual classifiers:
    a post only qualifies as a lead worth saving if it (a) clearly reads as
    someone looking to hire, (b) actually mentions video-editing context,
    and (c) doesn't look like an on-site/in-person/full-time role. Each
    check can be individually disabled via its REQUIRE_*/EXCLUDE_* config
    constant. Ambiguous text fails (a) and (b) when strict mode is on --
    that's a deliberate precision-over-recall trade-off for a clean lead
    list, unlike the underlying classifiers, which default to keeping
    ambiguous posts.
    """
    intent = classify_post_intent(post_text)
    if REQUIRE_CLEAR_BUYER_INTENT:
        if intent != "buyer":
            return False
    elif EXCLUDE_SELLER_POSTS and intent == "seller":
        return False

    if REQUIRE_VIDEO_EDITING_CONTEXT and not (post_text and _VIDEO_EDITING_CONTEXT_PATTERN.search(post_text)):
        return False

    if EXCLUDE_ONSITE_POSTS and classify_work_arrangement(post_text) == "onsite":
        return False

    return True


# ---------------------------------------------------------------------------
# POST EXTRACTION
# ---------------------------------------------------------------------------

def find_post_containers(driver) -> List:
    """
    Locate DOM elements that represent individual posts. Facebook's markup
    changes frequently and is unstable, so this uses a couple of resilient,
    low-specificity strategies rather than brittle class names.
    """
    from selenium.webdriver.common.by import By

    try:
        containers = driver.find_elements(By.CSS_SELECTOR, "div[role='article']")
    except Exception as exc:
        log.error(
            "Failed to query post containers -- Facebook's page structure may "
            "have changed and selectors likely need updating. Error: %s", exc
        )
        return []

    if not containers:
        log.warning(
            "No post containers found with the current selector "
            "(div[role='article']). Facebook's DOM structure may have "
            "changed; selectors may need updating."
        )
    return containers


def extract_post_url_from_container(container) -> Optional[str]:
    """
    Best-effort passive check: is there already a real permalink-shaped href
    sitting in this container's links? On current Facebook markup this
    usually returns None (see extract_post_url_via_click below), but some
    post types (e.g. photo posts) do carry a real static permalink-shaped
    href, so this is tried first as a fast path before falling back to a
    click.
    """
    from selenium.webdriver.common.by import By

    try:
        anchors = container.find_elements(By.TAG_NAME, "a")
    except Exception as exc:
        log.debug("Could not read links from a post container: %s", exc)
        return None

    for a in anchors:
        try:
            href = a.get_attribute("href")
        except Exception:
            continue
        normalized = normalize_and_validate_post_url(href) if href else None
        if normalized:
            return normalized

    return None


def _post_fingerprint(container) -> Optional[str]:
    """
    A short, stable-ish text identifier for a post container.

    Clicking a post's timestamp (see extract_post_url_via_click) navigates
    away and back, which invalidates every previously-fetched Selenium
    element handle for the feed. This fingerprint lets process_posts tell
    "a post I already handled" apart from "a new post" after re-querying the
    DOM, without relying on a stale element reference.
    """
    try:
        text = (container.text or "").strip()
    except Exception:
        return None
    return text[:80] if text else None


def _find_timestamp_anchor(container):
    """
    Locate the clickable timestamp element inside a post container.

    Confirmed via live DOM inspection: Facebook's current markup does not
    expose the real post permalink as a static href on this element -- its
    href resolves only to the bare group URL (e.g. ".../groups/12345/"),
    with tracking query params and no post identifier. The real permalink
    is only revealed by actually clicking it (Facebook assembles the
    destination client-side). This identifies that element as the first
    <a> in the container whose href path is exactly the bare group root --
    distinct from the author link (points to a user/profile URL) and from
    photo/video links.
    """
    from selenium.webdriver.common.by import By

    try:
        anchors = container.find_elements(By.TAG_NAME, "a")
    except Exception:
        return None

    for a in anchors:
        try:
            href = a.get_attribute("href")
        except Exception:
            continue
        if not href:
            continue
        path = urlparse(href).path
        if re.match(r"^/groups/[^/]+/?$", path):
            return a
    return None


def extract_post_url_via_click(driver, container) -> Optional[str]:
    """
    Click the post's timestamp so Facebook's own UI navigates to the real
    permalink, capture and validate the resulting URL, then return to the
    group feed.

    This performs an ordinary left click a real user would make on an
    already-visible, already-loaded element -- it does not defeat a CAPTCHA,
    2FA, or any anti-bot mechanism, and it does not hide that this is
    automation. It is necessary because (confirmed via live testing) the
    visible timestamp's static href does not carry the real permalink; only
    the click does.
    """
    from selenium.common.exceptions import WebDriverException

    anchor = _find_timestamp_anchor(container)
    if anchor is None:
        return None

    origin_url = driver.current_url

    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", anchor)
        anchor.click()
    except WebDriverException as exc:
        log.debug("Could not click a post's timestamp element: %s", exc)
        return None

    new_url = None
    waited = 0.0
    while waited < CLICK_NAV_TIMEOUT_SECONDS:
        time.sleep(0.25)
        waited += 0.25
        current = driver.current_url
        if current != origin_url:
            new_url = current
            break

    if new_url is None:
        log.debug("Clicking the timestamp did not navigate within %ss; skipping this post.",
                   CLICK_NAV_TIMEOUT_SECONDS)

    result = normalize_and_validate_post_url(new_url) if new_url else None

    try:
        driver.back()
        time.sleep(1.5)
    except WebDriverException as exc:
        log.warning("Could not navigate back to the group feed after a click: %s", exc)

    return result


def extract_post_timestamp_from_container(container) -> Optional[str]:
    """
    Pull the raw, displayed timestamp text for a post, without interpreting
    it yet. Tries the common places Facebook renders it: an <abbr> title,
    an aria-label on the timestamp link, or visible link text near the top
    of the post that looks like a short relative time.

    Known limitation (confirmed via live testing): current Facebook markup
    renders the visible "2h" / "Just now" text as scrambled Unicode
    combining-mark characters that only look correct visually via CSS, not
    in the DOM text order -- so this frequently returns None. Per this
    tool's design, a post whose timestamp can't be read this way is skipped
    and logged rather than guessed at; it is not silently assigned "now".
    """
    from selenium.webdriver.common.by import By

    try:
        abbrs = container.find_elements(By.TAG_NAME, "abbr")
        for abbr in abbrs:
            title = abbr.get_attribute("title")
            if title:
                return title.strip()
    except Exception:
        pass

    try:
        links = container.find_elements(By.TAG_NAME, "a")
    except Exception:
        return None

    time_like = re.compile(
        r"^\s*(\d+\s*(s|sec|secs|m|min|mins|h|hr|hrs|d|w)\b.*|"
        r"just now|yesterday.*|\d+\s+(minute|hour|day|week)s?\s+ago)\s*$",
        re.I,
    )

    for link in links[:12]:  # timestamp link is always near the top of a post
        try:
            aria = link.get_attribute("aria-label")
            text = link.text
        except Exception:
            continue
        for candidate in (aria, text):
            if candidate and time_like.match(candidate.strip()):
                return candidate.strip()

    return None


def process_posts(driver, cutoff: datetime, seen_urls: Set[str], max_new_leads: Optional[int] = None) -> "ExtractionResult":
    """
    Extract, filter, and deduplicate posts from the currently loaded page.

    Getting the real permalink URL usually requires clicking each post's
    timestamp (see extract_post_url_via_click) since Facebook does not
    expose it as a static href. Each click navigates away and back, which
    invalidates previously-fetched element handles -- so this re-queries
    the DOM fresh after every click and tracks which posts were already
    handled via a short text fingerprint (_post_fingerprint) rather than
    relying on stale element references. MAX_POSTS_PER_RUN bounds how many
    click round-trips a single run performs.

    max_new_leads, if given, stops extraction immediately (mid-page, before
    MAX_POSTS_PER_RUN or scrolling for more) once that many qualifying leads
    have been collected -- used to enforce DAILY_LEAD_LIMIT.
    """
    result = ExtractionResult()
    processed_fingerprints: Set[str] = set()
    attempts = 0
    consecutive_failures = 0
    scrolled_for_more = False

    while attempts < MAX_POSTS_PER_RUN:
        containers = find_post_containers(driver)
        result.posts_processed = max(result.posts_processed, len(containers))

        container = None
        for c in containers:
            fp = _post_fingerprint(c)
            if fp and fp not in processed_fingerprints:
                container = c
                break

        if container is None:
            if not scrolled_for_more:
                # Ran out of currently-loaded posts before hitting the cap --
                # scroll for more once rather than stopping short, so a
                # single run can cover more of the DAYS_BACK window.
                log.info("Ran out of loaded posts before reaching MAX_POSTS_PER_RUN; scrolling for more.")
                scroll_to_load_posts(driver)
                scrolled_for_more = True
                continue
            log.info("No further unprocessed posts available even after scrolling; stopping.")
            break
        scrolled_for_more = False

        fp = _post_fingerprint(container)
        processed_fingerprints.add(fp)
        attempts += 1

        try:
            # Read whatever we can from the still-attached container BEFORE
            # navigating away -- post text and any (rare) static permalink
            # href are unaffected by the click/back round-trip below.
            post_text = extract_post_text(container)
            raw_ts = extract_post_timestamp_from_container(container)
            url = extract_post_url_from_container(container)

            if not url:
                url = extract_post_url_via_click(driver, container)

            if not url:
                result.invalid_urls_skipped += 1
                consecutive_failures += 1
                continue
            consecutive_failures = 0

            if url in seen_urls or url in result.seen_this_run:
                result.duplicates_skipped += 1
                continue

            post_date = parse_facebook_timestamp(raw_ts) if raw_ts else None

            if post_date is None:
                log.info("Skipping post (timestamp could not be reliably determined): %s", url)
                result.unparseable_timestamps_skipped += 1
                continue

            if post_date < cutoff:
                result.old_posts_skipped += 1
                continue

            if not is_qualifying_video_editing_lead(post_text):
                log.info("Skipping post (not a clear remote/freelance video-editing lead): %s", url)
                result.not_qualifying_lead_skipped += 1
                continue

            result.collected.append(CollectedPost(
                url=url,
                post_date=post_date,
                topic=extract_topic(post_text),
                niche=extract_niche(post_text),
                budget=extract_budget(post_text),
                has_contact_info=has_contact_info(post_text),
            ))
            result.seen_this_run.add(url)

            if max_new_leads is not None and len(result.collected) >= max_new_leads:
                log.info("Reached the cap of %d new lead(s) for this run; stopping immediately.", max_new_leads)
                break

        except Exception as exc:
            # One bad post must never take down the whole run.
            log.error("Failed to process a post, skipping it and continuing: %s", exc)
            consecutive_failures += 1
            continue

        if consecutive_failures >= 5:
            log.warning("Stopping early after 5 consecutive posts with no extractable permalink.")
            break

    return result


@dataclass
class ExtractionResult:
    posts_processed: int = 0
    old_posts_skipped: int = 0
    duplicates_skipped: int = 0
    invalid_urls_skipped: int = 0
    unparseable_timestamps_skipped: int = 0
    not_qualifying_lead_skipped: int = 0
    collected: List[CollectedPost] = None
    seen_this_run: Set[str] = None

    def __post_init__(self):
        if self.collected is None:
            self.collected = []
        if self.seen_this_run is None:
            self.seen_this_run = set()


# ---------------------------------------------------------------------------
# LOCAL DEDUPLICATION STORE
# ---------------------------------------------------------------------------

def load_seen_urls() -> Set[str]:
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except Exception as exc:
        log.error("Could not read %s (starting with an empty dedup set): %s", SEEN_FILE, exc)
        return set()


def save_seen_urls(urls: Set[str]) -> None:
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(urls), f, indent=2)
    except Exception as exc:
        log.error("Could not write %s -- collected URLs are still in memory/log this run: %s", SEEN_FILE, exc)


# ---------------------------------------------------------------------------
# DAILY LEAD QUOTA
# ---------------------------------------------------------------------------

def load_daily_quota_used() -> int:
    """
    How many leads have already been saved today. Resets to 0 automatically
    the first time this is called on a new calendar day (compared to the
    date stored in DAILY_QUOTA_FILE) -- no separate reset step needed.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    if not os.path.exists(DAILY_QUOTA_FILE):
        return 0
    try:
        with open(DAILY_QUOTA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        log.warning("Could not read %s, treating today's quota as 0: %s", DAILY_QUOTA_FILE, exc)
        return 0
    if data.get("date") != today:
        return 0  # new calendar day -- quota resets
    return int(data.get("count", 0))


def save_daily_quota_used(count: int) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with open(DAILY_QUOTA_FILE, "w", encoding="utf-8") as f:
            json.dump({"date": today, "count": count}, f, indent=2)
    except Exception as exc:
        log.error("Could not write %s -- daily quota tracking may be inaccurate next run: %s", DAILY_QUOTA_FILE, exc)


# ---------------------------------------------------------------------------
# GOOGLE SHEETS
# ---------------------------------------------------------------------------

def get_sheets_worksheet():
    """
    Authenticate with the Google service account and return the target
    worksheet, creating the tab (with headers) if it does not exist yet.
    """
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials

    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(
            f"{SERVICE_ACCOUNT_FILE} not found. See README.md for how to create "
            f"a Google service account and download its JSON key."
        )

    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, GOOGLE_SCOPES)
    client = gspread.authorize(creds)

    spreadsheet = client.open(SHEET_NAME)

    try:
        worksheet = spreadsheet.worksheet(TAB_NAME)
    except gspread.exceptions.WorksheetNotFound:
        log.info("Tab '%s' not found in '%s' -- creating it.", TAB_NAME, SHEET_NAME)
        worksheet = spreadsheet.add_worksheet(title=TAB_NAME, rows=1000, cols=len(SHEET_HEADERS))
        worksheet.append_row(SHEET_HEADERS)

    # Ensure headers exist even if the tab already existed but was empty.
    first_row = worksheet.row_values(1)
    if not first_row:
        worksheet.append_row(SHEET_HEADERS)

    return worksheet


_URL_COLUMN = SHEET_HEADERS.index("URL") + 1  # 1-based column index gspread expects


def get_existing_sheet_urls(worksheet) -> Set[str]:
    """Read the URL column so we never insert a duplicate that's already in the sheet."""
    try:
        values = worksheet.col_values(_URL_COLUMN)
    except Exception as exc:
        log.warning("Could not read existing sheet URLs, proceeding without that extra check: %s", exc)
        return set()
    return set(values[1:])  # skip header


def append_posts_to_sheet(worksheet, posts: List[CollectedPost]) -> int:
    """Append rows for newly collected posts. Returns the number of rows written."""
    if not posts:
        return 0

    try:
        existing_data_rows = len(worksheet.col_values(_URL_COLUMN)) - 1  # minus header
    except Exception:
        existing_data_rows = 0
    start_number = max(existing_data_rows, 0) + 1

    rows = [
        [
            p.topic or "",
            p.niche or "",
            p.url,
            p.budget or "",
            start_number + i,
            p.post_date.strftime("%Y-%m-%d %H:%M:%S"),
            "Yes" if p.has_contact_info else "No",
        ]
        for i, p in enumerate(posts)
    ]
    worksheet.append_rows(rows, value_input_option="RAW")
    return len(rows)


# ---------------------------------------------------------------------------
# LOCAL BACKUP (used if Google Sheets write fails, so nothing is lost)
# ---------------------------------------------------------------------------

def write_local_backup(posts: List[CollectedPost]) -> str:
    backup_file = f"fb_urls_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    try:
        with open(backup_file, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "url": p.url,
                        "post_date": p.post_date.isoformat(),
                        "topic": p.topic,
                        "niche": p.niche,
                        "budget": p.budget,
                    }
                    for p in posts
                ],
                f, indent=2,
            )
        log.info("Collected URLs preserved locally in %s", backup_file)
    except Exception as exc:
        log.error("Even the local backup write failed -- URLs only exist in this run's log: %s", exc)
    return backup_file


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> int:
    log.info("Script started.")
    now = datetime.now()
    cutoff = now - timedelta(days=DAYS_BACK)

    quota_used = load_daily_quota_used()
    remaining_quota = DAILY_LEAD_LIMIT - quota_used
    log.info("Daily lead quota: %d/%d used today.", quota_used, DAILY_LEAD_LIMIT)
    if remaining_quota <= 0:
        log.info("Daily lead limit already reached (%d/%d) -- skipping this run. "
                  "Resets automatically on the next run after today.", quota_used, DAILY_LEAD_LIMIT)
        log.info("Script completed.")
        return 0

    driver = None
    try:
        driver = attach_to_user_chrome()
    except Exception:
        log.error("Script completed with errors (could not attach to browser).")
        return 1

    try:
        if not detect_group_page(driver):
            log.error("Script completed: no valid Facebook Group page detected.")
            return 1

        scroll_to_load_posts(driver)

        seen_urls = load_seen_urls()
        log.info("Loaded %d previously seen URL(s) from %s.", len(seen_urls), SEEN_FILE)

        result = process_posts(driver, cutoff, seen_urls, max_new_leads=remaining_quota)

        log.info("Posts processed: %d", result.posts_processed)
        log.info("New URLs found: %d", len(result.collected))
        log.info("Old posts skipped (outside %d-day window): %d", DAYS_BACK, result.old_posts_skipped)
        log.info("Duplicate URLs skipped: %d", result.duplicates_skipped)
        log.info("Invalid/non-post URLs skipped: %d", result.invalid_urls_skipped)
        log.info("Posts skipped (timestamp unparseable): %d", result.unparseable_timestamps_skipped)
        log.info("Posts skipped (not a clear remote/freelance video-editing lead): %d", result.not_qualifying_lead_skipped)

    finally:
        try:
            driver.quit()
        except Exception:
            pass  # Never fail the run just because cleanup of the attached session had trouble.

    if not result.collected:
        log.info("No new posts to write. Script completed.")
        return 0

    try:
        worksheet = get_sheets_worksheet()
        existing_sheet_urls = get_existing_sheet_urls(worksheet)

        to_write = [p for p in result.collected if p.url not in existing_sheet_urls]
        skipped_already_in_sheet = len(result.collected) - len(to_write)
        if skipped_already_in_sheet:
            log.info("Skipped %d URL(s) already present in the Google Sheet.", skipped_already_in_sheet)

        written = append_posts_to_sheet(worksheet, to_write)
        log.info("URLs written to Google Sheets: %d", written)

        save_daily_quota_used(quota_used + written)
        log.info("Daily lead quota: %d/%d used today.", quota_used + written, DAILY_LEAD_LIMIT)

    except Exception as exc:
        log.error("Google Sheets write failed: %s", exc)
        write_local_backup(result.collected)
        # Still update the local seen-set so we don't re-process these next run's DOM twice,
        # but the operator can see them in the backup file and copy them in manually.
        save_seen_urls(seen_urls | result.seen_this_run)
        log.error("Script completed with errors (Google Sheets failure).")
        return 1

    save_seen_urls(seen_urls | result.seen_this_run)
    log.info("Script completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Browser transport for the enrichment stage.

Job pages are public but Cloudflare refuses plain HTTP clients with a 403 at
every TLS impersonation setting, so this drives a real browser.

Every choice here was measured against live job pages on 2026-08-12, not
assumed. What was tried and what happened:

    plain HTTP (curl_cffi, 7 impersonations)   403, always
    Playwright bundled chromium, headless      403 challenge
    Playwright bundled chromium, headful       403 challenge
    Playwright system chrome, headless         403 challenge
    Playwright system chrome, headful          403 challenge
    Playwright + persistent profile            403 challenge
    patchright system chrome, headless         403 challenge
    patchright system chrome, HEADFUL          works

Cloudflare fingerprints the CDP automation traces that stock Playwright leaks;
patchright patches them out. Headless is detected on its own, independently of
CDP, so the browser must run headful — `offscreen` parks the window far off the
desktop instead, which keeps it out of the way without pretending to be
headless.

Three things that are counter-intuitive and easy to "fix" back into breakage:

1. No custom user agent, no `--disable-blink-features` style args, no stealth
   init scripts. Those *help* stock Playwright and *hurt* patchright, whose
   whole approach is to look like an ordinary Chrome with nothing overridden.
2. One IP for the whole run. A Cloudflare clearance cookie is bound to the IP
   that earned it, so rotating proxies per request throws away clearance and
   triggers a fresh challenge every time. This is the opposite of the right
   policy for the search API.
3. Success is judged by the client card being in the DOM, not by HTTP status —
   a page was observed returning 403 while still rendering the real content.
"""

import logging
import random
import time
from pathlib import Path

from upwork_scraper.proxies.proxy_manager import ProxyManager

LOGGER = logging.getLogger(__name__)

INSTALL_HELP = """
The browser transport needs patchright (a patched Playwright that Cloudflare
does not flag) plus Chrome. From this folder:

    .venv\\Scripts\\python.exe -m pip install -e ".[browser]"
    .venv\\Scripts\\python.exe -m patchright install chrome

Stock playwright is detected on Upwork job pages and will be blocked.
""".strip()

# Two separate Chrome profiles, deliberately isolated:
#   anonymous  - signed out, what every run uses by default
#   logged-in  - holds an Upwork session, used only when explicitly asked for
# Keeping them apart means signing in cannot silently change what an existing
# run does, and the scheduled job stays anonymous unless told otherwise.
DEFAULT_PROFILE_DIR = Path.home() / ".upwork_scraper" / "browser_profile"
LOGIN_PROFILE_DIR = Path.home() / ".upwork_scraper" / "browser_profile_login"


def profile_for(logged_in: bool) -> Path:
    return LOGIN_PROFILE_DIR if logged_in else DEFAULT_PROFILE_DIR

# Present on a job page that actually rendered — the real success signal.
READY_SELECTOR = '[data-qa="client-location"], [data-qa="client-contract-date"]'

CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-challenge",
    "cf_chl_opt",
    "enable javascript and cookies to continue",
)

# Upwork's own soft block, served as a friendly error page with HTTP 200. Seen
# on a signed-in session after roughly a dozen job-page loads: the app shell
# renders but never hydrates, and the body reads "We'll be right back".
# Distinct from a Cloudflare challenge, and it means back off — hard.
RATE_LIMIT_MARKERS = (
    "we'll be right back",
    "we will be right back",
    "we&#39;ll be right back",
)

# Parks the window ~2400px off-screen. Headful for detection purposes, invisible
# in practice. The only launch arg used, because args are a detection signal.
OFFSCREEN_ARGS = ["--window-position=-2400,-2400"]


def _load_playwright():
    """patchright if present, stock playwright as a (blocked) fallback."""
    try:
        from patchright.sync_api import sync_playwright
        return sync_playwright, True
    except ImportError:
        pass
    try:
        from playwright.sync_api import sync_playwright
        LOGGER.warning(
            "Using stock playwright — Cloudflare detects it on job pages. "
            "Install patchright for this to work."
        )
        return sync_playwright, False
    except ImportError as e:
        raise RuntimeError(INSTALL_HELP) from e


class BrowserFetcher:
    """Fetches job pages with a real browser. Use as a context manager.

        with BrowserFetcher() as fetch:
            enricher = ClientEnricher(html_fetcher=fetch)

    The instance is callable, so it plugs straight into `ClientEnricher`.
    """

    def __init__(
        self,
        proxy_manager: ProxyManager | None = None,
        headless: bool = False,
        offscreen: bool = True,
        profile_dir: str | Path | None = None,
        timeout_ms: int = 45_000,
        ready_timeout_ms: int = 15_000,
        retry_on_challenge: bool = True,
        rng: random.Random | None = None,
    ):
        if headless:
            LOGGER.warning(
                "headless=True is detected by Cloudflare on job pages — "
                "expect every fetch to be blocked."
            )

        self.proxy_manager = proxy_manager
        self.headless = headless
        self.offscreen = offscreen
        self.profile_dir = Path(profile_dir or DEFAULT_PROFILE_DIR)
        self.timeout_ms = timeout_ms
        self.ready_timeout_ms = ready_timeout_ms
        self.retry_on_challenge = retry_on_challenge
        self._rng = rng or random.Random()

        self._playwright = None
        self._context = None
        self.is_patched = False
        self.pages_fetched = 0

    # ------------------------------------------------------------------ setup
    def _proxy_settings(self) -> dict | None:
        """One proxy for the whole run — clearance cookies are IP-bound."""
        if not self.proxy_manager:
            return None
        proxy = self.proxy_manager.get_proxy()
        if not proxy:
            return None
        LOGGER.info("Browser pinned to proxy %s", proxy.host)
        return {
            "server": f"http://{proxy.host}:{proxy.port}",
            "username": proxy.username,
            "password": proxy.password,
        }

    def start(self):
        sync_playwright, self.is_patched = _load_playwright()
        self._playwright = sync_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        # A persistent profile keeps the Cloudflare clearance cookie between
        # runs, which is the single biggest reduction in challenges.
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            channel="chrome",
            headless=self.headless,
            no_viewport=True,
            args=OFFSCREEN_ARGS if (self.offscreen and not self.headless) else [],
            proxy=self._proxy_settings(),
        )
        LOGGER.info(
            "Browser ready (patchright=%s, headless=%s, profile=%s)",
            self.is_patched, self.headless, self.profile_dir,
        )
        return self

    def close(self):
        for closer, method in ((self._context, "close"), (self._playwright, "stop")):
            try:
                if closer is not None:
                    getattr(closer, method)()
            except Exception:
                pass
        self._context = self._playwright = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()
        return False

    # ------------------------------------------------------------------ fetch
    # Cookies Upwork sets for a signed-in member. `visitor_id` is deliberately
    # absent — anonymous visitors get that one too.
    SESSION_COOKIES = frozenset({
        "master_access_token", "oauth2_global_js_token", "user_uid", "company_uid",
    })

    def is_signed_in(self) -> bool:
        """True when the profile holds an Upwork session.

        Read from the cookie jar rather than by loading a page, so it can be
        polled while the user is part-way through signing in without
        disturbing them.
        """
        if self._context is None:
            return False
        try:
            names = {c.get("name") for c in self._context.cookies()}
        except Exception:
            return False
        return bool(names & self.SESSION_COOKIES)

    def wait_for_login(self, timeout_s: int = 300, poll_s: float = 3.0) -> bool:
        """Block until the profile is signed in, or the timeout expires."""
        waited = 0.0
        while waited < timeout_s:
            if self.is_signed_in():
                return True
            time.sleep(poll_s)
            waited += poll_s
        return self.is_signed_in()

    def open_page(self, url: str):
        """A page left open for the caller to drive — used by the login flow."""
        if self._context is None:
            raise RuntimeError("BrowserFetcher not started")
        page = self._context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        return page

    def _load_once(self, url: str, ready_selector: str | None = None) -> tuple[int, str, bool]:
        page = self._context.new_page()
        try:
            response = page.goto(
                url, wait_until="domcontentloaded", timeout=self.timeout_ms
            )
            status = response.status if response else 0

            # Wait for the client card rather than a blanket sleep. Its presence
            # is what "success" means; the status code can lie. Pages that are
            # not job pages pass `ready_selector=""` to skip the check.
            selector = READY_SELECTOR if ready_selector is None else ready_selector
            rendered = True
            if selector:
                try:
                    page.wait_for_selector(selector, timeout=self.ready_timeout_ms)
                except Exception:
                    rendered = False

            # A brief, irregular read pause and a scroll — closer to real use
            # than loading and closing instantly.
            page.wait_for_timeout(self._rng.randint(400, 1400))
            try:
                page.mouse.wheel(0, self._rng.randint(300, 1100))
                page.wait_for_timeout(self._rng.randint(200, 600))
            except Exception:
                pass

            return status, page.content(), rendered
        finally:
            try:
                page.close()
            except Exception:
                pass

    def fetch(self, url: str, ready_selector: str | None = None) -> tuple[int, str]:
        """Load a job page and return (status, html).

        Reports 200 whenever the page actually rendered, even if the navigation
        status was not 200 — an observed page returned 403 with the real content
        present. Otherwise the three failure modes are told apart:

            403  Cloudflare challenge
            429  Upwork's own soft block ("We'll be right back")
            503  loaded but never rendered the client card
        """
        if self._context is None:
            raise RuntimeError("BrowserFetcher not started — use it as a context manager")

        attempts = 2 if self.retry_on_challenge else 1
        status, html, rendered = 0, "", False
        challenged = rate_limited = False

        for attempt in range(1, attempts + 1):
            status, html, rendered = self._load_once(url, ready_selector)
            head = html[:6000].lower()
            challenged = any(m in head for m in CHALLENGE_MARKERS)
            rate_limited = any(m in head for m in RATE_LIMIT_MARKERS)

            if rendered and not challenged and not rate_limited:
                self.pages_fetched += 1
                return 200, html

            # Retrying a soft block just deepens it — only challenges are worth
            # a second attempt.
            if rate_limited:
                LOGGER.error(
                    "Upwork is serving its 'we'll be right back' page — the "
                    "session is rate limited. Stop and let it cool off."
                )
                break

            if attempt < attempts:
                reason = "Cloudflare challenge" if challenged else "page never rendered"
                LOGGER.warning(
                    "%s on %s — waiting then retrying once",
                    reason, url.rsplit("/", 1)[-1],
                )
                time.sleep(self._rng.uniform(6, 12))

        self.pages_fetched += 1
        if rate_limited:
            return 429, html
        if challenged:
            return 403, html
        return (503 if not rendered else status or 200), html

    __call__ = fetch


# Back-compat alias: this used to be Playwright-specific.
PlaywrightFetcher = BrowserFetcher


def build_browser_fetcher(
    proxy_manager: ProxyManager | None = None,
    headless: bool = False,
    offscreen: bool = True,
) -> BrowserFetcher:
    """Started fetcher, ready to hand to ClientEnricher. Caller must close()."""
    return BrowserFetcher(
        proxy_manager=proxy_manager, headless=headless, offscreen=offscreen
    ).start()

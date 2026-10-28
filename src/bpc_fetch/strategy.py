"""HTTP bypass strategy: apply correct headers per site."""
import logging
import random
import re
import httpx
from .sites import SiteStrategy

UA_GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
UA_BINGBOT = "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)"
UA_FACEBOOKBOT = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
UA_NORMAL = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"

REFERER_GOOGLE = "https://www.google.com/"
REFERER_FACEBOOK = "https://www.facebook.com/"
REFERER_TWITTER = "https://t.co/"

TIMEOUT = 30.0
logger = logging.getLogger(__name__)


class FetchError(RuntimeError):
    """A response was received, but cannot be used as an article."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def _is_challenge(html: str) -> bool:
    """Recognize access challenges without mistaking long error pages for articles."""
    lower = html.lower()
    headings = re.findall(r"<(?:title|h1)\b[^>]*>(.*?)</(?:title|h1)>", lower, re.DOTALL)
    title_markers = (
        "are you a robot", "robot check", "access denied", "just a moment",
        "verify you are human", "security check", "captcha", "attention required",
    )
    if any(marker in heading for heading in headings for marker in title_markers):
        return True
    return any(marker in lower for marker in (
        "please verify you are a human", "enable javascript and cookies to continue",
        "/cdn-cgi/challenge-platform/", "px-captcha", "id=\"captcha\"",
    ))


def build_headers(strategy: SiteStrategy) -> dict[str, str]:
    """Build HTTP headers based on the site's bypass strategy."""
    headers: dict[str, str] = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    if strategy.useragent_custom:
        headers["User-Agent"] = strategy.useragent_custom
    else:
        ua = strategy.useragent.lower() if strategy.useragent else ""
        if ua == "googlebot":
            headers["User-Agent"] = UA_GOOGLEBOT
        elif ua == "bingbot":
            headers["User-Agent"] = UA_BINGBOT
        elif ua in ("facebookbot", "facebook"):
            headers["User-Agent"] = UA_FACEBOOKBOT
        else:
            headers["User-Agent"] = UA_NORMAL

    ref = strategy.referer.lower() if strategy.referer else ""
    if ref == "google":
        headers["Referer"] = REFERER_GOOGLE
    elif ref == "facebook":
        headers["Referer"] = REFERER_FACEBOOK
    elif ref == "twitter":
        headers["Referer"] = REFERER_TWITTER
    elif not strategy.useragent and not strategy.useragent_custom:
        headers["Referer"] = REFERER_GOOGLE

    if strategy.random_ip:
        ip = f"{random.randint(1,223)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}"
        headers["X-Forwarded-For"] = ip

    return headers


def build_fallback_headers() -> dict[str, str]:
    """Fallback: Googlebot UA + Google referer."""
    return {
        "User-Agent": UA_GOOGLEBOT,
        "Referer": REFERER_GOOGLE,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


async def fetch_page(url: str, strategy: SiteStrategy | None = None, client: httpx.AsyncClient | None = None) -> tuple[str, int]:
    """Fetch page HTML with bypass headers. Returns (html, status_code)."""
    headers = build_headers(strategy) if strategy else build_fallback_headers()
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT)
    try:
        resp = await client.get(url, headers=headers)
        return resp.text, resp.status_code
    finally:
        if own_client:
            await client.aclose()


async def fetch_with_retries(
    url: str,
    strategy: SiteStrategy | None = None,
    client: httpx.AsyncClient | None = None,
    use_browser: bool | None = None,
    headless: bool = True,
) -> tuple[str, int, dict | None]:
    """Try primary strategy, fallback to googlebot, then browser, then archive.org.

    use_browser: True=force browser, False=skip browser, None=auto (blocking rules).
    Returns (html, status_code, dom_result_or_None).
    """
    should_browser = use_browser if use_browser is not None else (
        strategy is not None and bool(strategy.block_regex)
    )

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT)
    try:
        # An explicitly requested visible browser should be the first transport,
        # rather than probing the same URL with unrelated HTTP crawler identities.
        browser_error = None
        if should_browser and not headless:
            from .browser import BrowserLaunchError, fetch_with_browser_result
            try:
                html, status, dom_result = await fetch_with_browser_result(
                    url, strategy or SiteStrategy(domain=""), headless=False,
                )
                if status == 200 and _has_content(html) and not _is_paywalled(html):
                    return html, status, dom_result
            except BrowserLaunchError:
                raise
            except Exception as exc:
                browser_error = exc
                logger.warning("Browser navigation/extraction failed for %s; trying HTTP/archive: %s", url, exc)
            should_browser = False

        html, status = await fetch_page(url, strategy, client)
        if status == 200 and _has_content(html) and not _is_paywalled(html):
            return html, status, None

        if not strategy or strategy.useragent != "googlebot":
            fallback = SiteStrategy(domain=(strategy.domain if strategy else ""), useragent="googlebot")
            html, status = await fetch_page(url, fallback, client)
            if status == 200 and _has_content(html) and not _is_paywalled(html):
                return html, status, None

        # Browser fallback — also extracts DOM directly
        if should_browser:
            from .browser import BrowserLaunchError, fetch_with_browser_result
            browser_strategy = strategy or SiteStrategy(domain="")
            try:
                html, status, dom_result = await fetch_with_browser_result(url, browser_strategy, headless=headless)
                if status == 200 and _has_content(html) and not _is_paywalled(html):
                    return html, status, dom_result
            except BrowserLaunchError:
                raise
            except Exception as exc:
                browser_error = exc
                logger.warning("Browser navigation/extraction failed for %s; trying archive: %s", url, exc)

        # Archive.org fallback
        try:
            archive_url = f"https://web.archive.org/web/2/{url}"
            resp = await client.get(archive_url, headers={"User-Agent": UA_NORMAL}, follow_redirects=True)
            if (resp.status_code == 200 and _has_content(resp.text)
                    and _has_full_article(resp.text) and not _is_paywalled(resp.text)):
                return resp.text, 200, None
        except Exception:
            pass

        if browser_error is not None:
            raise FetchError(
                f"browser_failed: {browser_error}; archive fallback did not return a usable article",
                status,
            ) from browser_error
        if status == 200 and _is_challenge(html):
            raise FetchError("access_challenge: page requires robot or human verification (HTTP 200)", status)
        return html, status, None
    finally:
        if own_client:
            await client.aclose()


def _has_content(html: str) -> bool:
    """Check if HTML has meaningful content (not just a paywall/redirect)."""
    if len(html) < 500 or _is_challenge(html):
        return False
    lower = html.lower()
    if "<article" in lower or "articlebody" in lower or "article-body" in lower:
        return True
    if lower.count("<p") > 3:
        return True
    return len(html) > 5000


def _has_full_article(html: str) -> bool:
    """Check if HTML likely contains a full article (not just first paragraph)."""
    import re
    paragraphs = re.findall(r'<p[^>]*>(.+?)</p>', html, re.DOTALL)
    total_text = sum(len(re.sub(r'<[^>]+>', '', p)) for p in paragraphs)
    return total_text > 800 or len(paragraphs) > 5


def _is_paywalled(html: str) -> bool:
    """Detect if page shows paywall truncation markers."""
    markers = [
        "log in or create an account to continue",
        "subscribe to continue reading",
        "sign in to continue",
        "create a free account to continue",
        "this article is for subscribers",
        "to read the full story",
        "register for free to continue reading",
        "already a subscriber? sign in",
        "want to read more?",
        "unlock this article",
        "premium content",
        "members only",
    ]
    lower = html.lower()
    return any(m in lower for m in markers)

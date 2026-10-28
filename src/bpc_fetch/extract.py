"""Article extraction: HTML → markdown with local images."""
import re
import hashlib
import json
from html import escape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from bs4 import BeautifulSoup
from markdownify import markdownify

from .strategy import UA_NORMAL, TIMEOUT


def extract_article(html: str, url: str, dom_result: dict | None = None) -> dict:
    """Extract article content from HTML. Returns dict with title, text, markdown, images.

    Bloomberg uses its own body parser; other sites may use browser DOM results.
    """
    host = (urlparse(url).hostname or "").lower()
    if host == "bloomberg.com" or host.endswith(".bloomberg.com"):
        return _extract_bloomberg(html, url)

    if dom_result and dom_result.get("text") and len(dom_result["text"]) > 200:
        images = [img["src"] for img in dom_result.get("images", [])]
        return {
            "title": dom_result.get("title", ""),
            "author": "",
            "date": "",
            "text": dom_result["text"],
            "images": images,
            "url": url,
        }

    result = trafilatura.extract(
        html,
        url=url,
        include_images=True,
        include_links=True,
        include_tables=True,
        output_format="txt",
        favor_precision=False,
        favor_recall=True,
    )

    metadata = trafilatura.extract(
        html,
        url=url,
        output_format="xmltei",
        include_images=False,
        favor_recall=True,
    )

    title = _extract_title(html, metadata)
    author = _extract_author(metadata)
    date = _extract_date(metadata)
    images = _extract_image_urls(html, url)

    md_content = _clean_paywall_text(result or "")

    return {
        "title": title,
        "author": author,
        "date": date,
        "text": md_content,
        "images": images,
        "url": url,
    }


def _extract_bloomberg(html: str, url: str) -> dict:
    """Rebuild only Bloomberg's article body, following BPC's known schemas.

    BPC's legacy ArticleBody/FeatureBody props contain HTML; the current
    userscript reads props.pageProps.story.body.content from __NEXT_DATA__.
    References: csns1/bypass-paywalls-chrome-clean-magnolia1234/contentScript.js
    and https://greasyfork.org/en/scripts/542351-bypass-paywalls-clean-en/code.
    Never search arbitrary JSON values or fall back to a whole-page extractor:
    both can turn recommendations, subscription copy or a teaser into an article.
    """
    soup = BeautifulSoup(html, "html.parser")
    article = {"title": "", "author": "", "date": "", "text": "",
               "images": [], "url": url, "extraction_error": "bloomberg_body_missing"}
    heading_text = " ".join(h.get_text(" ", strip=True) for h in soup.select("title, h1")).lower()
    visible_text = soup.get_text(" ", strip=True).lower()
    if (soup.select_one("#px-captcha, #challenge-form")
            or "are you a robot" in heading_text
            or "just a moment" in heading_text
            or ("we've detected unusual activity" in visible_text
                and "confirm you're not a robot" in visible_text)):
        article["extraction_error"] = "bloomberg_challenge"
        return article

    def meta(*names):
        for name in names:
            tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return ""

    article["title"] = meta("og:title", "twitter:title") or _extract_title(html, None)
    article["author"] = meta("author", "article:author")
    article["date"] = meta("article:published_time", "date", "parsely-pub-date")
    candidates = []
    for script in soup.select('script#__NEXT_DATA__, script[data-component-props="ArticleBody"], script[data-component-props="FeatureBody"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            article["extraction_error"] = "bloomberg_invalid_json"
            continue
        if not isinstance(data, dict):
            continue
        if script.get("id") == "__NEXT_DATA__":
            props = data.get("props")
            page = props.get("pageProps") if isinstance(props, dict) else None
            story = page.get("story") if isinstance(page, dict) else None
            if isinstance(story, dict):
                candidates.append((story.get("body"), story))
        else:
            story = data.get("story")
            story = story if isinstance(story, dict) else data
            candidates.append((data.get("body") or story.get("body"), story))

    for body, story in candidates:
        if isinstance(body, str):
            body_html = body
        elif isinstance(body, dict) and isinstance(body.get("content"), list):
            body_html = _bloomberg_nodes(body["content"], url)
        else:
            continue
        article["extraction_error"] = "bloomberg_incomplete_body"
        text, images, prose = _bloomberg_body(body_html, url)
        # A single opening paragraph and headings/captions alone are not evidence
        # of a complete body. Keep this conservative when the payload is partial.
        if prose < 2 or len(text) < 400:
            continue
        article["text"] = text
        article["images"] = images
        for key in ("headline", "title"):
            if isinstance(story.get(key), str) and story[key].strip():
                article["title"] = BeautifulSoup(story[key], "html.parser").get_text(" ", strip=True)
                break
        authors = story.get("authors") or story.get("byline")
        if isinstance(authors, list):
            names = [a.get("name", "") if isinstance(a, dict) else a for a in authors]
            article["author"] = ", ".join(n for n in names if isinstance(n, str) and n) or article["author"]
        elif isinstance(authors, str):
            article["author"] = authors
        for key in ("publishedAt", "published", "datePublished", "publishedTime"):
            if isinstance(story.get(key), str):
                article["date"] = story[key]
                break
        break

    if not article["text"]:
        # Already-rendered, full bodies remain usable, but generic browser text
        # is deliberately ignored because it includes teasers and navigation.
        for container in soup.select('.body-copy-v2, .body-copy, .body-content, [class*="ArticleBody_body"]'):
            if re.search(r"teaser|shimmer|nearly-transparent", " ".join(container.get("class", [])), re.I):
                continue
            text, images, prose = _bloomberg_body(str(container), url)
            if prose >= 5 and len(text) >= 1500:
                article["text"], article["images"] = text, images
                break

    if article["text"]:
        article.pop("extraction_error", None)
        hero = _bloomberg_url(meta("og:image"), url)
        if hero and hero not in article["images"]:
            article["images"].insert(0, hero)
        article["images"] = article["images"][:20]
    return article


def _bloomberg_url(value, base_url: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        resolved = urljoin(base_url, value.strip())
        return resolved if urlparse(resolved).scheme in ("http", "https") else ""
    except ValueError:
        return ""


def _bloomberg_nodes(nodes: list, url: str, depth: int = 0) -> str:
    """Render known body nodes; unknown widgets do not contribute article text."""
    if depth > 40 or not isinstance(nodes, list):
        return ""
    parts = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        kind = node.get("type")
        if not isinstance(kind, str):
            continue
        content = lambda: _bloomberg_nodes(node.get("content", []), url, depth + 1)
        if kind == "text" and isinstance(node.get("value"), str):
            value = escape(node["value"])
            attributes = node.get("attributes")
            if isinstance(attributes, dict):
                if attributes.get("strong"):
                    value = "<strong>" + value + "</strong>"
                if attributes.get("emphasis"):
                    value = "<em>" + value + "</em>"
            parts.append(value)
        elif kind in ("paragraph", "heading", "list", "listItem", "div", "document", "body") or kind.endswith("quote"):
            tag = {"paragraph": "p", "heading": "h3", "list": "ul", "listItem": "li",
                   "document": "div", "body": "div"}.get(kind, "blockquote" if kind.endswith("quote") else "div")
            parts.append(f"<{tag}>" + content() + f"</{tag}>")
        elif kind in ("link", "entity"):
            data = node.get("data")
            data = data if isinstance(data, dict) else {}
            link = data.get("link")
            link = link if isinstance(link, dict) else {}
            href = data.get("webUrl") or data.get("href") or link.get("href")
            href = _bloomberg_url(href, url)
            value = content()
            parts.append(f'<a href="{escape(href, quote=True)}">{value}</a>' if href else value)
        elif kind == "media":
            data = node.get("data")
            data = data if isinstance(data, dict) else {}
            media = data.get("photo") if node.get("subType") == "photo" else data.get("chart") if node.get("subType") == "chart" else None
            if isinstance(media, dict):
                src = _bloomberg_url(media.get("fallback") or media.get("src"), url)
                if src:
                    caption = " ".join(str(media[k]) for k in ("caption", "credit") if media.get(k))
                    caption = BeautifulSoup(caption, "html.parser").get_text(" ", strip=True)
                    parts.append(f'<figure><img src="{escape(src, quote=True)}"><figcaption>{escape(caption)}</figcaption></figure>')
        elif kind in ("br", "hr"):
            parts.append(f"<{kind}>")
    return "".join(parts)


def _bloomberg_body(body_html: str, url: str) -> tuple[str, list[str], int]:
    body = BeautifulSoup(body_html, "html.parser")
    for unwanted in body.select('script, style, nav, aside, footer, form, button, [class*="teaser"], [class*="newsletter"], [class*="recirc"], [id*="paywall"], [class*="paywall"]'):
        unwanted.decompose()
    for link in body.find_all("a", href=True):
        href = _bloomberg_url(link["href"], url)
        if href:
            link["href"] = href
        else:
            del link["href"]
    images = []
    for image in body.find_all("img"):
        src = _bloomberg_url(image.get("data-native-src") or image.get("data-src") or image.get("src"), url)
        if src and src not in images:
            images.append(src)
        # Images are emitted by article_to_markdown using downloaded local paths.
        image.decompose()
    prose = [p.get_text(" ", strip=True) for p in body.find_all("p")]
    text = markdownify(str(body), heading_style="ATX", bullets="-").strip()
    text = _clean_paywall_text(text)
    prompt = re.compile(r"(?i)^(subscribe to continue reading|sign in to continue|log in or create an account to continue)")
    if prompt.match(text):
        text = ""
    # Count only paragraphs that survived truncation at a subscription prompt.
    prose_count = 0
    for paragraph in prose:
        if prompt.match(paragraph) or _clean_paywall_text(paragraph) != paragraph:
            break
        prose_count += len(paragraph) >= 40
    return text, images, prose_count


def article_to_markdown(article: dict, images_dir: str = "images") -> str:
    """Convert extracted article to markdown with frontmatter."""
    lines = []
    lines.append("---")
    lines.append(f"title: \"{_escape_yaml(article['title'])}\"")
    if article.get("author"):
        lines.append(f"author: \"{_escape_yaml(article['author'])}\"")
    if article.get("date"):
        lines.append(f"date: {article['date']}")
    lines.append(f"source: {article['url']}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {article['title']}")
    lines.append("")

    text = article["text"]

    # Strip paywall markers from text
    for marker in ["Enjoying our latest content?", "Log in or create an account to continue",
                   "Subscribe to continue reading", "Already a subscriber?"]:
        idx = text.find(marker)
        if idx > 0:
            text = text[:idx].rstrip()

    # Embed first image as hero, rest at end
    if article.get("images"):
        fname = _image_filename(article["images"][0], 0)
        lines.append(f"![image]({images_dir}/{fname})")
        lines.append("")

    lines.append(text)

    # Remaining images at end
    if article.get("images") and len(article["images"]) > 1:
        lines.append("")
        for i, img_url in enumerate(article["images"][1:], 1):
            fname = _image_filename(img_url, i)
            lines.append(f"![image]({images_dir}/{fname})")
            lines.append("")

    return "\n".join(lines)


async def download_images(
    image_urls: list[str],
    out_dir: Path,
    max_images: int = 20,
    client: httpx.AsyncClient | None = None,
) -> list[Path]:
    """Download images to out_dir. Returns list of saved paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT)

    saved: list[Path] = []
    try:
        for i, url in enumerate(image_urls[:max_images]):
            try:
                resp = await client.get(url, headers={"User-Agent": UA_NORMAL})
                if resp.status_code == 200 and len(resp.content) > 1024:
                    fname = _image_filename(url, i)
                    path = out_dir / fname
                    path.write_bytes(resp.content)
                    saved.append(path)
            except Exception:
                continue
    finally:
        if own_client:
            await client.aclose()
    return saved


def _extract_title(html: str, metadata: str | None) -> str:
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    title = m.group(1).strip() if m else ""
    if metadata:
        m2 = re.search(r"<title[^>]*>([^<]+)</title>", metadata)
        if m2:
            title = m2.group(1).strip()
    return title.split("|")[0].split(" - ")[0].strip()


def _extract_author(metadata: str | None) -> str:
    if not metadata:
        return ""
    m = re.search(r'<author[^>]*>.*?<persName>([^<]+)</persName>', metadata, re.DOTALL)
    return m.group(1).strip() if m else ""


def _extract_date(metadata: str | None) -> str:
    if not metadata:
        return ""
    m = re.search(r'when="(\d{4}-\d{2}-\d{2})"', metadata)
    return m.group(1) if m else ""


def _extract_image_urls(html: str, base_url: str) -> list[str]:
    """Extract article images only — skip UI, nav, footer, sidebar images."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')

    # Find article container
    article = (
        soup.find('article') or
        soup.find(attrs={"data-article": True}) or
        soup.find(class_=re.compile(r'article.body|story.body|post.content|entry.content'))
    )
    container = article if article else soup.find('main') or soup.body

    if not container:
        return []

    urls = []
    seen = set()
    skip_patterns = ["pixel", "tracking", "1x1", "logo", "icon", "avatar", "badge",
                     "button", "arrow", "spinner", "loading", "placeholder", "svg+xml",
                     "data:image", "gravatar", "emoji", "widget"]

    for img in container.find_all('img', src=True):
        src = img.get('src', '')
        if not src or any(skip in src.lower() for skip in skip_patterns):
            continue
        # Skip tiny images (likely icons)
        width = img.get('width', '')
        height = img.get('height', '')
        if width and width.isdigit() and int(width) < 50:
            continue
        if height and height.isdigit() and int(height) < 50:
            continue
        full = urljoin(base_url, src)
        if full not in seen:
            seen.add(full)
            urls.append(full)

    return urls[:20]


def _image_filename(url: str, index: int) -> str:
    parsed = urlparse(url)
    ext = Path(parsed.path).suffix or ".jpg"
    if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"):
        ext = ".jpg"
    h = hashlib.md5(url.encode()).hexdigest()[:8]
    return f"img_{index:03d}_{h}{ext}"


def _escape_yaml(s: str) -> str:
    return s.replace('"', '\\"').replace("\n", " ")


def _clean_paywall_text(text: str) -> str:
    """Remove paywall/login prompts that leak into extracted text."""
    markers = [
        "Enjoying our latest content?",
        "Log in or create an account to continue",
        "Subscribe to continue reading",
        "Already a subscriber? Sign in",
        "Sign in to continue",
        "Create a free account to continue",
        "Register for free to continue reading",
        "Want to read more?",
        "Access the most recent journalism",
        "Explore the latest features & opinion",
    ]
    for marker in markers:
        idx = text.find(marker)
        if idx > 0:
            text = text[:idx].rstrip()
    # Also strip trailing "or" / login button text
    for tail in ["\nor\n", "\nor", "\nSign in", "\nLog in", "\nSubscribe"]:
        if text.endswith(tail):
            text = text[:-len(tail)].rstrip()
    return text

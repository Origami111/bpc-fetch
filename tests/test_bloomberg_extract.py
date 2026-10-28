"""Synthetic schema fixtures only; no real Bloomberg article is reproduced."""
import json
import unittest
from unittest.mock import patch

from bpc_fetch.extract import extract_article


URL = "https://www.bloomberg.com/opinion/articles/2026-10-27/synthetic-test"
PARAGRAPHS = [
    "Synthetic opening paragraph for a parser test. It describes an imaginary "
    "policy discussion and contains enough ordinary prose to distinguish an "
    "article body from a navigation label, a headline, or an isolated subscription "
    "message. These words are fabricated test data rather than publisher content.",
    "Synthetic closing paragraph for a parser test. It supplies a second complete "
    "thought with a distinct ending so the test can verify that the extractor "
    "retains the entire supplied body. It also checks that recommendations and "
    "other unrelated page state do not appear in the resulting article text.",
]


def paragraph(text):
    return {"type": "paragraph", "content": [{"type": "text", "value": text}]}


def page(payload, component="__NEXT_DATA__", extra=""):
    attribute = (f'id="{component}"' if component == "__NEXT_DATA__"
                 else f'data-component-props="{component}"')
    encoded = json.dumps(payload).replace("<", "\\u003c")
    return ('<html><head><title>Synthetic title - Bloomberg</title>'
            '<meta name="author" content="Meta Author">'
            '<meta property="article:published_time" content="2026-10-27T08:00:00Z">'
            '</head><body><nav>Navigation should never become article text</nav>'
            f'<script {attribute} type="application/json">{encoded}</script>'
            f'{extra}</body></html>')


def next_page(nodes=None, **story_fields):
    story = {"body": {"content": nodes if nodes is not None else [paragraph(p) for p in PARAGRAPHS]}}
    story.update(story_fields)
    return page({"props": {"pageProps": {"story": story,
                 "recommendations": ["Unrelated recommendation must not leak"]}}})


class BloombergExtractionTests(unittest.TestCase):
    def test_current_next_body_precedes_browser_teaser(self):
        html = next_page(headline="Synthetic &amp; headline", authors=[{"name": "Test Writer"}],
                         publishedAt="2026-10-27T09:30:00Z")
        teaser = {"title": "Wrong teaser", "text": "Browser navigation and teaser. " * 30,
                  "images": [{"src": "https://example.test/nav.jpg"}]}
        with patch("bpc_fetch.extract.trafilatura.extract", side_effect=AssertionError("generic fallback")):
            result = extract_article(html, URL, teaser)
        self.assertEqual(result["title"], "Synthetic & headline")
        self.assertEqual(result["author"], "Test Writer")
        self.assertEqual(result["date"], "2026-10-27T09:30:00Z")
        for text in PARAGRAPHS:
            self.assertIn(text, result["text"])
        self.assertNotIn("Unrelated recommendation must not leak", result["text"])
        self.assertNotIn("Navigation", result["text"])
        self.assertNotIn("extraction_error", result)

    def test_legacy_article_and_feature_html(self):
        body = ''.join(f'<p>{p}</p>' for p in PARAGRAPHS)
        body += ('<img src="/thumb.jpg" data-native-src="/full.jpg">'
                 '<nav>Injected navigation</nav><script>injected script</script>')
        for component, payload in (("ArticleBody", {"body": body}),
                                   ("FeatureBody", {"story": {"body": body}})):
            with self.subTest(component=component):
                result = extract_article(page(payload, component), URL)
                self.assertEqual(result["title"], "Synthetic title")
                self.assertEqual(result["author"], "Meta Author")
                self.assertEqual(result["date"], "2026-10-27T08:00:00Z")
                self.assertEqual(result["images"], ["https://www.bloomberg.com/full.jpg"])
                self.assertIn(PARAGRAPHS[-1], result["text"])
                self.assertNotIn("Injected", result["text"])
                self.assertNotIn("injected script", result["text"])

    def test_recursive_rich_text_lists_quotes_and_media(self):
        nodes = [paragraph(PARAGRAPHS[0]),
                 {"type": "heading", "content": [{"type": "text", "value": "A synthetic section"}]},
                 {"type": "div", "content": [paragraph(PARAGRAPHS[1])]},
                 {"type": "paragraph", "content": [
                     {"type": "text", "value": "Bold", "attributes": {"strong": True}},
                     {"type": "text", "value": " and "},
                     {"type": "link", "data": {"href": "/test-link"},
                      "content": [{"type": "text", "value": "linked words"}]}]},
                 {"type": "list", "content": [{"type": "listItem", "content": [paragraph("Test list item")]}]},
                 {"type": "blockquote", "content": [paragraph("A synthetic quoted statement")]},
                 {"type": "media", "subType": "photo", "data": {"photo": {
                     "src": "https://images.example.test/photo.jpg", "caption": "A test caption", "credit": "Test Credit"}}},
                 {"type": "media", "subType": "chart", "data": {"chart": {
                     "src": "https://images.example.test/chart.svg", "fallback": "/chart.png"}}},
                 {"type": "inline-recirc", "content": [paragraph("Related story must not leak")]},
                 {"type": "unknown-widget", "content": [paragraph("Unknown widget must not leak")]}]
        result = extract_article(next_page(nodes), URL)
        self.assertIn("### A synthetic section", result["text"])
        self.assertIn("**Bold** and [linked words](https://www.bloomberg.com/test-link)", result["text"])
        self.assertIn("- Test list item", result["text"])
        self.assertIn("> A synthetic quoted statement", result["text"])
        self.assertIn("A test caption Test Credit", result["text"])
        self.assertEqual(result["images"], ["https://images.example.test/photo.jpg", "https://www.bloomberg.com/chart.png"])
        self.assertNotIn("must not leak", result["text"])

    def test_metadata_hero_and_duplicate_images(self):
        html = page({"body": ''.join(f'<p>{p}</p>' for p in PARAGRAPHS)
                     + '<img src="/hero.jpg"><img src="/body.jpg"><img src="/body.jpg">'}, "ArticleBody")
        html = html.replace('</head>', '<meta property="og:image" content="/hero.jpg"></head>')
        result = extract_article(html, URL)
        self.assertEqual(result["images"], ["https://www.bloomberg.com/hero.jpg", "https://www.bloomberg.com/body.jpg"])

    def test_recommendation_heading_trims_legacy_bodies_and_images(self):
        for component in ("ArticleBody", "FeatureBody"):
            for heading in ("<p><strong>More From Bloomberg Opinion:</strong></p>",
                            "<p>  more FROM Bloomberg   Opinion : </p>",
                            "<h3>More From Bloomberg Opinion:</h3>",
                            "<p>**More From Bloomberg Opinion:**</p>"):
                with self.subTest(component=component, heading=heading):
                    body = ''.join(f'<p>{p}</p>' for p in PARAGRAPHS)
                    body += '<div>' + heading + '<p>Recommended story must disappear</p></div>'
                    body += '<p>Later footer must disappear</p><img src="/recommended.jpg">'
                    result = extract_article(page({"body": body}, component), URL)
                    self.assertNotIn("extraction_error", result)
                    self.assertIn(PARAGRAPHS[-1], result["text"])
                    self.assertNotIn("bloomberg opinion", result["text"].lower())
                    self.assertNotIn("must disappear", result["text"])
                    self.assertEqual(result["images"], [])

    def test_recommendation_heading_trims_current_body(self):
        for kind, value, attributes in (("paragraph", "More From Bloomberg Opinion:", {"strong": True}),
                                        ("paragraph", " more FROM Bloomberg   Opinion : ", {}),
                                        ("heading", "More From Bloomberg Opinion:", {}),
                                        ("paragraph", "**More From Bloomberg Opinion:**", {})):
            with self.subTest(kind=kind, value=value):
                nodes = [paragraph(p) for p in PARAGRAPHS]
                nodes.append({"type": kind, "content": [{"type": "text", "value": value, "attributes": attributes}]})
                nodes += [paragraph("Recommended story must disappear"),
                          {"type": "media", "subType": "photo", "data": {"photo": {"src": "/recommended.jpg"}}}]
                result = extract_article(next_page(nodes), URL)
                self.assertNotIn("extraction_error", result)
                self.assertIn(PARAGRAPHS[-1], result["text"])
                self.assertNotIn("bloomberg opinion", result["text"].lower())
                self.assertNotIn("must disappear", result["text"])
                self.assertEqual(result["images"], [])

    def test_recommendation_words_in_a_sentence_do_not_truncate(self):
        sentence = 'The label "More From Bloomberg Opinion:" appears in this ordinary sentence.'
        nodes = [paragraph(PARAGRAPHS[0]), paragraph(sentence), paragraph(PARAGRAPHS[1])]
        legacy = ''.join(f'<p>{text}</p>' for text in (PARAGRAPHS[0], sentence, PARAGRAPHS[1]))
        legacy = legacy.replace('More From Bloomberg Opinion:', '<strong>More From Bloomberg Opinion:</strong>')
        for html in (next_page(nodes), page({"body": legacy}, "ArticleBody")):
            with self.subTest(html=html):
                text = extract_article(html, URL)["text"]
                self.assertIn("ordinary sentence", text)
                self.assertIn("More From Bloomberg Opinion:", text)
                self.assertIn(PARAGRAPHS[-1], text)

    def test_body_without_recommendation_heading_is_unchanged(self):
        expected = "\n\n".join(PARAGRAPHS)
        legacy = ''.join(f'<p>{p}</p>' for p in PARAGRAPHS)
        for html in (next_page(), page({"body": legacy}, "ArticleBody"), page({"body": legacy}, "FeatureBody")):
            with self.subTest(html=html):
                self.assertEqual(extract_article(html, URL)["text"], expected)

    def test_malformed_json_does_not_raise_or_extract_teaser(self):
        html = '<title>Test</title><script id="__NEXT_DATA__">{broken}</script>'
        html += '<article><p>' + PARAGRAPHS[0] + '</p></article>'
        result = extract_article(html, URL, {"text": "browser teaser " * 50})
        self.assertEqual(result["text"], "")
        self.assertEqual(result["extraction_error"], "bloomberg_invalid_json")

    def test_valid_legacy_survives_malformed_next_data(self):
        html = page({"body": ''.join(f'<p>{p}</p>' for p in PARAGRAPHS)}, "FeatureBody")
        html += '<script id="__NEXT_DATA__">{broken}</script>'
        self.assertIn(PARAGRAPHS[-1], extract_article(html, URL)["text"])

    def test_missing_or_wrong_body_does_not_search_all_json(self):
        bad_payloads = [None, [], {}, {"props": []},
                        {"body": {"content": [paragraph(p) for p in PARAGRAPHS]}},
                        {"props": {"pageProps": {"recommendations": {"body": PARAGRAPHS}}}}]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                result = extract_article(page(payload), URL)
                self.assertEqual(result["text"], "")
                self.assertEqual(result["images"], [])
                self.assertEqual(result["extraction_error"], "bloomberg_body_missing")

    def test_single_paragraph_or_empty_body_is_incomplete(self):
        for nodes in ([], [paragraph(PARAGRAPHS[0] * 3)],
                      [{"type": "heading", "content": [{"type": "text", "value": "Heading " * 100}]}]):
            with self.subTest(nodes=nodes):
                result = extract_article(next_page(nodes), URL)
                self.assertEqual(result["text"], "")
                self.assertEqual(result["extraction_error"], "bloomberg_incomplete_body")

    def test_challenges_are_rejected_even_with_long_browser_text(self):
        for challenge in ('<title>Bloomberg - Are you a robot?</title>',
                          '<title>Just a moment...</title>', '<div id="px-captcha"></div>',
                          "<p>We've detected unusual activity. Please confirm you're not a robot.</p>"):
            with self.subTest(challenge=challenge):
                result = extract_article(challenge, URL, {"text": "challenge " * 100})
                self.assertEqual(result["text"], "")
                self.assertEqual(result["extraction_error"], "bloomberg_challenge")

    def test_known_full_rendered_body_can_be_used(self):
        body = ''.join(f'<p>{p} Extra section {i}.</p>' for i, p in enumerate(PARAGRAPHS * 3))
        result = extract_article('<div class="body-copy-v2">' + body + '</div>', URL)
        self.assertIn("Extra section 5.", result["text"])
        self.assertNotIn("extraction_error", result)

    def test_teaser_container_is_never_used_as_full_rendered_body(self):
        body = ''.join(f'<p>{p}</p>' for p in PARAGRAPHS * 3)
        result = extract_article('<div class="body-content teaser-content_test">' + body + '</div>', URL)
        self.assertEqual(result["text"], "")

    def test_subscription_prompts_cannot_supply_missing_body(self):
        body = '<p>' + PARAGRAPHS[0] * 3 + '</p><p>Subscribe to continue reading</p><p>' + PARAGRAPHS[1] + '</p>'
        self.assertEqual(extract_article(page({"body": body}, "ArticleBody"), URL)["text"], "")

    def test_unsafe_urls_and_malformed_nodes_are_ignored(self):
        nodes = [paragraph(p) for p in PARAGRAPHS] + [None, "widget", {"type": None},
                 {"type": "paragraph", "content": "invalid"},
                 {"type": "link", "data": {"href": "javascript:alert(1)"},
                  "content": [{"type": "text", "value": "safe label"}]},
                 {"type": "media", "subType": "photo", "data": {"photo": {"src": "data:image/png,AAA"}}}]
        nodes.append({"type": "media", "subType": "photo", "data": {"photo": {"src": "https://["}}})
        result = extract_article(next_page(nodes), URL)
        self.assertIn("safe label", result["text"])
        self.assertNotIn("javascript", result["text"])
        self.assertEqual(result["images"], [])

    def test_non_bloomberg_retains_generic_extraction(self):
        for url in ("https://example.test/story", "https://bloomberg.com.example.test/story"):
            with self.subTest(url=url), patch("bpc_fetch.extract.trafilatura.extract", side_effect=["Generic article", None]) as extract:
                result = extract_article('<title>Generic title</title>', url)
                self.assertEqual(result["text"], "Generic article")
                self.assertEqual(extract.call_count, 2)


if __name__ == "__main__":
    unittest.main()

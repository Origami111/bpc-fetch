"""Offline regressions for request routing, browser failures and access challenges."""
import re
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from bpc_fetch import browser, cli, sites, strategy


URL = "https://www.bloomberg.com/opinion/articles/example"
ARTICLE = "<html><title>Budget outlook</title><article>" + "<p>A complete paragraph about fiscal policy.</p>" * 30 + "</article></html>"
CHALLENGE = "<html><title>Bloomberg - Are you a robot?</title>" + "<p>Please verify your browser.</p>" * 80 + "</html>"


def bloomberg():
    return sites.SiteStrategy(
        domain="bloomberg.com", cs_dompurify=True,
        block_regex=r"\.bwbx\.io\/s3\/fence\/fortress-client\/",
    )


class RuleTests(unittest.TestCase):
    def test_real_bloomberg_rule_keeps_both_capabilities(self):
        rules = sites.parse_sites_js(Path(__file__).resolve().parents[1] / "data" / "sites.js")
        rule = rules["bloomberg.com"]
        self.assertTrue(rule.cs_dompurify)
        self.assertTrue(rule.block_regex)
        self.assertEqual(rule.bypass_type(), "block_js")
        pattern, = browser._build_route_patterns(rule)
        self.assertIsInstance(pattern, re.Pattern)
        self.assertIsNotNone(pattern.search("https://assets.bwbx.io/s3/fence/fortress-client/main.js"))
        self.assertIsNone(pattern.search("https://assets.bwbx.io/s3/other/main.js"))

    def test_regex_alternation_and_grouping_remain_intact(self):
        rule = sites.SiteStrategy(domain="example.com", block_regex=r"https://(a|b)\.example\.com/(paywall|gate)\.js$")
        pattern, = browser._build_route_patterns(rule)
        self.assertEqual(pattern.pattern, rule.block_regex)
        self.assertIsNotNone(pattern.search("https://b.example.com/gate.js"))
        self.assertIsNone(pattern.search("https://b.example.com/gate.js?v=2"))

    def test_challenge_length_does_not_make_it_content(self):
        self.assertFalse(strategy._has_content(CHALLENGE))
        self.assertTrue(strategy._has_content(ARTICLE))

    def test_packaged_sites_data_precedes_source_fallback(self):
        packaged = Path(sites.__file__).resolve().parent / "data" / "sites.js"
        with patch.object(Path, "exists", lambda path: path == packaged):
            self.assertEqual(sites._default_sites_js(), packaged)


class FetchTests(unittest.IsolatedAsyncioTestCase):
    def client(self, body=CHALLENGE, status=403):
        def respond(request):
            if request.url.host == "web.archive.org":
                return httpx.Response(404, text="No snapshot")
            return httpx.Response(status, text=body)
        return httpx.AsyncClient(transport=httpx.MockTransport(respond))

    async def test_browser_trigger_uses_rule_even_with_archive_label(self):
        dom = {"text": "Article text " * 50, "title": "Budget outlook", "images": []}
        with patch.object(sites.SiteStrategy, "bypass_type", return_value="archive"), patch.object(browser, "fetch_with_browser_result", AsyncMock(return_value=(ARTICLE, 200, dom))) as fetch:
            async with self.client() as client:
                result = await strategy.fetch_with_retries(URL, bloomberg(), client)
        fetch.assert_awaited_once()
        self.assertEqual(result, (ARTICLE, 200, dom))

    async def test_headed_browser_precedes_http_probes(self):
        with patch.object(browser, "fetch_with_browser_result", AsyncMock(return_value=(ARTICLE, 200, None))) as fetch, patch.object(strategy, "fetch_page", AsyncMock()) as http:
            async with self.client() as client:
                result = await strategy.fetch_with_retries(URL, bloomberg(), client, use_browser=True, headless=False)
        self.assertEqual(result, (ARTICLE, 200, None))
        self.assertFalse(fetch.await_args.kwargs["headless"])
        http.assert_not_awaited()

    async def test_headed_flag_reaches_transport(self):
        args = SimpleNamespace(url=URL, sites_js=None, out_dir=MagicMock(), no_images=True, headed=True)
        with patch.object(cli, "get_sites_map", return_value={}), patch.object(strategy, "fetch_with_retries", AsyncMock(return_value=(CHALLENGE, 403, None))) as fetch:
            result = await cli._cmd_fetch(args)
        self.assertFalse(result["ok"])
        self.assertEqual(fetch.await_args.kwargs, {"use_browser": True, "headless": False})

    async def test_browser_false_disables_rule(self):
        with patch.object(browser, "fetch_with_browser_result", AsyncMock()) as fetch:
            async with self.client() as client:
                html, status, dom = await strategy.fetch_with_retries(URL, bloomberg(), client, use_browser=False)
        fetch.assert_not_awaited()
        self.assertEqual((html, status, dom), (CHALLENGE, 403, None))

    async def test_forced_browser_also_works_without_site_strategy(self):
        with patch.object(browser, "fetch_with_browser_result", AsyncMock(return_value=(ARTICLE, 200, None))) as fetch:
            async with self.client() as client:
                self.assertEqual(await strategy.fetch_with_retries(URL, None, client, use_browser=True), (ARTICLE, 200, None))
        fetch.assert_awaited_once()

    async def test_browser_challenge_403_is_not_promoted_by_dom(self):
        with patch.object(browser, "fetch_with_browser_result", AsyncMock(return_value=(CHALLENGE, 403, {"text": "fake article" * 100}))):
            async with self.client() as client:
                self.assertEqual(await strategy.fetch_with_retries(URL, bloomberg(), client), (CHALLENGE, 403, None))

    async def test_browser_challenge_200_is_rejected(self):
        with patch.object(browser, "fetch_with_browser_result", AsyncMock(return_value=(CHALLENGE, 200, {"text": "fake article" * 100}))):
            async with self.client() as client:
                with self.assertRaises(strategy.FetchError) as error:
                    await strategy.fetch_with_retries(URL, bloomberg(), client)
        self.assertEqual(error.exception.status_code, 200)
        self.assertIn("access_challenge", str(error.exception))

    async def test_http_challenge_200_is_rejected(self):
        async with self.client(status=200) as client:
            with self.assertRaises(strategy.FetchError) as error:
                await strategy.fetch_with_retries(URL, None, client)
        self.assertEqual(error.exception.status_code, 200)

    async def test_missing_browser_error_is_not_swallowed(self):
        requests = []

        def respond(request):
            requests.append(request.url)
            if request.url.host == "web.archive.org":
                return httpx.Response(200, text=ARTICLE)
            return httpx.Response(403, text=CHALLENGE)

        driver = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(side_effect=RuntimeError("Executable doesn't exist"))), stop=AsyncMock())
        with patch.object(browser, "async_playwright", return_value=SimpleNamespace(start=AsyncMock(return_value=driver))):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                with self.assertRaisesRegex(browser.BrowserLaunchError, "Executable doesn't exist.*playwright install chromium"):
                    await strategy.fetch_with_retries(URL, bloomberg(), client)
        self.assertEqual([url.host for url in requests], ["www.bloomberg.com", "www.bloomberg.com"])
        driver.stop.assert_awaited_once()

    async def test_navigation_failure_continues_to_archive(self):
        requests = []

        def respond(request):
            requests.append(request.url)
            if request.url.host == "web.archive.org":
                return httpx.Response(200, text=ARTICLE)
            return httpx.Response(403, text=CHALLENGE)

        with patch.object(browser, "fetch_with_browser_result", AsyncMock(side_effect=RuntimeError("navigation timeout"))):
            with self.assertLogs(strategy.logger, level="WARNING") as diagnostic:
                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    result = await strategy.fetch_with_retries(URL, bloomberg(), client)
        self.assertEqual(result, (ARTICLE, 200, None))
        self.assertEqual([url.host for url in requests], ["www.bloomberg.com", "www.bloomberg.com", "web.archive.org"])
        self.assertIn("navigation timeout", diagnostic.output[0])

    async def test_navigation_and_archive_failures_preserve_browser_diagnostic(self):
        navigation_error = RuntimeError("navigation timeout")
        with patch.object(browser, "fetch_with_browser_result", AsyncMock(side_effect=navigation_error)):
            with self.assertLogs(strategy.logger, level="WARNING"):
                async with self.client() as client:
                    with self.assertRaisesRegex(strategy.FetchError, "navigation timeout.*archive fallback") as error:
                        await strategy.fetch_with_retries(URL, bloomberg(), client)
        self.assertEqual(error.exception.status_code, 403)
        self.assertIs(error.exception.__cause__, navigation_error)

    async def test_regular_http_article_does_not_launch_browser(self):
        with patch.object(browser, "fetch_with_browser_result", AsyncMock()) as fetch:
            async with self.client(body=ARTICLE, status=200) as client:
                self.assertEqual(await strategy.fetch_with_retries(URL, bloomberg(), client), (ARTICLE, 200, None))
        fetch.assert_not_awaited()

    async def test_fetch_command_does_not_extract_or_save_challenge(self):
        args = SimpleNamespace(url=URL, sites_js=None, out_dir=MagicMock(), no_images=True)
        with patch.object(cli, "get_sites_map", return_value={}), patch.object(strategy, "fetch_with_retries", AsyncMock(side_effect=strategy.FetchError("access_challenge", 200))), patch("bpc_fetch.extract.extract_article") as extract:
            result = await cli._cmd_fetch(args)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 200)
        extract.assert_not_called()
        args.out_dir.mkdir.assert_not_called()

    async def test_fetch_command_preserves_http_403(self):
        args = SimpleNamespace(url=URL, sites_js=None, out_dir=MagicMock(), no_images=True)
        with patch.object(cli, "get_sites_map", return_value={}), patch.object(strategy, "fetch_with_retries", AsyncMock(return_value=(CHALLENGE, 403, None))), patch("bpc_fetch.extract.extract_article") as extract:
            result = await cli._cmd_fetch(args)
        self.assertEqual(result["error"], "HTTP 403")
        self.assertEqual(result["status_code"], 403)
        extract.assert_not_called()
        args.out_dir.mkdir.assert_not_called()


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_headed_pool_launches_visible_engine_with_native_user_agent(self):
        page = SimpleNamespace(close=AsyncMock())
        context = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
        engine = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
        driver = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=engine)), stop=AsyncMock())
        with patch.object(browser, "async_playwright", return_value=SimpleNamespace(start=AsyncMock(return_value=driver))):
            async with browser.BrowserPool(headless=False) as pool:
                async with pool.page() as actual_page:
                    self.assertIs(actual_page, page)
        driver.chromium.launch.assert_awaited_once_with(headless=False)
        engine.new_context.assert_awaited_once_with()

    async def test_ensure_browser_attempts_launch_and_reports_missing_binary(self):
        engine = SimpleNamespace(launch=AsyncMock(side_effect=RuntimeError("Executable doesn't exist: chromium_headless_shell-1243")))
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=SimpleNamespace(chromium=engine))
        manager.__aexit__ = AsyncMock(return_value=False)
        with patch.object(browser, "async_playwright", return_value=manager):
            result = await browser.ensure_browser()
        engine.launch.assert_awaited_once_with(headless=True)
        self.assertFalse(result["ok"])
        self.assertIn("chromium_headless_shell-1243", result["error"])
        self.assertEqual(result["install_cmd"], "playwright install chromium")

    async def test_launch_failure_closes_driver_and_preserves_actionable_error(self):
        driver = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(side_effect=RuntimeError("missing executable"))), stop=AsyncMock())
        manager = SimpleNamespace(start=AsyncMock(return_value=driver))
        with patch.object(browser, "async_playwright", return_value=manager):
            with self.assertRaisesRegex(browser.BrowserLaunchError, "missing executable.*playwright install chromium"):
                await browser.BrowserPool().start()
        driver.stop.assert_awaited_once()

    async def test_browser_api_preserves_non_200_status(self):
        page = SimpleNamespace(route=AsyncMock(), goto=AsyncMock(return_value=SimpleNamespace(status=403)), content=AsyncMock(return_value=CHALLENGE))

        @asynccontextmanager
        async def fake_page():
            yield page

        pool = SimpleNamespace(page=fake_page)
        result = await browser.fetch_with_browser(URL, bloomberg(), pool)
        self.assertEqual(result, (CHALLENGE, 403))
        self.assertIsInstance(page.route.await_args_list[0].args[0], re.Pattern)

    async def test_navigation_failure_is_not_replaced_by_status_200(self):
        page = SimpleNamespace(route=AsyncMock(), goto=AsyncMock(side_effect=RuntimeError("navigation failed")))

        @asynccontextmanager
        async def fake_page():
            yield page

        with self.assertRaisesRegex(RuntimeError, "navigation failed"):
            await browser.fetch_with_browser(URL, bloomberg(), SimpleNamespace(page=fake_page))

    async def test_doctor_reports_actual_launch_failure(self):
        args = SimpleNamespace(sites_js=Path(__file__))
        failure = {"ok": False, "error": "Executable doesn't exist: chromium_headless_shell-1243", "install_cmd": "playwright install chromium"}
        with patch.object(cli, "get_sites_map", return_value={}), patch.object(browser, "ensure_browser", AsyncMock(return_value=failure)) as ensure:
            result = await cli._dispatch(SimpleNamespace(command="doctor", sites_js=args.sites_js))
        ensure.assert_awaited_once()
        self.assertFalse(result["ok"])
        self.assertFalse(result["chromium_installed"])
        self.assertTrue(any("chromium_headless_shell-1243" in issue for issue in result["issues"]))


if __name__ == "__main__":
    unittest.main()

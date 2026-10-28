### Local deployment fixes

This fork maintains fixes for standalone article collection with bpc-fetch. It does not require a Chrome/Edge BPC extension or the user's everyday browser profile.

#### Browser transport

- `fetch --headed` launches the project's own visible Chromium and tries it before HTTP crawler requests. The default CLI mode remains headless.
- Browser contexts use the installed engine's native user agent unless the site's BPC rule specifies a custom one.
- Site-specific blocking expressions retain their regular-expression semantics. Generic provider rules are used only when no site-specific blocking expression is supplied.
- Blocking and DOM reconstruction flags can coexist. Bloomberg's blocking rule is no longer hidden by its DOMPurify capability flag.
- HTTP status codes and access challenges remain failures. A long robot page is never treated as an article.
- Browser startup errors remain visible; navigation failures can still use the archive fallback.

#### Bloomberg extraction

Known article data is extracted before generic DOM text: legacy ArticleBody/FeatureBody JSON and current `__NEXT_DATA__.props.pageProps.story.body.content`. Metadata, paragraphs, headings, lists, quotes and images are retained. Missing, malformed or incomplete bodies fail conservatively; extremely short articles may require further tuning.

#### Validation

On 2026-10-28, a standalone headed fetch of the requested Bloomberg opinion URL completed with 5,063 text characters and one downloaded image. Headless requests received an HTTP 403 robot page in the same environment. This is evidence for that article and environment, not a guarantee for every site or future request.

Run the offline regressions with `python -m unittest discover -s tests -v` after installing the project dependencies. On the author's Windows deployment, the required interpreter is `D:\PyCharmMiscProject\.venv\Scripts\python.exe`.

The original upstream repository remains read-only in this workflow; changes are maintained in Origami111/bpc-fetch.

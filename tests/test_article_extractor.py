"""Phase 3 tests: extraction, metadata, paywall detection, text/HTML input, fetching."""
from __future__ import annotations

import socket
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import pytest
from bs4 import BeautifulSoup

import src.article_extractor as ae
from src.article_extractor import (
    Article,
    ArticleInaccessibleError,
    BlockKind,
    Completeness,
    ExtractionError,
    FetchedPage,
    FetchError,
    HttpFetcher,
    InvalidSourceError,
    NoContentError,
    PaywallError,
    SubstackExtractor,
    blocks_from_html,
    blocks_from_text,
    detect_paywall,
    extract_metadata,
    is_post_url,
    parse_date,
    validate_url,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeFetcher:
    """Returns canned HTML without touching the network."""

    def __init__(self, html: str, final_url: Optional[str] = None) -> None:
        self.html, self.final_url, self.requested = html, final_url, []

    def fetch(self, url: str) -> FetchedPage:
        self.requested.append(url)
        return FetchedPage(url=self.final_url or url, html=self.html)


@pytest.fixture
def extractor() -> SubstackExtractor:
    return SubstackExtractor(fetcher=FakeFetcher(load("substack_free.html")))


def kinds(article: Article) -> list[BlockKind]:
    return [b.kind for b in article.blocks]


# =========================================================== URL validation
class TestValidateUrl:
    @pytest.mark.parametrize("url", [
        "https://appliedai.substack.com/p/understanding-rag",
        "https://www.customdomain.com/p/some-post",
        "  https://appliedai.substack.com/p/x  ",
    ])
    def test_accepts_public_urls(self, url: str) -> None:
        assert validate_url(url).startswith("https://")

    def test_http_is_upgraded_to_https(self) -> None:
        assert validate_url("http://a.substack.com/p/x") == "https://a.substack.com/p/x"

    def test_fragment_is_stripped_and_query_kept(self) -> None:
        assert validate_url("https://a.substack.com/p/x?utm=1#comments") == "https://a.substack.com/p/x?utm=1"

    @pytest.mark.parametrize("url", [
        "", "   ", "not a url", "ftp://a.substack.com/p/x", "file:///etc/passwd",
        "javascript:alert(1)", "https://", "https://user:pw@a.substack.com/p/x",
        "https://localhost/p/x", "https://127.0.0.1/p/x", "https://10.0.0.5/p/x",
        "https://192.168.1.1/p/x", "https://169.254.169.254/latest", "https://[::1]/p/x",
        "https://intranet/p/x", "https://printer.local/p/x", "https://a b.com/p/x",
    ])
    def test_rejects_unsafe_or_malformed(self, url: str) -> None:
        with pytest.raises(InvalidSourceError):
            validate_url(url)

    def test_rejects_non_string(self) -> None:
        with pytest.raises(InvalidSourceError):
            validate_url(None)  # type: ignore[arg-type]

    def test_private_hosts_allowed_only_when_explicit(self) -> None:
        assert validate_url("http://127.0.0.1:8000/p/x", allow_private_hosts=True).startswith("http://")

    @pytest.mark.parametrize("url,expected", [
        ("https://a.substack.com/p/my-post", True),
        ("https://a.substack.com/p/my-post/", True),
        ("https://a.substack.com/home/post/p-123456", True),
        ("https://a.substack.com/", False),
        ("https://a.substack.com/archive", False),
        ("https://a.substack.com/about", False),
    ])
    def test_is_post_url(self, url: str, expected: bool) -> None:
        assert is_post_url(url) is expected


# =========================================================== dates
class TestParseDate:
    @pytest.mark.parametrize("raw,expected", [
        ("2025-03-14T09:30:00.000Z", date(2025, 3, 14)),
        ("2025-03-14T09:30:00+00:00", date(2025, 3, 14)),
        ("2025-03-14", date(2025, 3, 14)),
        ("Mar 14, 2025", date(2025, 3, 14)),
        ("March 14, 2025", date(2025, 3, 14)),
        ("14 March 2025", date(2025, 3, 14)),
        ("Published on 2025-03-14 by someone", date(2025, 3, 14)),
    ])
    def test_valid(self, raw: str, expected: date) -> None:
        assert parse_date(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   ", "yesterday", "2025-13-45", "not a date", 42])
    def test_invalid_returns_none(self, raw: object) -> None:
        assert parse_date(raw) is None  # type: ignore[arg-type]


# =========================================================== metadata
class TestMetadata:
    def test_json_ld_preferred(self) -> None:
        meta = extract_metadata(BeautifulSoup(load("substack_free.html"), "lxml"))
        assert meta.title == "Understanding Retrieval-Augmented Generation"
        assert meta.author == "Priya Raman"
        assert meta.published == date(2025, 3, 14)
        assert meta.site_name == "The Applied AI Letter"
        assert meta.accessible_for_free is True

    def test_json_ld_graph_multiple_authors_and_string_bool(self) -> None:
        meta = extract_metadata(BeautifulSoup(load("substack_custom_domain.html"), "lxml"))
        assert meta.author == "Mei Tanaka, Luis Ortega"
        assert meta.published == date(2025, 1, 20)
        assert meta.accessible_for_free is True

    def test_falls_back_to_meta_tags_and_dom(self) -> None:
        html = ("<html><head><title>Fallback Title - Site</title><meta name='author' content='Ann'>"
                "<meta property='article:published_time' content='2024-05-01'></head><body></body></html>")
        meta = extract_metadata(BeautifulSoup(html, "lxml"))
        assert (meta.title, meta.author, meta.published) == ("Fallback Title", "Ann", date(2024, 5, 1))

    def test_time_tag_and_rel_author_fallback(self) -> None:
        html = ("<html><body><h1>Heading Title</h1><a rel='author'>Bob</a>"
                "<time datetime='2023-02-03'>x</time></body></html>")
        meta = extract_metadata(BeautifulSoup(html, "lxml"))
        assert (meta.title, meta.author, meta.published) == ("Heading Title", "Bob", date(2023, 2, 3))

    def test_missing_everything(self) -> None:
        meta = extract_metadata(BeautifulSoup("<html><body><p>x</p></body></html>", "lxml"))
        assert meta.title is None and meta.author is None and meta.published is None

    def test_malformed_json_ld_is_ignored(self) -> None:
        html = ("<html><head><script type='application/ld+json'>{not json</script>"
                "<meta property='og:title' content='OG Title'></head></html>")
        assert extract_metadata(BeautifulSoup(html, "lxml")).title == "OG Title"

    def test_duplicate_authors_deduplicated(self) -> None:
        html = ("<html><head><script type='application/ld+json'>"
                '{"@type":"Article","headline":"T","author":[{"name":"A"},{"name":"A"}]}'
                "</script></head></html>")
        assert extract_metadata(BeautifulSoup(html, "lxml")).author == "A"


# =========================================================== full free article
class TestFreeArticle:
    @pytest.fixture
    def article(self, extractor: SubstackExtractor) -> Article:
        return extractor.extract_from_url("https://appliedai.substack.com/p/understanding-rag")

    def test_metadata(self, article: Article) -> None:
        assert article.title == "Understanding Retrieval-Augmented Generation"
        assert article.author == "Priya Raman"
        assert article.published == date(2025, 3, 14)
        assert article.url == "https://appliedai.substack.com/p/understanding-rag"
        assert article.source_kind == "url"

    def test_reported_complete_with_primary_extractor(self, article: Article) -> None:
        assert article.completeness is Completeness.COMPLETE and article.is_complete
        assert article.completeness_reasons == []
        assert article.extractor == "trafilatura"
        assert article.word_count > 450

    def test_headings_and_levels(self, article: Article) -> None:
        headings = [(b.level, b.text) for b in article.blocks if b.kind is BlockKind.HEADING]
        assert (2, "Why retrieval matters") in headings
        assert (2, "How the pipeline works") in headings
        assert (3, "A minimal example") in headings
        assert (3, "Choosing a chunk size") in headings
        assert (2, "Conclusion") in headings

    def test_title_heading_not_duplicated(self, article: Article) -> None:
        assert all(b.text != article.title for b in article.blocks if b.kind is BlockKind.HEADING)

    def test_bullet_list_with_nesting(self, article: Article) -> None:
        bullet = next(b for b in article.blocks if b.kind is BlockKind.LIST and not b.ordered)
        assert not bullet.ordered
        assert [i.depth for i in bullet.items] == [0, 0, 1, 0]
        assert bullet.items[2].text.startswith("New documents are searchable")

    def test_ordered_list(self, article: Article) -> None:
        ordered = next(b for b in article.blocks if b.kind is BlockKind.LIST and b.ordered)
        assert len(ordered.items) == 4
        assert ordered.items[0].text.startswith("Split the documents")

    def test_code_block_preserves_language_and_indentation(self, article: Article) -> None:
        code = next(b for b in article.blocks if b.kind is BlockKind.CODE)
        assert code.language == "python"
        assert code.text.startswith("def retrieve(question_vec, chunks, k=3):\n    scored = ")
        assert "\n    return [c for _, c in scored[:k]]" in code.text

    def test_table(self, article: Article) -> None:
        table = next(b for b in article.blocks if b.kind is BlockKind.TABLE)
        assert table.has_header
        assert table.rows[0] == ("Chunk size", "Strength", "Weakness")
        assert table.rows[1] == ("Small (100 words)", "Precise matches", "Loses context")
        assert len(table.rows) == 4

    def test_blockquote(self, article: Article) -> None:
        quote = next(b for b in article.blocks if b.kind is BlockKind.QUOTE)
        assert quote.text == "Measure retrieval quality before you tune the prompt."

    @pytest.mark.parametrize("junk", [
        "Subscribe", "Type your email", "Privacy", "Collection notice", "Great post, thank you",
        "Archive", "Comments", "RAG pipeline diagram", "retrieval and generation stages", "Like",
    ])
    def test_boilerplate_removed(self, article: Article, junk: str) -> None:
        assert junk not in article.text

    def test_footnote_marker_not_glued_to_sentence(self, article: Article) -> None:
        assert "answer using them.1" not in article.text
        assert "answer using them." in article.text

    def test_footnote_body_kept(self, article: Article) -> None:
        assert "2020 research paper from Facebook AI Research" in article.text

    def test_key_facts_preserved(self, article: Article) -> None:
        for phrase in ["Retrieval-Augmented Generation", "cosine similarity", "embedding",
                       "knowledge cutoff", "re-ranking step"]:
            assert phrase in article.text

    def test_no_warnings_about_substack_or_metadata(self, article: Article) -> None:
        assert article.warnings == []


# =========================================================== custom domain
class TestCustomDomain:
    def test_extracts_from_custom_domain(self) -> None:
        fetcher = FakeFetcher(load("substack_custom_domain.html"))
        article = SubstackExtractor(fetcher).extract_from_url("https://www.edgenotes.io/p/small-models")
        assert fetcher.requested == ["https://www.edgenotes.io/p/small-models"]
        assert article.title == "Why Small Models Win at the Edge"
        assert article.author == "Mei Tanaka, Luis Ortega"
        assert article.is_complete
        assert any(b.kind is BlockKind.LIST and len(b.items) == 4 for b in article.blocks)

    def test_final_url_after_redirect_is_recorded(self) -> None:
        fetcher = FakeFetcher(load("substack_custom_domain.html"), final_url="https://edge.example.org/p/final")
        article = SubstackExtractor(fetcher).extract_from_url("https://old.example.org/p/start")
        assert article.url == "https://edge.example.org/p/final"

    def test_non_substack_page_gets_a_warning(self) -> None:
        html = "<html><head><title>T</title></head><body><article>" + "<p>" + "word " * 200 + ".</p></article></body></html>"
        article = SubstackExtractor(FakeFetcher(html)).extract_from_url("https://blog.example.com/p/post")
        assert any("does not look like a Substack" in w for w in article.warnings)


# =========================================================== paywall / truncation
class TestPaywall:
    @pytest.fixture
    def article(self) -> Article:
        return SubstackExtractor(FakeFetcher(load("substack_paywalled.html"))).extract_from_url(
            "https://infra.substack.com/p/hidden-costs"
        )

    def test_flagged_partial_never_complete(self, article: Article) -> None:
        assert article.completeness is Completeness.PARTIAL
        assert not article.is_complete

    def test_reason_explains_paywall(self, article: Article) -> None:
        assert any("Paywall detected" in r for r in article.completeness_reasons)

    def test_only_the_public_preview_is_extracted(self, article: Article) -> None:
        assert "Cost one: memory, not disk" in article.text
        assert "Cost two: re-embedding" in article.text
        assert "I will walk through the numbers" in article.text

    def test_paywall_prompt_is_not_part_of_the_content(self, article: Article) -> None:
        assert "paid subscribers" not in article.text
        assert "Sign in" not in article.text

    def test_metadata_still_available(self, article: Article) -> None:
        assert article.title == "The Hidden Costs of Vector Databases"
        assert article.author == "Daniel Okafor"

    def test_paywall_with_no_preview_raises(self) -> None:
        html = ("<html><head><meta property='og:title' content='Locked'></head><body>"
                "<div class='paywall'><h2>This post is for paid subscribers</h2></div></body></html>")
        with pytest.raises(PaywallError):
            SubstackExtractor(FakeFetcher(html)).extract_from_url("https://x.substack.com/p/locked")

    def test_paywall_error_is_an_inaccessible_error(self) -> None:
        assert issubclass(PaywallError, ArticleInaccessibleError)
        assert issubclass(ArticleInaccessibleError, ExtractionError)

    def test_detect_paywall_structural_and_phrase(self) -> None:
        assert detect_paywall(BeautifulSoup("<div class='paywall-cta'>x</div>", "lxml"))
        assert detect_paywall(BeautifulSoup("<div data-component-name='PaywallToDOM'></div>", "lxml"))
        assert detect_paywall(BeautifulSoup("<h2>Subscribe to continue reading</h2>", "lxml"))
        assert detect_paywall(BeautifulSoup("<a>Keep reading with a 7-day free trial</a>", "lxml"))
        assert detect_paywall(BeautifulSoup("<p>hello</p>", "lxml")) == []

    def test_article_that_merely_discusses_paywalls_is_not_flagged(self) -> None:
        prose = ("In this essay I argue about media economics. " * 12 +
                 "Many sites show the line 'This post is for paid subscribers' in the middle of a long "
                 "paragraph of analysis like this one, which is prose and not a call to action. " * 2)
        html = f"<html><body><article><h1>Media</h1><p>{prose}</p><p>{'More analysis here. ' * 40}</p></article></body></html>"
        article = SubstackExtractor().extract_from_html(html)
        assert article.is_complete

    def test_short_post_is_partial_with_reason(self) -> None:
        article = SubstackExtractor(FakeFetcher(load("short_post.html"))).extract_from_url(
            "https://sam.substack.com/p/quick-note"
        )
        assert article.completeness is Completeness.PARTIAL
        assert any("may be truncated or very short" in r for r in article.completeness_reasons)

    def test_unfinished_last_paragraph_warns(self) -> None:
        html = ("<html><body><article>" + "<p>" + "A complete sentence follows another one. " * 40 + "</p>"
                "<p>This paragraph stops in the middle of a</p></article></body></html>")
        article = SubstackExtractor().extract_from_html(html)
        assert any("does not end with punctuation" in w for w in article.warnings)


# =========================================================== inaccessible / bad pages
class TestInaccessible:
    def test_login_wall(self) -> None:
        with pytest.raises(ArticleInaccessibleError, match="sign"):
            SubstackExtractor(FakeFetcher(load("login_wall.html"))).extract_from_url("https://x.substack.com/p/a")

    def test_empty_page_raises_no_content(self) -> None:
        with pytest.raises(NoContentError):
            SubstackExtractor().extract_from_html("<html><body></body></html>")

    def test_page_with_only_navigation(self) -> None:
        with pytest.raises(NoContentError):
            SubstackExtractor().extract_from_html("<html><body><nav>Home About</nav><footer>(c)</footer></body></html>")

    @pytest.mark.parametrize("bad", ["", "   \n", None])
    def test_empty_inputs(self, bad: Optional[str]) -> None:
        ex = SubstackExtractor()
        with pytest.raises(InvalidSourceError):
            ex.extract(bad)  # type: ignore[arg-type]
        with pytest.raises(InvalidSourceError):
            ex.extract_from_html(bad)  # type: ignore[arg-type]
        with pytest.raises(InvalidSourceError):
            ex.extract_from_text(bad)  # type: ignore[arg-type]

    def test_non_post_url_rejected_before_any_fetch(self) -> None:
        fetcher = FakeFetcher("<html></html>")
        with pytest.raises(InvalidSourceError, match="/p/"):
            SubstackExtractor(fetcher).extract_from_url("https://a.substack.com/archive")
        assert fetcher.requested == []

    def test_invalid_url_rejected_before_any_fetch(self) -> None:
        fetcher = FakeFetcher("<html></html>")
        with pytest.raises(InvalidSourceError):
            SubstackExtractor(fetcher).extract_from_url("http://localhost/p/x")
        assert fetcher.requested == []

    def test_fetch_errors_propagate(self) -> None:
        class Boom:
            def fetch(self, url: str) -> FetchedPage:
                raise FetchError("down")

        with pytest.raises(FetchError, match="down"):
            SubstackExtractor(Boom()).extract_from_url("https://a.substack.com/p/x")

    def test_malformed_html_still_extracts(self) -> None:
        html = ("<html><body><article><h2>Broken <b>markup<p>" + "Some real article text here. " * 30 +
                "<ul><li>one<li>two</article>")
        article = SubstackExtractor().extract_from_html(html)
        assert article.word_count > 100

    def test_unicode_is_preserved(self) -> None:
        html = ("<html><head><meta property='og:title' content='తెలుగు శీర్షిక'></head><body><article><p>"
                + "తెలుగు వచనం మరియు emoji 🚀 café naïve. " * 25 + "</p></article></body></html>")
        article = SubstackExtractor().extract_from_html(html)
        assert article.title == "తెలుగు శీర్షిక"
        assert "🚀" in article.text and "café" in article.text and "తెలుగు" in article.text


# =========================================================== extractor fallback behaviour
class TestFallback:
    def test_readability_used_when_trafilatura_returns_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ae.trafilatura, "extract", lambda *a, **k: None)
        article = SubstackExtractor().extract_from_html(load("substack_free.html"))
        assert article.extractor == "readability"
        assert "cosine similarity" in article.text
        assert article.word_count > 100

    def test_readability_used_when_trafilatura_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*a: object, **k: object) -> None:
            raise RuntimeError("parser exploded")

        monkeypatch.setattr(ae.trafilatura, "extract", boom)
        article = SubstackExtractor().extract_from_html(load("substack_custom_domain.html"))
        assert article.extractor == "readability"

    def test_fallback_when_trafilatura_keeps_too_little(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tiny = "<doc><main><p>" + "short " * 45 + "</p></main></doc>"  # 45 words, above the minimum
        monkeypatch.setattr(ae.trafilatura, "extract", lambda *a, **k: tiny)
        article = SubstackExtractor().extract_from_html(load("substack_free.html"))
        assert article.extractor == "readability"
        assert article.word_count > 300

    def test_primary_kept_when_fallback_is_no_better(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ae, "Document", lambda html: (_ for _ in ()).throw(ValueError("unparseable")))
        monkeypatch.setattr(ae, "_words", ae._words)
        tiny = "<doc><main><p>" + "keep " * 60 + "</p></main></doc>"
        monkeypatch.setattr(ae.trafilatura, "extract", lambda *a, **k: tiny)
        article = SubstackExtractor().extract_from_html(load("substack_free.html"))
        assert article.extractor == "trafilatura"
        assert any("some content may be missing" in r for r in article.completeness_reasons)
        assert article.completeness is Completeness.PARTIAL

    def test_both_extractors_failing_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ae.trafilatura, "extract", lambda *a, **k: None)
        monkeypatch.setattr(ae, "Document", lambda html: (_ for _ in ()).throw(ValueError("x")))
        with pytest.raises(NoContentError):
            SubstackExtractor().extract_from_html(load("substack_free.html"))


# =========================================================== input dispatch + user HTML
class TestUserHtml:
    def test_user_html_is_extracted_and_labelled(self) -> None:
        article = SubstackExtractor().extract_from_html(load("substack_free.html"), url="https://a.substack.com/p/x")
        assert article.source_kind == "user_html"
        assert article.url == "https://a.substack.com/p/x"
        assert any("user-supplied HTML" in w for w in article.warnings)
        assert article.title == "Understanding Retrieval-Augmented Generation"

    def test_user_html_of_paywalled_page_is_still_partial(self) -> None:
        article = SubstackExtractor().extract_from_html(load("substack_paywalled.html"))
        assert article.completeness is Completeness.PARTIAL

    def test_dispatch_url(self) -> None:
        article = SubstackExtractor(FakeFetcher(load("substack_free.html"))).extract("https://a.substack.com/p/x")
        assert article.source_kind == "url"

    def test_dispatch_html(self) -> None:
        assert SubstackExtractor().extract(load("substack_free.html")).source_kind == "user_html"

    def test_dispatch_text(self) -> None:
        assert SubstackExtractor().extract(load("user_notes.txt")).source_kind == "user_text"


# =========================================================== plain text
class TestPlainText:
    @pytest.fixture
    def article(self) -> Article:
        return SubstackExtractor().extract_from_text(load("user_notes.txt"), author="Me", published=date(2025, 1, 1))

    def test_title_taken_from_leading_heading(self, article: Article) -> None:
        assert article.title == "Gradient Descent Basics"
        assert all(b.text != article.title for b in article.blocks if b.kind is BlockKind.HEADING)

    def test_completeness_is_unverified_not_complete(self, article: Article) -> None:
        assert article.completeness is Completeness.UNVERIFIED
        assert not article.is_complete
        assert article.extractor == "text" and article.source_kind == "user_text"

    def test_structure(self, article: Article) -> None:
        assert kinds(article) == [
            BlockKind.PARAGRAPH, BlockKind.HEADING, BlockKind.LIST, BlockKind.LIST,
            BlockKind.CODE, BlockKind.TABLE, BlockKind.QUOTE, BlockKind.PARAGRAPH,
        ]

    def test_soft_wrapped_lines_are_joined(self, article: Article) -> None:
        assert article.blocks[0].text.startswith("Gradient descent is an optimization algorithm that repeatedly")
        assert "\n" not in article.blocks[0].text
        assert article.blocks[-1].text == "A final paragraph that wraps across two lines in the source text."

    def test_lists(self, article: Article) -> None:
        bullets, numbered = article.blocks[2], article.blocks[3]
        assert not bullets.ordered and numbered.ordered
        assert [i.depth for i in bullets.items] == [0, 0, 1, 1]
        assert len(numbered.items) == 3

    def test_code_and_table(self, article: Article) -> None:
        code = next(b for b in article.blocks if b.kind is BlockKind.CODE)
        assert code.language == "python" and "    grad = compute_grad(params)" in code.text
        table = next(b for b in article.blocks if b.kind is BlockKind.TABLE)
        assert table.rows == (("Term", "Meaning"), ("Epoch", "One pass over the data"), ("Batch", "A subset used per update"))

    def test_supplied_metadata_is_used(self, article: Article) -> None:
        assert article.author == "Me" and article.published == date(2025, 1, 1)
        assert not any("Author not supplied" in w for w in article.warnings)

    def test_missing_metadata_warns(self) -> None:
        article = SubstackExtractor().extract_from_text("# T\n\nBody paragraph.")
        assert any("Author not supplied" in w for w in article.warnings)
        assert any("date not supplied" in w for w in article.warnings)

    def test_explicit_title_overrides_and_keeps_heading(self) -> None:
        article = SubstackExtractor().extract_from_text("# Heading\n\nBody.", title="Given Title")
        assert article.title == "Given Title"
        assert article.blocks[0].kind is BlockKind.HEADING

    def test_untitled_fallback_warns(self) -> None:
        article = SubstackExtractor().extract_from_text("Just a paragraph with no title at all.")
        assert article.title == "Untitled Article"
        assert any("Untitled" in w for w in article.warnings)

    def test_only_title_raises(self) -> None:
        with pytest.raises(NoContentError):
            SubstackExtractor().extract_from_text("# Only A Title")

    def test_unclosed_code_fence_is_tolerated(self) -> None:
        blocks = blocks_from_text("Intro.\n\n```js\nconsole.log(1)\n")
        assert blocks[-1].kind is BlockKind.CODE and blocks[-1].text == "console.log(1)"

    def test_windows_line_endings(self) -> None:
        blocks = blocks_from_text("# H\r\n\r\npara one\r\nstill one\r\n\r\n- a\r\n- b\r\n")
        assert [b.kind for b in blocks] == [BlockKind.HEADING, BlockKind.PARAGRAPH, BlockKind.LIST]
        assert blocks[1].text == "para one still one"


# =========================================================== html block parsing
class TestHtmlBlocks:
    def test_table_without_thead_and_header_detection(self) -> None:
        blocks = blocks_from_html("<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>")
        assert blocks[0].has_header and blocks[0].rows == (("A", "B"), ("1", "2"))
        plain = blocks_from_html("<table><tr><td>1</td><td>2</td></tr></table>")
        assert not plain[0].has_header

    def test_nested_lists(self) -> None:
        blocks = blocks_from_html("<ol><li>a<ul><li>b</li></ul></li><li>c</li></ol>")
        assert blocks[0].ordered
        assert [(i.text, i.depth) for i in blocks[0].items] == [("a", 0), ("b", 1), ("c", 0)]

    def test_wrapper_divs_are_traversed_and_scripts_ignored(self) -> None:
        blocks = blocks_from_html("<div><div><p>Hi there</p><script>x=1</script></div></div>")
        assert [(b.kind, b.text) for b in blocks] == [(BlockKind.PARAGRAPH, "Hi there")]

    def test_pre_without_code_tag(self) -> None:
        blocks = blocks_from_html("<pre>line1\n  line2\n</pre>")
        assert blocks[0].kind is BlockKind.CODE and blocks[0].text == "line1\n  line2"


# =========================================================== HttpFetcher (fake session)
class FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"<html></html>", headers: Optional[dict] = None) -> None:
        self.status_code = status
        self._body = body
        self.headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}
        self.closed = False

    @property
    def is_redirect(self) -> bool:
        return self.status_code in (301, 302, 303, 307, 308)

    def iter_content(self, chunk_size: int = 1024):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, *responses: FakeResponse) -> None:
        self.responses, self.calls = list(responses), []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


@pytest.fixture(autouse=False)
def no_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ae, "_ensure_public_host", lambda host: None)


@pytest.mark.usefixtures("no_dns")
class TestHttpFetcherFake:
    def fetch(self, *responses: FakeResponse, **kw: object) -> FetchedPage:
        return HttpFetcher(session=FakeSession(*responses), **kw).fetch("https://a.substack.com/p/x")  # type: ignore[arg-type]

    def test_success_and_request_hygiene(self) -> None:
        session = FakeSession(FakeResponse(body="héllo".encode()))
        page = HttpFetcher(session=session).fetch("https://a.substack.com/p/x")  # type: ignore[arg-type]
        url, kwargs = session.calls[0]
        assert page.html == "héllo" and page.status == 200
        assert kwargs["allow_redirects"] is False
        assert set(kwargs["headers"]) == {"User-Agent", "Accept"}  # no cookies / auth headers
        assert "cookie" not in {k.lower() for k in kwargs["headers"]}

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_required(self, status: int) -> None:
        with pytest.raises(ArticleInaccessibleError):
            self.fetch(FakeResponse(status))

    @pytest.mark.parametrize("status,match", [(404, "not found"), (410, "not found"), (429, "Rate limited"), (500, "HTTP 500"), (503, "HTTP 503")])
    def test_error_statuses(self, status: int, match: str) -> None:
        with pytest.raises(FetchError, match=match):
            self.fetch(FakeResponse(status))

    def test_redirect_is_followed(self) -> None:
        page = self.fetch(FakeResponse(301, headers={"Location": "/p/final"}), FakeResponse(body=b"ok"))
        assert page.url == "https://a.substack.com/p/final"

    def test_redirect_to_sign_in_is_inaccessible(self) -> None:
        with pytest.raises(ArticleInaccessibleError, match="sign-in"):
            self.fetch(FakeResponse(302, headers={"Location": "https://a.substack.com/sign-in?redirect=/p/x"}))

    def test_redirect_to_private_host_is_blocked(self) -> None:
        with pytest.raises(InvalidSourceError):
            self.fetch(FakeResponse(302, headers={"Location": "http://169.254.169.254/latest/meta-data"}))

    def test_redirect_loop(self) -> None:
        loop = [FakeResponse(302, headers={"Location": "/p/y"}) for _ in range(10)]
        with pytest.raises(FetchError, match="Too many redirects"):
            self.fetch(*loop, max_redirects=3)

    def test_non_html_rejected(self) -> None:
        with pytest.raises(FetchError, match="HTML"):
            self.fetch(FakeResponse(headers={"Content-Type": "application/pdf"}))

    def test_oversized_response_rejected(self) -> None:
        with pytest.raises(FetchError, match="larger than"):
            self.fetch(FakeResponse(body=b"x" * 5000), max_bytes=1000)

    def test_unknown_charset_falls_back_to_utf8(self) -> None:
        page = self.fetch(FakeResponse(body=b"ok", headers={"Content-Type": "text/html; charset=bogus-9"}))
        assert page.html == "ok"

    def test_network_error_is_wrapped(self) -> None:
        import requests

        class Broken:
            def get(self, *a: object, **k: object) -> None:
                raise requests.ConnectionError("no route")

        with pytest.raises(FetchError, match="Network error"):
            HttpFetcher(session=Broken()).fetch("https://a.substack.com/p/x")  # type: ignore[arg-type]

    def test_responses_are_closed(self) -> None:
        resp = FakeResponse()
        self.fetch(resp)
        assert resp.closed


class TestPublicHostCheck:
    def test_private_resolution_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 0))])
        with pytest.raises(InvalidSourceError, match="non-public"):
            ae._ensure_public_host("evil.example.com")

    def test_public_resolution_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
        ae._ensure_public_host("example.com")

    def test_unresolvable_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(*a: object, **k: object) -> None:
            raise socket.gaierror("nope")

        monkeypatch.setattr(socket, "getaddrinfo", fail)
        with pytest.raises(FetchError, match="resolve"):
            ae._ensure_public_host("nonexistent.invalid")

    def test_default_fetcher_refuses_loopback_literal(self) -> None:
        with pytest.raises(InvalidSourceError):
            HttpFetcher().fetch("http://127.0.0.1:9/p/x")


# =========================================================== HttpFetcher against a real local server
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        routes = {
            "/p/ok": (200, "text/html; charset=utf-8", load("substack_free.html").encode()),
            "/p/forbidden": (403, "text/html", b"nope"),
            "/p/missing": (404, "text/html", b"gone"),
            "/p/json": (200, "application/json", b"{}"),
        }
        status, ctype, body = routes.get(self.path, (404, "text/html", b""))
        if self.path == "/p/moved":
            self.send_response(302)
            self.send_header("Location", "/p/ok")
            self.end_headers()
            return
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence test output
        pass


@pytest.fixture(scope="module")
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


class TestHttpFetcherRealServer:
    def fetcher(self) -> HttpFetcher:
        return HttpFetcher(timeout=5, allow_private_hosts=True)

    def test_end_to_end_extraction_over_http(self, server: str) -> None:
        # extract_from_url applies the public-host rule, so drive the fetcher + pipeline directly.
        page = self.fetcher().fetch(f"{server}/p/ok")
        article = SubstackExtractor()._from_html(page.html, page.url, "url")
        assert article.is_complete and article.title.startswith("Understanding")

    def test_redirect_followed(self, server: str) -> None:
        assert self.fetcher().fetch(f"{server}/p/moved").url.endswith("/p/ok")

    def test_403(self, server: str) -> None:
        with pytest.raises(ArticleInaccessibleError):
            self.fetcher().fetch(f"{server}/p/forbidden")

    def test_404(self, server: str) -> None:
        with pytest.raises(FetchError, match="not found"):
            self.fetcher().fetch(f"{server}/p/missing")

    def test_json_rejected(self, server: str) -> None:
        with pytest.raises(FetchError, match="HTML"):
            self.fetcher().fetch(f"{server}/p/json")

    def test_connection_refused(self) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with pytest.raises(FetchError, match="Network error"):
            self.fetcher().fetch(f"http://127.0.0.1:{port}/p/x")

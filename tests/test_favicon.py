import base64

import httpx

from rss_ticker import favicon as favicon_module
from rss_ticker.favicon import MAX_FAVICON_BYTES, refresh_favicons, resolve_favicon
from rss_ticker.store import Store

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
SMALL_PNG = PNG_MAGIC + b"fake-png-bytes"


def transport(handler):
    return httpx.MockTransport(handler)


async def call(handler, feed_url="https://x.example/rss"):
    async with httpx.AsyncClient(transport=transport(handler)) as client:
        return await resolve_favicon(client, feed_url)


def _b64_roundtrip(data_uri: str, expected: bytes, mime: str) -> None:
    assert data_uri.startswith(f"data:{mime};base64,")
    encoded = data_uri.split(",", 1)[1]
    assert base64.b64decode(encoded) == expected


async def test_favicon_ico_png_resolves_directly():
    def handler(request):
        assert str(request.url) == "https://x.example/favicon.ico"
        return httpx.Response(
            200, content=SMALL_PNG, headers={"Content-Type": "image/png"}
        )

    out = await call(handler)
    assert out is not None
    _b64_roundtrip(out, SMALL_PNG, "image/png")


async def test_favicon_ico_404_falls_back_to_homepage_relative_link():
    def handler(request):
        url = str(request.url)
        if url == "https://x.example/favicon.ico":
            return httpx.Response(404)
        if url == "https://x.example/":
            return httpx.Response(
                200,
                content=b'<html><head><link rel="shortcut icon" '
                b'href="/static/fav.png"></head></html>',
                headers={"Content-Type": "text/html"},
            )
        if url == "https://x.example/static/fav.png":
            return httpx.Response(
                200, content=SMALL_PNG, headers={"Content-Type": "image/png"}
            )
        raise AssertionError(f"unexpected request: {url}")

    out = await call(handler)
    assert out is not None
    _b64_roundtrip(out, SMALL_PNG, "image/png")


async def test_favicon_ico_404_falls_back_to_homepage_absolute_link():
    def handler(request):
        url = str(request.url)
        if url == "https://x.example/favicon.ico":
            return httpx.Response(404)
        if url == "https://x.example/":
            return httpx.Response(
                200,
                content=b'<html><head><link rel="icon" '
                b'href="https://cdn.example/icons/fav.png"></head></html>',
                headers={"Content-Type": "text/html"},
            )
        if url == "https://cdn.example/icons/fav.png":
            return httpx.Response(
                200, content=SMALL_PNG, headers={"Content-Type": "image/png"}
            )
        raise AssertionError(f"unexpected request: {url}")

    out = await call(handler)
    assert out is not None
    _b64_roundtrip(out, SMALL_PNG, "image/png")


async def test_apple_touch_icon_is_accepted_when_only_icon_link():
    def handler(request):
        url = str(request.url)
        if url == "https://x.example/favicon.ico":
            return httpx.Response(404)
        if url == "https://x.example/":
            return httpx.Response(
                200,
                content=b'<html><head><link rel="apple-touch-icon" '
                b'href="/apple-touch-icon.png"></head></html>',
                headers={"Content-Type": "text/html"},
            )
        if url == "https://x.example/apple-touch-icon.png":
            return httpx.Response(
                200, content=SMALL_PNG, headers={"Content-Type": "image/png"}
            )
        raise AssertionError(f"unexpected request: {url}")

    out = await call(handler)
    assert out is not None
    _b64_roundtrip(out, SMALL_PNG, "image/png")


async def test_favicon_larger_than_cap_is_none():
    oversized = PNG_MAGIC + b"x" * MAX_FAVICON_BYTES

    def handler(request):
        return httpx.Response(
            200, content=oversized, headers={"Content-Type": "image/png"}
        )

    out = await call(handler)
    assert out is None


async def test_html_error_page_served_as_200_is_none():
    def handler(request):
        url = str(request.url)
        if url == "https://x.example/favicon.ico":
            return httpx.Response(
                200, content=b"<html>not found</html>", headers={"Content-Type": "text/html"}
            )
        if url == "https://x.example/":
            return httpx.Response(
                200, content=b"<html><head></head></html>", headers={"Content-Type": "text/html"}
            )
        raise AssertionError(f"unexpected request: {url}")

    out = await call(handler)
    assert out is None


async def test_network_error_never_raises():
    def handler(request):
        raise httpx.ConnectError("refused")

    out = await call(handler)
    assert out is None


async def test_no_host_is_none():
    out = await call(lambda r: httpx.Response(200), feed_url="not a url")
    assert out is None


async def test_svg_favicon_resolves():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="1"/></svg>'

    def handler(request):
        return httpx.Response(200, content=svg, headers={"Content-Type": "image/svg+xml"})

    out = await call(handler)
    assert out is not None
    _b64_roundtrip(out, svg, "image/svg+xml")


def _store():
    s = Store(":memory:")
    s.upsert_user("art", None)
    return s


def _unused_client() -> httpx.AsyncClient:
    # resolve_favicon itself is monkeypatched in every test below, so this
    # client's transport is never actually asked to do anything -- it only
    # needs to exist to satisfy refresh_favicons's signature.
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))


async def test_refresh_favicons_updates_a_changed_icon(monkeypatch):
    store = _store()
    try:
        fid = store.upsert_feed("https://a.example/rss", now=0)
        store.set_feed_favicon(fid, "data:image/png;base64,OLD")

        async def spy(client, feed_url):
            return "data:image/png;base64,NEW"

        monkeypatch.setattr(favicon_module, "resolve_favicon", spy)

        async with _unused_client() as client:
            await refresh_favicons(store, client)

        assert store.get_feed(fid).favicon == "data:image/png;base64,NEW"
    finally:
        store.close()


async def test_refresh_favicons_keeps_the_old_icon_on_a_failed_recheck(monkeypatch):
    # The crux of this change: a re-check that fails must never null out a
    # favicon that was already good.
    store = _store()
    try:
        fid = store.upsert_feed("https://a.example/rss", now=0)
        store.set_feed_favicon(fid, "data:image/png;base64,OLD")

        async def spy(client, feed_url):
            return None

        monkeypatch.setattr(favicon_module, "resolve_favicon", spy)

        async with _unused_client() as client:
            await refresh_favicons(store, client)

        assert store.get_feed(fid).favicon == "data:image/png;base64,OLD"
    finally:
        store.close()


async def test_refresh_favicons_resolves_a_feed_that_had_none(monkeypatch):
    store = _store()
    try:
        fid = store.upsert_feed("https://a.example/rss", now=0)
        assert store.get_feed(fid).favicon is None

        async def spy(client, feed_url):
            return "data:image/png;base64,Zm9v"

        monkeypatch.setattr(favicon_module, "resolve_favicon", spy)

        async with _unused_client() as client:
            await refresh_favicons(store, client)

        assert store.get_feed(fid).favicon == "data:image/png;base64,Zm9v"
    finally:
        store.close()


async def test_refresh_favicons_checks_every_feed_not_just_unresolved_ones(monkeypatch):
    store = _store()
    try:
        with_icon = store.upsert_feed("https://a.example/rss", now=0)
        without_icon = store.upsert_feed("https://b.example/rss", now=0)
        store.set_feed_favicon(with_icon, "data:image/png;base64,existing")

        calls = []

        async def spy(client, feed_url):
            calls.append(feed_url)
            return "data:image/png;base64,Zm9v"

        monkeypatch.setattr(favicon_module, "resolve_favicon", spy)

        async with _unused_client() as client:
            await refresh_favicons(store, client)

        assert sorted(calls) == ["https://a.example/rss", "https://b.example/rss"]
        assert store.get_feed(with_icon).favicon == "data:image/png;base64,Zm9v"
        assert store.get_feed(without_icon).favicon == "data:image/png;base64,Zm9v"
    finally:
        store.close()


async def test_refresh_favicons_no_feeds_is_a_no_op(monkeypatch):
    store = _store()
    try:
        calls = []

        async def spy(client, feed_url):
            calls.append(feed_url)
            return "data:image/png;base64,Zm9v"

        monkeypatch.setattr(favicon_module, "resolve_favicon", spy)

        async with _unused_client() as client:
            await refresh_favicons(store, client)

        assert calls == []
    finally:
        store.close()


async def test_refresh_favicons_one_feeds_failure_does_not_stop_the_others(monkeypatch):
    store = _store()
    try:
        bad = store.upsert_feed("https://bad.example/rss", now=0)
        good = store.upsert_feed("https://good.example/rss", now=0)

        async def spy(client, feed_url):
            if feed_url == "https://bad.example/rss":
                raise RuntimeError("favicon boom")
            return "data:image/png;base64,Zm9v"

        monkeypatch.setattr(favicon_module, "resolve_favicon", spy)

        async with _unused_client() as client:
            # Must not raise even though one of the two feeds' resolve blows up.
            await refresh_favicons(store, client)

        assert store.get_feed(bad).favicon is None
        assert store.get_feed(good).favicon == "data:image/png;base64,Zm9v"
    finally:
        store.close()

import pytest
from fastapi.testclient import TestClient

from rss_ticker import api as api_mod
from rss_ticker.api import (
    DEGRADED_AFTER_FAILURES,
    admin_key_ok,
    bearer_token,
    create_app,
    redact_feed_url,
    secret_ok,
    token_ok,
)
from rss_ticker.broadcast import Broadcaster
from rss_ticker.config import Config, UserConfig
from rss_ticker.store import NewArticle, Store

TOKEN = "tkn-" + "0123456789abcdef" * 3
# Configured even though tailscale_auth is off below, so that a header-honoring
# regression in identity_user has an actual mapping to (wrongly) match against
# instead of failing for the unrelated reason that no such mapping exists.
LOGIN = "you@github"
CFG = Config(
    public_base_url="http://nas.local:8088",
    admin_key="s3cret",
    manifest_key="mk",
    users=(UserConfig(id="art", tailscale_login=LOGIN),),
)
AUTH = {"user": "art", "token": TOKEN}
BEARER = {"Authorization": f"Bearer {TOKEN}"}
ADMIN = {"X-Admin-Key": "s3cret"}


@pytest.fixture
def store():
    s = Store(":memory:")
    s.upsert_user("art", "Art", token=TOKEN)
    yield s
    s.close()


@pytest.fixture
def client(store):
    return TestClient(create_app(CFG, store, Broadcaster(store)))


def seed(store, n=3):
    fid = store.upsert_feed("https://x.example/rss", name="X", now=0)
    store.subscribe("art", fid)
    store.insert_articles(
        fid,
        [NewArticle(f"g{i}", f"headline {i}", "https://l", None, 1000 + i) for i in range(n)],
        now=1000,
    )
    return fid


def test_root_returns_service_info(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "rss-ticker" in r.json()["service"]


def test_news_returns_newest_first(client, store):
    seed(store, 3)
    body = client.get("/api/news", params=AUTH).json()
    assert [a["title"] for a in body["articles"]] == ["headline 2", "headline 1", "headline 0"]


def test_news_paging_uses_cursor(client, store):
    seed(store, 5)
    first = client.get("/api/news", params={**AUTH, "limit": 2}).json()
    assert first["next_cursor"]
    second = client.get(
        "/api/news", params={**AUTH, "limit": 2, "before": first["next_cursor"]}
    ).json()
    assert not ({a["id"] for a in first["articles"]} & {a["id"] for a in second["articles"]})


def test_news_after_cursor_pages_forward_oldest_first(client, store):
    # What the widget does on reconnect: ask for everything newer than the
    # newest article it holds, following next_cursor until it is exhausted.
    seed(store, 5)
    backlog = client.get("/api/news", params=AUTH).json()["articles"]
    held = backlog[-1]  # oldest; everything else counts as the gap

    first = client.get(
        "/api/news", params={**AUTH, "limit": 2, "after": held["cursor"]}
    ).json()
    assert [a["title"] for a in first["articles"]] == ["headline 1", "headline 2"]
    assert first["next_cursor"], "next_cursor must be returned while more remain"

    second = client.get(
        "/api/news",
        params={**AUTH, "limit": 2, "after": first["next_cursor"]},
    ).json()
    assert [a["title"] for a in second["articles"]] == ["headline 3", "headline 4"]
    assert second["next_cursor"] is None

    collected = [a["id"] for a in first["articles"] + second["articles"]]
    assert len(set(collected)) == 4
    assert held["id"] not in collected


def test_news_after_and_before_together_is_400(client, store):
    seed(store, 2)
    cursor = client.get("/api/news", params=AUTH).json()["articles"][0][
        "cursor"
    ]
    r = client.get(
        "/api/news", params={**AUTH, "before": cursor, "after": cursor}
    )
    assert r.status_code == 400


def test_news_requires_user(client):
    assert client.get("/api/news").status_code == 422


def test_unknown_user_and_bad_token_are_indistinguishable(client, store):
    # Was test_news_unknown_user_is_400. A 400 here is a user-id oracle, and
    # enumerating user ids is step 1 of the chain this work exists to break.
    seed(store, 1)
    unknown = client.get("/api/news", params={"user": "nobody", "token": TOKEN})
    wrong = client.get("/api/news", params={"user": "art", "token": "wrong-" + TOKEN})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json() == {"detail": "Invalid credentials"}


def test_unknown_user_without_a_token_is_also_401(client):
    r = client.get("/api/news", params={"user": "nobody"})
    assert r.status_code == 401
    assert r.json() == {"detail": "Invalid credentials"}


def test_news_limit_is_capped(client, store):
    seed(store, 3)
    r = client.get("/api/news", params={**AUTH, "limit": 5000})
    assert r.status_code == 422


def test_news_bad_cursor_is_400(client, store):
    seed(store, 1)
    r = client.get("/api/news", params={**AUTH, "before": "!!!"})
    assert r.status_code == 400


def test_news_marks_highlighted(client, store):
    seed(store, 1)
    store.add_filter("art", "headline", "highlight")
    body = client.get("/api/news", params=AUTH).json()
    assert body["articles"][0]["highlighted"] is True


def test_list_feeds(client, store):
    seed(store)
    body = client.get("/api/feeds", params={"user": "art"}, headers=ADMIN).json()
    assert body["feeds"][0]["url"] == "https://x.example/rss"


def test_feeds_carry_the_resolved_favicon(client, store):
    # The widget maps feed_id -> this favicon, so the icon travels once per
    # feed here rather than on every article payload.
    fid = seed(store)
    store.set_feed_favicon(fid, "data:image/png;base64,Zm9v")
    body = client.get("/api/feeds", params=AUTH).json()
    assert body["feeds"][0]["favicon"] == "data:image/png;base64,Zm9v"


def test_feeds_favicon_is_null_before_resolution(client, store):
    seed(store)
    body = client.get("/api/feeds", params=AUTH).json()
    assert body["feeds"][0]["favicon"] is None


def test_feeds_carry_the_group(client, store):
    seed(store)
    store.upsert_feed("https://x.example/rss", now=0, group="Markets")
    body = client.get("/api/feeds", params=AUTH).json()
    assert body["feeds"][0]["group"] == "Markets"


def test_post_feed_requires_admin_key(client):
    r = client.post("/api/feeds", json={"user": "art", "url": "https://n.example/rss"})
    assert r.status_code == 401


def test_post_feed_subscribes_user(client, store):
    r = client.post(
        "/api/feeds",
        json={"user": "art", "url": "https://n.example/rss", "name": "N"},
        headers=ADMIN,
    )
    assert r.status_code == 201
    assert "https://n.example/rss" in [f.url for f in store.list_feeds("art")]


def test_post_feed_with_group_stores_it(client, store):
    r = client.post(
        "/api/feeds",
        json={"user": "art", "url": "https://n.example/rss", "group": "Markets"},
        headers=ADMIN,
    )
    assert r.status_code == 201
    feed = store.get_feed(r.json()["id"])
    assert feed.group == "Markets"
    assert store.list_feeds("art")[0].group == "Markets"


def test_post_feed_twice_is_idempotent(client, store):
    payload = {"user": "art", "url": "https://n.example/rss"}
    client.post("/api/feeds", json=payload, headers=ADMIN)
    client.post("/api/feeds", json=payload, headers=ADMIN)
    assert len(store.list_feeds("art")) == 1


def test_delete_feed_unsubscribes_only(client, store):
    fid = seed(store)
    store.upsert_user("bob", None)
    store.subscribe("bob", fid)
    r = client.delete(
        f"/api/feeds/{fid}", params={"user": "art"}, headers=ADMIN
    )
    assert r.status_code == 204
    assert store.list_feeds("art") == []
    assert [f.id for f in store.list_feeds("bob")] == [fid]


def test_delete_feed_requires_admin_key(client, store):
    fid = seed(store)
    assert client.delete(f"/api/feeds/{fid}", params={"user": "art"}).status_code == 401


def test_delete_unsubscribed_feed_is_404(client, store):
    fid = seed(store)
    store.unsubscribe("art", fid)
    r = client.delete(
        f"/api/feeds/{fid}", params={"user": "art"}, headers=ADMIN
    )
    assert r.status_code == 404


def test_cors_allows_openbb_origin(client):
    r = client.get("/api/health", headers={"Origin": "https://pro.openbb.co"})
    assert r.headers["access-control-allow-origin"] == "https://pro.openbb.co"


def test_admin_key_ok_none_is_false():
    assert admin_key_ok(None, "k") is False


def test_admin_key_ok_matching_is_true():
    assert admin_key_ok("k", "k") is True


def test_admin_key_ok_wrong_is_false():
    assert admin_key_ok("wrong", "k") is False


def test_admin_key_ok_non_ascii_does_not_raise():
    # Regression test: hmac.compare_digest raises TypeError when comparing
    # str operands containing non-ASCII characters. TestClient/httpx cannot
    # transmit such a header client-side, so this must be exercised directly.
    assert admin_key_ok("\xe9", "k") is False


def test_admin_key_ok_non_ascii_match_is_true():
    assert admin_key_ok("café", "café") is True


def test_news_without_a_token_is_401(client, store):
    seed(store, 1)
    assert client.get("/api/news", params={"user": "art"}).status_code == 401


def test_news_accepts_a_bearer_header(client, store):
    seed(store, 1)
    assert client.get("/api/news", params={"user": "art"}, headers=BEARER).status_code == 200


def test_bearer_header_wins_over_a_bad_query_param(client, store):
    seed(store, 1)
    r = client.get("/api/news", params={"user": "art", "token": "junk"}, headers=BEARER)
    assert r.status_code == 200


def test_news_missing_user_is_still_422(client):
    # A request with no `user` at all reveals nothing about which users exist,
    # so this one keeps its distinct code.
    assert client.get("/api/news", params={"token": TOKEN}).status_code == 422


def test_admin_key_alone_satisfies_a_read_endpoint(client, store):
    seed(store, 1)
    assert client.get("/api/news", params={"user": "art"}, headers=ADMIN).status_code == 200


def test_admin_key_with_an_unknown_user_is_401(client):
    assert client.get("/api/news", params={"user": "nobody"}, headers=ADMIN).status_code == 401


def test_feeds_without_a_token_is_401(client, store):
    seed(store)
    assert client.get("/api/feeds", params={"user": "art"}).status_code == 401


def test_a_users_token_does_not_unlock_another_user(client, store):
    store.upsert_user("bob", None, token="bobs-" + TOKEN)
    seed(store)
    assert client.get("/api/news", params={"user": "bob", "token": TOKEN}).status_code == 401


def test_write_endpoints_keep_400_for_an_unknown_user(client):
    # The admin caller can already list every user, so there is nothing to leak.
    r = client.post(
        "/api/feeds", json={"user": "nobody", "url": "https://n.example/rss"},
        headers=ADMIN,
    )
    assert r.status_code == 400


def test_secret_ok_rejects_an_empty_expected_secret():
    # compare_digest(b"", b"") is True, so the emptiness guard must come first:
    # a user row whose token is NULL or "" must authenticate nothing.
    assert secret_ok("", "") is False
    assert secret_ok(None, None) is False
    assert secret_ok("anything", None) is False
    assert secret_ok("anything", "") is False


def test_secret_ok_non_ascii_does_not_raise():
    assert secret_ok("\xe9", "k") is False


def test_token_ok_rejects_a_missing_expected_token():
    assert token_ok(TOKEN, None) is False
    assert token_ok(TOKEN, "") is False
    assert token_ok(None, TOKEN) is False
    assert token_ok(TOKEN, TOKEN) is True


def test_an_empty_stored_token_rejects_even_the_decoy_value():
    # The decoy stands in for a missing token so the comparison happens either
    # way. It must never be an accepted credential: with `expected is not None`
    # as the gate, a user row with token = '' authenticated anyone who knew it.
    assert token_ok(api_mod._DECOY_TOKEN, "") is False
    assert token_ok(api_mod._DECOY_TOKEN, None) is False


def test_token_ok_compares_even_when_there_is_no_stored_token(monkeypatch):
    # Returning early on a missing expected token makes an unknown user
    # measurably faster than a wrong token -- the same oracle with a stopwatch.
    calls = []
    real = api_mod.hmac.compare_digest
    monkeypatch.setattr(
        api_mod.hmac, "compare_digest",
        lambda a, b: calls.append(1) or real(a, b),
    )
    token_ok(TOKEN, None)
    token_ok(TOKEN, "other-" + TOKEN)
    assert len(calls) == 2


def test_bearer_token_parsing():
    assert bearer_token("Bearer abc") == "abc"
    assert bearer_token("bearer abc") == "abc"
    assert bearer_token("Bearer  abc ") == "abc"
    assert bearer_token("Basic abc") is None
    assert bearer_token("Bearer") is None
    assert bearer_token("Bearer   ") is None
    assert bearer_token(None) is None


def test_feeds_are_redacted_to_scheme_and_host_for_a_token_caller(client, store):
    fid = store.upsert_feed("https://api.vendor.example/rss?apikey=SUPERSECRET", now=0)
    store.subscribe("art", fid)
    urls = [f["url"] for f in client.get("/api/feeds", params=AUTH).json()["feeds"]]
    assert "https://api.vendor.example" in urls
    assert not any("SUPERSECRET" in u for u in urls)


def test_feeds_redaction_drops_userinfo(client, store):
    # urlsplit().netloc keeps `user:password@`; only .hostname drops it, and
    # feed URLs of this shape are exactly how vendors ship a token.
    fid = store.upsert_feed("https://user:SUPERSECRET@host.example/rss", now=0)
    store.subscribe("art", fid)
    body = client.get("/api/feeds", params=AUTH).json()
    assert body["feeds"][0]["url"] == "https://host.example"


def test_feeds_redaction_keeps_a_nondefault_port(client, store):
    fid = store.upsert_feed("http://host.example:9000/rss?k=v", now=0)
    store.subscribe("art", fid)
    body = client.get("/api/feeds", params=AUTH).json()
    assert body["feeds"][0]["url"] == "http://host.example:9000"


def test_feeds_are_full_for_an_admin_caller(client, store):
    fid = store.upsert_feed("https://api.vendor.example/rss?apikey=SUPERSECRET", now=0)
    store.subscribe("art", fid)
    body = client.get("/api/feeds", params={"user": "art"}, headers=ADMIN).json()
    assert body["feeds"][0]["url"].endswith("apikey=SUPERSECRET")


def test_redact_feed_url_handles_garbage():
    assert redact_feed_url("not a url") == "(redacted)"
    assert redact_feed_url("") == "(redacted)"


def test_redact_feed_url_handles_a_non_numeric_port():
    # urlsplit().port raises ValueError for a non-numeric port -- it must
    # redact rather than propagate, and the secret must not leak in the process.
    url = "http://host.example:abc/rss?apikey=SUPERSECRET"
    result = redact_feed_url(url)
    assert result == "(redacted)"
    assert "SUPERSECRET" not in result


def test_feeds_survives_one_malformed_feed_for_a_token_caller(client, store):
    # a single bad feed url used to blow up the whole list comprehension and
    # 500 the endpoint for every caller, not just the owner of the bad feed.
    good = store.upsert_feed("https://api.vendor.example/rss?apikey=SUPERSECRET", now=0)
    bad = store.upsert_feed("http://host.example:abc/rss?apikey=OTHERSECRET", now=0)
    store.subscribe("art", good)
    store.subscribe("art", bad)
    r = client.get("/api/feeds", params=AUTH)
    assert r.status_code == 200
    urls = [f["url"] for f in r.json()["feeds"]]
    assert not any("SUPERSECRET" in u or "OTHERSECRET" in u for u in urls)


def test_public_health_keeps_the_published_url_but_drops_feed_detail(client, store):
    # public_base_url is not a secret -- the caller already knows the host --
    # and it is the only symptom of a misconfigured base URL, which otherwise
    # presents as a blank iframe with nothing else to go on.
    seed(store)
    body = client.get("/api/health").json()
    assert body["public_base_url"] == "http://nas.local:8088"
    assert body["status"] == "ok"
    assert "feeds" not in body


def test_admin_health_has_feed_detail(client, store):
    seed(store)
    body = client.get("/api/health", headers=ADMIN).json()
    assert body["feeds"][0]["url"] == "https://x.example/rss"
    assert body["public_base_url"] == "http://nas.local:8088"


def test_health_is_ok_below_the_degraded_threshold(client, store):
    # One transient failure must not mark the deployment unhealthy: with
    # `restart: unless-stopped` in front of it that is a restart loop caused by
    # someone else's flaky feed.
    fid = seed(store)
    store.record_failure(fid, error="http 500", now=10, next_poll_at=20)
    store.record_failure(fid, error="http 500", now=30, next_poll_at=40)
    assert client.get("/api/health").json()["status"] == "ok"


def test_health_reports_degraded_at_the_threshold(client, store):
    fid = seed(store)
    for i in range(DEGRADED_AFTER_FAILURES):
        store.record_failure(fid, error="http 500", now=10 * i, next_poll_at=20 * i)
    assert client.get("/api/health").json()["status"] == "degraded"


def test_health_stays_200_when_degraded(client, store):
    # Docker's HEALTHCHECK only tests for HTTP 200.
    fid = seed(store)
    for i in range(DEGRADED_AFTER_FAILURES):
        store.record_failure(fid, error="http 500", now=10 * i, next_poll_at=20 * i)
    assert client.get("/api/health").status_code == 200


def test_health_ignores_disabled_feeds_when_deciding_degraded(client, store):
    fid = seed(store)
    for i in range(DEGRADED_AFTER_FAILURES):
        store.record_failure(fid, error="http 500", now=10 * i, next_poll_at=20 * i)
    store.unsubscribe("art", fid)
    assert client.get("/api/health").json()["status"] == "ok"


def test_post_feed_with_title_format_stores_it(client, store):
    r = client.post(
        "/api/feeds",
        json={
            "user": "art",
            "url": "https://n.example/rss",
            "title_format": "{title} - {author}",
        },
        headers=ADMIN,
    )
    assert r.status_code == 201
    assert store.get_feed(r.json()["id"]).title_format == "{title} - {author}"


def test_feeds_carry_the_title_format(client, store):
    seed(store)
    store.upsert_feed("https://x.example/rss", now=0, title_format="{title} - {author}")
    body = client.get("/api/feeds", params=AUTH).json()
    assert body["feeds"][0]["title_format"] == "{title} - {author}"


def test_post_feed_rejects_non_positive_poll_interval(client):
    r = client.post(
        "/api/feeds",
        json={"user": "art", "url": "https://n.example/rss", "poll_interval_s": -5},
        headers=ADMIN,
    )
    assert r.status_code == 422


def strict_client(store):
    return TestClient(create_app(CFG, store, Broadcaster(store), health_strict=True))


def test_strict_health_returns_503_when_degraded(store):
    # HEALTH_STRICT flips the deliberate 200-when-degraded behavior so a
    # degraded state trips the container's HEALTHCHECK and reaches the NAS
    # Notification Center. The body still says "degraded".
    fid = seed(store)
    for i in range(DEGRADED_AFTER_FAILURES):
        store.record_failure(fid, error="http 500", now=10 * i, next_poll_at=20 * i)
    r = strict_client(store).get("/api/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"


def test_strict_health_returns_200_when_ok(store):
    seed(store)
    r = strict_client(store).get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_default_health_stays_200_when_degraded(store):
    # Without strict mode the original behavior is unchanged.
    fid = seed(store)
    for i in range(DEGRADED_AFTER_FAILURES):
        store.record_failure(fid, error="http 500", now=10 * i, next_poll_at=20 * i)
    r = TestClient(create_app(CFG, store, Broadcaster(store))).get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"


IDENT = {"Tailscale-User-Login": LOGIN}
TS_CFG = Config(
    public_base_url="https://t.example",
    admin_key="s3cret",
    tailscale_auth=True,
    bind_host="127.0.0.1",
    users=(UserConfig(id="art", tailscale_login=LOGIN),),
)


@pytest.fixture
def ts_client(store):
    return TestClient(create_app(TS_CFG, store, Broadcaster(store)))


def test_identity_authenticates_a_read(ts_client, store):
    seed(store, 1)
    r = ts_client.get("/api/news", params={"user": "art"}, headers=IDENT)
    assert r.status_code == 200


def test_identity_for_a_different_user_is_401(ts_client, store):
    store.upsert_user("bob", None)
    seed(store, 1)
    r = ts_client.get("/api/news", params={"user": "bob"}, headers=IDENT)
    assert r.status_code == 401
    assert r.json() == {"detail": "Invalid credentials"}


def test_unknown_identity_is_401(ts_client, store):
    seed(store, 1)
    r = ts_client.get(
        "/api/news", params={"user": "art"},
        headers={"Tailscale-User-Login": "nobody@github"},
    )
    assert r.status_code == 401


def test_no_identity_and_no_token_is_401(ts_client, store):
    seed(store, 1)
    assert ts_client.get("/api/news", params={"user": "art"}).status_code == 401


def test_identity_header_is_ignored_when_tailscale_auth_is_off(client, store):
    # THE security-critical test. Without the flag the header is just something
    # any client can type, so honouring it would be an open door on every
    # non-Tailscale deployment.
    seed(store, 1)
    r = client.get("/api/news", params={"user": "art"}, headers=IDENT)
    assert r.status_code == 401


def test_identity_authenticates_the_widget(ts_client, store):
    seed(store, 1)
    assert ts_client.get("/widget", params={"user": "art"}, headers=IDENT).status_code == 200


def test_identity_authenticates_feeds(ts_client, store):
    seed(store)
    assert ts_client.get("/api/feeds", params={"user": "art"}, headers=IDENT).status_code == 200


def test_admin_key_still_works_under_tailscale_auth(ts_client, store):
    seed(store, 1)
    r = ts_client.get("/api/news", params={"user": "art"}, headers={"X-Admin-Key": "s3cret"})
    assert r.status_code == 200


def test_writes_still_need_the_admin_key_under_tailscale_auth(ts_client):
    r = ts_client.post(
        "/api/feeds", json={"user": "art", "url": "https://n.example/rss"}, headers=IDENT
    )
    assert r.status_code == 401


def test_stale_token_is_rejected_after_switching_to_tailscale_auth(store):
    # The store here already has "art" upserted with TOKEN by the `store`
    # fixture -- reconciling a tailscale_auth config with no token: line for
    # that user must clear it, not preserve it via upsert_user's COALESCE.
    from rss_ticker.config import UserConfig as UC
    from rss_ticker.reconcile import reconcile

    reconcile(
        store,
        Config(
            public_base_url="https://t.example", admin_key="k", tailscale_auth=True,
            bind_host="127.0.0.1", users=(UC(id="art", tailscale_login=LOGIN),),
        ),
        now=100,
    )
    ts_client = TestClient(create_app(TS_CFG, store, Broadcaster(store)))
    r = ts_client.get("/api/news", params={"user": "art", "token": TOKEN})
    assert r.status_code == 401
    # Identity still works for the same user.
    r = ts_client.get("/api/news", params={"user": "art"}, headers=IDENT)
    assert r.status_code == 200

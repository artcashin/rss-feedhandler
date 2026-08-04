import pytest
from rss_ticker.config import Config, UserConfig, FeedConfig, FilterConfig
from rss_ticker.reconcile import reconcile
from rss_ticker.store import NewArticle, Store


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def cfg(users):
    return Config(public_base_url="http://x", admin_key="k", users=tuple(users))


def test_creates_users_feeds_subscriptions_and_filters(store):
    c = cfg([
        UserConfig(
            id="art",
            name="Art",
            feeds=(FeedConfig(url="https://a.example/rss", name="A"),),
            filters=(FilterConfig(pattern="nvidia", action="highlight"),),
        )
    ])
    reconcile(store, c, now=100)
    feeds = store.list_feeds("art")
    assert [f.url for f in feeds] == ["https://a.example/rss"]
    assert store.filters_for("art")[0].pattern == "nvidia"


def test_is_idempotent(store):
    c = cfg([UserConfig(id="art", feeds=(FeedConfig(url="https://a.example/rss"),))])
    reconcile(store, c, now=100)
    reconcile(store, c, now=200)
    assert len(store.list_feeds("art")) == 1


def test_does_not_delete_feeds_added_outside_config(store):
    reconcile(store, cfg([UserConfig(id="art")]), now=100)
    fid = store.upsert_feed("https://runtime.example/rss", now=100)
    store.subscribe("art", fid)
    reconcile(store, cfg([UserConfig(id="art")]), now=200)
    assert "https://runtime.example/rss" in [f.url for f in store.list_feeds("art")]


def test_does_not_delete_filters_added_outside_config(store):
    reconcile(store, cfg([UserConfig(id="art")]), now=100)
    store.add_filter("art", "runtime", "include")
    reconcile(store, cfg([UserConfig(id="art")]), now=200)
    assert "runtime" in [f.pattern for f in store.filters_for("art")]


def test_config_feed_and_runtime_feed_coexist_across_reconcile(store):
    c = cfg([UserConfig(id="art", feeds=(FeedConfig(url="https://from-config.example/rss"),))])
    reconcile(store, c, now=100)
    runtime = store.upsert_feed("https://from-api.example/rss", now=150)
    store.subscribe("art", runtime)
    reconcile(store, c, now=200)
    urls = {f.url for f in store.list_feeds("art")}
    assert urls == {"https://from-config.example/rss", "https://from-api.example/rss"}


def test_config_filter_and_runtime_filter_coexist_across_reconcile(store):
    c = cfg([UserConfig(id="art", filters=(FilterConfig(pattern="from-config", action="include"),))])
    reconcile(store, c, now=100)
    store.add_filter("art", "from-api", "highlight")
    reconcile(store, c, now=200)
    patterns = {f.pattern for f in store.filters_for("art")}
    assert patterns == {"from-config", "from-api"}


def test_updates_feed_name_and_interval(store):
    reconcile(store, cfg([UserConfig(
        id="art", feeds=(FeedConfig(url="https://a.example/rss", name="Old"),))]), now=100)
    reconcile(store, cfg([UserConfig(
        id="art",
        feeds=(FeedConfig(url="https://a.example/rss", name="New", poll_interval_s=600),),
    )]), now=200)
    feed = store.list_feeds("art")[0]
    assert feed.name == "New"
    assert feed.poll_interval_s == 600


def test_feed_group_is_persisted(store):
    c = cfg([UserConfig(
        id="art",
        feeds=(FeedConfig(url="https://a.example/rss", group="Markets"),),
    )])
    reconcile(store, c, now=100)
    feed = store.list_feeds("art")[0]
    assert feed.group == "Markets"


def test_shared_feed_is_polled_once(store):
    c = cfg([
        UserConfig(id="art", feeds=(FeedConfig(url="https://same.example/rss"),)),
        UserConfig(id="bob", feeds=(FeedConfig(url="https://same.example/rss"),)),
    ])
    reconcile(store, c, now=100)
    assert len(store.due_feeds(now=100)) == 1


TOKEN = "tkn-" + "0123456789abcdef" * 3


def test_token_is_persisted(store):
    reconcile(store, cfg([UserConfig(id="art", token=TOKEN)]), now=100)
    assert store.token_for("art") == TOKEN


def test_rotated_token_overwrites_the_stored_one(store):
    reconcile(store, cfg([UserConfig(id="art", token=TOKEN)]), now=100)
    reconcile(store, cfg([UserConfig(id="art", token="rotated-" + TOKEN)]), now=200)
    assert store.token_for("art") == "rotated-" + TOKEN


def test_tokenless_database_user_is_warned_about(store, caplog):
    store.upsert_user("orphan", None)
    with caplog.at_level("WARNING"):
        reconcile(store, cfg([UserConfig(id="art", token=TOKEN)]), now=100)
    assert "orphan" in caplog.text


def test_the_warning_does_not_contain_a_token(store, caplog):
    store.upsert_user("orphan", None)
    with caplog.at_level("WARNING"):
        reconcile(store, cfg([UserConfig(id="art", token=TOKEN)]), now=100)
    assert any(r.levelname == "WARNING" for r in caplog.records)
    assert TOKEN not in caplog.text


BOB_TOKEN = "bob-" + "0123456789abcdef" * 3


def test_configured_user_keeps_token_across_reconcile(store):
    c = cfg([UserConfig(id="art", token=TOKEN), UserConfig(id="bob", token=BOB_TOKEN)])
    reconcile(store, c, now=100)
    reconcile(store, c, now=200)
    assert store.token_for("art") == TOKEN
    assert store.token_for("bob") == BOB_TOKEN


def test_user_dropped_from_config_has_their_token_revoked(store):
    reconcile(
        store,
        cfg([UserConfig(id="art", token=TOKEN), UserConfig(id="bob", token=BOB_TOKEN)]),
        now=100,
    )
    reconcile(store, cfg([UserConfig(id="art", token=TOKEN)]), now=200)
    assert store.token_for("bob") is None


def test_dropped_users_row_feeds_and_articles_survive(store):
    c = cfg([
        UserConfig(
            id="bob",
            token=BOB_TOKEN,
            feeds=(FeedConfig(url="https://bob.example/rss"),),
        ),
        UserConfig(id="art", token=TOKEN),
    ])
    reconcile(store, c, now=100)
    fid = store.list_feeds("bob")[0].id
    store.insert_articles(
        fid,
        [NewArticle(guid="g1", title="T", link=None, summary=None, published_at=None)],
        now=100,
    )
    reconcile(store, cfg([UserConfig(id="art", token=TOKEN)]), now=200)

    assert store.user_exists("bob") is True
    assert [f.url for f in store.list_feeds("bob")] == ["https://bob.example/rss"]
    articles, _ = store.page_news("bob", limit=10)
    assert [a.title for a in articles] == ["T"]


def test_readding_a_dropped_user_restores_their_access(store):
    c_both = cfg([UserConfig(id="art", token=TOKEN), UserConfig(id="bob", token=BOB_TOKEN)])
    c_art_only = cfg([UserConfig(id="art", token=TOKEN)])
    reconcile(store, c_both, now=100)
    reconcile(store, c_art_only, now=200)
    assert store.token_for("bob") is None
    reconcile(store, c_both, now=300)
    assert store.token_for("bob") == BOB_TOKEN


def test_revocation_is_logged_by_id_and_never_leaks_the_token(store, caplog):
    reconcile(
        store,
        cfg([UserConfig(id="art", token=TOKEN), UserConfig(id="bob", token=BOB_TOKEN)]),
        now=100,
    )
    with caplog.at_level("WARNING"):
        reconcile(store, cfg([UserConfig(id="art", token=TOKEN)]), now=200)
    assert "bob" in caplog.text
    assert TOKEN not in caplog.text
    assert BOB_TOKEN not in caplog.text


def test_an_empty_config_revokes_nobody(store):
    # A config with no users at all is far more likely to be a misread or
    # truncated file than a deliberate "delete every user" instruction, so
    # reconcile treats it as a no-op for revocation rather than a mass
    # lockout. See reconcile.py for the same reasoning inline.
    reconcile(store, cfg([UserConfig(id="art", token=TOKEN)]), now=100)
    reconcile(store, cfg([]), now=200)
    assert store.token_for("art") == TOKEN


def test_boot_logs_enabled_feeds_absent_from_config(store, caplog):
    # Reconciliation is additive: a feed removed from config.yaml keeps its
    # subscription and stays polled forever. That is deliberate -- but it
    # must be VISIBLE, or a removed feed silently burns polls for months.
    c = cfg([UserConfig(id="art", feeds=(
        FeedConfig(url="https://a.example/rss", name="A"),
        FeedConfig(url="https://old.example/rss?apikey=hunter2", name="Old"),
    ))])
    reconcile(store, c, now=100)

    trimmed = cfg([UserConfig(id="art", feeds=(
        FeedConfig(url="https://a.example/rss", name="A"),
    ))])
    import logging
    with caplog.at_level(logging.WARNING, logger="rss_ticker.reconcile"):
        reconcile(store, trimmed, now=200)
    warning = "\n".join(r.message for r in caplog.records)
    assert "Old" in warning, "the orphaned feed must be named"
    assert "old.example" in warning
    assert "hunter2" not in warning, "feed URLs carry credentials; the log must redact"


def test_no_orphan_warning_when_config_covers_every_enabled_feed(store, caplog):
    c = cfg([UserConfig(id="art", feeds=(FeedConfig(url="https://a.example/rss", name="A"),))])
    reconcile(store, c, now=100)
    import logging
    with caplog.at_level(logging.WARNING, logger="rss_ticker.reconcile"):
        reconcile(store, c, now=200)
    assert not any("absent from config" in r.message for r in caplog.records)


LOGIN = "you@github"


def ts_cfg(users):
    return Config(
        public_base_url="https://x", admin_key="k", tailscale_auth=True,
        bind_host="127.0.0.1", users=tuple(users),
    )


def test_switching_to_tailscale_auth_revokes_the_stale_token(store):
    # The token being retired here is exactly the one published in
    # widgets.json, in OpenBB's saved dashboard config, and in proxy logs --
    # the exposure tailscale_auth exists to eliminate. It must not survive
    # the switch by upsert_user's COALESCE.
    reconcile(store, cfg([UserConfig(id="art", token=TOKEN)]), now=100)
    assert store.token_for("art") == TOKEN

    reconcile(
        store,
        ts_cfg([UserConfig(id="art", tailscale_login=LOGIN)]),
        now=200,
    )
    assert store.token_for("art") is None


def test_identity_still_authenticates_after_the_switch(store):
    reconcile(store, cfg([UserConfig(id="art", token=TOKEN)]), now=100)
    reconcile(
        store,
        ts_cfg([UserConfig(id="art", tailscale_login=LOGIN)]),
        now=200,
    )
    assert store.user_exists("art") is True


def test_token_mode_reconcile_does_not_touch_the_stored_token(store):
    # tailscale_auth is off here, so the new clear_token branch in reconcile
    # must never fire -- the stored token survives repeated reconciles
    # exactly as it did before this change.
    c = cfg([UserConfig(id="art", token=TOKEN)])
    reconcile(store, c, now=100)
    reconcile(store, c, now=200)
    reconcile(store, c, now=300)
    assert store.token_for("art") == TOKEN


def test_tailscale_identity_user_is_not_warned_about_as_tokenless(store, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="rss_ticker.reconcile"):
        reconcile(
            store,
            ts_cfg([UserConfig(id="art", tailscale_login=LOGIN)]),
            now=100,
        )
    assert not any("cannot authenticate" in r.message for r in caplog.records)

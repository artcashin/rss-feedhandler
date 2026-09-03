import pytest
from rss_ticker.store import NewArticle, Store


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_upsert_feed_is_idempotent_by_url(store):
    a = store.upsert_feed("https://x.example/rss", name="X", now=100)
    b = store.upsert_feed("https://x.example/rss", name="X renamed", now=200)
    assert a == b


def test_repeat_upsert_feed_does_not_reset_poll_schedule(store):
    fid = store.upsert_feed("https://x.example/rss", name="X", now=100)
    assert store.get_feed_state(fid).next_poll_at == 100
    store.upsert_feed("https://x.example/rss", name="X renamed", now=999)
    assert store.get_feed_state(fid).next_poll_at == 100


def test_upsert_feed_creates_feed_state_with_next_poll_now(store):
    fid = store.upsert_feed("https://x.example/rss", now=100)
    st = store.get_feed_state(fid)
    assert st.next_poll_at == 100
    assert st.last_success_at is None
    assert st.consecutive_failures == 0


def test_upsert_feed_canonicalises_the_url(store):
    a = store.upsert_feed("HTTPS://X.example/rss/", name="X", now=0)
    b = store.upsert_feed("https://x.example/rss", now=0)
    assert a == b
    assert store.get_feed(a).url == "https://x.example/rss"
    assert store.feed_by_url("https://X.EXAMPLE/rss/").id == a
    assert store.feed_by_url("https://y.example/rss") is None


def test_stored_name_wins_over_a_later_subscribers_name(store):
    fid = store.upsert_feed("https://x.example/rss", name="First", now=0)
    store.upsert_feed("https://x.example/rss", name="Second", now=1)
    assert store.get_feed(fid).name == "First"
    unnamed = store.upsert_feed("https://y.example/rss", now=0)
    store.upsert_feed("https://y.example/rss", name="Named later", now=1)
    assert store.get_feed(unnamed).name == "Named later"


def test_canonical_url_rules():
    from rss_ticker.store import canonical_url

    assert (
        canonical_url("HTTPS://Feeds.Bloomberg.com/markets/news.rss/")
        == "https://feeds.bloomberg.com/markets/news.rss"
    )
    assert canonical_url("https://x.example/Feed?A=1") == "https://x.example/Feed?A=1"
    assert canonical_url("https://u:P@X.example:8443/f/") == "https://u:P@x.example:8443/f"
    assert canonical_url("https://x.example/") == "https://x.example"
    assert canonical_url("https://x.example") == "https://x.example"
    # An IPv6 literal keeps its brackets and its port, or the result is not a
    # URL any more and every poll of that feed fails forever.
    assert canonical_url("http://[::1]:8080/feed/") == "http://[::1]:8080/feed"
    assert canonical_url("HTTP://[2001:DB8::1]/f") == "http://[2001:db8::1]/f"
    # One trailing slash, not all of them.
    assert canonical_url("https://x.example/feed//") == "https://x.example/feed/"
    # An unparseable port leaves nothing safe to rebuild: returned as given.
    assert canonical_url("https://x.example:abc/f") == "https://x.example:abc/f"


def test_enable_disable_and_drop(store):
    a = store.upsert_feed("https://a.example/rss", now=0)
    b = store.upsert_feed("https://b.example/rss", now=0)
    store.insert_articles(a, [NewArticle("g", "t", None, None, 1)], now=1)
    store.disable_all_feeds()
    assert not store.get_feed(a).enabled and not store.get_feed(b).enabled
    assert store.due_feeds(now=10) == []
    store.set_enabled(b, True)
    assert [f.id for f in store.due_feeds(now=10)] == [b]
    assert store.drop_disabled_feeds() == 1
    assert store.get_feed(a) is None
    assert store.get_feed_state(a) is None
    assert store.page_news(limit=10)[0] == []
    assert store.get_feed(b).id == b


FAVICON = "data:image/png;base64,AAAA"


def test_new_feed_has_no_favicon_by_default(store):
    fid = store.upsert_feed("https://x.example/rss", now=0)
    assert store.get_feed(fid).favicon is None


def test_set_feed_favicon_round_trips(store):
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.set_feed_favicon(fid, FAVICON)
    assert store.get_feed(fid).favicon == FAVICON


def test_upsert_feed_preserves_a_previously_set_favicon(store):
    fid = store.upsert_feed("https://x.example/rss", name="X", now=100)
    store.set_feed_favicon(fid, FAVICON)
    store.upsert_feed("https://x.example/rss", name="X renamed", now=200)
    assert store.get_feed(fid).favicon == FAVICON


def test_all_feeds_returns_every_feed_ordered_by_id(store):
    with_icon = store.upsert_feed("https://a.example/rss", now=0)
    without_icon = store.upsert_feed("https://b.example/rss", now=0)
    store.set_feed_favicon(with_icon, FAVICON)
    result = store.all_feeds()
    assert [f.id for f in result] == [with_icon, without_icon]
    assert [f.favicon for f in result] == [FAVICON, None]


def test_a_database_predating_the_favicon_column_is_migrated(tmp_path):
    path = str(tmp_path / "old.db")
    old = Store(path)
    fid = old.upsert_feed("https://x.example/rss", name="X", now=0)
    old.db.execute("ALTER TABLE feeds DROP COLUMN favicon")
    old.db.commit()
    old.close()

    migrated = Store(path)
    try:
        feed = migrated.get_feed(fid)
        assert feed is not None
        assert feed.favicon is None
    finally:
        migrated.close()

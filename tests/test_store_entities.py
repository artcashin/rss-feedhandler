import pytest
from rss_ticker.store import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_upsert_feed_is_idempotent_by_url(store):
    a = store.upsert_feed("https://x.example/rss", name="X", now=100)
    b = store.upsert_feed("https://x.example/rss", name="X renamed", now=200)
    assert a == b
    assert store.get_feed(a).name == "X renamed"


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


def test_subscribe_is_idempotent(store):
    store.upsert_user("art", "Art")
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.subscribe("art", fid)
    store.subscribe("art", fid)
    assert [f.id for f in store.list_feeds("art")] == [fid]


def test_two_users_share_one_feed_row(store):
    store.upsert_user("art", "Art")
    store.upsert_user("bob", "Bob")
    a = store.upsert_feed("https://x.example/rss", now=0)
    b = store.upsert_feed("https://x.example/rss", now=0)
    store.subscribe("art", a)
    store.subscribe("bob", b)
    assert a == b
    assert sorted(store.subscribers_of(a)) == ["art", "bob"]


def test_unsubscribe_keeps_feed_for_other_subscribers(store):
    store.upsert_user("art", None)
    store.upsert_user("bob", None)
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.subscribe("art", fid)
    store.subscribe("bob", fid)
    store.unsubscribe("art", fid)
    assert store.get_feed(fid).enabled is True
    assert store.subscribers_of(fid) == ["bob"]


def test_last_unsubscribe_disables_feed_but_keeps_row(store):
    store.upsert_user("art", None)
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.subscribe("art", fid)
    assert store.unsubscribe("art", fid) is True
    feed = store.get_feed(fid)
    assert feed is not None
    assert feed.enabled is False


def test_resubscribe_reenables_feed(store):
    store.upsert_user("art", None)
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.subscribe("art", fid)
    store.unsubscribe("art", fid)
    store.subscribe("art", fid)
    assert store.get_feed(fid).enabled is True


def test_unsubscribe_returns_false_when_not_subscribed(store):
    store.upsert_user("art", None)
    fid = store.upsert_feed("https://x.example/rss", now=0)
    assert store.unsubscribe("art", fid) is False


def test_user_exists(store):
    assert store.user_exists("art") is False
    store.upsert_user("art", None)
    assert store.user_exists("art") is True


TOKEN = "tkn-" + "0123456789abcdef" * 3


def test_token_round_trips(store):
    store.upsert_user("art", "Art", token=TOKEN)
    assert store.token_for("art") == TOKEN


def test_token_for_unknown_user_is_none(store):
    assert store.token_for("nobody") is None


def test_user_created_without_a_token_has_none(store):
    store.upsert_user("art", "Art")
    assert store.token_for("art") is None


def test_upsert_without_a_token_preserves_the_existing_one(store):
    store.upsert_user("art", "Art", token=TOKEN)
    store.upsert_user("art", "Art renamed")
    assert store.token_for("art") == TOKEN


def test_upsert_with_a_new_token_rotates_it(store):
    store.upsert_user("art", "Art", token=TOKEN)
    store.upsert_user("art", "Art", token="rotated-" + TOKEN)
    assert store.token_for("art") == "rotated-" + TOKEN


def test_users_without_tokens_lists_them(store):
    store.upsert_user("art", None, token=TOKEN)
    store.upsert_user("bob", None)
    assert store.users_without_tokens() == ["bob"]


def test_revoke_tokens_except_clears_tokens_not_in_the_set(store):
    store.upsert_user("art", None, token=TOKEN)
    store.upsert_user("bob", None, token="bobs-" + TOKEN)
    revoked = store.revoke_tokens_except({"art"})
    assert revoked == ["bob"]
    assert store.token_for("bob") is None


def test_revoke_tokens_except_sets_null_not_empty_string(store):
    store.upsert_user("bob", None, token="bobs-" + TOKEN)
    store.revoke_tokens_except({"art"})
    row = store.db.execute("SELECT token FROM users WHERE id = 'bob'").fetchone()
    assert row["token"] is None


def test_revoke_tokens_except_keeps_configured_users_untouched(store):
    store.upsert_user("art", None, token=TOKEN)
    store.revoke_tokens_except({"art"})
    assert store.token_for("art") == TOKEN


def test_revoke_tokens_except_does_not_report_users_already_tokenless(store):
    store.upsert_user("orphan", None)
    revoked = store.revoke_tokens_except({"art"})
    assert revoked == []


def test_revoke_tokens_except_with_an_empty_set_revokes_nobody(store):
    # An empty configured-id set must not be read as "revoke everybody" --
    # that would turn a misread or empty config file into a mass lockout.
    store.upsert_user("art", None, token=TOKEN)
    store.upsert_user("bob", None, token="bobs-" + TOKEN)
    revoked = store.revoke_tokens_except(set())
    assert revoked == []
    assert store.token_for("art") == TOKEN
    assert store.token_for("bob") == "bobs-" + TOKEN


def test_a_database_predating_the_token_column_is_migrated(tmp_path):
    path = str(tmp_path / "old.db")
    old = Store(path)
    old.upsert_user("art", "Art")
    old.db.execute("ALTER TABLE users DROP COLUMN token")
    old.db.commit()
    old.close()

    migrated = Store(path)
    try:
        assert migrated.user_exists("art") is True
        assert migrated.token_for("art") is None
        assert migrated.users_without_tokens() == ["art"]
    finally:
        migrated.close()


FAVICON = "data:image/png;base64,AAAA"


def test_new_feed_has_no_favicon_by_default(store):
    fid = store.upsert_feed("https://x.example/rss", now=0)
    assert store.get_feed(fid).favicon is None


def test_set_feed_favicon_round_trips(store):
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.set_feed_favicon(fid, FAVICON)
    assert store.get_feed(fid).favicon == FAVICON


def test_list_feeds_carries_favicon(store):
    store.upsert_user("art", "Art")
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.subscribe("art", fid)
    store.set_feed_favicon(fid, FAVICON)
    feeds = store.list_feeds("art")
    assert feeds[0].favicon == FAVICON


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


def test_new_feed_has_no_group_by_default(store):
    fid = store.upsert_feed("https://x.example/rss", now=0)
    assert store.get_feed(fid).group is None


def test_upsert_feed_group_round_trips(store):
    fid = store.upsert_feed("https://x.example/rss", now=0, group="Markets")
    assert store.get_feed(fid).group == "Markets"


def test_list_feeds_carries_group(store):
    store.upsert_user("art", "Art")
    fid = store.upsert_feed("https://x.example/rss", now=0, group="Markets")
    store.subscribe("art", fid)
    feeds = store.list_feeds("art")
    assert feeds[0].group == "Markets"


def test_all_feeds_carries_group(store):
    store.upsert_feed("https://x.example/rss", now=0, group="Markets")
    result = store.all_feeds()
    assert result[0].group == "Markets"


def test_upsert_feed_preserves_a_previously_set_group_when_omitted(store):
    fid = store.upsert_feed("https://x.example/rss", name="X", now=100, group="Markets")
    store.upsert_feed("https://x.example/rss", name="X renamed", now=200)
    assert store.get_feed(fid).group == "Markets"


def test_a_database_predating_the_group_column_is_migrated(tmp_path):
    path = str(tmp_path / "old.db")
    old = Store(path)
    fid = old.upsert_feed("https://x.example/rss", name="X", now=0)
    old.db.execute('ALTER TABLE feeds DROP COLUMN "group"')
    old.db.commit()
    old.close()

    migrated = Store(path)
    try:
        feed = migrated.get_feed(fid)
        assert feed is not None
        assert feed.group is None
    finally:
        migrated.close()


def test_upsert_feed_persists_title_format(store):
    fid = store.upsert_feed(
        "https://x.example/rss", now=0, title_format="{title} - {author}"
    )
    assert store.get_feed(fid).title_format == "{title} - {author}"


def test_upsert_feed_without_title_format_keeps_existing(store):
    fid = store.upsert_feed(
        "https://x.example/rss", now=0, title_format="{title} - {author}"
    )
    store.upsert_feed("https://x.example/rss", now=1)
    assert store.get_feed(fid).title_format == "{title} - {author}"

"""Deterministic proof that Store serializes cross-thread access.

The production bug: Store shares one sqlite3 connection
(`check_same_thread=False`) across the asyncio loop thread, FastAPI's
threadpool, and a to_thread sweeper. A transaction is connection-level on a
single connection, so one thread's commit/rollback can tear another
thread's in-flight transaction. The fix wraps every public Store method in
a `threading.RLock` so a method body (including any `with self.db:` block)
completes atomically with respect to every other thread.

The test below does not rely on timing luck. Thread A is forced to block
*while still holding the lock* (by monkeypatching `store.db.execute` to
wait on an Event before delegating to the real call), and thread B is then
proven unable to make progress until A releases the lock. This must fail
every time against unlocked code and pass every time against locked code.
"""

import threading
import time

import pytest

from rss_ticker.store import Store


class _HookedConnection:
    """Transparent proxy around a live sqlite3.Connection.

    sqlite3.Connection attributes (including bound methods like `execute`)
    are read-only on the instance, so a test cannot monkeypatch
    `store.db.execute` directly. This proxy stands in for `store.db` and
    lets a single call to `execute` be intercepted -- e.g. to block a
    thread mid-call so it can be proven to still hold the store's lock --
    while every other call (execute, executescript, commit, the `with`
    transaction protocol, etc.) passes straight through to the real
    connection unchanged.
    """

    def __init__(self, real_conn, on_execute=None):
        object.__setattr__(self, "_real", real_conn)
        object.__setattr__(self, "_on_execute", on_execute)

    def execute(self, *args, **kwargs):
        hook = object.__getattribute__(self, "_on_execute")
        if hook is not None:
            hook(*args, **kwargs)
        return object.__getattribute__(self, "_real").execute(*args, **kwargs)

    def __enter__(self):
        return object.__getattribute__(self, "_real").__enter__()

    def __exit__(self, *exc):
        return object.__getattribute__(self, "_real").__exit__(*exc)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)


@pytest.fixture
def store(tmp_path):
    # A lock test needs a real thread-shareable connection; ":memory:" is
    # connection-private and would not exercise cross-thread sharing at all.
    s = Store(str(tmp_path / "concurrency.db"))
    yield s
    s.close()


def test_one_thread_inside_a_store_method_blocks_all_others(store):
    a_is_inside = threading.Event()
    a_may_proceed = threading.Event()

    real_db = store.db
    call_count = {"n": 0}

    def on_execute(*_args, **_kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First call made by thread A's store method: announce we are
            # inside the locked region, then wait to be released. Because
            # the lock (if present) wraps the whole method body, thread A
            # is holding it for the entire duration of this wait.
            a_is_inside.set()
            a_may_proceed.wait(timeout=5)

    store.db = _HookedConnection(real_db, on_execute=on_execute)

    def thread_a():
        # upsert_feed touches self.db.execute then commits -- a normal
        # write, but its first execute() call is now an artificial stall.
        store.upsert_feed("https://alice.example/rss", name="Alice", now=1)

    def thread_b():
        # A different store method. If the lock holds, this cannot
        # complete (or even start executing SQL) until thread A releases.
        store.upsert_feed("https://bob.example/rss", name="Bob", now=2)
        b_done.set()

    b_done = threading.Event()

    t_a = threading.Thread(target=thread_a)
    t_b = threading.Thread(target=thread_b)

    t_a.start()
    assert a_is_inside.wait(timeout=5), "thread A never reached the locked region"

    t_b.start()
    # Bounded window: with the lock in place thread B must still be
    # blocked waiting to acquire it. Without the lock, B races straight
    # through and finishes almost immediately.
    t_b.join(timeout=0.5)
    assert t_b.is_alive(), (
        "thread B completed while thread A was still inside a store method -- "
        "mutual exclusion is not being enforced"
    )
    assert not b_done.is_set()

    # Release thread A; both threads should now finish promptly.
    a_may_proceed.set()
    t_a.join(timeout=5)
    t_b.join(timeout=5)
    assert not t_a.is_alive()
    assert not t_b.is_alive()
    assert b_done.is_set()

    # Restore the real connection before the fixture calls store.close().
    store.db = real_db

    # Both writes landed -- the point of serializing, not tearing, either one.
    assert store.feed_by_url("https://alice.example/rss") is not None
    assert store.feed_by_url("https://bob.example/rss") is not None


def test_failing_insert_articles_batch_does_not_lose_a_concurrent_write(store):
    """Optional integration-level backstop for the lost-write scenario.

    One thread repeatedly runs a batch that is forced to raise partway
    through `insert_articles`'s `with self.db:` block (triggering an
    implicit rollback on the shared connection); another thread
    concurrently commits an unrelated feed write. Without serialization,
    A's rollback can discard B's committed row if the two interleave
    inside the same connection-level transaction. This test is a realistic
    backstop, not the deterministic proof above -- it is included in
    addition to, never instead of, the forced-interleave test.
    """
    from rss_ticker.store import NewArticle

    feed_id = store.upsert_feed("https://x.example/rss", now=0)
    FAVICON = "data:image/png;base64,AAAA"

    stop = threading.Event()
    iterations = {"n": 0}
    real_db = store.db

    def on_execute(*args, **kwargs):
        sql = args[0] if args else kwargs.get("sql", "")
        if isinstance(sql, str) and sql.strip().upper().startswith("UPDATE ARTICLES"):
            raise RuntimeError("synthetic failure mid-batch")

    def hammer_failing_inserts():
        while not stop.is_set() and iterations["n"] < 200:
            iterations["n"] += 1
            store.db = _HookedConnection(real_db, on_execute=on_execute)
            try:
                store.insert_articles(
                    feed_id,
                    [NewArticle(guid=f"g{iterations['n']}", title="t", link=None,
                                summary=None, published_at=None)],
                    now=iterations["n"],
                )
            except RuntimeError:
                pass
            finally:
                store.db = real_db

    def write_once():
        time.sleep(0.001)
        store.set_feed_favicon(feed_id, FAVICON)

    t_a = threading.Thread(target=hammer_failing_inserts)
    t_b = threading.Thread(target=write_once)
    t_a.start()
    t_b.start()
    t_b.join(timeout=10)
    stop.set()
    t_a.join(timeout=10)

    assert not t_a.is_alive()
    assert not t_b.is_alive()
    assert store.get_feed(feed_id).favicon == FAVICON, (
        "the committed write was torn by a concurrent thread's rollback"
    )

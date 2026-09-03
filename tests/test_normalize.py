from pathlib import Path

from rss_ticker.normalize import parse_feed, normalize_entry

FIX = Path(__file__).parent / "fixtures"


def test_parses_titles_links_summaries_and_dates():
    entries, dropped = parse_feed((FIX / "simple.xml").read_bytes(), now=999)
    assert dropped == 0
    assert [e.title for e in entries] == ["Fed holds rates steady", "Oil slips below $70"]
    assert entries[0].guid == "urn:a"
    assert entries[0].link == "https://ex.example/a"
    assert "central bank" in entries[0].summary
    assert entries[0].published_at == 1784642520


def test_missing_publish_date_yields_none_not_now():
    entries, _ = parse_feed((FIX / "no_guid.xml").read_bytes(), now=999)
    assert entries[0].published_at is None


def test_guid_falls_back_to_link():
    entries, _ = parse_feed((FIX / "no_guid.xml").read_bytes(), now=999)
    assert entries[0].guid == "https://ex.example/c"


def test_guid_falls_back_to_hash_when_no_id_or_link():
    entries, _ = parse_feed((FIX / "no_guid.xml").read_bytes(), now=999)
    titles = {e.title: e for e in entries}
    hashed = titles["Headline with nothing else"]
    assert len(hashed.guid) == 64
    assert hashed.guid != "Headline with nothing else"


def test_hash_guid_is_stable_across_calls():
    a, _ = parse_feed((FIX / "no_guid.xml").read_bytes(), now=1)
    b, _ = parse_feed((FIX / "no_guid.xml").read_bytes(), now=2)
    assert [e.guid for e in a] == [e.guid for e in b]


def test_entry_without_title_is_dropped_and_counted():
    entries, dropped = parse_feed((FIX / "no_guid.xml").read_bytes(), now=999)
    assert dropped == 1
    assert all(e.title for e in entries)


def test_malformed_feed_with_entries_is_accepted():
    entries, _ = parse_feed((FIX / "malformed_with_entries.xml").read_bytes(), now=999)
    assert [e.title for e in entries] == ["Still parseable"]


def test_normalize_entry_returns_none_for_titleless_entry():
    assert normalize_entry({"link": "https://x"}, now=1) is None


def test_hash_guid_distinguishes_same_title_different_dates():
    a = normalize_entry({"title": "Same"}, now=1)
    b = normalize_entry({"title": "Same", "published_parsed": (2026, 7, 21, 0, 0, 0, 0, 1, 0)},
                        now=1)
    assert a.guid != b.guid


def test_the_entry_title_is_stored_verbatim():
    e = normalize_entry({"title": "T", "author": "A"}, now=1)
    assert e.title == "T"


def test_author_is_captured_from_author_and_dc_creator():
    entries, _ = parse_feed((FIX / "substack.xml").read_bytes(), now=999)
    # substack.xml carries <dc:creator>; feedparser folds it to `author`.
    assert entries[0].author
    entries, _ = parse_feed((FIX / "simple.xml").read_bytes(), now=999)
    assert entries[0].author is None

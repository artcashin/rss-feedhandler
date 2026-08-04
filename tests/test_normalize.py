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


def test_title_format_appends_author():
    entries, _ = parse_feed(
        (FIX / "substack.xml").read_bytes(), now=999,
        title_format="{title} - {author}",
    )
    by_guid = {e.guid: e for e in entries}
    assert by_guid["sub:1"].title == "Markets close higher - Bob Pisani"


def test_title_format_accepts_the_raw_xml_tag_name():
    # Substack's byline arrives as <dc:creator>; the placeholder may name the
    # tag as it appears in the XML, not only feedparser's normalized key.
    entries, _ = parse_feed(
        (FIX / "substack.xml").read_bytes(), now=999,
        title_format="{dc:creator}: {title}",
    )
    by_guid = {e.guid: e for e in entries}
    assert by_guid["sub:1"].title == "Bob Pisani: Markets close higher"


def test_title_format_missing_field_falls_back_to_plain_title():
    entries, _ = parse_feed(
        (FIX / "substack.xml").read_bytes(), now=999,
        title_format="{title} - {author}",
    )
    by_guid = {e.guid: e for e in entries}
    assert by_guid["sub:2"].title == "No byline here"


def test_title_format_blank_field_falls_back_to_plain_title():
    entries, _ = parse_feed(
        (FIX / "substack.xml").read_bytes(), now=999,
        title_format="{title} - {author}",
    )
    by_guid = {e.guid: e for e in entries}
    assert by_guid["sub:3"].title == "Blank byline"


def test_no_title_format_leaves_title_unchanged():
    e = normalize_entry({"title": "T", "author": "A"}, now=1)
    assert e.title == "T"


def test_title_format_does_not_change_hash_guid():
    # The fallback guid hashes the raw feed title, so retuning a feed's
    # title_format later cannot resurrect every cached article as "new".
    a = normalize_entry({"title": "Same"}, now=1)
    b = normalize_entry({"title": "Same", "author": "X"}, now=1,
                        title_format="{title} - {author}")
    assert a.guid == b.guid
    assert b.title == "Same - X"

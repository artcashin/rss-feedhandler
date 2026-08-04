"""Run the widget's inline script under node against a fake DOM.

The widget is the only place in the project where behaviour lives in
JavaScript, so the reconnect gap-fill needs a real execution test rather than
a substring assertion on the served HTML.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WIDGET = Path(__file__).resolve().parents[1] / "src/rss_ticker/static/widget.html"
HARNESS = Path(__file__).resolve().parent / "harness"

NODE = shutil.which("node") or "/opt/homebrew/bin/node"

pytestmark = pytest.mark.skipif(
    not Path(NODE).exists(), reason="node is required to execute the widget script"
)


def widget_script() -> str:
    match = re.search(r"<script>(.*?)</script>", WIDGET.read_text(), re.S)
    assert match, "widget.html has no inline <script> block"
    return match.group(1)


def test_widget_script_is_valid_javascript(tmp_path: Path):
    path = tmp_path / "widget.js"
    path.write_text(widget_script())
    proc = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def run_widget(tmp_path: Path, mode: str = "default") -> dict:
    bundle = "\n".join(
        [
            (HARNESS / "widget_prelude.js").read_text(),
            widget_script(),
            (HARNESS / "widget_driver.js").read_text(),
        ]
    )
    path = tmp_path / "bundle.js"
    path.write_text(bundle)
    env = dict(os.environ)
    env["WIDGET_TEST_MODE"] = mode
    proc = subprocess.run(
        [NODE, str(path)], capture_output=True, text=True, timeout=60, env=env
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_reconnect_gap_fill_pages_through_every_missed_article(tmp_path: Path):
    # 250 articles arrive during a disconnect but /api/news caps a page at 200.
    # Dropping next_cursor leaves a permanent hole in the MIDDLE of the list,
    # which backward scrollback can never reach.
    result = run_widget(tmp_path)
    assert result["after_fetches"] >= 2, "gap fill stopped after the first page"
    assert result["has_first_gap"] is True
    assert result["has_last_gap"] is True, "the oldest end of the gap was never fetched"
    assert result["rows"] == 261, result  # 10 backlog + 250 gap + 1 live
    assert result["unique_rows"] == result["rows"], "articles were rendered twice"


def test_live_article_arriving_mid_fill_is_still_rendered(tmp_path: Path):
    result = run_widget(tmp_path)
    assert result["has_live"] is True, "buffered live article was dropped"


def test_every_news_fetch_carries_the_token(tmp_path: Path):
    result = run_widget(tmp_path)
    assert result["news_fetches_all_authed"] is True


def test_the_websocket_url_carries_the_token(tmp_path: Path):
    # A browser cannot set a header on a WebSocket constructor, so the URL is
    # the only channel there is.
    result = run_widget(tmp_path)
    assert result["ws_url_has_token"] is True


def test_the_token_is_never_rendered_into_the_dom(tmp_path: Path):
    result = run_widget(tmp_path)
    assert result["dom_has_token"] is False


def test_a_401_from_news_denies_and_stops_fetching(tmp_path: Path):
    # The startup backlog load is the widget's very first /api/news call. A
    # 401 there must flip the widget to its denied state and never issue
    # another /api/news call afterward.
    result = run_widget(tmp_path, mode="news_401")
    assert result["empty_text"] == "not authorized"
    assert result["news_count_at_denial"] is not None
    assert result["news_count_at_denial"] >= 1, "the rejected call itself should count"
    assert result["news_count_final"] == result["news_count_at_denial"], (
        "the widget kept issuing /api/news calls after being denied"
    )


def test_fillgap_401_denies_and_stops_paging(tmp_path: Path):
    # The backlog loads fine, but the socket's first gap-fill page comes back
    # 401. fillGap's own catch must route that into deny(), not swallow it.
    result = run_widget(tmp_path, mode="fillgap_401")
    assert result["empty_text"] == "not authorized"
    assert result["news_count_at_denial"] is not None
    assert result["news_count_at_denial"] >= 1
    assert result["news_count_final"] == result["news_count_at_denial"], (
        "fillGap kept issuing calls after being denied"
    )
    assert result["after_fetches"] == 1, "fillGap should have stopped paging after the 401"


def test_socket_4401_denies_without_reconnecting(tmp_path: Path):
    # 4401 is the server's one rejection code and is not fixable by waiting --
    # reconnecting against it is a denial of service we'd inflict on ourselves.
    result = run_widget(tmp_path, mode="socket_4401")
    assert result["empty_text"] == "not authorized"
    assert result["ws_construct_count"] == 1, "a 4401 close must not open a new socket"
    assert result["reconnect_schedules"] == 0, "a 4401 close must not schedule a reconnect"


def test_favicon_is_rendered_from_the_feed_map(tmp_path: Path):
    # headline 8 has feed_id 1, which the fixture /api/feeds gives a favicon.
    result = run_widget(tmp_path)
    assert result["fav_is_img"] is True
    assert result["fav_src"] == "data:image/png;base64,Zml4dHVyZQ=="


def test_fallback_letter_is_rendered_when_a_feed_has_no_favicon(tmp_path: Path):
    # headline 9 has feed_id 2, whose fixture favicon is null -- the row must
    # fall back to a plain letter, not an <img>.
    result = run_widget(tmp_path)
    assert result["fallback_is_img"] is False
    assert result["fallback_text"] == "F"


def test_single_click_does_not_open_and_double_click_does(tmp_path: Path):
    result = run_widget(tmp_path)
    assert result["window_open_after_click"] == 0, "a single click must not open the article"
    assert result["window_open_after_dblclick"] == 1, "a double-click must open exactly once"
    assert result["window_open_last_url"] == "https://example.test/5"
    assert result["window_open_last_target"] == "_blank"


def test_the_token_never_reaches_favicons_or_the_settings_panel(tmp_path: Path):
    # Re-asserts dom_has_token with favicons rendered and the settings panel
    # opened (the driver clicks the gear before taking this snapshot).
    result = run_widget(tmp_path)
    assert result["dom_has_token"] is False


def test_an_ordinary_socket_close_still_reconnects(tmp_path: Path):
    # Contrast case for the 4401 test above: an everyday close (no code) must
    # NOT deny, and must still schedule a reconnect -- otherwise the 4401 test
    # would also pass against a widget that simply never reconnects at all.
    result = run_widget(tmp_path, mode="socket_ordinary")
    assert result["empty_text"] != "not authorized"
    assert result["reconnect_schedules"] >= 1, "an ordinary close must still reconnect"


def test_tabs_render_all_first_then_groups_in_feed_order(tmp_path: Path):
    # Fixture feed 1 is "Markets", feed 2 is "Wealth M", in that order from
    # /api/feeds -- the tab bar must be All, Markets, Wealth M, in that order.
    result = run_widget(tmp_path, mode="tabs")
    assert result["tab_labels"] == ["All", "Markets", "Wealth M"]


def test_clicking_a_tab_filters_to_that_groups_headlines(tmp_path: Path):
    # Mode "tabs" never pushes a live article -- every row on screen at the
    # moment of the click is a BACKLOG row, rendered by append() well before
    # any tab was ever touched. That is what this test needs to prove:
    # applyFilter (run by the click handler) must re-evaluate rows that
    # already existed, not just rows created afterward -- row() already sets
    # display correctly for new rows on its own via isVisible, so a test that
    # only pushed a live article after switching tabs would exercise row(),
    # not applyFilter, and would pass even if applyFilter were a no-op.
    #
    # Backlog is exactly the 10-article default (ids 0..9), feed_id
    # alternating 1/2 -> Markets ids are even (0,2,4,6,8), Wealth M ids are
    # odd (1,3,5,7,9).
    result = run_widget(tmp_path, mode="tabs")
    assert result["rows"] == 10, "expected only the 10-article backlog, no live pushes"
    markets_titles = set(result["visible_titles_after_markets"])
    expected_markets = {"headline " + str(i) for i in range(0, 10, 2)}
    expected_wealthm = {"headline " + str(i) for i in range(1, 10, 2)}
    assert markets_titles == expected_markets, (
        "the Markets tab must show exactly the backlog's own Markets headlines, "
        "not more or fewer"
    )
    assert not (expected_wealthm & markets_titles), (
        "a Wealth M backlog headline (rendered before the tab click) leaked onto the Markets tab"
    )


def test_clicking_all_restores_every_row(tmp_path: Path):
    result = run_widget(tmp_path, mode="tabs")
    assert len(result["visible_titles_after_all"]) == result["rows"]
    assert "headline 8" in result["visible_titles_after_all"]
    assert "headline 9" in result["visible_titles_after_all"]


def test_live_headline_for_a_non_active_tab_is_hidden_and_does_not_bump_the_pill(
    tmp_path: Path,
):
    # On the Markets tab, a Wealth M (hidden) and a Markets (visible) live
    # headline arrive together, batched into a single prepend() call (see
    # MODE "tabs_live" in the driver). Both rows must exist -- nothing is
    # dropped -- but only the Markets one may be visible, and the pill must
    # count exactly that one, not both. A single-headline push can't pin
    # this: a lone hidden push is already excluded by prepend()'s
    # `freshVisible > 0` guard regardless of what's added inside it, and a
    # lone visible push has freshVisible === fresh.length === 1 either way.
    # Only a mixed batch (fresh.length 2, freshVisible 1) exposes
    # "pending += fresh.length" miscounting the hidden row too.
    result = run_widget(tmp_path, mode="tabs_live")
    assert result["wealthm_live_in_rows"] is True, "the hidden live row was dropped entirely"
    assert result["wealthm_live_visible"] is False, "a Wealth M headline leaked onto the Markets tab"
    assert result["markets_live_in_rows"] is True, "the visible live row was dropped entirely"
    assert result["markets_live_visible"] is True, "a Markets headline failed to show on the Markets tab"
    assert result["pill_display_before"] != "block"
    assert result["pill_display_after"] == "block", "the pill should show for the one visible headline"
    assert result["pill_text_after"] == "1 new headline ↑", (
        "the pill must count only the headline visible on this tab, not the hidden one too"
    )


def test_tab_bar_is_hidden_when_no_feed_has_a_group(tmp_path: Path):
    result = run_widget(tmp_path, mode="no_groups")
    assert result["tab_labels"] == [], "a lone All tab should not render"
    assert result["tabs_display"] == "none", "the tab bar container must be hidden"


def test_token_still_never_reaches_the_dom_with_tabs_rendered_and_a_tab_active(
    tmp_path: Path,
):
    # Re-asserts dom_has_token, now including the tab bar in the scanned DOM,
    # and with a non-All tab (Markets) active at the moment of the snapshot.
    result = run_widget(tmp_path, mode="tabs")
    assert result["dom_has_token"] is False


def test_switching_to_a_tab_with_no_loaded_rows_backfills_from_history(tmp_path: Path):
    # The initial page is all Markets/Wealth M; every Substack row is deeper
    # in history. An empty filtered pane has no scrollbar, so without an
    # explicit backfill the tab would stay blank forever even though the
    # server has the articles.
    result = run_widget(tmp_path, mode="tabs_backfill")
    assert result["news_fetches_before_click"] == 1, "fixture drift: expected one initial page"
    assert result["visible_rows"] == 10, result["visible_titles"]
    assert "headline 9" in result["visible_titles"]
    assert "headline 0" in result["visible_titles"], "backfill stopped before the oldest page"


def test_backfill_stops_once_history_is_exhausted(tmp_path: Path):
    result = run_widget(tmp_path, mode="tabs_backfill")
    # 150 articles at 50 a page: the initial load plus two backfill pages.
    assert result["news_fetches_settled"] == 3, result
    assert result["news_fetches_final"] == result["news_fetches_settled"], (
        "backfill kept fetching after history was exhausted"
    )


def test_backfill_stops_after_a_failed_page_instead_of_retrying(tmp_path: Path):
    # A 500 (or 429) on a backward page must end the backfill chain -- the
    # old error path resolved the promise as if it had succeeded, so the
    # chain immediately re-fired the identical request up to the page cap:
    # an unthrottled retry burst against an already-struggling server.
    result = run_widget(tmp_path, mode="tabs_backfill_500")
    assert result["news_fetches_final"] == 2, (
        "expected the initial page plus exactly one failed backfill attempt, "
        f"got {result['news_fetches_final']}"
    )

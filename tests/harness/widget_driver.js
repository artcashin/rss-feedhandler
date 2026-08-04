// Drives the widget under different scenarios, selected by WIDGET_TEST_MODE
// (read by the prelude too, so both sides agree on what's being tested):
//
//   default         - startup backlog + reconnect gap-fill with a live
//                      message landing mid-fill (the original coverage).
//   news_401        - the very first /api/news call (the startup backlog
//                      load) is rejected with 401; the widget must deny and
//                      never fetch again.
//   fillgap_401     - the backlog loads fine, but the first gap-fill page
//                      (the "after" call fillGap() makes once the socket
//                      opens) is rejected with 401; the widget must deny and
//                      stop paging.
//   socket_4401     - the socket closes with code 4401 (the server's one
//                      rejection code); the widget must deny and must not
//                      schedule a reconnect.
//   socket_ordinary - the socket closes with no code at all (an everyday
//                      close); the widget must NOT deny and must still
//                      schedule a reconnect -- the contrast case that keeps
//                      the 4401 test honest.
"use strict";

// MODE is declared once, by the prelude (which the widget's own fetch/close
// stubs also need it for); this file just reads it.

const LIVE = {
  id: 9999,
  cursor: "9999",
  title: "arrived while filling",
  link: "https://example.test/live",
  summary: null,
  source: "Fixture",
  published_at: 99999,
  sort_at: 99999,
  highlighted: false,
  feed_id: 1,
};

// feed_id 2 is "Wealth M" in the fixture -- used to drive a live headline for
// a group other than the active tab.
const WEALTHM_LIVE = {
  id: 8888,
  cursor: "8888",
  title: "wealth m live headline",
  link: "https://example.test/wealthm-live",
  summary: null,
  source: "Fixture",
  published_at: 88888,
  sort_at: 88888,
  highlighted: false,
  feed_id: 2,
};

// feed_id 1 is "Markets" -- the contrast case, pushed together with
// WEALTHM_LIVE in a single batched prepend() call (see MODE "tabs_live"
// below) so the pill-count assertion can actually distinguish
// "pending += freshVisible" from "pending += fresh.length". A lone push of
// either article can't: a lone hidden push is already excluded by the
// `freshVisible > 0` guard regardless of what's added inside it, and a lone
// visible push has freshVisible === fresh.length === 1 either way. Only a
// mixed batch (fresh.length 2, freshVisible 1) makes the two expressions
// diverge.
const MARKETS_LIVE = {
  id: 7777,
  cursor: "7777",
  title: "markets live headline",
  link: "https://example.test/markets-live",
  summary: null,
  source: "Fixture",
  published_at: 77777,
  sort_at: 77777,
  highlighted: false,
  feed_id: 1,
};

// Finds a rendered tab by its exact label text (among the tab container's
// children) and invokes its onclick, the same way the gear is driven above.
function clickTab(label) {
  const t = els.tabs.children.find((c) => c.textContent === label);
  if (!t) throw new Error("no tab labelled " + JSON.stringify(label));
  t.onclick();
}

function textOf(el) {
  return (
    [el.textContent, el.href, el.title, el.className, el.src, el.alt].join(" ") +
    el.children.map(textOf).join(" ")
  );
}

function findRow(title) {
  return els.list.children.find(
    (c) => c.tagName === "A" && c.children[2].textContent === title
  );
}

function snapshot() {
  const rows = els.list.children.filter((c) => c.tagName === "A");
  const titles = rows.map((r) => r.children[2].textContent);
  const visibleRows = rows.filter((r) => r.style.display !== "none");
  const visibleTitles = visibleRows.map((r) => r.children[2].textContent);
  // Includes the gear/settings elements AND the tab bar -- a hidden panel or
  // an inactive tab is still part of the DOM, and the token must not appear
  // in any of it.
  const dom = ["list", "state", "count", "empty", "pill", "dot", "gear", "config", "cfglist", "cfgclose", "tabs"]
    .map((id) => textOf(els[id]))
    .join(" ");
  return {
    after_fetches: state.afterFetches.length,
    rows: rows.length,
    unique_rows: new Set(titles).size,
    has_live: titles.indexOf(LIVE.title) >= 0,
    has_first_gap: titles.indexOf("headline " + BASE_COUNT) >= 0,
    has_last_gap: titles.indexOf("headline " + (BASE_COUNT + GAP_COUNT - 1)) >= 0,
    ws_url_has_token: String(state.wsUrl || "").indexOf(TOKEN) >= 0,
    news_fetches_all_authed: state.newsFetches.every(
      (u) => u.indexOf("token=" + TOKEN) >= 0
    ),
    dom_has_token: dom.indexOf(TOKEN) >= 0,
    empty_text: els.empty.textContent,
    state_text: els.state.textContent,
    news_fetch_count: state.newsFetches.length,
    ws_construct_count: state.wsConstructCount || 0,
    reconnect_schedules: state.reconnectSchedules || 0,
    news_count_at_denial: null,
    news_count_final: state.newsFetches.length,
    // New fields for the tabs feature. rows/unique_rows/titles above are
    // untouched -- these are additive, computed over the visible-only subset.
    visible_rows: visibleRows.length,
    visible_titles: visibleTitles,
    tab_labels: els.tabs.children.map((c) => c.textContent),
    tabs_display: els.tabs.style.display,
  };
}

(async () => {
  const tick = () => new Promise((r) => setTimeout(r, 5));

  // Polls until deny() has visibly landed (the empty element flips to "not
  // authorized"), recording the /api/news call count at that instant, then
  // keeps ticking so a caller can check the count never grows afterward.
  async function watchForDenial(maxWait, settleTicks) {
    let atDenial = null;
    for (let i = 0; i < maxWait; i++) {
      await tick();
      if (atDenial === null && els.empty.textContent === "not authorized") {
        atDenial = state.newsFetches.length;
      }
    }
    for (let i = 0; i < settleTicks; i++) await tick();
    return atDenial;
  }

  if (MODE === "news_401") {
    const atDenial = await watchForDenial(20, 20);
    const out = snapshot();
    out.news_count_at_denial = atDenial;
    console.log(JSON.stringify(out));
    return;
  }

  if (MODE === "fillgap_401") {
    for (let i = 0; i < 5; i++) await tick();
    state.ws.onopen();
    const atDenial = await watchForDenial(20, 20);
    const out = snapshot();
    out.news_count_at_denial = atDenial;
    console.log(JSON.stringify(out));
    return;
  }

  if (MODE === "socket_4401") {
    for (let i = 0; i < 5; i++) await tick();
    state.ws.onclose({ code: 4401 });
    for (let i = 0; i < 10; i++) await tick();
    console.log(JSON.stringify(snapshot()));
    return;
  }

  if (MODE === "socket_ordinary") {
    for (let i = 0; i < 5; i++) await tick();
    state.ws.onclose();
    for (let i = 0; i < 10; i++) await tick();
    console.log(JSON.stringify(snapshot()));
    return;
  }

  if (MODE === "no_groups") {
    // Every fixture feed has group: null -- the tab bar must not render at
    // all (a lone "All" tab would be pointless).
    for (let i = 0; i < 5; i++) await tick();
    console.log(JSON.stringify(snapshot()));
    return;
  }

  if (MODE === "tabs") {
    // Backlog loads (feed_id alternates 1/2 -> Markets/Wealth M), tabs render,
    // then drive: click Markets, snapshot, click All, snapshot, click Markets
    // again so the final snapshot (also used for the token-safety re-check)
    // lands with a non-All tab active.
    for (let i = 0; i < 5; i++) await tick();
    clickTab("Markets");
    const afterMarkets = snapshot();
    clickTab("All");
    const afterAll = snapshot();
    clickTab("Markets");
    const out = snapshot();
    out.visible_titles_after_markets = afterMarkets.visible_titles;
    out.visible_titles_after_all = afterAll.visible_titles;
    console.log(JSON.stringify(out));
    return;
  }

  if (MODE === "tabs_backfill") {
    // The initial page holds only Markets/Wealth M rows; every Substack row
    // is deeper in history. Clicking the Substack tab must page backward
    // until those rows are on screen -- an empty pane has no scrollbar, so
    // the scroll handler alone can never ask for more.
    for (let i = 0; i < 5; i++) await tick();
    const fetchesBeforeClick = state.newsFetches.length;
    clickTab("Substack");
    for (let i = 0; i < 60; i++) await tick();
    const out = snapshot();
    out.news_fetches_before_click = fetchesBeforeClick;
    const settled = state.newsFetches.length;
    for (let i = 0; i < 20; i++) await tick();
    out.news_fetches_settled = settled;
    out.news_fetches_final = state.newsFetches.length;
    console.log(JSON.stringify(out));
    return;
  }

  if (MODE === "tabs_backfill_500") {
    // Every backward page 500s. The backfill chain must stop after the
    // first failure rather than burning through the page cap in an
    // unthrottled retry burst against a struggling server.
    for (let i = 0; i < 5; i++) await tick();
    clickTab("Substack");
    for (let i = 0; i < 60; i++) await tick();
    const out = snapshot();
    out.news_fetches_final = state.newsFetches.length;
    console.log(JSON.stringify(out));
    return;
  }

  if (MODE === "tabs_live") {
    // On the Markets tab, a hidden (Wealth M) and a visible (Markets) live
    // headline arrive together. "Force not-at-top" alone isn't enough to
    // pin pill-honesty (see the comment on MARKETS_LIVE above) -- the two
    // messages are pushed synchronously right after ws.onopen(), while
    // fillGap() has set filling=true, so BOTH land in pendingLive and are
    // flushed together as one prepend([...]) call once the (empty, per the
    // tabs_live fixture override) gap fetch resolves. That single call has
    // fresh.length === 2 but only one visible row, which is what actually
    // distinguishes "pending += freshVisible" from "pending += fresh.length".
    for (let i = 0; i < 5; i++) await tick();
    clickTab("Markets");
    els.list.scrollTop = 100; // not-at-top, so prepend() takes the pill-bump branch
    state.ws.onopen(); // filling = true
    const pillBefore = { text: els.pill.textContent, display: els.pill.style.display };
    state.ws.onmessage({ data: JSON.stringify(WEALTHM_LIVE) }); // hidden
    state.ws.onmessage({ data: JSON.stringify(MARKETS_LIVE) }); // visible
    for (let i = 0; i < 10; i++) await tick(); // let the (empty) gap fetch resolve, then flush
    const out = snapshot();
    const rows = els.list.children.filter((c) => c.tagName === "A");
    out.wealthm_live_in_rows = rows.some((r) => r.children[2].textContent === WEALTHM_LIVE.title);
    out.wealthm_live_visible = out.visible_titles.indexOf(WEALTHM_LIVE.title) >= 0;
    out.markets_live_in_rows = rows.some((r) => r.children[2].textContent === MARKETS_LIVE.title);
    out.markets_live_visible = out.visible_titles.indexOf(MARKETS_LIVE.title) >= 0;
    out.pill_text_before = pillBefore.text;
    out.pill_display_before = pillBefore.display;
    out.pill_text_after = els.pill.textContent;
    out.pill_display_after = els.pill.style.display;
    console.log(JSON.stringify(out));
    return;
  }

  // default: the original reconnect gap-fill coverage, byte-for-byte, plus
  // favicon/fallback rendering and click/double-click coverage appended --
  // none of it depends on socket/network state, so it rides along on the
  // same run instead of needing its own scenario.
  for (let i = 0; i < 5; i++) await tick();
  state.ws.onopen();
  state.ws.onmessage({ data: JSON.stringify(LIVE) });
  for (let i = 0; i < 60; i++) await tick();

  // Opening the settings panel is part of what the token-safety snapshot
  // below must cover, per the dom_has_token check above.
  els.gear.onclick();

  // headline 8 -> feed_id 1 (has a favicon); headline 9 -> feed_id 2 (none).
  const favIcon = findRow("headline 8").children[1];
  const fallbackIcon = findRow("headline 9").children[1];

  // A single click on a row must never call window.open; a double-click
  // (on the very same row) must call it exactly once, with that row's link.
  const clickRow = findRow("headline 5");
  clickRow.onclick({ preventDefault() {} });
  const windowOpenAfterClick = state.windowOpenCalls.length;
  clickRow.ondblclick({ preventDefault() {} });
  const windowOpenAfterDblclick = state.windowOpenCalls.length;
  const lastOpen = state.windowOpenCalls[state.windowOpenCalls.length - 1];

  const out = snapshot();
  out.fav_is_img = favIcon.tagName === "IMG";
  out.fav_src = favIcon.src || null;
  out.fallback_is_img = fallbackIcon.tagName === "IMG";
  out.fallback_text = fallbackIcon.textContent;
  out.window_open_after_click = windowOpenAfterClick;
  out.window_open_after_dblclick = windowOpenAfterDblclick;
  out.window_open_last_url = lastOpen ? lastOpen.url : null;
  out.window_open_last_target = lastOpen ? lastOpen.target : null;
  console.log(JSON.stringify(out));
})();

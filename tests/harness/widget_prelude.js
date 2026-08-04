// Minimal fake DOM + network, just enough to run the widget's IIFE under node.
// The widget is plain ES5 DOM code, so a handful of stubs is cheaper (and far
// more predictable) than pulling in a headless browser.
"use strict";

const BASE_COUNT = 10; // articles the widget already holds before the gap
const GAP_COUNT = 250; // articles that arrived while the socket was down
const PAGE_LIMIT = 200; // what a single /api/news call can return

const state = { newsFetches: [], afterFetches: [], ws: null, windowOpenCalls: [] };

const TOKEN = "tkn-" + "0123456789abcdef".repeat(3);

// Fixture feed favicons: feed 1 has one (a data URI, same as the server
// would resolve), feed 2 deliberately has none so fallback rendering gets
// exercised too.
const FAVICON_1 = "data:image/png;base64,Zml4dHVyZQ==";

// Selects which rejection scenario (if any) this run drives. "default" keeps
// every fetch/close succeeding, exactly as before -- the existing tests never
// set this variable, so their behaviour is untouched.
const MODE = (typeof process !== "undefined" && process.env.WIDGET_TEST_MODE) || "default";

// Every setTimeout call the WIDGET makes (as opposed to the driver's own 5ms
// ticks) is a reconnect schedule -- fillPage/loadOlder never use a timer.
const realSetTimeout = globalThis.setTimeout;
state.reconnectSchedules = 0;
globalThis.setTimeout = function (fn, delay) {
  if (delay !== 5) state.reconnectSchedules++;
  return realSetTimeout(fn, delay);
};

globalThis.location = {
  search: "?user=art&token=" + TOKEN,
  protocol: "http:",
  host: "ticker.test",
};

function makeEl(tag) {
  return {
    // Real browsers uppercase Element.tagName for HTML elements
    // (createElement("a").tagName === "A", not "a"). Modeling it lowercase
    // here let a real production bug (widget.html's applyFilter comparing
    // tagName to a lowercase "a" literal) pass every test while silently
    // never filtering a single already-rendered row in an actual browser.
    tagName: String(tag).toUpperCase(),
    className: "",
    textContent: "",
    href: "",
    target: "",
    rel: "",
    title: "",
    // Generic img-ish fields. Real DOM elements that aren't <img> simply
    // ignore src/alt; here every element carries them so createElement("img")
    // rows can record what was assigned, without a tag-specific branch.
    src: "",
    alt: "",
    style: {},
    children: [],
    parentNode: null,
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 0,
    isFragment: tag === "#fragment",
    // Attribute bag backing setAttribute/getAttribute. The widget uses this
    // (from the tabs feature) to stash a row's feed_id (data-feed) so
    // visibility can be re-evaluated later without keeping a side table.
    attrs: {},
    setAttribute(name, value) {
      this.attrs[name] = String(value);
    },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this.attrs, name)
        ? this.attrs[name]
        : null;
    },
    get firstChild() {
      return this.children[0] || null;
    },
    get nextSibling() {
      if (!this.parentNode) return null;
      const i = this.parentNode.children.indexOf(this);
      return i < 0 ? null : this.parentNode.children[i + 1] || null;
    },
    addEventListener() {},
    appendChild(node) {
      return this.insertBefore(node, null);
    },
    insertBefore(node, ref) {
      const kids = node.isFragment ? node.children.splice(0) : [node];
      let at = ref ? this.children.indexOf(ref) : -1;
      if (at < 0) at = this.children.length;
      this.children.splice(at, 0, ...kids);
      kids.forEach((k) => {
        k.parentNode = this;
      });
      return node;
    },
  };
}

const els = {};
["list", "pill", "empty", "dot", "state", "count", "gear", "config", "cfglist", "cfgclose", "tabs"].forEach(
  (id) => {
    els[id] = makeEl("div");
  }
);
els.list.appendChild(els.pill);
els.list.appendChild(els.empty);

globalThis.document = {
  getElementById: (id) => els[id],
  createElement: (tag) => makeEl(tag),
  createDocumentFragment: () => makeEl("#fragment"),
};

// window.open spy: the only way the widget is allowed to navigate anywhere is
// through this call, on a double-click (or Enter on a focused row).
globalThis.window = {
  open: function (url, target, features) {
    state.windowOpenCalls.push({ url: url, target: target, features: features });
  },
};

// Article i has id i, sort_at 1000+i and cursor String(i). Ids 0..BASE_COUNT-1
// are the backlog the widget loads at startup; the rest are the gap.
// feed_id alternates between the two fixture feeds below, so every run mixes
// favicon and fallback rows without any test needing its own scenario.
function article(i) {
  return {
    id: i,
    cursor: String(i),
    title: "headline " + i,
    link: "https://example.test/" + i,
    summary: null,
    source: "Fixture",
    published_at: 1000 + i,
    sort_at: 1000 + i,
    highlighted: false,
    feed_id: i % 2 === 0 ? 1 : 2,
  };
}

function jsonOk(body) {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
}

function jsonDenied() {
  return Promise.resolve({
    ok: false,
    status: 401,
    json: () => Promise.resolve({ error: "unauthorized" }),
  });
}

// news_401: the very first /api/news call (the startup backlog load) is
// rejected. fillgap_401: the backlog loads fine, but the first gap-fill
// ("after") page is rejected. Each fires only once, so the rest of a run
// behaves normally if anything keeps calling in (it shouldn't).
let newsCallDenied = false;
let afterCallDenied = false;

// tabs_backfill: 150 articles, newest first, paged 50 at a time. The ten
// OLDEST (ids 0..9) belong to feed 3 ("Substack"); everything newer is feeds
// 1/2. So the initial page contains no Substack row at all -- the scenario a
// low-volume feed drowns in.
const BACKFILL_TOTAL = 150;
const BACKFILL_PAGE = 50;

function backfillArticle(i) {
  const a = article(i);
  if (i < 10) a.feed_id = 3;
  return a;
}

function backfillPage(beforeCursor) {
  const start = beforeCursor === null ? BACKFILL_TOTAL - 1 : Number(beforeCursor) - 1;
  const stop = Math.max(start - BACKFILL_PAGE + 1, 0);
  const articles = [];
  for (let i = start; i >= stop; i--) articles.push(backfillArticle(i));
  return {
    articles: articles,
    next_cursor: stop > 0 ? String(stop) : null,
  };
}

globalThis.fetch = function (url) {
  if (url.startsWith("/api/feeds")) {
    if (MODE === "tabs_backfill" || MODE === "tabs_backfill_500") {
      return jsonOk({
        feeds: [
          { id: 1, name: "Feed One", url: "https://feed1.test/rss", favicon: FAVICON_1, group: "Markets" },
          { id: 2, name: "Feed Two", url: "https://feed2.test/rss", favicon: null, group: "Wealth M" },
          { id: 3, name: "Feed Three", url: "https://feed3.test/rss", favicon: null, group: "Substack" },
        ],
      });
    }
    // no_groups drives the "hide the tab bar entirely" case: every feed's
    // group is null. Every other mode (including all the pre-tabs scenarios)
    // gets two distinct groups, so tabs render as a matter of course without
    // any of the older assertions (which never look at groups) noticing.
    if (MODE === "no_groups") {
      return jsonOk({
        feeds: [
          { id: 1, name: "Feed One", url: "https://feed1.test/rss", favicon: FAVICON_1, group: null },
          { id: 2, name: "Feed Two", url: "https://feed2.test/rss", favicon: null, group: null },
        ],
      });
    }
    return jsonOk({
      feeds: [
        { id: 1, name: "Feed One", url: "https://feed1.test/rss", favicon: FAVICON_1, group: "Markets" },
        { id: 2, name: "Feed Two", url: "https://feed2.test/rss", favicon: null, group: "Wealth M" },
      ],
    });
  }

  const q = new URLSearchParams(url.split("?")[1] || "");
  state.newsFetches.push(url);

  if (q.has("after")) {
    state.afterFetches.push(url);
    if (MODE === "fillgap_401" && !afterCallDenied) {
      afterCallDenied = true;
      return jsonDenied();
    }
    if (MODE === "tabs_live") {
      // No real gap in this scenario -- keeps the eventual batched prepend()
      // limited to exactly the two "live" messages the driver pushes while
      // filling is true, so the pill-count assertion isn't diluted by
      // unrelated gap articles.
      return jsonOk({ articles: [], next_cursor: null });
    }
    const from = Number(q.get("after")) + 1;
    const limit = Math.min(Number(q.get("limit") || "50"), PAGE_LIMIT);
    const last = BASE_COUNT + GAP_COUNT - 1;
    const upto = Math.min(from + limit - 1, last);
    const articles = [];
    for (let i = from; i <= upto; i++) articles.push(article(i));
    return jsonOk({
      articles: articles,
      next_cursor: upto < last ? String(upto) : null,
    });
  }

  if (MODE === "news_401" && !newsCallDenied) {
    newsCallDenied = true;
    return jsonDenied();
  }

  if (MODE === "tabs_backfill") {
    return jsonOk(backfillPage(q.get("before")));
  }

  // tabs_backfill_500: same fixture, but every backward ("before") page
  // fails with 500 -- the server is down or rate-limiting mid-backfill.
  if (MODE === "tabs_backfill_500") {
    if (q.has("before")) {
      return Promise.resolve({
        ok: false,
        status: 500,
        json: () => Promise.resolve({ error: "boom" }),
      });
    }
    return jsonOk(backfillPage(null));
  }

  // Startup backlog, newest first, and there is nothing older.
  const articles = [];
  for (let i = BASE_COUNT - 1; i >= 0; i--) articles.push(article(i));
  return jsonOk({ articles: articles, next_cursor: null });
};

globalThis.WebSocket = function (url) {
  state.ws = this;
  state.wsUrl = url;
  state.wsConstructCount = (state.wsConstructCount || 0) + 1;
  this.close = function () {};
};

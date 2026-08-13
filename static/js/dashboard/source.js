// source.js: the DeckSource port, its shaping, and ServerSource (the
// dashboard JSON payload). The dashboard components never touch
// storage or the network; they render an overview a source hands
// them, so the same views run on every surface.
//
// LocalSource lives in local-source.js, not here: it pulls in the
// offline store and the study scheduler, and the signed-in dashboard
// is on the critical path of the most-trafficked page. Importing this
// module must not drag the offline stack into it.
//
// DeckSource contract (one method, async):
//
//   overview() -> {
//     user: {display_name, is_anonymous} | null,
//     decks: [Deck],
//     due, total,                   aggregate over every card, which
//                                   is NOT the sum of the deck rows:
//                                   a card filed under no deck counts
//                                   here and in no row.
//     nextDueMinutes: number|null,  minutes until the next FUTURE due,
//                                   null when nothing is scheduled.
//     unsynced: {reviews, cards} | null,
//   }
//
//   Deck: {id, slug, display_name, due, total, deck_type, pinned,
//          trivia_stats}
//
//     `slug` is the URL segment (the server's deck.name);
//     `display_name` always carries something renderable, falling
//     back to the slug. `trivia_stats` is {mastered, wrong, total} or
//     null; only a trivia deck has one.
//
// null means "this surface cannot answer", never "zero". `user` null
// is an unresolved identity, not an anonymous one. `unsynced` null is
// a surface with no outbox (the server); LocalSource always answers
// with counts, {reviews: 0, cards: 0} included, because it knows.
//
// Failures from ServerSource are DashboardSourceError, whose `code` a
// host can branch on: network, unauthorized, not_found, server,
// bad_response.

// ---- shaping -----------------------------------------------------------

function num(value, fallback = 0) {
  return Number.isFinite(value) ? value : fallback;
}

// The label a deck renders and sorts under, on either surface: always
// something renderable, falling back to the slug.
export function deckLabel(raw) {
  return raw.display_name || raw.slug || raw.name || "";
}

export function shapeDeck(raw, counts) {
  const stats = raw.trivia_stats || null;
  return {
    id: raw.id,
    slug: raw.slug || raw.name || "",
    display_name: deckLabel(raw),
    due: num(counts ? counts.due : raw.due),
    total: num(counts ? counts.total : raw.total),
    deck_type: raw.deck_type || "srs",
    // The server payload states the fact; a snapshot row carries the
    // timestamp the server pins by, which the offline order needs too.
    pinned: Boolean(raw.pinned ?? raw.pinned_at),
    trivia_stats: stats
      ? {
          mastered: num(stats.mastered),
          wrong: num(stats.wrong),
          total: num(stats.total),
        }
      : null,
  };
}

// A payload from anywhere (an embedded script tag, a JSON response)
// coerced into the contract, so a view never sees a missing key.
export function normalizeOverview(raw) {
  const source = raw || {};
  const user = source.user
    ? {
        display_name: source.user.display_name || null,
        is_anonymous: Boolean(source.user.is_anonymous),
      }
    : null;
  const decks = (source.decks || []).map((deck) => shapeDeck(deck, null));
  const unsynced = source.unsynced
    ? {reviews: num(source.unsynced.reviews), cards: num(source.unsynced.cards)}
    : null;
  return {
    user,
    decks,
    due: num(source.due, decks.reduce((sum, d) => sum + d.due, 0)),
    total: num(source.total, decks.reduce((sum, d) => sum + d.total, 0)),
    nextDueMinutes: Number.isFinite(source.nextDueMinutes) ? source.nextDueMinutes : null,
    unsynced,
  };
}

// ---- the server source -------------------------------------------------

export class DashboardSourceError extends Error {
  constructor(code, message, detail) {
    super(message || code);
    this.name = "DashboardSourceError";
    this.code = code;
    if (detail) Object.assign(this, detail);
  }
}

// The deploy's root path, derived from this module's own URL: the
// module is served under <root>/static/js/[v<build>/]dashboard/source.js.
const ROOT_PATH = new URL(import.meta.url).pathname.replace(/\/static\/js\/.*$/, "");

const EMBEDDED_ID = "dashboard-overview";

// The overview the dashboard shell embeds for the first paint. Absent
// (no element) and unreadable (bad JSON) both answer null: either way
// the host has to fetch, and telling them apart would only change a
// log line.
export function readEmbeddedOverview(doc = document, id = EMBEDDED_ID) {
  const node = doc.getElementById(id);
  if (!node) return null;
  try {
    return normalizeOverview(JSON.parse(node.textContent || ""));
  } catch (e) {
    return null;
  }
}

// DeckSource over the dashboard JSON endpoint. Holds no DOM and no
// view state: just where to ask and how to fail.
export class ServerSource {
  constructor({basePath = ROOT_PATH, fetchImpl = null} = {}) {
    this.basePath = basePath;
    this._fetch = fetchImpl || ((...args) => fetch(...args));
  }

  async overview() {
    return normalizeOverview(await this._request(`${this.basePath}/api/dashboard/overview`));
  }

  async _request(url) {
    let res;
    try {
      res = await this._fetch(url, {
        method: "GET",
        headers: {Accept: "application/json"},
        // Identity rides cookies or a proxy-injected header, both of
        // which need the request treated as same-origin credentialed.
        credentials: "same-origin",
        cache: "no-store",
      });
    } catch (e) {
      throw new DashboardSourceError("network", "could not reach the server", {cause: e});
    }
    return this._parse(res, await res.text().catch(() => ""));
  }

  _parse(res, text) {
    let payload = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch (e) {
        payload = null;
      }
    }
    // Accept: application/json makes the app answer 401 with JSON
    // rather than bouncing to the identity provider, so an expired
    // session is a status code here. A redirect that still landed on
    // a non-JSON body is the same condition on a deploy that bounces
    // earlier, in middleware.
    if (res.status === 401 || res.status === 403) {
      throw new DashboardSourceError("unauthorized", "you are no longer signed in");
    }
    if (res.redirected && payload === null) {
      throw new DashboardSourceError("unauthorized", "you are no longer signed in");
    }
    const err = (payload && payload.error) || {};
    if (res.status === 404) {
      throw new DashboardSourceError("not_found", err.message || "not found");
    }
    if (!res.ok) {
      throw new DashboardSourceError(
        err.code || "server",
        err.message || `request failed (${res.status})`,
        {status: res.status}
      );
    }
    if (payload === null) {
      throw new DashboardSourceError("bad_response", "the server did not answer with JSON");
    }
    return payload;
  }
}

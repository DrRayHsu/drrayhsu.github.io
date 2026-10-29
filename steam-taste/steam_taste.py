#!/usr/bin/env python3
"""Find out what you actually like to play, from your Steam data.

Pulls owned games, playtime, wishlist and achievements from the Steam Web API,
tags every game with SteamSpy's user tags, and writes:

  out/report.html         charts and tables, open in a browser
  out/taste_summary.json  the same findings, compact enough to paste into Claude

Standard library only. Usage:

  export STEAM_API_KEY=...   # https://steamcommunity.com/dev/apikey
  export STEAM_ID=...        # SteamID64, vanity name, or profile URL
  python3 steam_taste.py [--purchase-history history.html]
"""

import argparse
import html
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

API = "https://api.steampowered.com"
STEAMSPY = "https://steamspy.com/api.php"
STORE = "https://store.steampowered.com/api/appdetails"

DAY = 86400
TOP_TAGS = 12  # tags kept per game, by vote count
MEMBER = 0.3  # a game "is" a tag if the tag has >=30% of the votes of its top tag


# ---------------------------------------------------------------- fetching


class FetchError(Exception):
    def __init__(self, code, where):
        super().__init__(f"HTTP {code} from {where}")
        self.code = code


class Fetcher:
    """JSON GETs with an on-disk cache and per-host throttling."""

    INTERVAL = {"steamspy.com": 1.1, "store.steampowered.com": 1.6, "api.steampowered.com": 0.15}

    def __init__(self, cache_dir, refresh=False):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.refresh = refresh
        self._last = {}

    def _path(self, key):
        return self.cache_dir / f"{key}.json"

    def put(self, key, data):
        self._path(key).write_text(json.dumps(data))

    def get(self, url, params, cache_key=None, max_age=None):
        if cache_key and not self.refresh:
            p = self._path(cache_key)
            if p.exists() and (max_age is None or time.time() - p.stat().st_mtime < max_age):
                return json.loads(p.read_text())

        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        wait = self.INTERVAL.get(host, 0.5) - (time.monotonic() - self._last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(params)}", headers={"User-Agent": "steam-taste/1.0"}
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.load(r)
                break
            except urllib.error.HTTPError as e:
                # Never surface the URL: it carries the API key.
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(5 * 2**attempt)
                    continue
                raise FetchError(e.code, host + parts.path) from None
            except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
                if attempt < 3:
                    time.sleep(5 * 2**attempt)
                    continue
                raise FetchError("network", host + parts.path) from None
            finally:
                self._last[host] = time.monotonic()
        if cache_key:
            self.put(cache_key, data)
        return data


def progress(msg):
    print(msg, file=sys.stderr, flush=True)


def resolve_steamid(f, key, ident):
    ident = ident.strip().rstrip("/")
    m = re.search(r"(\d{17})$", ident)
    if m:
        return m.group(1)
    m = re.search(r"/id/([^/?#]+)$", ident)
    vanity = m.group(1) if m else ident
    r = f.get(f"{API}/ISteamUser/ResolveVanityURL/v1/", {"key": key, "vanityurl": vanity})
    r = r.get("response", {})
    if r.get("success") != 1:
        sys.exit(f"Could not resolve Steam ID {ident!r}. Use your 17-digit SteamID64.")
    return r["steamid"]


def fetch_owned(f, key, steamid):
    r = f.get(
        f"{API}/IPlayerService/GetOwnedGames/v1/",
        {"key": key, "steamid": steamid, "include_appinfo": 1, "include_played_free_games": 1},
        cache_key=f"owned_{steamid}",
        max_age=6 * 3600,
    )
    games = r.get("response", {}).get("games")
    if not games:
        sys.exit(
            "Steam returned no games. Set Profile > Edit Profile > Privacy Settings > "
            "Game details to Public (you can switch it back afterwards)."
        )
    return [
        {
            "appid": g["appid"],
            "name": g.get("name") or str(g["appid"]),
            "minutes": g.get("playtime_forever", 0),
            "last_played": g.get("rtime_last_played", 0),
        }
        for g in games
    ]


def fetch_wishlist(f, key, steamid):
    try:
        r = f.get(
            f"{API}/IWishlistService/GetWishlist/v1/",
            {"key": key, "steamid": steamid},
            cache_key=f"wishlist_{steamid}",
            max_age=6 * 3600,
        )
    except FetchError as e:
        progress(f"Wishlist unavailable ({e}); skipping.")
        return []
    items = r.get("response", {}).get("items", [])
    if not items:
        progress("Wishlist is empty or private; skipping the wishlist analysis.")
    return [{"appid": i["appid"], "added": i.get("date_added", 0)} for i in items]


def fetch_tags(f, appid):
    """Return (name, {tag: weight}) with weights relative to the game's top tag."""
    try:
        d = f.get(STEAMSPY, {"request": "appdetails", "appid": appid},
                  cache_key=f"spy_{appid}", max_age=60 * DAY)
    except FetchError:
        d = {}
    name = d.get("name") or ""
    tags = d.get("tags")
    tags = tags if isinstance(tags, dict) else {}
    if not tags:
        # SteamSpy has no user tags for some apps; fall back to store genres.
        try:
            s = f.get(STORE, {"appids": appid, "l": "english"},
                      cache_key=f"store_{appid}", max_age=60 * DAY)
            entry = s.get(str(appid)) or {}
        except FetchError:
            entry = {}
        if entry.get("success"):
            data = entry["data"]
            name = name or data.get("name", "")
            tags = {g["description"]: 1 for g in data.get("genres", [])}
    top = sorted(tags.items(), key=lambda kv: -kv[1])[:TOP_TAGS]
    peak = top[0][1] if top else 1
    return name, {t: v / peak for t, v in top}


def fetch_achievements(f, key, steamid, appid):
    """Return (unlocked, total), or None when the game has no achievements."""
    ck = f"ach_{steamid}_{appid}"
    try:
        r = f.get(
            f"{API}/ISteamUserStats/GetPlayerAchievements/v1/",
            {"key": key, "steamid": steamid, "appid": appid},
            cache_key=ck,
            max_age=7 * DAY,
        )
    except FetchError as e:
        if e.code in (400, 403):  # no stats for this app
            f.put(ck, {})
            return None
        raise
    ach = r.get("playerstats", {}).get("achievements") or []
    if not ach:
        return None
    return sum(a.get("achieved", 0) for a in ach), len(ach)


# ---------------------------------------------------------------- purchase history


class _HistoryParser(HTMLParser):
    """Reads the table on store.steampowered.com/account/history."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._row = None
        self._cell = None
        self._segs = []
        self._buf = []
        self._skip = [False]

    def _flush(self):
        text = " ".join("".join(self._buf).split())
        if text and not self._skip[-1]:
            self._segs.append(text)
        self._buf = []

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class") or ""
        if tag == "tr" and "wallet_table_row" in cls:
            self._row = {}
        elif tag == "td" and self._row is not None:
            self._cell = next((c for c in cls.split() if c.startswith("wht_")), "other")
            self._segs, self._buf, self._skip = [], [], [False]
        elif tag == "div" and self._cell:
            self._flush()
            self._skip.append(self._skip[-1] or "payment" in cls)

    def handle_endtag(self, tag):
        if tag == "div" and self._cell:
            self._flush()
            if len(self._skip) > 1:
                self._skip.pop()
        elif tag == "td" and self._cell:
            self._flush()
            self._row[self._cell] = self._segs
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell:
            self._buf.append(data)


def _parse_date(s):
    s = s.strip()
    for fmt in ("%d %b, %Y", "%b %d, %Y", "%d %b %Y", "%b %d %Y", "%Y-%m-%d", "%d %B, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def parse_purchase_history(text):
    p = _HistoryParser()
    p.feed(text)
    txns = []
    for row in p.rows:
        date = _parse_date((row.get("wht_date") or [""])[0])
        kind = " ".join(row.get("wht_type") or [])
        items = row.get("wht_items") or []
        if date and items:
            txns.append({"date": date, "type": kind, "items": items})
    return txns


def norm_name(s):
    s = re.sub(r"[™®©]", "", s.lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# ---------------------------------------------------------------- analysis


def hours(g):
    return g["minutes"] / 60


def engagement(g):
    # log so a single 2,000-hour game doesn't drown out everything else
    return math.log1p(hours(g))


def tag_dist(games, weight=lambda g: 1.0):
    """Share of each tag across games; each game's tags sum to 1 before weighting."""
    totals, total_w = Counter(), 0.0
    for g in games:
        w, s = weight(g), sum(g["tags"].values())
        if w <= 0 or not s:
            continue
        total_w += w
        for t, v in g["tags"].items():
            totals[t] += w * v / s
    return {t: v / total_w for t, v in totals.items()} if total_w else {}


def support(games):
    return Counter(t for g in games for t, v in g["tags"].items() if v >= MEMBER)


def has(g, tag):
    return g["tags"].get(tag, 0) >= MEMBER


def tag_affinity(games, min_support):
    """Lift = share of your play time / share of your library, per tag."""
    owned, played, sup = tag_dist(games), tag_dist(games, engagement), support(games)
    rows = [
        {"tag": t, "lift": played.get(t, 0) / owned[t], "games": sup[t],
         "library_share": owned[t], "play_share": played.get(t, 0)}
        for t in owned
        if sup[t] >= min_support and owned[t] > 0
    ]
    return sorted(rows, key=lambda r: -r["lift"])


def wishlist_gap(games, wishlist, min_support):
    played = [g for g in games if g["minutes"] > 0]
    wl, pl = tag_dist(wishlist), tag_dist(played, engagement)
    wsup, psup = support(wishlist), support(played)
    rows = [
        {"tag": t, "wishlist_share": wl.get(t, 0), "play_share": pl.get(t, 0),
         "gap": wl.get(t, 0) - pl.get(t, 0)}
        for t in set(wl) | set(pl)
        if wsup[t] >= min_support or psup[t] >= min_support
    ]
    aspirational = sorted((r for r in rows if r["gap"] > 0), key=lambda r: -r["gap"])
    comfort = sorted((r for r in rows if r["gap"] < 0), key=lambda r: r["gap"])
    return aspirational[:10], comfort[:10]


def bounce_by_tag(games, bounce_minutes, min_support):
    started = [g for g in games if g["minutes"] > 0]
    if not started:
        return 0.0, []
    bounced = lambda g: g["minutes"] < bounce_minutes
    overall = sum(map(bounced, started)) / len(started)
    rows = []
    for tag, n in support(started).items():
        if n < min_support:
            continue
        members = [g for g in started if has(g, tag)]
        owned = [g for g in games if has(g, tag)]
        rows.append({
            "tag": tag,
            "games": n,
            "bounce_rate": sum(map(bounced, members)) / n,
            "unplayed_rate": sum(g["minutes"] == 0 for g in owned) / len(owned),
        })
    return overall, sorted(rows, key=lambda r: -r["bounce_rate"])


def core_games(games):
    played = sorted((g for g in games if g["minutes"] > 0), key=lambda g: -g["minutes"])
    if not played:
        return [], []
    hrs = [hours(g) for g in played]
    p90 = statistics.quantiles(hrs, n=10)[-1] if len(hrs) >= 10 else hrs[0]
    threshold = max(20.0, p90)
    core = [g for g in played if hours(g) >= threshold] or played[:5]
    base, cd, sup = tag_dist(played), tag_dist(core), support(core)
    lift = sorted(
        ({"tag": t, "lift": cd[t] / base[t], "games": sup[t]} for t in cd
         if sup[t] >= min(2, len(core)) and base.get(t)),
        key=lambda r: -r["lift"],
    )
    return core, lift


def completion(games, ach, min_support):
    rows = []
    for g in games:
        a = ach.get(g["appid"])
        if a:
            rows.append({**g, "unlocked": a[0], "total": a[1], "rate": a[0] / a[1]})
    if not rows:
        return None
    by_tag = []
    for tag, n in support(rows).items():
        if n >= min_support:
            rates = [r["rate"] for r in rows if has(r, tag)]
            by_tag.append({"tag": tag, "games": n, "avg_rate": sum(rates) / n})
    return {
        "games": len(rows),
        "median_rate": statistics.median(r["rate"] for r in rows),
        "finished": sorted((r for r in rows if r["rate"] >= 0.9), key=lambda r: -r["minutes"]),
        "by_tag": sorted(by_tag, key=lambda r: -r["avg_rate"]),
    }


def drift(games, now, min_support):
    played = [g for g in games if g["minutes"] > 0]
    recent = [g for g in played if g["last_played"] and now - g["last_played"] < 365 * DAY]
    if len(recent) < 3:
        return []
    base, cur, sup = tag_dist(played, engagement), tag_dist(recent, engagement), support(recent)
    rows = [{"tag": t, "ratio": cur[t] / base[t], "games": sup[t]}
            for t in cur if base.get(t) and sup[t] >= min_support]
    return sorted(rows, key=lambda r: -r["ratio"])


def purchase_analysis(txns, games, bounce_minutes):
    by_name = {norm_name(g["name"]): g for g in games}
    bought, refunds, unmatched = [], [], 0
    for t in txns:
        kind = t["type"].lower()
        if "refund" in kind:
            refunds.extend((t["date"], name) for name in t["items"])
            continue
        if "purchase" not in kind or any(k in kind for k in ("gift", "in-game", "market")):
            continue
        for name in t["items"]:
            g = by_name.get(norm_name(name))
            if g:
                bought.append((t["date"], g))
            else:
                unmatched += 1

    def bucket(pairs):
        n = len(pairs)
        return {
            "bought": n,
            "never_played": sum(g["minutes"] == 0 for _, g in pairs) / n if n else 0,
            "bounced": sum(0 < g["minutes"] < bounce_minutes for _, g in pairs) / n if n else 0,
        }

    months = {m: bucket([p for p in bought if p[0].month == m]) for m in range(1, 13)}
    years = {y: bucket([p for p in bought if p[0].year == y])
             for y in sorted({d.year for d, _ in bought})}
    return {
        "matched": len(bought),
        "unmatched_items": unmatched,
        "by_month": months,
        "by_year": years,
        "refunds": [{"date": d.isoformat(), "name": n} for d, n in sorted(refunds)],
    }


def analyze(games, wishlist, ach, txns, now, min_support, bounce_minutes):
    played = [g for g in games if g["minutes"] > 0]
    total_h = sum(hours(g) for g in games)
    top10 = sum(hours(g) for g in sorted(games, key=lambda g: -g["minutes"])[:10])
    aff = tag_affinity(games, min_support)
    aspirational, comfort = wishlist_gap(games, wishlist, min_support) if wishlist else ([], [])
    overall_bounce, bounce = bounce_by_tag(games, bounce_minutes, min_support)
    core, core_lift = core_games(games)
    return {
        "overview": {
            "owned": len(games),
            "played": len(played),
            "never_played_rate": 1 - len(played) / len(games),
            "total_hours": total_h,
            "top10_hours_share": top10 / total_h if total_h else 0,
            "wishlist": len(wishlist),
            "untagged": sum(not g["tags"] for g in games),
        },
        "affinity": aff,
        "core": core,
        "core_lift": core_lift,
        "aspirational": aspirational,
        "comfort": comfort,
        "overall_bounce": overall_bounce,
        "bounce": bounce,
        "completion": completion(games, ach, min_support) if ach else None,
        "drift": drift(games, now, min_support),
        "purchases": purchase_analysis(txns, games, bounce_minutes) if txns else None,
        "bounce_minutes": bounce_minutes,
    }


def summary_json(r):
    """Compact findings for pasting into a brainstorming chat."""
    tags = lambda g: [t for t, v in sorted(g["tags"].items(), key=lambda kv: -kv[1]) if v >= MEMBER][:6]
    pct = lambda x: round(100 * x)
    c, p = r["completion"], r["purchases"]
    return {
        "how_to_read": (
            "lift = share of play time (log-hours) / share of library. >1 means played more than "
            "owned. Wishlist gap = wishlist share minus play share. Bounce = started but played "
            f"under {r['bounce_minutes']} minutes."
        ),
        "overview": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r["overview"].items()},
        "core_games": [{"name": g["name"], "hours": round(hours(g)), "tags": tags(g)} for g in r["core"][:20]],
        "tags_overrepresented_in_core_games": [x["tag"] for x in r["core_lift"][:12]],
        "tags_played_more_than_owned": [{"tag": x["tag"], "lift": round(x["lift"], 2)} for x in r["affinity"][:12]],
        "tags_owned_but_not_played": [{"tag": x["tag"], "lift": round(x["lift"], 2)} for x in r["affinity"] if x["lift"] < 1][-10:][::-1],
        "wishlisted_more_than_played": [x["tag"] for x in r["aspirational"]],
        "played_more_than_wishlisted": [x["tag"] for x in r["comfort"]],
        "bounce_rate_overall_pct": pct(r["overall_bounce"]),
        "most_bounced_tags": [{"tag": x["tag"], "bounce_pct": pct(x["bounce_rate"])} for x in r["bounce"] if x["bounce_rate"] > r["overall_bounce"]][:10],
        "least_bounced_tags": [{"tag": x["tag"], "bounce_pct": pct(x["bounce_rate"])} for x in r["bounce"] if x["bounce_rate"] <= r["overall_bounce"]][-8:][::-1],
        "rising_in_last_12_months": [{"tag": x["tag"], "ratio": round(x["ratio"], 2)} for x in r["drift"][:8]],
        "completion": c and {
            "games_with_achievements": c["games"],
            "median_completion_pct": pct(c["median_rate"]),
            "finished_90pct": [g["name"] for g in c["finished"][:15]],
            "tags_most_finished": [x["tag"] for x in c["by_tag"][:8]],
            "tags_least_finished": [x["tag"] for x in c["by_tag"][-8:][::-1]],
        },
        "purchases": p and {
            "refunded": [x["name"] for x in p["refunds"]],
            "never_played_pct_by_purchase_month": {m: pct(v["never_played"]) for m, v in p["by_month"].items() if v["bought"]},
        },
    }


# ---------------------------------------------------------------- report

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a65;--line:#e4e1d9;--card:#fff;--a:#3b6fd8;--b:#d9773b;--neg:#a7a49b}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--muted:#9c9a92;--line:#2f2e2b;--card:#1f1f1d;--a:#7aa2f0;--b:#f0a070;--neg:#5f5d57}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif}
main{max-width:860px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:28px;margin:0 0 4px}h2{font-size:20px;margin:40px 0 4px}h3{font-size:15px;margin:20px 0 6px}
p.lede,.note{color:var(--muted)}.note{font-size:13px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:20px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.tile b{display:block;font-size:22px}.tile span{color:var(--muted);font-size:13px}
.bars{margin:8px 0}
.bar{display:grid;grid-template-columns:minmax(80px,36%) 1fr;gap:10px;align-items:center;font-size:13px;margin:4px 0}
.bar .l{text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar .t{position:relative;display:flex;align-items:center;gap:6px;min-height:16px}
.bar i{display:block;height:12px;border-radius:3px;min-width:2px;flex:none}
.bar em{font-style:normal;color:var(--muted);font-size:12px;white-space:nowrap}
.bar.pair .t{flex-direction:column;align-items:flex-start;gap:2px}.bar.pair i{height:7px}
.bars.ref .t::after{content:"";position:absolute;left:var(--ref);top:-4px;bottom:-4px;border-left:1px dashed var(--neg)}
.months{display:grid;grid-template-columns:repeat(12,1fr);gap:4px;margin:12px 0;font-size:12px;text-align:center}
.months .col{display:flex;align-items:flex-end;height:120px}
.months .col>i{display:flex;align-items:flex-end;width:100%;border-radius:3px 3px 0 0;min-height:1px}
.months .col>i>i{display:block;width:100%;border-radius:3px 3px 0 0}
.months b{display:block;font-weight:500;margin-top:4px}.months em{font-style:normal;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:500}td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.legend span{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px 0 12px}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:24px}
pre{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto;font-size:12px;max-height:360px}
"""

esc = html.escape


def _w(v, top):
    # bar width inside a track that keeps 56px free for the value label
    return f"calc((100% - 56px) * {v / top:.4f})"


def bars(rows, value, fmt, color="var(--a)", ref=None):
    """Horizontal bars. `ref` draws a dashed reference line (e.g. lift = 1)."""
    if not rows:
        return "<p class=note>Not enough data.</p>"
    top = max(max(value(r) for r in rows), ref or 0) or 1
    style = f' style="--ref:{_w(ref, top)}"' if ref else ""
    out = [f'<div class="bars{" ref" if ref else ""}"{style}>']
    for r in rows:
        v = value(r)
        out.append(
            f'<div class=bar><span class=l title="{esc(r["tag"])}">{esc(r["tag"])}</span>'
            f'<span class=t><i style="width:{_w(v, top)};background:{color}"></i><em>{fmt(v)}</em></span></div>'
        )
    out.append("</div>")
    return "".join(out)


def pair_bars(rows, a, b):
    if not rows:
        return "<p class=note>Not enough data.</p>"
    top = max(max(a(r), b(r)) for r in rows) or 1
    out = ['<div class=bars>']
    for r in rows:
        out.append(
            f'<div class="bar pair"><span class=l title="{esc(r["tag"])}">{esc(r["tag"])}</span><span class=t>'
            f'<i style="width:{_w(a(r), top)};background:var(--a)"></i>'
            f'<i style="width:{_w(b(r), top)};background:var(--b)"></i></span></div>'
        )
    out.append("</div>")
    return "".join(out)


def month_bars(by_month):
    names = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    top = max(v["bought"] for v in by_month.values()) or 1
    out = ['<div class=months>']
    for i, m in enumerate(range(1, 13)):
        v = by_month[m]
        h = 100 * v["bought"] / top
        out.append(
            f'<div><span class=col><i style="height:{h:.1f}%;background:var(--a)">'
            f'<i style="height:{100 * v["never_played"]:.1f}%;background:var(--b)"></i></i></span>'
            f'<b>{names[i]}</b><em>{v["bought"]}</em></div>'
        )
    out.append("</div>")
    return "".join(out)


def tag_list(g):
    return ", ".join(t for t, v in sorted(g["tags"].items(), key=lambda kv: -kv[1]) if v >= MEMBER)[:90]


def render(r, summary, generated):
    o = r["overview"]
    pct = lambda x: f"{100 * x:.0f}%"
    x = lambda v: f"{v:.2f}×"
    s = []
    s.append(f"""<h1>Your play profile</h1>
<p class=lede>Built from your Steam library on {generated}. Everything here is about behavior (hours, drop-offs,
completion), not what you'd say you like.</p>
<div class=tiles>
<div class=tile><b>{o['owned']}</b><span>games owned</span></div>
<div class=tile><b>{o['total_hours']:,.0f}</b><span>hours played</span></div>
<div class=tile><b>{pct(o['never_played_rate'])}</b><span>never launched</span></div>
<div class=tile><b>{pct(o['top10_hours_share'])}</b><span>of hours in your top 10 games</span></div>
<div class=tile><b>{pct(r['overall_bounce'])}</b><span>of games started, dropped under {r['bounce_minutes']} min</span></div>
</div>""")

    aff = r["affinity"]
    s.append(f"""<h2>What you play more than you'd expect</h2>
<p class=lede>Lift compares a tag's share of your play time with its share of your library. 2× means
you play that kind of game twice as much as owning it would predict. The dashed line is 1×.</p>
{bars(aff[:14], lambda a: a['lift'], x, ref=1)}
<h3>Owned but not played</h3>
<p class=note>Tags you keep acquiring but don't spend time in.</p>
{bars([a for a in aff if a['lift'] < 1][-8:][::-1], lambda a: a['lift'], x, color="var(--neg)", ref=1)}""")

    rows = "".join(
        f"<tr><td>{esc(g['name'])}</td><td class=n>{hours(g):,.0f} h</td><td>{esc(tag_list(g))}</td></tr>"
        for g in r["core"][:20]
    )
    s.append(f"""<h2>Your core games</h2>
<p class=lede>The games you've sunk the most time into. Look for what they have in common at the level of
mechanics, which tags only partly capture.</p>
<table><tr><th>Game</th><th class=n>Hours</th><th>Main tags</th></tr>{rows}</table>
<h3>Tags over-represented in core games vs. everything you've played</h3>
{bars(r['core_lift'][:10], lambda a: a['lift'], x, ref=1)}""")

    if r["aspirational"] or r["comfort"]:
        s.append(f"""<h2>Wishlist vs. reality</h2>
<p class=lede>What catches your eye (wishlist) compared with what holds your attention (play time).
<span class=legend><span style="background:var(--a)"></span>wishlist share<span style="background:var(--b)"></span>play share</span></p>
<div class=cols><div><h3>Wishlisted more than played</h3>{pair_bars(r['aspirational'], lambda a: a['wishlist_share'], lambda a: a['play_share'])}</div>
<div><h3>Played more than wishlisted</h3>{pair_bars(r['comfort'], lambda a: a['wishlist_share'], lambda a: a['play_share'])}</div></div>""")

    above = [a for a in r["bounce"] if a["bounce_rate"] > r["overall_bounce"]]
    below = [a for a in r["bounce"] if a["bounce_rate"] <= r["overall_bounce"]]
    s.append(f"""<h2>Where you bounce off</h2>
<p class=lede>Of the games you started, the share you dropped in under {r['bounce_minutes']} minutes.
Overall: {pct(r['overall_bounce'])} (dashed line). Steam's refund window is 2 hours, so this is also where refunds happen.</p>
<div class=cols><div><h3>Most dropped</h3>{bars(above[:10], lambda a: a['bounce_rate'], pct, color="var(--b)", ref=r['overall_bounce'])}</div>
<div><h3>Least dropped</h3>{bars(below[-10:][::-1], lambda a: a['bounce_rate'], pct, ref=r['overall_bounce'])}</div></div>""")

    c = r["completion"]
    if c:
        fin = "".join(f"<tr><td>{esc(g['name'])}</td><td class=n>{hours(g):,.0f} h</td><td class=n>{pct(g['rate'])}</td></tr>"
                      for g in c["finished"][:12])
        s.append(f"""<h2>Finishing</h2>
<p class=lede>Achievement completion across {c['games']} games with achievements; median {pct(c['median_rate'])}.</p>
<div class=cols><div><h3>Average completion by tag</h3>{bars(c['by_tag'][:12], lambda a: a['avg_rate'], pct)}</div>
<div><h3>Games you nearly 100%'d</h3><table>{fin or '<tr><td>None yet.</td></tr>'}</table></div></div>""")

    if r["drift"]:
        s.append(f"""<h2>How your taste is shifting</h2>
<p class=lede>Tags among games you played in the last 12 months vs. all time. Above 1× means rising.</p>
{bars(r['drift'][:10], lambda a: a['ratio'], x, ref=1)}""")

    p = r["purchases"]
    if p:
        ref = "".join(f"<tr><td class=n>{esc(x_['date'])}</td><td>{esc(x_['name'])}</td></tr>" for x_ in p["refunds"])
        yrs = "".join(f"<tr><td>{y}</td><td class=n>{v['bought']}</td><td class=n>{pct(v['never_played'])}</td><td class=n>{pct(v['bounced'])}</td></tr>"
                      for y, v in p["by_year"].items())
        s.append(f"""<h2>Buying habits</h2>
<p class=lede>Purchases by month of the year, matched to {p['matched']} games still in your library
({p['unmatched_items']} items, mostly bundles, DLC and refunded games, didn't match).
<span class=legend><span style="background:var(--a)"></span>bought<span style="background:var(--b)"></span>never launched</span></p>
{month_bars(p['by_month'])}
<div class=cols><div><h3>By year</h3><table><tr><th>Year</th><th class=n>Bought</th><th class=n>Never played</th><th class=n>Bounced</th></tr>{yrs}</table></div>
<div><h3>Refunds ({len(p['refunds'])})</h3><table>{ref or '<tr><td>None.</td></tr>'}</table></div></div>""")

    s.append(f"""<h2>Brainstorm with this</h2>
<p class=lede>Paste <code>taste_summary.json</code> (below) into Claude with something like: <i>"Here's my play data.
What mechanics do my core games share, which of my aspirational tags have I never found a game that delivers on,
and what small game could I make that sits in that gap?"</i></p>
<pre>{esc(json.dumps(summary, indent=2))}</pre>
<p class=note>Tags are SteamSpy's top {TOP_TAGS} user tags per game, weighted by votes. A game counts as having a tag
when the tag has at least {MEMBER:.0%} of the votes of the game's top tag. Play time is weighted by log(1 + hours).
{o['untagged']} games had no tags and are left out of tag analyses.</p>""")

    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Play Profile</title>
<style>{CSS}</style></head><body><main>{''.join(s)}</main></body></html>"""


# ---------------------------------------------------------------- main


def collect(f, key, steamid, max_ach_games):
    progress("Fetching library and wishlist…")
    games = fetch_owned(f, key, steamid)
    wishlist = fetch_wishlist(f, key, steamid)

    apps = {g["appid"] for g in games} | {w["appid"] for w in wishlist}
    progress(f"Tagging {len(apps)} games via SteamSpy (about 1 per second the first time; cached after).")
    tags = {}
    for i, appid in enumerate(sorted(apps), 1):
        tags[appid] = fetch_tags(f, appid)
        if i % 50 == 0:
            progress(f"  {i}/{len(apps)}")
    for g in games:
        g["tags"] = tags[g["appid"]][1]
    for w in wishlist:
        w["name"], w["tags"] = tags[w["appid"]]

    ach = {}
    if max_ach_games:
        candidates = sorted((g for g in games if g["minutes"] >= 120), key=lambda g: -g["minutes"])[:max_ach_games]
        progress(f"Checking achievements for {len(candidates)} games…")
        for g in candidates:
            a = fetch_achievements(f, key, steamid, g["appid"])
            if a:
                ach[g["appid"]] = a
    return games, wishlist, ach


def main(argv=None, fetcher=None, now=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--steamid", default=os.environ.get("STEAM_ID"), help="SteamID64, vanity name or profile URL (or $STEAM_ID)")
    ap.add_argument("--purchase-history", type=Path, help="saved HTML of store.steampowered.com/account/history")
    ap.add_argument("--out", type=Path, default=Path("out"), help="output directory (default: out)")
    ap.add_argument("--cache", type=Path, default=Path(".cache"), help="cache directory (default: .cache)")
    ap.add_argument("--refresh", action="store_true", help="ignore cached API responses")
    ap.add_argument("--min-support", type=int, default=3, help="min games per tag before it's reported (default: 3)")
    ap.add_argument("--bounce-minutes", type=int, default=120, help="'bounced' = started but played less than this (default: 120)")
    ap.add_argument("--max-achievement-games", type=int, default=200, help="0 skips achievements (default: 200)")
    args = ap.parse_args(argv)

    key = os.environ.get("STEAM_API_KEY")
    if not key:
        sys.exit("Set STEAM_API_KEY (get one at https://steamcommunity.com/dev/apikey).")
    if not args.steamid:
        sys.exit("Set STEAM_ID or pass --steamid (your SteamID64, vanity name or profile URL).")

    f = fetcher or Fetcher(args.cache, refresh=args.refresh)
    steamid = resolve_steamid(f, key, args.steamid)
    games, wishlist, ach = collect(f, key, steamid, args.max_achievement_games)

    txns = []
    if args.purchase_history:
        txns = parse_purchase_history(args.purchase_history.read_text(encoding="utf-8", errors="replace"))
        progress(f"Read {len(txns)} transactions from purchase history.")
        if not txns:
            progress("  No rows found. Save the page after clicking 'Load more transactions' until it stops.")

    now = now or time.time()
    r = analyze(games, wishlist, ach, txns, now, args.min_support, args.bounce_minutes)
    summary = summary_json(r)
    generated = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "taste_summary.json").write_text(json.dumps(summary, indent=2))
    (args.out / "report.html").write_text(render(r, summary, generated))

    progress(f"\nWrote {args.out / 'report.html'} and {args.out / 'taste_summary.json'}")
    progress("Played more than owned: " + ", ".join(a["tag"] for a in r["affinity"][:6]))
    if r["aspirational"]:
        progress("Wishlisted more than played: " + ", ".join(a["tag"] for a in r["aspirational"][:6]))
    dropped = [a["tag"] for a in r["bounce"] if a["bounce_rate"] > r["overall_bounce"]]
    progress("Dropped more often than average: " + (", ".join(dropped[:6]) or "none"))
    return r


if __name__ == "__main__":
    main()

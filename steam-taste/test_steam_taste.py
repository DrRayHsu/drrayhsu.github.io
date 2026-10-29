"""Offline tests: a fake Steam/SteamSpy backend with a synthetic library."""

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import steam_taste as st

NOW = 1_760_000_000
STEAMID = "76561197960287930"

ROGUE = {"Roguelike": 900, "Deckbuilding": 600, "Strategy": 300, "Indie": 200}
METROID = {"Metroidvania": 800, "Platformer": 500, "Indie": 300}
SIM = {"Simulation": 700, "Building": 500, "Management": 400}
HORROR = {"Horror": 900, "Survival": 400, "Atmospheric": 300}


def library():
    games = []
    # Played a lot: roguelikes
    for i, h in enumerate([300, 150, 90, 60, 40]):
        games.append((100 + i, f"Rogue {i}", h * 60, ROGUE))
    # Started and dropped: metroidvanias
    for i in range(5):
        games.append((200 + i, f"Metroid {i}", 30 if i < 4 else 600, METROID))
    # Owned, never played: sims
    for i in range(6):
        games.append((300 + i, f"Sim {i}", 0, SIM))
    # Middling: horror
    for i, h in enumerate([10, 5, 3]):
        games.append((400 + i, f"Horror {i}", h * 60, HORROR))
    games.append((999, "No Tags Game", 45, {}))
    return games


class FakeFetcher:
    def __init__(self):
        self.games = library()
        self.tags = {a: t for a, _, _, t in self.games}
        self.tags.update({500 + i: SIM for i in range(4)})
        self.tags.update({600 + i: METROID for i in range(3)})
        self.stored = {}

    def put(self, key, data):
        self.stored[key] = data

    def get(self, url, params, cache_key=None, max_age=None):
        if "GetOwnedGames" in url:
            return {"response": {"games": [
                {"appid": a, "name": n, "playtime_forever": m,
                 "rtime_last_played": NOW - (30 if a < 200 else 800) * st.DAY if m else 0}
                for a, n, m, _ in self.games
            ]}}
        if "GetWishlist" in url:
            return {"response": {"items": [{"appid": a, "date_added": NOW} for a in [500, 501, 502, 503, 600, 601, 602]]}}
        if url == st.STEAMSPY:
            a = params["appid"]
            return {"name": f"App {a}", "tags": self.tags.get(a) or []}
        if url == st.STORE:
            return {str(params["appids"]): {"success": False}}
        if "GetPlayerAchievements" in url:
            a = params["appid"]
            if a >= 400:
                raise st.FetchError(400, "api")
            done = 20 if a < 200 else 2
            return {"playerstats": {"achievements": [{"achieved": int(i < done)} for i in range(20)]}}
        raise AssertionError(url)


HISTORY = """
<table class="wallet_history_table"><tbody>
<tr class="wallet_table_row wallet_table_row_amt_change">
  <td class="wht_date">6 Jul, 2024</td>
  <td class="wht_items"><div>Sim 0</div><div>Sim 1</div></td>
  <td class="wht_type"><div>Purchase</div><div class="wth_payment">Visa</div></td>
  <td class="wht_total">$20.00</td>
</tr>
<tr class="wallet_table_row">
  <td class="wht_date">Jul 3, 2024</td>
  <td class="wht_items"><div>Rogue 0&trade;</div></td>
  <td class="wht_type"><div>Purchase</div></td>
  <td class="wht_total">$15.00</td>
</tr>
<tr class="wallet_table_row">
  <td class="wht_date">2 Mar, 2023</td>
  <td class="wht_items"><div>Some Refunded Game</div></td>
  <td class="wht_type"><div>Refund</div></td>
  <td class="wht_total">$9.99</td>
</tr>
<tr class="wallet_table_row">
  <td class="wht_date">1 Feb, 2023</td>
  <td class="wht_items"><div>Hat</div></td>
  <td class="wht_type"><div>In-Game Purchase</div></td>
  <td class="wht_total">$1.00</td>
</tr>
</tbody></table>
"""


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "history.html").write_text(HISTORY)
        env = mock.patch.dict(os.environ, {"STEAM_API_KEY": "k", "STEAM_ID": STEAMID})
        env.start()
        self.addCleanup(env.stop)
        self.r = st.main(
            ["--out", str(self.dir / "out"), "--purchase-history", str(self.dir / "history.html")],
            fetcher=FakeFetcher(), now=NOW,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def tags(self, rows):
        return [r["tag"] for r in rows]

    def test_affinity_ranks_played_over_owned(self):
        aff = self.tags(self.r["affinity"])
        # Roguelike/Deckbuilding/Strategy always co-occur, so they tie for first
        self.assertEqual(set(aff[:3]), {"Roguelike", "Deckbuilding", "Strategy"})
        self.assertLess(aff.index("Roguelike"), aff.index("Metroidvania"))
        self.assertIn(aff[-1], SIM)  # never played: lift 0
        self.assertEqual(self.r["overview"]["untagged"], 1)

    def test_bounce(self):
        b = {r["tag"]: r for r in self.r["bounce"]}
        self.assertAlmostEqual(b["Metroidvania"]["bounce_rate"], 0.8)
        self.assertEqual(b["Roguelike"]["bounce_rate"], 0)
        self.assertNotIn("Simulation", b)  # never started, so no bounce data

    def test_wishlist_gap(self):
        self.assertIn("Simulation", self.tags(self.r["aspirational"])[:3])
        self.assertEqual(self.tags(self.r["comfort"])[0], "Roguelike")

    def test_core_and_completion(self):
        self.assertEqual(self.r["core"][0]["name"], "Rogue 0")
        c = self.r["completion"]
        self.assertEqual(c["games"], 6)  # 5 rogues + 1 metroid >= 2h; horror has no achievements
        self.assertEqual(c["by_tag"][0]["tag"], "Roguelike")

    def test_drift(self):
        self.assertEqual(self.r["drift"][0]["tag"], "Roguelike")

    def test_purchases(self):
        p = self.r["purchases"]
        self.assertEqual(p["matched"], 3)
        self.assertEqual(p["by_month"][7]["bought"], 3)
        self.assertAlmostEqual(p["by_month"][7]["never_played"], 2 / 3)
        self.assertEqual([x["name"] for x in p["refunds"]], ["Some Refunded Game"])

    def test_outputs(self):
        page = (self.dir / "out" / "report.html").read_text()
        self.assertIn("Your play profile", page)
        self.assertIn("Buying habits", page)
        summary = json.loads((self.dir / "out" / "taste_summary.json").read_text())
        top = {t["tag"] for t in summary["tags_played_more_than_owned"][:3]}
        self.assertIn("Roguelike", top)


class Units(unittest.TestCase):
    def test_history_parser_skips_payment(self):
        txns = st.parse_purchase_history(HISTORY)
        self.assertEqual(txns[0]["items"], ["Sim 0", "Sim 1"])
        self.assertEqual(txns[0]["type"], "Purchase")
        self.assertEqual(txns[1]["date"], date(2024, 7, 3))

    def test_norm_name(self):
        self.assertEqual(st.norm_name("Rogue 0™"), st.norm_name("ROGUE 0"))

    def test_resolve_numeric_and_url(self):
        self.assertEqual(st.resolve_steamid(None, "k", STEAMID), STEAMID)
        self.assertEqual(st.resolve_steamid(None, "k", f"https://steamcommunity.com/profiles/{STEAMID}/"), STEAMID)


if __name__ == "__main__":
    unittest.main()

"""Проверка разбора партнёрского фида на маленьком выдуманном файле (python tests/test_feed_yoox.py).
Колонки — как в стандартном фиде Awin; значения синтетические."""
from __future__ import annotations

import gzip
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources import feed_yoox  # noqa: E402
from sources.base import Query  # noqa: E402

CSV = """aw_product_id,merchant_product_id,product_name,brand_name,merchant_category,search_price,rrp_price,currency,merchant_deep_link,aw_deep_link,merchant_image_url,alternate_image,Fashion:size,Fashion:suitable_for,colour,Fashion:material,in_stock
111,16012345AB,Polo shirt,Boss,Uomo > Polo,59.00,120.00,EUR,https://www.yoox.com/it/16012345AB/item,https://www.awin1.com/cread.php?x=1,https://img.example/16012345ab_14_f.jpg,https://img.example/16012345ab_14_r.jpg,"S, M, L",Male,Blu,100% Cotone,1
112,16099999CD,Sneakers,Tod's,Uomo > Scarpe,"1.234,50",,EUR,https://www.yoox.com/it/16099999CD/item,,//img.example/16099999cd.jpg,,40|41|42,Mens,Bianco,Pelle,yes
113,17000000EF,Dress,Gucci,Donna > Abiti,300,500,EUR,https://www.yoox.com/it/17000000EF/item,,,,"S:in stock;M:out of stock",Female,Nero,Seta,0
,,No id row,Boss,Uomo,10,20,EUR,,,,,,,,,1
"""


class FeedTest(unittest.TestCase):
    def parse(self, raw: bytes, **so):
        return feed_yoox.parse_feed(feed_yoox._open_text(raw), {"id_regex": r"([0-9]{8}[A-Z]{2})", **so})

    def test_awin_columns(self):
        items = {p.source_item_id: p for p in self.parse(CSV.encode())}
        self.assertEqual(set(items), {"16012345AB", "16099999CD", "17000000EF"})   # строка без id пропущена
        polo = items["16012345AB"]
        self.assertEqual((polo.brand, polo.source, polo.currency), ("Boss", "yoox", "EUR"))
        self.assertEqual((polo.price_now, polo.price_old, polo.discount_pct), (59.0, 120.0, 50.8))
        self.assertEqual(polo.sizes, ["S", "M", "L"])
        self.assertEqual((polo.gender, polo.type), ("men", "футболки и поло"))
        self.assertEqual(len(polo.images), 2)
        self.assertEqual(polo.url, "https://www.yoox.com/it/16012345AB/item")    # ссылка магазина, не партнёрская
        shoes = items["16099999CD"]
        self.assertEqual((shoes.price_now, shoes.discount_pct, shoes.sizes), (1234.5, None, ["40", "41", "42"]))
        self.assertEqual(shoes.images, ["https://img.example/16099999cd.jpg"])
        dress = items["17000000EF"]
        self.assertEqual((dress.gender, dress.sizes, dress.in_stock), ("women", ["S"], False))

    def test_gzip_and_column_mapping(self):
        other = CSV.replace("search_price", "sale_price").replace("brand_name", "brand")
        items = self.parse(gzip.compress(other.encode()), columns={"price": "sale_price", "brand": ["brand"]})
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].brand, "Boss")

    def test_fetch_filters_and_min_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feed.csv"
            path.write_text(CSV, encoding="utf-8")
            q = Query(brands=["Boss", "Gucci"], genders=["men"], discount_min=40,
                      source_opts={"path": str(path), "min_rows": 1, "id_regex": r"([0-9]{8}[A-Z]{2})"})
            got = feed_yoox.fetch(q)
            self.assertEqual([p.source_item_id for p in got], ["16012345AB"])      # Tod's — не тот бренд, Gucci — женское
            self.assertTrue(feed_yoox.LAST_RUN["absence_means_gone"])
            q.source_opts["min_rows"] = 100
            with self.assertRaises(RuntimeError):
                feed_yoox.fetch(q)


if __name__ == "__main__":
    unittest.main(verbosity=2)

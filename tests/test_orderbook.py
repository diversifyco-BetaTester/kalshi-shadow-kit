from kalshi.orderbook import OrderBook


def test_parses_new_shape_and_derives_asks():
    ob = OrderBook.from_api("T", {"orderbook_fp": {"yes_dollars": [["0.01", "5"], ["0.03", "2"]],
                                                   "no_dollars": [["0.98", "10"], ["0.99", "250000"]]}})
    assert ob.best_yes_bid == 0.03
    assert ob.best_yes_ask == 0.01          # NO bid at 99c = YES ask at 1c
    assert ob.size_at_ask("yes", 0.01) == 250000
    assert ob.depth_at_or_better("yes", 0.02) == 250010


def test_parses_legacy_cents_shape():
    ob = OrderBook.from_api("T", {"orderbook": {"yes": [[40, 3]], "no": [[55, 7]]}})
    assert ob.best_yes_bid == 0.40 and ob.best_no_bid == 0.55 and ob.best_yes_ask == 0.45

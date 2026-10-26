from examples.crypto_maker.quote import make_quotes, p_yes
from examples.crypto_maker.shadow import queue_ahead, queue_terms
from kalshi.orderbook import OrderBook
from shadow.queue_sim import QueuePosition


def test_default_quotes_join_the_touch_never_improve_it():
    q = make_quotes(fair=0.50, best_bid=0.44, best_ask=0.56, edge=0.04)
    assert (q.bid, q.ask) == (0.44, 0.56)          # back of the best level, not 1c inside it
    q = make_quotes(fair=0.50, best_bid=0.49, best_ask=0.51, edge=0.04)
    assert (q.bid, q.ask) == (0.46, 0.54)          # behind the touch at fair ± edge


def test_improve_steps_inside_the_touch_and_never_crosses():
    q = make_quotes(fair=0.50, best_bid=0.44, best_ask=0.56, edge=0.02, improve=True)
    assert (q.bid, q.ask) == (0.45, 0.55)
    q = make_quotes(fair=0.70, best_bid=0.60, best_ask=0.62, edge=0.02, improve=True)
    assert (q.bid, q.ask) == (0.61, 0.72)          # market lags fair: bid improves, ask rests deep


def test_no_quotes_outside_band_or_on_an_empty_side():
    assert make_quotes(fair=0.95, best_bid=0.93, best_ask=0.96, edge=0.02).ask is None
    q = make_quotes(fair=0.50, best_bid=None, best_ask=0.56, edge=0.04)
    assert q.bid is None and q.ask == 0.56          # an empty side would put us first in line
    assert make_quotes(fair=0.50, best_bid=None, best_ask=0.56, edge=0.04, improve=True).bid == 0.46


def test_fair_value_direction_and_symmetry():
    assert p_yes(100.0, 100.0, 1e-4, 300) == 0.5
    assert p_yes(101.0, 100.0, 1e-4, 300) > 0.9 > 0.1 > p_yes(99.0, 100.0, 1e-4, 300)


def test_yes_bid_queue_is_the_no_ask_queue():
    book = OrderBook.from_api("T", {"orderbook_fp": {"yes_dollars": [["0.79", "700"]],
                                                     "no_dollars": [["0.20", "300"]]}})
    assert queue_ahead(book, "bid", 0.79) == 700 and queue_ahead(book, "ask", 0.80) == 300
    q = QueuePosition(5, queue_terms("bid", 0.79), 700)
    assert q.on_trade(1 - 0.79, 698) == 0          # takers sell YES into the bids ahead of us
    assert q.on_trade(1 - 0.79, 4) == 2
    assert q.on_trade(1 - 0.78, 10) == 3           # a print below our bid means we were hit first

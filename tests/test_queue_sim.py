from shadow.queue_sim import QueuePosition


def test_queue_ahead_fills_first():
    q = QueuePosition(size=5, price=0.01, ahead=10)
    assert q.on_trade(0.01, 8, ts=1) == 0 and q.ahead == 2
    assert q.on_trade(0.01, 4, ts=2) == 2      # 2 ahead consumed, then 2 to us
    assert q.on_trade(0.01, 9, ts=3) == 3 and q.done and q.fill_ts == 3


def test_trade_through_our_price_fills_us_first():
    q = QueuePosition(size=5, price=0.01, ahead=1_000_000)
    assert q.on_trade(0.02, 3) == 3 and q.ahead == 0


def test_book_cap_only_shrinks_queue():
    q = QueuePosition(size=5, price=0.01, ahead=100)
    q.on_book(40)
    assert q.ahead == 40
    q.on_book(500)                              # orders joined behind us: no effect
    assert q.ahead == 40


def test_no_fill_after_done_or_below_price():
    q = QueuePosition(size=1, price=0.02, ahead=0)
    assert q.on_trade(0.01, 5) == 0             # cheaper level traded, ours untouched
    q.on_trade(0.02, 1)
    assert q.on_trade(0.02, 1) == 0

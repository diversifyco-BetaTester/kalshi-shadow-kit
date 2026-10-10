# Dead buckets on Kalshi weather markets: a real edge you probably can't capture

Research log, October 2026. All numbers come from the code in this folder, over 90 days
(2026-07-12 → 2026-10-09) of settled Kalshi daily-high-temperature markets in 24 US cities.

## The idea

Kalshi lists daily-high markets in 2°F buckets ("72° to 73°"). The day's high can only go
up, so once the settlement station has already read 75°F, every bucket below 74°F is
**logically dead** and can never win. Yet takers keep buying YES on those buckets at 1c.
Someone resting a 1c YES ask collects that 1c, and in theory never loses.

The catch is negative skew. Selling at 1c means risking 99c to make 1c, so it only breaks
even if fewer than 1 in 100 "dead" calls are wrong. The rule has to be strictly logical.

## Finding 1: the naive rule breaks, and the failure is a sensor spike

The first version ("dead once any reading passes the bucket") flagged 1,325 dead buckets.
**One of them settled YES.**

New York, 2026-08-27: Central Park reported 80°F at 19:51 UTC, between readings of 70°F and
71°F. NWS quality control later threw it out, and the official high was 77°F. The
"79° or below" bucket looked dead and won.

A second near miss: Chicago Midway on 2026-09-20 sent a special report of 21°C, which IEM
shows as 69.8°F. It rounds to 70, but the true value could be as low as 68.9°F, and the
official high was 69°F.

**Fixes:**
- Require 2 consecutive readings to clear the bucket, which kills isolated spikes.
- Count whole-°C readings at their lower bound.

**Result:** 0 breaks in 3,048 dead buckets across 24 cities, and no day where the confirmed
readings exceeded the official high. The 95% upper bound on the break rate is about 0.1%,
under the 1% break-even.

## Finding 2: Kalshi can settle on a preliminary report

On Miami, 2026-08-29, Kalshi paid "90° to 91°". The NWS climate report says 85°F, and no
reading that day went above 85°F. The archive explains it: NWS first issued the report
saying **90°F at 3:25 PM**, then corrected it 45 minutes later to 85°F. Kalshi settled on the
first version. (The market rules name "The Weather Company" as the source.)

A preliminary report that's too high can't hurt a dead-bucket seller, because dead buckets
sit below the observed high. One that's too low could. This is the strategy's real tail risk,
and the shadow runner alerts on it.

## Finding 3: the flow is real, and so is the queue

| | Original 7 cities | 17 newer cities |
|---|---|---|
| Dead buckets (90 days) | 1,111 | 1,937 |
| Taker YES bought at 1c after death | 4.4M contracts | 1.8M contracts |
| Per dead bucket | ~3,900 | ~930 |
| 1c queue already resting on a dead bucket (one live snapshot) | median **225,000** | 71 – 21,000 in the thinner cities |

In the big cities, roughly a quarter-million contracts sit at the 1c ask ahead of you, about
58 days of taker flow, on a bucket that lives for less than a day. A new order at the back
never fills. Snapshots of tomorrow's buckets show tiny 1c queues (1–800), so the queue forms
**after** death. Priority goes to whoever posts first, which makes it a speed race, not a
wall of money that was already there.

Thinner cities (Atlanta, San Francisco, Louisville) have queues a small order could clear.
`shadow.py` measures that live with a FIFO replay of every trade.

## Finding 4: the capital math

Selling YES at 1c ties up 99c to earn 1c. Even with every order filling every day, a small
account earns about 1% a day on the capital deployed. It's a real edge, but it doesn't scale
for a small account, and the large accounts are already in the queue.

## Takeaways for any Kalshi strategy

1. Backtest against **settlement**, not prices, and look for the single case that breaks
   your logic. Ours was one bad sensor reading in 90 days.
2. Read the market rules for the actual settlement source and its correction policy.
3. Volume is not fills. Measure the queue you'd join before trusting a maker backtest.
4. Shadow-run before risking money. The framework in this repo exists for that.

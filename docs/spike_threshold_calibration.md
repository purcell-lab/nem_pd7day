# Calibrating SPIKE_CAPABILITY_DEPRESSION

Prepared 2026-09-20. All timestamps NEM time, UTC+10, no DST. PD7DAY
INTERVAL_DATETIME is the interval END, so it equals `nemtime`; `time` is 30
minutes earlier.

## Bottom line

The threshold is not identifiable from the data. Every value between 0.05 and
0.50 produces the same accuracy to within noise, the ranking between values
reverses from one period to the next, and the underlying ratio is barely
related to whether a forecast spike actually happens. Setting it to 0.25 is not
wrong, but it cannot be justified as calibrated, and no other number can
either.

The narrower finding matters more than the number: the depression clause adds
flags without adding accuracy. The part of the gate that carries what little
signal exists is the net import test combined with a shut link.

## Why forecast_history could not be used

`forecast_history` and the observation log persist `qni_mwflow` and
`qni_violation` and nothing else about the network. Neither `exportlimit` nor
`importlimit` is recorded, and no per region link breakdown is kept, so the
capability ratio the threshold acts on cannot be reconstructed from anything
the integration has stored. The store also prunes at MAX_FORECAST_AGE_DAYS,
which would have left a fortnight of data even if the fields were there.

The calibration was therefore rebuilt from AEMO's published archives, which is
reproducible by anyone and carries no personal data.

## Method

Forecasts come from the PD7DAY monthly archive table dump
(`PUBLIC_ARCHIVE#PD7DAY_PRICESOLUTION`, `#PD7DAY_INTERCONNECTORSOLUTION`,
`#PD7DAY_MARKET_SUMMARY` on nemweb), restricted to the daily 18:00 run and to
INTERVENTION = 0. Settled prices come from AEMO's price and demand files,
averaged from five minute dispatch to the 30 minute interval ending at each
`nemtime`.

Population is every (run, region, interval) the 18:00 run forecast at or above
$3000/MWh, from 2024-06-01 to 2026-09-01. Outcome is the settled 30 minute RRP
for that interval.

| | |
|---|---|
| Runs | 817 |
| Eligible (run, region, interval) triples | 105,620 |
| Network answerable under the new code | 105,620 (100%) |
| Gas above 150 TJ | 86,138 (81.6%) |
| Settled at or above $3000/MWh | 1,314 (1.24%) |
| Settled at or above $300/MWh | 9,286 (8.79%) |

The vectorised gate used here was checked against the shipped
`network_covariates_for_interval` on 40 randomly drawn intervals, with zero
mismatches on both `network_tight` and `network_min_capability_ratio`.

## Result 1: the threshold does not separate anything

Precision of the full gate, gas and network together, against a base rate of
1.24%. The 95% intervals are bootstrapped by run, because intervals inside one
run are not independent.

| T | flagged | precision | 95% CI | lift |
|---|---------|-----------|--------|------|
| 0.05 | 10,314 | 1.99% | 1.44 to 2.80% | 1.6x |
| 0.10 | 11,293 | 2.11% | 1.54 to 2.71% | 1.7x |
| 0.15 | 12,232 | 2.08% | 1.50 to 2.65% | 1.7x |
| 0.20 | 12,863 | 2.05% | 1.49 to 2.66% | 1.6x |
| 0.25 | 13,519 | 2.01% | 1.44 to 2.71% | 1.6x |
| 0.30 | 14,191 | 2.11% | 1.61 to 2.67% | 1.7x |
| 0.40 | 15,577 | 2.04% | 1.52 to 2.60% | 1.6x |
| 0.50 | 17,015 | 1.98% | 1.52 to 2.53% | 1.6x |
| 0.75 | 22,633 | 1.68% | 1.27 to 2.15% | 1.3x |

The confidence interval at 0.25 contains the point estimate of every other
threshold in the table. Only 0.75 is visibly worse.

## Result 2: the choice is not stable over time

Precision by period, outcome at or above $3000/MWh.

| Period | n | spikes | T=0.05 | T=0.10 | T=0.25 | T=0.50 | T=0.75 | best |
|--------|---|--------|--------|--------|--------|--------|--------|------|
| 2024-06 to 2025-03 | 42,407 | 614 | 2.13% | 2.58% | 2.50% | 2.69% | 2.42% | 0.50 |
| 2025-04 to 2025-12 | 29,664 | 503 | 2.42% | 2.33% | 2.13% | 1.82% | 1.51% | 0.05 |
| 2026-01 to 2026-09 | 25,757 | 118 | 1.58% | 1.54% | 1.33% | 1.04% | 0.82% | 0.05 |

The first period prefers a loose threshold and the next two prefer the
tightest available, in opposite directions. On the softer outcome, settled at
or above $300, the 2026 slice reverses again and improves monotonically as the
threshold loosens, from 3.6% at 0.05 to 6.5% at 0.75. A parameter whose
optimum flips direction between periods is fitting noise.

## Result 3: the ratio is not monotonic in spike risk

A threshold rule assumes that a tighter link means a higher chance of a spike.
Restricting to net importing intervals with every link open, so the shut clause
cannot interfere, the relationship is not there.

| capability / run median | n | spiked | rate | lift |
|---|---|---|---|---|
| 0.00 to 0.05 | 652 | 8 | 1.23% | 1.0x |
| 0.05 to 0.10 | 979 | 33 | 3.37% | 2.7x |
| 0.10 to 0.15 | 939 | 16 | 1.70% | 1.4x |
| 0.15 to 0.25 | 1,287 | 18 | 1.40% | 1.1x |
| 0.25 to 0.40 | 2,058 | 46 | 2.24% | 1.8x |
| 0.40 to 0.60 | 3,283 | 38 | 1.16% | 0.9x |
| 0.60 to 0.80 | 5,428 | 41 | 0.76% | 0.6x |
| 0.80 to 0.95 | 6,768 | 67 | 0.99% | 0.8x |
| 0.95 to 1.00 | 36,272 | 448 | 1.24% | 1.0x |

The most depressed band of all sits exactly at the base rate. The band above it
looks like the best in the table on 33 events, which is not enough to build a
constant on.

As a ranking score the ratio has an AUC of 0.526 against the $3000 outcome,
where 0.50 is no skill. By region it is 0.705 in QLD1, 0.587 in NSW1, 0.608 in
TAS1, 0.505 in SA1 and 0.369 in VIC1. The Victorian figure is inverted, meaning
a tighter import path there is associated with a slightly lower chance of a
spike, not a higher one.

## Result 4: where the little signal there is actually comes from

| Rule | flagged | spiked | precision | lift |
|---|---|---|---|---|
| Net import only | 67,402 | 912 | 1.35% | 1.1x |
| Net import and a shut link | 9,662 | 197 | 2.04% | 1.6x |
| Net import and ratio at or below 0.25, no shut link | 3,857 | 75 | 1.94% | 1.6x |
| Gas above 150 TJ alone | 86,138 | | 1.43% | 1.1x |
| Gas and network together, T = 0.25 | 11,012 | 247 | 2.24% | 1.8x |

The shut clause does most of the work and fires two and a half times as often
as the depression clause. Adding the depression clause at 0.25 contributes 3,857
extra flags at a precision indistinguishable from the shut clause alone, so it
changes the count of flags without changing how often they are right.

## Result 5: two things worth knowing beyond the threshold

By lead time, at T = 0.25:

| Lead | n | base rate | flagged | precision | lift |
|---|---|---|---|---|---|
| under 24h | 842 | 18.76% | 270 | 15.93% | 0.8x |
| 24 to 48h | 7,366 | 2.62% | 1,528 | 3.08% | 1.2x |
| 48 to 96h | 47,021 | 1.04% | 5,014 | 2.01% | 1.9x |
| over 96h | 50,371 | 0.91% | 4,198 | 1.33% | 1.5x |

Inside 24 hours the gate is worse than no gate at all. A forecast spike in that
window is already the most informative thing available, and marking it credible
on network grounds selects a slightly worse subset than leaving it alone.

By region, at T = 0.25, lift over each region's own base rate is 5.0x in QLD1,
1.9x in NSW1, 2.1x in SA1, 0.8x in VIC1 and 0.0x in TAS1.

That Queensland figure should not be leaned on. It rests on 3 spiked intervals
out of 46 flagged, with a bootstrap interval running from 0.00 to 19.42%. The
depression clause on its own in QLD1, meaning net importing with every link
open, covers 220 intervals and catches a single spike. Splitting QLD1 by period
makes it worse: every one of those events falls in 2024-06 to 2025-03, the
2025-04 to 2025-12 slice flags 6 intervals and gets none, and the 2026 slice
has no eligible flags at all. The apparent Queensland advantage is a handful of
events in one summer, not a property of the region.

## What this means for the flag

Even at its best the gate turns a 1.24% chance of a spike into about 2.2%. A
`spike_credible` of true still means no spike roughly 49 times out of 50. That
is worth stating wherever the flag is surfaced, because the word credible
invites a much stronger reading than the evidence supports.

## Where the gate turns harmful

Because the lead time effect was the strongest thing in the analysis, it is
worth resolving more finely than four bins.

| lead | n | base | flagged | spiked | precision | lift |
|---|---|---|---|---|---|---|
| 0 to 6h | 295 | 32.54% | 65 | 23 | 35.38% | 1.09x |
| 6 to 12h | 18 | 0.00% | 1 | 0 | 0.00% | n/a |
| 12 to 18h | 154 | 9.09% | 67 | 4 | 5.97% | 0.66x |
| 18 to 24h | 375 | 12.80% | 137 | 16 | 11.68% | 0.91x |
| 24 to 30h | 787 | 11.56% | 186 | 26 | 13.98% | 1.21x |
| 36 to 48h | 6,430 | 1.59% | 1,293 | 21 | 1.62% | 1.02x |
| 48 to 72h | 23,917 | 1.02% | 2,652 | 53 | 2.00% | 1.97x |
| 72 to 96h | 23,104 | 1.06% | 2,362 | 48 | 2.03% | 1.91x |
| over 96h | 50,371 | 0.91% | 4,198 | 56 | 1.33% | 1.47x |

Cumulatively, suppressing every flag at or below a horizon H removes flags that
were right less often than the base rate for every H up to 36h, and crosses
over between 36h and 48h.

| H | flags removed | of which spiked | precision of removed | base below H |
|---|---|---|---|---|
| 6h | 67 | 23 | 34.33% | 36.19% |
| 12h | 68 | 23 | 33.82% | 34.23% |
| 18h | 135 | 27 | 20.00% | 26.28% |
| 24h | 272 | 43 | 15.81% | 20.42% |
| 36h | 507 | 69 | 13.61% | 14.85% |
| 48h | 1,800 | 90 | 5.00% | 4.48% |

24h is the conservative choice. 36h would remove slightly more bad flags but
would also start suppressing the 24 to 30h band, which is genuinely positive at
1.21x. For QLD1 specifically the 24h cut removes 3 flags, none of which were
right, and removes nothing from any band where the region scores above its base
rate.

## Options

1. Keep 0.25 and record in the code that it is a chosen constant, not a
   calibrated one, with a pointer to this analysis. Smallest change, and honest
   about what the number is.
2. Drop the depression clause and keep net import combined with a shut link.
   Same measured accuracy, one fewer unjustified constant, and a rule that
   states something physical rather than statistical.
3. Suppress the flag inside 24 hours of the interval, where it measurably
   subtracts information.

Options 2 and 3 are independent of each other and both can be taken.

## What was implemented

Options 1 and 3.

`SPIKE_CAPABILITY_DEPRESSION` stays at 0.25 and `const.py` now records that it
is a chosen constant rather than a calibrated one, with a pointer to this
analysis, so the next reader does not mistake it for a fitted value.

The short lead suppression is applied where `spike_credible` is published as a
sensor attribute, in `sensor._published_spike_credible`, and not inside
`CalibrationStore.apply_to_price`. Below `SPIKE_COVARIATE_MIN_HORIZON_H` the
attribute reports None, meaning no opinion, rather than True or False.

The placement matters. The camera spike callouts require `spike_credible` to be
True and only exist inside 48 hours, so suppressing the gate in the store would
have switched off the whole 0 to 24 hour callout band, which is the band users
look at. The callout path therefore keeps reading the raw gate result straight
off the calibration dict and its behaviour is unchanged, which the tests pin in
both directions.

The consequence is that the flag means slightly different things in the two
places it is read. On a sensor attribute it is a claim this calibration
supports. On the chart it remains the raw gate. That is a deliberate trade,
taken because the alternative was to change what the chart draws on the
strength of an analysis that was not scoped to callouts.

Option 2 was not taken. On the Queensland evidence the difference between
keeping and dropping the depression clause is 2 spiked intervals against 3, and
that is not a basis for removing a clause.

Still open, and not addressed here: inside 24 hours the callouts are still
selected by a gate this analysis found to be worse than the raw forecast at
that lead. Changing that changes what the chart draws and deserves its own
issue.

## Reproducing

Scripts are in `cal/` in the workspace: `calibrate.py` builds the panel,
`analyse2.py` covers discrimination and parity, `analyse3.py` covers bootstrap
intervals and temporal stability. Sources:

- PD7DAY archive, https://nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM/2026/
- PD7DAY current runs, https://www.nemweb.com.au/REPORTS/CURRENT/PD7Day/
- Settled prices, https://aemo.com.au/energy-systems/electricity/national-electricity-market-nem/data-nem/aggregated-data

# MLB Intelligence Brief

## Report Metadata

- Report ID: mlb-brief:v1:62bc6c0d4a03
- Generated at: 2026-07-26T12:00:00+00:00
- Slate date: 2026-07-27
- League: MLB
- Data freshness: fresh (last successful refresh: 2026-07-26T12:00:00+00:00)
- Methodology version: mlb-intelligence-brief-v1

## Slate Summary

- Games analyzed: 2
- Markets available: moneyline/full_game
- Books represented: draftkings, fanduel
- Stale or incomplete markets: mlb:game:v1:away:atlanta-braves:home:new-york-mets:instance:start:20260727T021000Z (stale)
- Highest-priority research items: mlb:game:v1:away:los-angeles-dodgers:home:new-york-yankees:instance:start:20260727T001000Z: investigate; mlb:game:v1:away:atlanta-braves:home:new-york-mets:instance:start:20260727T021000Z: avoid

## Game Cards

### New York Yankees vs Los Angeles Dodgers
- Canonical game ID: mlb:game:v1:away:los-angeles-dodgers:home:new-york-yankees:instance:start:20260727T001000Z
- Scheduled start: 2026-07-27T00:10:00+00:00
- Data-quality status: verified
- Confidence: 0.680
- Research selection: away
- Consensus no-vig market probability: 0.530
- Experimental model probability: 0.590
- Probability difference: +0.060
- Verdict: Investigate
- Verdict detail: Qualified angle is worth manual research review before any decision.
- Available sportsbook quotes:
  - fanduel away 118
    - Source/provider: sportsgameodds
    - Observed timestamp: 2026-07-26T11:58:00+00:00
    - Provider event ID: provider-b-888
    - Provider quote ID: quote:v1:early-away-fd
    - Source URL: https://example.test/early/fd/away
  - fanduel home -138
    - Source/provider: sportsgameodds
    - Observed timestamp: 2026-07-26T11:58:00+00:00
    - Provider event ID: provider-b-888
    - Provider quote ID: quote:v1:early-home-fd
    - Source URL: https://example.test/early/fd/home
  - draftkings away 120
    - Source/provider: odds_api_io
    - Observed timestamp: 2026-07-26T11:59:00+00:00
    - Provider event ID: provider-a-101
    - Provider quote ID: quote:v1:early-away-dk
    - Source URL: https://example.test/early/dk/away
  - draftkings home -140
    - Source/provider: odds_api_io
    - Observed timestamp: 2026-07-26T11:59:00+00:00
    - Provider event ID: provider-a-101
    - Provider quote ID: quote:v1:early-home-dk
    - Source URL: https://example.test/early/dk/home
- Evidence references:
  - bullpen-rest-edge
- Contradictions:
  - Pitching note conflicts with public lineup report
- Risk flags:
  - weather-volatility

### New York Mets vs Atlanta Braves
- Canonical game ID: mlb:game:v1:away:atlanta-braves:home:new-york-mets:instance:start:20260727T021000Z
- Scheduled start: 2026-07-27T02:10:00+00:00
- Data-quality status: stale
- Confidence: Unavailable
- Research selection: away
- Consensus no-vig market probability: 0.530
- Experimental model probability: Unavailable (No experimental model probability was persisted.)
- Probability difference: Unavailable (Model and market probabilities were not both available.)
- Verdict: Avoid
- Verdict detail: No-bet status preserved from evaluation: NO_BET
- Stale quote warning: One or more quotes are stale; oldest observed quote is 45.0 minutes old.
- Available sportsbook quotes:
  - draftkings away 110
    - Source/provider: odds_api_io
    - Observed timestamp: 2026-07-26T11:15:00+00:00
    - Provider event ID: provider-c-301
    - Provider quote ID: quote:v1:late-away-dk
    - Source URL: https://example.test/late/dk/away
  - draftkings home -130
    - Source/provider: odds_api_io
    - Observed timestamp: 2026-07-26T11:15:00+00:00
    - Provider event ID: provider-c-301
    - Provider quote ID: quote:v1:late-home-dk
    - Source URL: https://example.test/late/dk/home
- Evidence references:
  - Unavailable: no evidence references were persisted.
- Contradictions:
  - None recorded.
- Risk flags:
  - lineup-uncertain
  - market-thin

## Audit And Disclosure

- Generated timestamp: 2026-07-26T12:00:00+00:00
- Audit trail:
  - Source/provider: odds_api_io
    - Observed timestamp: 2026-07-26T11:15:00+00:00
    - Canonical game ID: mlb:game:v1:away:atlanta-braves:home:new-york-mets:instance:start:20260727T021000Z
    - Provider event ID: provider-c-301
    - Provider quote ID: quote:v1:late-away-dk
    - Source URL: https://example.test/late/dk/away
  - Source/provider: odds_api_io
    - Observed timestamp: 2026-07-26T11:15:00+00:00
    - Canonical game ID: mlb:game:v1:away:atlanta-braves:home:new-york-mets:instance:start:20260727T021000Z
    - Provider event ID: provider-c-301
    - Provider quote ID: quote:v1:late-home-dk
    - Source URL: https://example.test/late/dk/home
  - Source/provider: sportsgameodds
    - Observed timestamp: 2026-07-26T11:58:00+00:00
    - Canonical game ID: mlb:game:v1:away:los-angeles-dodgers:home:new-york-yankees:instance:start:20260727T001000Z
    - Provider event ID: provider-b-888
    - Provider quote ID: quote:v1:early-away-fd
    - Source URL: https://example.test/early/fd/away
  - Source/provider: sportsgameodds
    - Observed timestamp: 2026-07-26T11:58:00+00:00
    - Canonical game ID: mlb:game:v1:away:los-angeles-dodgers:home:new-york-yankees:instance:start:20260727T001000Z
    - Provider event ID: provider-b-888
    - Provider quote ID: quote:v1:early-home-fd
    - Source URL: https://example.test/early/fd/home
  - Source/provider: odds_api_io
    - Observed timestamp: 2026-07-26T11:59:00+00:00
    - Canonical game ID: mlb:game:v1:away:los-angeles-dodgers:home:new-york-yankees:instance:start:20260727T001000Z
    - Provider event ID: provider-a-101
    - Provider quote ID: quote:v1:early-away-dk
    - Source URL: https://example.test/early/dk/away
  - Source/provider: odds_api_io
    - Observed timestamp: 2026-07-26T11:59:00+00:00
    - Canonical game ID: mlb:game:v1:away:los-angeles-dodgers:home:new-york-yankees:instance:start:20260727T001000Z
    - Provider event ID: provider-a-101
    - Provider quote ID: quote:v1:early-home-dk
    - Source URL: https://example.test/early/dk/home
- Model limitations:
  - Experimental model probabilities are directional research inputs and may be missing for some games.
  - Consensus no-vig market probabilities depend on available complete sportsbook books and may be unavailable.
- Disclosure: This report is research and analysis for manual review, not a guarantee of results or outcomes.
- Disclosure: This report supports research workflows only; no automated execution is authorized or implied.

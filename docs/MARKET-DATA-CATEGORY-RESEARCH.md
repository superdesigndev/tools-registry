# Market data — provider research, for review before testing

**Status:** research only. Nothing added, nothing tested.

A user asked for market data and they are right that it is missing: 40 providers across SEO (9),
Advertising (10), Social media (10), Enrichment (10) and Community (1), and **not one financial
provider**.

---

## Read this first: the category does not price the way our others do

Every existing treg provider is **credit-priced**. Hunter sells 2,000 credits for $49, an endpoint
declares how many credits it costs, and `fx.yaml` turns that into dollars per call. That is how the
catalogue produces an honest per-call price.

**Market data almost universally does not work that way.** It is sold as a flat monthly subscription
with a **rate limit**, and unlimited calls inside it:

| Provider | Entry plan | What the money buys |
|---|---|---|
| Alpha Vantage | $49.99/mo | 75 requests **per minute**, no daily limit |
| Twelve Data | $29/mo | 55 credits/min, **unlimited** daily |
| Polygon (now Massive) | $29/mo | unlimited calls, per asset class |
| Finnhub | ~$12–100/mo | rate limit, flat fee |
| FMP | $15/mo | flat fee |
| Marketstack | $9.99/mo | flat fee |
| EODHD | $19.99/mo | flat fee |
| Tiingo | $30/mo | flat fee |

There is no "credits included" number to divide by, so **plan price ÷ credits is undefined**. This is
exactly the "$10/month for unlimited" case, and it forces a decision we have not had to make before.

### The three options, and what each costs us

**A. Price them at $0 (`type: free`).** Once treg pays the monthly fee, the marginal cost of one more
call really is zero, so this is *honest*. It also means treg absorbs the subscription with no
per-call recovery — at 0% margin that is a straight monthly expense, roughly **$150–250/mo** for a
reasonable spread of the providers above.

**B. Amortise: divide the monthly fee by expected calls.** Produces a per-call number, but it is a
forecast, not a price. Guess the volume wrong and every call is mispriced. It would also be the first
number in the catalogue that is not traceable to a provider's own rate card, which breaks the rule
that has kept our pricing defensible.

**C. Leave them unpriced and own-key only.** They join the 55: callable with the user's own key,
never on treg's. Costs nothing, and the category still has real value — but the headline feature
("no key of your own") would not apply to it.

**My read:** start with **C** for the expensive ones and **A** for one or two cheap, high-value
providers, so the category exists and something in it is genuinely keyless. Going straight to A
across the board turns a free catalogue into a monthly bill with no recovery.

The one clean exception is below.

---

## The shortlist

Ordered by how well they fit treg, not by how good the data is.

### Tier 1 — fits the model cleanly

**1. CoinGecko** (crypto) — *the only true credit-priced one I found*
- **$35/mo ÷ 100,000 credits = $0.00035/credit.** Real rate card, divides exactly like Hunter.
- Auth: `x-cg-pro-api-key` header, or query. Header, so our injector handles it.
- Free demo tier (10k credits/mo) for probing.
- Fills crypto, which nothing in the catalogue touches.

### Tier 2 — strong data, flat-fee pricing (needs the A/B/C decision)

**2. Polygon / Massive** — US equities, options, forex, crypto; tick-level. $29/mo Starter. Auth:
`Authorization: Bearer` **or** `?apiKey=`. Free tier 5 calls/min, 15-min delayed — enough to probe.
The strongest data of the group. Note the 2026 rebrand to Massive; the domain moved.

**3. Finnhub** — real-time quotes, company fundamentals, news sentiment, alternative data. Free tier
is the most generous of any (60 calls/min). Auth: `X-Finnhub-Token` header or `?token=`. Free tier is
non-commercial, which we must read carefully before serving it on treg's key.

**4. Twelve Data** — equities, forex, crypto, 800 free calls/day. Auth `?apikey=`. Uses the word
"credits" but sells them per minute, not as a pool, so it is still a flat fee.

**5. Alpha Vantage** — 200,000+ tickers, 20+ exchanges, long history, plus economic indicators. Free
tier 25 requests/day. Auth `?apikey=`. Cheapest paid tier is $49.99, the priciest entry here.

**6. Financial Modeling Prep** — fundamentals, statements, ratios, 250 free calls/day, $15/mo Starter.
Auth `?apikey=`. Distinct from the quote-focused ones: this is the fundamentals gap.

**7. EODHD** — end-of-day and historical across global exchanges, $19.99/mo. Auth `?api_token=`.
Fills *global + historical*, which the US-centric ones do not.

**8. Marketstack** — cheapest at $9.99/mo, global stock prices, simple. Auth `?access_key=`. A good
low-cost candidate if we do pick one to serve on treg's key.

**9. Tiingo** — equities, news, fundamentals, 30+ years of history. $30/mo. Auth
`Authorization: Token <key>` header. Free tier limited to 500 symbols/month.

### Tier 3 — worth adding for coverage, needs checking

**10. A filings / macro source** — SEC filings (sec-api.io) or economic data (FRED, which is free with
a key). I did not verify pricing or auth for either, so I am listing the gap rather than pretending
to have picked one.

---

## Rejected, with reasons

- **Yahoo Finance** — no official API. Every wrapper is unofficial and has been broken or
  legally pressured before. Same class of risk as Proxycurl, which the guide says to reject on sight.
- **Intrinio** — enterprise sales-gated, no self-serve key. Breaks the fast path.
- **IEX Cloud** — shut down in 2024. Dead.
- **Bloomberg / Refinitiv** — terminal licensing, not an API anyone self-serves.

---

## What I have NOT verified, and would before adding any of them

The guide is explicit that the load-bearing step is watching a provider **reject a bogus key**, and
that `base_url`, probe path, and bad-key behaviour must be confirmed live rather than assumed.

For every provider above I have the pricing and the *documented* auth location. I have **not**
confirmed:

1. **Bad-key behaviour.** Several finance APIs return `200` with an empty payload or an
   `{"Error Message": …}` body for an invalid key rather than a 401. That is the Apollo/Majestic trap,
   and it needs `token_verify_field` rather than a status check. **Alpha Vantage is a known offender
   here** — it commonly answers 200 with a note.
2. **A free probe endpoint** for each that a valid key passes and an invalid key fails.
3. **Whether the free tier's terms permit serving it through treg** — Finnhub's free tier is
   explicitly non-commercial, and that matters if treg's key is the one being used.

None of that is research; it is the live test, and it is the next step once you have picked.

---

## What I would like you to decide

1. **A, B or C** on pricing — the structural question, and it applies to the whole category.
2. **Which of the ten** to take forward, and how many.
3. Whether to pay for any subscription at all right now, or ship the category **own-key only** and
   add treg-key serving later once there is demand to justify the spend.

Sources: the providers' own pricing pages, plus 2026 comparison write-ups; every price above is from
the vendor's published rate card except Finnhub's range, which came from a third-party summary and
should be confirmed on their page before it is recorded anywhere.

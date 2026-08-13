"""Pulling the money out of a post.

`context:budget` in patterns.py answers "does this post mention money?", which
is all the classifier needs to score it. The question a person asks next —
"how much?" — needs the number itself, and that is what this module extracts.

The forms that actually show up in X job posts:

    "$500 per 30-second reel"      symbol first, with a rate unit
    "$500-$1000/video"             a range
    "paying 5k/month"              magnitude suffix, no symbol
    "budget is 200 usd"            currency as a code, after the number
    "my budget is around 150"      no currency at all, only the anchor word

The last form is why a bare number is accepted *only* when a budget word
introduces it. Every post on X is full of numbers — follower counts, view
counts, "30-second", "3 videos a week" — so an unanchored digit is noise.

The guard lists exist for the same reason. "I made $10k last month" and
"$500 giveaway" are both money, and neither is a budget; they are the guru
and engagement-bait posts the classifier already tries to reject, and without
the guards they would still arrive with a confident-looking number attached.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from x_leads.leads.patterns import normalise

# 1,500 must be tried before the plain form, or it matches as a bare "1".
_NUM = r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?"

_CODES = "usd|eur|gbp|inr|aud|cad|sgd|nzd|aed|php|pkr|ngn"

_SYMBOL_FOR = {"usd": "$", "eur": "€", "gbp": "£", "inr": "₹"}

_MULTIPLIER = {"k": 1_000, "m": 1_000_000}

_FLAGS = re.IGNORECASE | re.VERBOSE

# One pass, so the branches compete in document order rather than by which
# pattern happens to be tried first. Group names have to be unique across
# alternatives, hence the per-branch prefixes.
_MONEY = re.compile(rf"""
      (?P<sym>[$€£₹¥]) \s? (?P<sym_amt>{_NUM}) \s? (?P<sym_mul>[km])?\b
      # "30$ per thumbnail" — common from non-US posters. The lookahead keeps
      # a ticker ("1 $SOL") from reading as one dollar.
    | \b (?P<tail_amt>{_NUM}) \s? (?P<tail_mul>[km])? \s? (?P<tail_sym>[$€£₹]) (?![a-z0-9])
    | \b (?P<post_amt>{_NUM}) \s? (?P<post_mul>[km])? \s? (?P<post_code>{_CODES}) \b
    | \b (?P<pre_code>{_CODES}) \s? (?P<pre_amt>{_NUM}) \s? (?P<pre_mul>[km])?\b
      # "5k/month" — no currency anywhere, but the rate unit makes it money.
    | \b (?P<k_amt>{_NUM}) \s? k \s? (?=(?:/|per\b|a\b|an\b) \s?
          (?:month|mo\b|video|week|edit|short|reel|clip))
""", _FLAGS)

# A number with no currency mark at all, rescued by the word in front of it.
_ANCHORED = re.compile(rf"""
    \b (?: budget (?:\s+ is | \s+ of | \s* :)? \s+ (?:around \s+|about \s+|~\s?)?
         | pay (?:ing|s)? \s+ (?:you \s+)? (?:up \s+ to \s+)?
         | rate \s+ is \s+ | offering \s+ )
    \$? (?P<anchor_amt>{_NUM}) \s? (?P<anchor_mul>[km])? \b
""", _FLAGS)

# Continues a match into a range: "$500-1k", "150 to 200".
_RANGE = re.compile(rf"""
    ^ \s? (?:-|–|—|to|~) \s? [$€£₹]? \s?
    (?P<amt>{_NUM}) \s? (?P<mul>[km])? \b
""", _FLAGS)

# The same range written so that only the *second* number carries the currency
# — "150 to 200 usd" — which is where the scan starts. Without this the low end
# is silently dropped and the post reads as a flat 200.
_RANGE_BEFORE = re.compile(rf"""
    \b (?P<amt>{_NUM}) \s? (?P<mul>[km])? \s? (?:-|–|—|to) \s? [$€£₹]? \s? $
""", _FLAGS)

# "per 30-second reel" is why anything is allowed between the preposition and
# the unit noun.
_PERIOD = re.compile(r"""
    (?: / | \b per \s+ | \b an? \s+ | \b each \s+ ) [\w\s-]{0,15}?
    \b (?P<unit>video|vid|reel|short|edit|clip|thumbnail|design|post|month|mo|week
              |day|hour|hr|min(?:ute)?|project|episode|podcast) s? \b
  | \b (?P<adverb>monthly|weekly|hourly|daily) \b
""", _FLAGS)

_PERIOD_NAME = {"mo": "month", "hr": "hour", "min": "minute", "minute": "minute",
                "vid": "video",
                "monthly": "month", "weekly": "week", "hourly": "hour", "daily": "day"}

# Money that belongs to someone else's story, not to an offer of work.
_BEFORE_BLOCKLIST = re.compile(r"""
    (?: \b(?: made | make | making | earn (?:ed|ing|s)? | generat\w+ | profit\w* | revenue
            | raised | won | win | worth | saved | grew | scaled | flipped
            | market \s? cap | mcap | giveaway | airdrop | prize | raffl\w+ )\b
      | \b giv (?:e|ing|es) \s+ away \b | \b gave \s+ away \b )
    [^.!?]{0,25} $
""", _FLAGS)

_AFTER_BLOCKLIST = re.compile(r"""
    ^ [^.!?]{0,25} \b(?: giveaway | airdrop | prize | raffle | market \s? cap | mcap
                       | in \s+ (?:sales|revenue|profit) | followers? | subs(?:cribers?)?
                       | views? | likes? )\b
""", _FLAGS)


@dataclass(slots=True)
class Budget:
    """An amount, and whatever the post said about the unit it buys."""

    low: float
    high: float | None = None
    currency: str = ""      # a display token: "$", "€", "USD", or "" if unstated
    period: str = ""        # "video", "month", "hour", ... or "" if unstated

    @property
    def display(self) -> str:
        low = _fmt(self.low)
        if self.high is not None and self.high != self.low:
            body = f"{self._money(low)}-{self._money(_fmt(self.high))}"
        else:
            body = self._money(low)
        return f"{body}/{self.period}" if self.period else body

    def _money(self, amount: str) -> str:
        if not self.currency:
            return amount
        if len(self.currency) == 1:          # a symbol: prefix, no space
            return f"{self.currency}{amount}"
        return f"{amount} {self.currency}"   # a code: suffix, spaced


def extract_budget(text: str) -> str:
    """The budget as a person would write it — "$500/video" — or "" if unstated.

    Returns a string rather than the `Budget` object because that is what the
    CSV, the JSON and the digest all want. Call `find_budget` for the parts.
    """
    found = find_budget(text)
    return found.display if found else ""


def find_budget(text: str) -> Budget | None:
    body = normalise(text)
    if not body:
        return None

    for match in _MONEY.finditer(body):
        budget = _build(body, match)
        if budget is not None:
            return budget

    for match in _ANCHORED.finditer(body):
        budget = _build(body, match)
        if budget is not None:
            return budget

    return None


def _build(body: str, match: re.Match[str]) -> Budget | None:
    groups = {k: v for k, v in match.groupdict().items() if v is not None}

    amount = _amount(
        _pick(groups, "sym_amt", "tail_amt", "post_amt", "pre_amt", "k_amt",
              "anchor_amt"),
        _pick(groups, "sym_mul", "tail_mul", "post_mul", "pre_mul", "anchor_mul")
        or ("k" if "k_amt" in groups else None),
    )
    if not amount:
        return None

    if _BEFORE_BLOCKLIST.search(body[: match.start()]):
        return None
    if _AFTER_BLOCKLIST.match(body[match.end():]):
        return None

    end = match.end()
    high = None
    if (rng := _RANGE.match(body[end:end + 12])) is not None:
        high = _amount(rng.group("amt"), rng.group("mul"))
        end += rng.end()
    elif (before := _RANGE_BEFORE.search(body[max(0, match.start() - 14):match.start()])):
        low = _amount(before.group("amt"), before.group("mul"))
        if 0 < low < amount:
            amount, high = low, amount

    code = _pick(groups, "post_code", "pre_code")
    symbol = _pick(groups, "sym", "tail_sym")
    currency = symbol or (_SYMBOL_FOR.get(code, code.upper()) if code else "")

    return Budget(low=amount, high=high, currency=currency,
                  period=_period(body[end:end + 30]))


def _pick(groups: dict[str, str], *names: str) -> str | None:
    for name in names:
        if name in groups:
            return groups[name]
    return None


def _amount(raw: str | None, multiplier: str | None) -> float:
    if not raw:
        return 0.0
    value = float(raw.replace(",", ""))
    if multiplier:
        value *= _MULTIPLIER.get(multiplier.lower(), 1)
    # "$0 budget" is a real phrase and never means a paying job.
    return value if value > 0 else 0.0


def _period(window: str) -> str:
    match = _PERIOD.search(window)
    # Anything further out than a few words belongs to the next clause:
    # "$500 and 3 videos a week" must not become "$500/week".
    if not match or match.start() > 15:
        return ""
    unit = (match.group("unit") or match.group("adverb") or "").lower()
    return _PERIOD_NAME.get(unit, unit)


def _fmt(value: float) -> str:
    return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"

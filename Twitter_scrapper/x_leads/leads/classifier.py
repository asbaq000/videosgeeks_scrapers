"""Scoring a tweet as a lead.

The model is a weighted sum with a few hard rules on top, chosen over anything
learned because there is no labelled training data, and because a rule-based
score can explain itself: every lead carries the list of signals that produced
it, so a wrong call points at the rule that made it instead of at a black box.

Shape of the decision:

    off-topic                       -> rejected immediately
    reader-facing sales question    -> rejected, whatever else it scored
    noise with no real ask          -> rejected
    otherwise  score = asks - offers, banded into hot / warm / cold

The one rule worth understanding before changing anything: a second-person
pitch ("Are you looking for a video editor?") is a hard reject *unless* the
post also asks applicants to send work. That exception exists because real job
posts do sometimes open with a hook — "Need a video editor? We're hiring, drop
your reel below" — and only the applicant-facing imperative tells the two
apart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from x_leads.leads import patterns as P
from x_leads.models import Lead, Tweet

LOGGER = logging.getLogger(__name__)

# Sort key for tweets whose timestamp X did not return.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Per-rule weights. Anything not listed uses the group default. These are
# tuned, not derived - see tests/test_classifier.py for the cases that pin
# each one down.
GROUP_WEIGHTS = {
    "demand": 4,
    "imperative": 5,
    "context": 2,
    "supply": -5,
    "pitch": -9,
    "noise": -4,
    "bio_supply": -3,
    "bio_demand": 1,
}

RULE_WEIGHTS = {
    # Asking for portfolios is the most reliable buyer signal there is.
    "demand:send-portfolio": 6,
    "demand:reply-with-work": 6,
    "demand:portfolio-below": 6,
    # A stated budget nearly always means a real engagement.
    "context:budget": 3,
    # "need someone to edit" is the weakest ask in the set — on a live run it
    # was mostly fans wanting a clip cut together, with no channel and no
    # budget behind it. Weighted to land below the threshold on its own, so it
    # only counts when something else (ownership, budget, a role) supports it.
    "demand:need-someone": 3,
    # Declaring yourself an editor is definitive; showing off work is not.
    "supply:i-am-editor": -7,
    "supply:hire-me": -7,
    "supply:my-portfolio": -6,
    "noise:showcase": -3,
    "noise:complaint": -6,
    "noise:guru-funnel": -6,
    "noise:engagement-bait": -6,
    "noise:crypto": -8,
}

# Group caps stop one long post stacking six variations of the same idea into a
# runaway score.
GROUP_CAPS = {
    "demand": 10,
    "imperative": 12,
    "context": 7,
    "supply": -14,
    "noise": -12,
}

HOT, WARM, COLD = 11, 7, 4


@dataclass(slots=True)
class Thresholds:
    hot: int = HOT
    warm: int = WARM
    cold: int = COLD


def _hits(text: str, rules: list[P.Rule]) -> list[str]:
    return [name for name, pattern in rules if pattern.search(text)]


def _score_group(names: list[str], group: str) -> int:
    default = GROUP_WEIGHTS[group]
    total = sum(RULE_WEIGHTS.get(name, default) for name in names)
    cap = GROUP_CAPS.get(group)
    if cap is None:
        return total
    return max(total, cap) if cap < 0 else min(total, cap)


class LeadClassifier:
    """Turns tweets into scored leads.

    `min_followers` is off by default on purpose. A follower count is a proxy
    for budget and a bad one — several of the clearest buyers in the sample had
    under 500 followers, because someone starting a channel is exactly who
    needs an editor.
    """

    def __init__(
        self,
        thresholds: Thresholds | None = None,
        min_followers: int = 0,
        require_niche: bool = True,
        use_bio: bool = True,
    ):
        self.t = thresholds or Thresholds()
        self.min_followers = min_followers
        self.require_niche = require_niche
        self.use_bio = use_bio

    def classify(self, tweet: Tweet) -> Lead:
        text = P.normalise(tweet.text)
        lead = Lead(tweet=tweet)

        if not text:
            lead.reject_reason = "empty text"
            return lead

        niche = _hits(text, P.NICHE_TERMS)
        if self.require_niche and not niche:
            lead.reject_reason = "not about video or social content"
            return lead

        demand = _hits(text, P.DEMAND)
        imperative = _hits(text, P.DEMAND_IMPERATIVE)
        context = _hits(text, P.DEMAND_CONTEXT)
        supply = _hits(text, P.SUPPLY)
        pitch = _hits(text, P.SECOND_PERSON_PITCH)
        noise = _hits(text, P.NOISE)

        signals = demand + imperative + context + supply + pitch + noise

        score = (
            _score_group(demand, "demand")
            + _score_group(imperative, "imperative")
            + _score_group(context, "context")
            + _score_group(supply, "supply")
            + _score_group(noise, "noise")
        )

        if self.use_bio:
            bio = P.normalise(tweet.author.bio)
            if bio:
                bio_supply = _hits(bio, P.BIO_SUPPLY)
                bio_demand = _hits(bio, P.BIO_DEMAND)
                # Only the strongest bio signal counts, and supply wins ties:
                # "video editor | content creator" is an editor.
                if bio_supply:
                    score += GROUP_WEIGHTS["bio_supply"]
                    signals.append(bio_supply[0])
                elif bio_demand:
                    score += GROUP_WEIGHTS["bio_demand"]
                    signals.append(bio_demand[0])

            profession = (tweet.author.professional_category or "").strip().lower()
            if profession in P.SUPPLY_PROFESSIONS:
                score += GROUP_WEIGHTS["bio_supply"]
                signals.append(f"profile:{profession.replace(' ', '-')}")

        lead.score = score
        lead.signals = signals

        # ---- hard rules, applied after scoring so the score is still visible
        asks = bool(demand or imperative)

        if pitch and not imperative:
            lead.reject_reason = "sales pitch aimed at the reader"
            return lead

        if not asks:
            lead.reject_reason = "no request for help"
            return lead

        if _score_group(supply, "supply") <= -10 and not imperative:
            lead.reject_reason = "author is offering editing services"
            return lead

        if len(noise) >= 2 and not imperative:
            lead.reject_reason = "promotional or off-topic"
            return lead

        if self.min_followers and tweet.author.followers < self.min_followers:
            lead.reject_reason = f"under {self.min_followers} followers"
            return lead

        if score >= self.t.hot:
            lead.verdict = "hot"
        elif score >= self.t.warm:
            lead.verdict = "warm"
        elif score >= self.t.cold:
            lead.verdict = "cold"
        else:
            lead.reject_reason = f"score {score} below threshold {self.t.cold}"
        return lead

    def classify_all(self, tweets: list[Tweet]) -> list[Lead]:
        """Every tweet scored, best first.

        Rejected leads come back too. Throwing them away here would make the
        filter impossible to audit, and `--include-rejected` is how you find
        out the classifier is dropping something it shouldn't.
        """
        leads = [self.classify(t) for t in tweets]
        leads.sort(key=lambda l: (l.score, l.tweet.created_at or _EPOCH), reverse=True)
        return leads


def classify(tweet: Tweet, **kwargs) -> Lead:
    return LeadClassifier(**kwargs).classify(tweet)


def classify_all(tweets: list[Tweet], **kwargs) -> list[Lead]:
    return LeadClassifier(**kwargs).classify_all(tweets)

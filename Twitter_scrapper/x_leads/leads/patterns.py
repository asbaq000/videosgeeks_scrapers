"""The rules that separate a client from a competitor.

Every pattern here was written against real X output — the 167 posts the old
scraper collected on 2026-08-12, plus live searches on 2026-08-13. The problem
they solve is that **a keyword match tells you nothing about which side of the
deal the poster is on.** These two posts share a phrase, word for word:

    "Looking for a video editor! $500 per 30-second reel."   <- a client
    "Looking for a Video Editor? DM Me!"                     <- a competitor

In the sample, freelance editors advertising themselves outnumbered actual
buyers roughly three to one, and they use the buyer's exact vocabulary because
they are writing *bait* for it. So the discriminator cannot be the words used;
it has to be grammatical stance:

    who is asking            "I need", "we're hiring", "drop your portfolio"
    who is offering          "I'm an editor", "hire me", "are you looking for?"

`SECOND_PERSON_PITCH` is the single highest-value rule. "Are you looking for a
video editor?" and "If you're looking for an editor, DM" are pure sales copy,
and they were the largest single category of false positives.

Tuning notes for later:
* Weights live in `classifier.py`. This module only says what matched.
* Add a phrase here, then run `python -m pytest tests/test_classifier.py` —
  the tricky real-world cases are all pinned there as tests.
"""

from __future__ import annotations

import re
import unicodedata

Rule = tuple[str, re.Pattern[str]]

_FLAGS = re.IGNORECASE | re.VERBOSE

# Stripped before matching: a t.co link is noise, and its random characters
# create accidental word-boundary hits.
_URL_RE = re.compile(r"https?://\S+|\bt\.co/\S+", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Lowercase, de-styled, link-free text for matching.

    NFKC matters more than it looks: job posts love styled unicode
    ("𝐇𝐈𝐑𝐈𝐍𝐆 𝐕𝐈𝐃𝐄𝐎 𝐄𝐃𝐈𝐓𝐎𝐑"), which no plain regex would ever match.
    NFKC folds those code points back to ASCII letters.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _URL_RE.sub(" ", text)
    text = (
        text.replace("’", "'").replace("‘", "'")
        .replace("“", '"').replace("”", '"')
        .replace("—", " ").replace("–", " ")
    )
    return _WS_RE.sub(" ", text).strip().lower()


def _compile(rules: dict[str, str]) -> list[Rule]:
    return [(name, re.compile(pattern, _FLAGS)) for name, pattern in rules.items()]


# The services this business sells. A post has to be about one of them.
ROLE = r"""
    (?: video \s+ editor s? | videographer s? | editor s? | editing
      | thumbnail \s+ (?:designer|artist) s? | motion \s+ (?:designer|graphics?)
      | animator s? | animation | vfx \s+ artist s? | colou?r \s+ grader s?
      | shorts? \s+ editor s? | reels? \s+ editor s? | content \s+ editor s?
      | social \s+ media \s+ (?:manager|editor) s? | podcast \s+ editor s?
      | (?:ugc|content|video) \s+ (?:creator|manager) s? | post[- ]production )
"""

# The words allowed between a need verb and the role it refers to, as in
# "looking for a **full-time, US-based** video editor". An earlier version used
# `[\w\s]`, which stops dead at a hyphen — and job posts are full of
# "full-time", "long-form", "US-based". Every one of those was being missed.
GAP = r"[-\w\s'/&,\.\+]{0,30}?"

# Wider than ROLE, and only for "I am one of these" self-declarations. A bare
# "designer" is too vague to decide the niche on — it catches logo and web work
# — but "I'm actually the designer" is self-identifying whatever the noun.
SELF_ROLE = rf"(?: {ROLE} | designer s? | creator s? | artist s? | freelancer s? )"

NICHE_TERMS = _compile({
    "niche:role": rf"\b{ROLE}",
    "niche:platform": r"""\b(?: youtube | yt \b | tiktok | instagram | \b ig \b
                              | reels? | shorts? | twitch | podcast | vlog
                              | social \s+ media | ugc )""",
    "niche:craft": r"""\b(?: video s? | footage | b[-\s]?roll | edit s? | edits?
                            | thumbnail s? | captions? | subtitles?
                            | premiere \s+ pro | after \s+ effects? | capcut
                            | davinci | final \s+ cut )\b""",
})


# ---------------------------------------------------------------- the ask
# Someone stating a need. Deliberately requires the need verb; "video editor"
# on its own is not an ask.
DEMAND = _compile({
    "demand:hiring": r"""\b(?: hiring | we'?re \s+ hiring | now \s+ hiring
                              | looking \s+ to \s+ hire | want \s+ to \s+ hire
                              | ready \s+ to \s+ hire | hiring: )\b""",
    "demand:looking-for": rf"""\b(?: (?:i'?m|im|we'?re|we \s+ are|i \s+ am) \s+ )?
                               (?: looking \s+ for | searching \s+ for | seeking
                                 | in \s+ need \s+ of | on \s+ the \s+ hunt \s+ for )
                               \s+ (?:a|an|some|my|our|the)? \s* {GAP} {ROLE}""",
    "demand:need": rf"""\b(?: i | we ) \s+ (?: need | needed | require | want )
                        \s+ (?:a|an|some|to \s+ hire)? \s* {GAP} {ROLE}""",
    "demand:need-bare": rf"""\b need \s+ (?:a|an) \s+ {GAP} {ROLE}""",
    "demand:need-someone": r"""\b need \s+ (?:someone|somebody|help|an \s+ extra \s+ hand)
                               \s+ (?:to|who|with|for) \b""",
    "demand:help-me": r"""\b(?: help \s+ me \s+ (?:edit|make|create|with)
                              | can \s+ (?:someone|anyone) \s+ (?:edit|help) )\b""",
    "demand:recommendation": r"""\b(?: any(?:one|body)? \s+ know
                                    | any(?:one|body)? \s+ recommend
                                    | recommend (?:ations?)? \s+ (?:for|me|a|an)
                                    | who'?s? \s+ (?:a|the) \s+ (?:best|good)
                                    | suggest \s+ (?:me \s+)? (?:a|an|some) )""",
    "demand:who-can": r"""\b who \s+ (?:can|could|wants \s+ to|is \s+ free \s+ to)
                          \s+ (?:edit|make|help|design) """,
    "demand:job-post": r"""\b(?: job \s+ (?:opening|post(?:ing)?|opportunity|offer)
                               | (?:position|vacancy|role|opening) \s+ (?:open|available)
                               | (?:paid|remote|freelance) \s+ (?:\w+ \s+)?
                                 (?:opportunity|gig|role|job|position)
                               | we \s+ are \s+ looking \s+ for
                               | join \s+ (?:my|our) \s+ team
                               | taking \s+ applications )\b""",
    # Structured job ads sometimes never use a need verb at all — the title is
    # the ask ("REMOTE VIDEO EDITOR / ₦250,000 per month / Requirements: ...").
    # The section headings are what give them away.
    # Classified-ad phrasing, where the job title carries the whole request:
    # "VIDEOGRAPHER NEEDED FOR ...", "REMOTE VIDEO EDITOR / ₦250,000 a month".
    "demand:role-wanted": rf"""(?: {ROLE} \s+ (?:needed|wanted|required|vacancy)
                                 | (?:remote|freelance|full[-\s]?time|part[-\s]?time
                                     |in[-\s]?house|contract) \s+ {ROLE} \b )""",
    "demand:job-spec": r"""\b(?: requirements? | responsibilities | qualifications
                               | what \s+ you'?ll \s+ (?:do|edit|be \s+ doing)
                               | what \s+ we'?re \s+ looking \s+ for
                               | how \s+ to \s+ apply | to \s+ apply
                               | job \s+ type | deliverables | scope \s+ of \s+ work
                               | send \s+ (?:your \s+)? (?:cv|resume|application) )""",
})

# Instructions aimed at applicants. The strongest signal in the whole set:
# only someone doing the hiring asks to be *sent* a portfolio. An editor
# advertising says "here is my portfolio" instead.
DEMAND_IMPERATIVE = _compile({
    "demand:send-portfolio": r"""\b(?: drop | send | share | post | comment | submit
                                     | show \s+ me | dm \s+ me | reply \s+ with )
                                 \s+ (?:me \s+)? (?:your|ur|the|a|some)? \s*
                                 (?: portfolio s? | reel s? | showreel s? | sample s?
                                   | work s? | cv s? | resume s? | rate s? | link s? )\b""",
    "demand:reply-with-work": r"""\b(?: reply | comment | dm | message ) \b
                                  [^.!?]{0,30}?
                                  \b (?:with \s+)? (?:your|ur) \s+
                                  (?: work | portfolio | reel | sample s? | edits? )\b""",
    "demand:portfolio-below": r"""\b(?: portfolio s? | reel s? | work | sample s? )
                                  \s+ (?: below | in \s+ the \s+ (?:comments?|replies) )\b""",
    "demand:apply": r"""\b apply \s+ (?: here | below | now | via | through | using ) \b""",
    "demand:tag-someone": r"""\b tag \s+ (?:a|an|some|your) \b [^.!?]{0,20} \b editor""",
})

# Terms and conditions of an actual engagement. Nobody attaches a budget to
# small talk, so these are what promote a plausible post to a confident one.
DEMAND_CONTEXT = _compile({
    "context:budget": r"""(?: \$ \s? \d | \b \d+ \s? (?:usd|eur|gbp|inr|sgd|aud|cad)\b
                             | \b (?:budget|salary|compensation|pay(?:ing|ment)?|paid)\b
                             | \b \d+ \s? k \s? (?:/|per|a) \s? (?:month|mo|video)\b )""",
    "context:rate": r"""\b(?: per \s+ (?:video|reel|edit|short|month|week|hour|clip)
                            | /(?:video|month|mo|week|hr|hour)
                            | monthly | weekly | retainer | hourly )\b""",
    "context:commitment": r"""\b(?: full[-\s]?time | part[-\s]?time | long[-\s]?term
                                  | ongoing | permanent | contract | freelance \s+ role
                                  | remote )\b""",
    "context:volume": r"""\b \d+ \s* (?:\+ \s*)? (?: videos? | reels? | shorts? | clips? | edits? )
                          \s+ (?:a|per|every|each) \s+ (?:day|week|month) """,
    "context:ownership": r"""\b(?: my | our ) \s+
                             (?: channel | youtube | video s? | podcast | brand | content
                               | company | team | startup | agency | client s? | page
                               | account | business )\b""",
    # Bare "today" is not urgency — it is the most common word on X. It was
    # firing on holiday snaps and news posts and inflating their scores.
    "context:urgency": r"""\b(?: urgent(?:ly)? | asap | immediately
                               | (?:starting|start|need(?:ed)?|hiring) \s+
                                 (?:today|tomorrow|this \s+ week|immediately)
                               | deadline | by \s+ (?:friday|monday|the \s+ weekend) )\b""",
})


# --------------------------------------------------------------- the offer
# A competitor. Everything below is someone selling the service.
SUPPLY = _compile({
    # Up to two words before the article, and `the` alongside `a/an`: the
    # strict form missed "I'm actually the designer" and "I'm literally a
    # video editor", which are as self-declaring as it gets.
    "supply:i-am-editor": rf"""\b(?: i'?m | im | i \s+ am ) \s+ (?:\w+ \s+){{0,2}}
                               (?:a|an|the) \s+ {GAP} {SELF_ROLE}""",
    "supply:i-do-work": r"""\b i \s+ (?: edit | create | make | design | produce | craft
                                       | specialise | specialize | offer | provide
                                       | deliver | help \s+ (?:you|brands|creators) )\b""",
    "supply:i-can": r"""\b i \s+ (?:can|could|will|would) \s+
                        (?: edit | help | make | create | design | do \s+ (?:it|this|that) )\b""",
    "supply:hire-me": r"""\b(?: hire \s+ me | work \s+ with \s+ me | let'?s \s+ work
                              | book \s+ me | choose \s+ me | trust \s+ me \s+ (?:to|with) )\b""",
    # "my work" needs the lookahead: "Looking for a Shorts Editor to add to my
    # work team" is a buyer, and without it that post was scored as a
    # competitor and thrown away.
    "supply:my-portfolio": r"""\b my \s+ (?: portfolio | reel | showreel | rate s?
                                            | service s? | edit s? | client s? | dm s?
                                            | work s? \b (?! \s+ (?:team|place|force|flow
                                                                 |shop|space|ethic)) )\b""",
    "supply:check-my": r"""\b(?: check \s+ (?:out \s+)? my | here'?s \s+ my
                               | this \s+ is \s+ my \s+ (?:work|edit|reel)
                               | portfolio \s* [:\-] | link \s+ in \s+ bio )""",
    "supply:available": r"""\b(?: available \s+ (?:for|to) \s+ (?:work|hire|projects?|new)
                                | open \s+ (?:for|to) \s+ (?:work|commissions?|projects?|collabs?)
                                | (?:slots?|spots?) \s+ (?:open|available|left)
                                | accepting \s+ (?:new \s+)? clients?
                                | taking \s+ (?:on \s+)? (?:new \s+)? clients? )\b""",
    "supply:dm-for": r"""\b(?: dm \s+ (?:me \s+)? (?:for|if|to \s+ (?:work|discuss|book))
                             | dm s? \s+ (?:are \s+ )? open
                             | hit \s+ (?:the|my|up \s+ my) \s+ dm s?
                             | slide \s+ in(?:to)? \s+ (?:the|my) \s+ dm s?
                             | send \s+ me \s+ a \s+ (?:dm|message)
                             | inbox \s+ me | message \s+ me )\b""",
    "supply:seeking-work": r"""\b i'?m? \s+ (?:looking \s+ for | seeking | in \s+ search \s+ of)
                               \s+ (?: a \s+ job | work | clients? | opportunit
                                     | an \s+ internship | remote \s+ work )""",
    "supply:experience-pitch": r"""\b(?: years? \s+ of \s+ experience
                                       | i'?ve \s+ (?:edited|worked \s+ with|helped)
                                       | worked \s+ with \s+ \d+ \+? \s+ clients? )\b""",
    # Agencies pitch in the third person — "I run X, a video editing team" —
    # which slips past every first-person rule above.
    "supply:runs-agency": r"""\b i \s+ (?:run|own|lead|founded|head) \s+
                              (?:a|an|my|our)? \s* \w* \s*
                              (?: agency | studio | team | company | collective )""",
    # Replies to somebody else's job post: an editor applying, not a buyer
    # asking. They quote the buyer's words, so they look like demand.
    "supply:applying": r"""\b(?: i \s+ saw \s+ (?:you|your|ur|this)
                              | can \s+ i \s+ (?:get|have|be|apply|help)
                              | i'?m \s+ (?:interested|down|available|keen)
                              | i'?d \s+ love \s+ to \s+ (?:help|work|edit|be)
                              | count \s+ me \s+ in
                              | (?:just \s+)? (?:sent|slid \s+ into) \s+ (?:you \s+)? a? \s* dm
                              | check \s+ your \s+ dm s? )\b""",
})

# The decisive one. A question aimed at *the reader* is advertising copy, not a
# brief. "Are you looking for a video editor?" was the single biggest source of
# false positives in the 2026-08-12 sample.
SECOND_PERSON_PITCH = _compile({
    # Both of these require the *role* to appear inside the question. Without
    # that, "still need a video editor ... if you can help, my budget is low"
    # tripped the rule and a genuine buyer was thrown out as a sales pitch.
    "pitch:are-you-looking": rf"""\b(?: are | r ) \s+ (?:you|u|ya) \b [^.!?]{{0,40}}?
                                  \b(?: looking \s+ for | in \s+ need \s+ of | need
                                      | want | struggling \s+ with | tired \s+ of )\b
                                  [^.!?]{{0,40}}? {ROLE}""",
    "pitch:if-you-need": rf"""\b if \s+ (?: you'?re | you \s+ are | u \s+ r | ur | you | u ) \b
                              [^.!?]{{0,30}}? \b(?: looking \s+ for | need | want )\b
                              [^.!?]{{0,40}}? {ROLE}""",
    "pitch:do-you-need": r"""\b do \s+ (?:you|u) \s+ (?: need | want | have \s+ a \s+ need ) \b""",
    # "Hiring Video Editor?" reads as a job ad but is a freelancer's hook — the
    # question mark is the tell, so `hiring` belongs in this list too.
    "pitch:rhetorical": rf"""\b(?: looking \s+ for | need s? | want | hiring ) \s+
                             (?:a|an|some)? \s* [\w\s]{{0,25}}? {ROLE} \s* \?""",
    "pitch:anyone-need": r"""\b any(?:one|body)? \s+ (?: need s? | want s? | looking \s+ for )\b""",
    "pitch:your-brand": r"""\b(?: your | ur ) \s+ (?: brand | channel | business | content
                                                   | videos? | company | page )\b
                            [^.!?]{0,40}? \b(?: deserve | need | grow | scale | stand \s+ out )""",
    # Whose work is it? A buyer says "an editor for my channel"; a seller says
    # "an editor for your next video". The possessive settles it.
    "pitch:for-your": rf"""{ROLE} \s+ (?:for|to \s+ \w+) \s+ (?:your|ur|u'?r) \b""",
})


# ------------------------------------------------------------------- noise
# Not a buyer and not a competitor: growth-hack threads, platform news, memes
# and course funnels. These dominated the old scraper's output.
NOISE = _compile({
    # The quotes around the magic word are load-bearing. Without them this
    # matched "reply to this with your work" and "drop your reel below" — the
    # instructions in a real job post — and penalised the best leads in the set.
    "noise:engagement-bait": r"""(?: \b like \s* \+ \s* comment \b
                                   | \b comment \s+ ['"]? \w+ ['"]? \s+ (?:and|&) \s+ i'?ll \b
                                   | \b reply \s+ (?:with \s+)? ['"] \w+ ['"]
                                   | \b drop \s+ (?:a|an) \s+ ['"] \w+ ['"]
                                   | \b retweet \s+ (?:this|and|&|to) \b
                                   | \b (?:rt|like) \s+ (?:\+|and|&) \s+ follow \b
                                   | \b giveaway \b | \b tag \s+ \d+ \s+ friends? \b )""",
    "noise:guru-funnel": r"""\b(?: faceless \s+ (?:youtube \s+)? channel s?
                                 | monetiz \w*
                                 | (?:make|earn) \s+ \$? \d+ k? \s* (?:/|per|a) \s* month
                                 | first \s+ \$ \d+
                                 | free \s+ (?:guide|course|blueprint|template|ebook|masterclass|resources?)
                                 | i'?ll \s+ send \s+ (?:you|it)
                                 | dm \s+ me \s+ ['"]? \w+ ['"]? \s+ (?:and|to \s+ get)
                                 | side \s+ hustle | passive \s+ income )\b""",
    "noise:listicle": r"""\b(?: here \s+ are \s+ \d+ | \d+ \s+ (?:prompts?|tools?|tips?|ways?|hacks?|plugins?|presets?|secrets?)
                              | thread \s* (?: \b | 🧵 ) | a \s+ thread | mega \s+ thread
                              | step[-\s]by[-\s]step \s+ guide )""",
    "noise:ai-hype": r"""\b(?: ai \s+ (?:just \s+)? (?:turned|can \s+ now|will \s+ replace|is \s+ replacing)
                             | this \s+ ai \s+ tool | \b gpt \b | claude \s+ can
                             | built \s+ (?:this|it) \s+ with \s+ ai )""",
    "noise:crypto": r"""\b(?: airdrop | nft s? | crypto | token | presale | web ?3
                            | \$ [A-Z]{3,} \b | pump | memecoin )\b""",
    "noise:platform-news": r"""\b(?: youtube \s+ (?:is|has|will|just) \s+ (?:making|changing|updating|rolling|announced)
                                   | (?:terminated|demonetiz \w* | strike d?) \b [^.!?]{0,30} \b channel
                                   | partner \s+ program
                                   | algorithm \s+ (?:change|update) )""",
    "noise:complaint": r"""\b(?: why \s+ is \s+ (?:video \s+)? editing \s+ so
                               | (?:video \s+)? editing \s+ is \s+ (?:so \s+)? (?:hard|difficult|tedious|boring|painful)
                               | i \s+ hate \s+ editing
                               | hate s? \s+ editing \s+ videos?
                               | editing \s+ takes \s+ (?:so \s+ long|forever) )""",
    "noise:learning": r"""\b(?: learn \s+ (?:a \s+ skill | video \s+ editing | how \s+ to \s+ edit)
                              | tutorial s? | how \s+ to \s+ edit \s+ like
                              | free \s+ (?:resources?|drive \s+ link) )""",
    "noise:showcase": r"""\b(?: recent \s+ (?:work|edit|re-?edit) | latest \s+ edit
                              | highlights? \s+ from | made \s+ this | i \s+ made
                              | new \s+ edit \s+ (?:out|drop) )\b""",
})


# Author-profile signals. Weaker than the text on purpose: a working editor can
# still be hiring a second pair of hands, and someone's bio is often years out
# of date. These nudge a borderline score; they never decide one alone.
BIO_SUPPLY = _compile({
    "bio:is-editor": rf"""\b(?: {ROLE} | freelance r? | for \s+ hire | dm \s+ for )""",
    "bio:portfolio": r"""\b(?: portfolio | showreel | my \s+ work | hire \s+ me
                             | available \s+ for )\b""",
})

BIO_DEMAND = _compile({
    "bio:is-creator": r"""\b(?: youtuber | content \s+ creator | streamer | podcaster?
                              | creator \b | influencer | vlogger )""",
    "bio:is-business": r"""\b(?: founder | co-?founder | ceo | owner | entrepreneur
                               | coach | consultant | agency | marketing | brand
                               | we \s+ help | building )\b""",
})

# X's own self-declared profession. A cleaner supply signal than the bio prose,
# because the user picked it from a fixed list rather than writing it.
SUPPLY_PROFESSIONS = frozenset({
    "editor", "video creator", "photographer", "artist", "graphic designer",
    "animator", "designer", "videographer",
})

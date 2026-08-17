"""Working out which country a lead's author is in, and dropping the unwanted.

Four countries are excluded by default — India, Pakistan, Bangladesh and the
Philippines. Nothing about that list is special to this module: it is a default,
replaceable per run (`--exclude-country` / `--allow-country`) or per caller
(`LocationFilter.from_names`).

Why this is harder than the Upwork equivalent
---------------------------------------------
`upwork_scraper/country_filter.py` reads a *verified billing country* — one of
~200 canonical values, spelled either as a full name or an ISO code. It can get
away with an alias table and exact matching.

X's location is a free-text profile field that is optional and unverified. What
people actually put in it:

    "Karachi"            a city, never the country
    "Mumbai, India"      city plus country
    "Dhaka 🇧🇩"           a flag emoji
    ""                   blank — very common
    "Worldwide"          unjudgeable
    "your DMs"           a joke

So the bulk of the work here is a **city -> country** table, not a country
alias table: someone in Lahore does not write "Pakistan". Matching is done on
word boundaries rather than substrings, because "Indiana" and "Indianapolis"
both contain "india" and neither is in India.

Sources, in the order they are tried
------------------------------------
1. `location` — country name, ISO-3166 alpha-3, city, or region  (`location`)
2. a flag emoji in `location` or the display name                (`flag`)
3. the website's country TLD, e.g. `.pk`                         (`website`)
4. an international dialling code in the bio, e.g. `+92`         (`bio-phone`)
5. an explicit "based in <place>" in the bio                     (`bio-mention`)

1-2 are strong. 3-5 are weaker and exist because the location field is blank so
often; each is labelled with its source so a run can be audited per source and
the weak ones dropped if they turn out to misfire.

Deliberately *not* a source: bare city names anywhere in the bio. "I edit for
creators in Mumbai and Dubai" is not evidence about where the author lives,
and scanning free bio text for city names is how a filter starts eating real
leads. Only the explicit "based in" form counts.

Ambiguity
---------
Some names span two countries: Punjab and Hyderabad exist in both India and
Pakistan, Sindh only in Pakistan. Both countries are excluded by default, so
the drop decision is unaffected and only the breakdown label could be wrong.
Names that clash with a *non*-excluded country are left out of the table
entirely rather than guessed at — "Laguna" (Philippines, but also California
and Spain) and "NCR" (both Delhi and Metro Manila) are absent on purpose.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

LOGGER = logging.getLogger(__name__)

# The default block list. Entries are canonical keys — see `_COUNTRY_NAMES`.
# Egypt is *not* here, unlike the Upwork filter's default; add it with
# `--exclude-country egypt` if you want parity.
DEFAULT_EXCLUDED_COUNTRIES: tuple[str, ...] = (
    "india",
    "pakistan",
    "bangladesh",
    "philippines",
)

# ---------------------------------------------------------------- name tables
# canonical -> spellings of the country itself (long name, ISO alpha-3, and any
# form seen in the wild). ISO alpha-2 is handled separately in `_ISO2` because
# two letters are too short to match safely inside free text.
_COUNTRY_NAMES: dict[str, tuple[str, ...]] = {
    "india": ("india", "ind", "republic of india", "bharat"),
    "pakistan": ("pakistan", "pak", "islamic republic of pakistan"),
    "bangladesh": (
        "bangladesh", "bgd", "peoples republic of bangladesh",
        "people s republic of bangladesh",
    ),
    "philippines": (
        "philippines", "phl", "the philippines",
        "republic of the philippines", "pilipinas",
    ),
    "egypt": ("egypt", "egy", "arab republic of egypt"),
    # Not excluded by default. Present so that a location which *is* judgeable
    # resolves to "allowed" rather than "unknown" — which is what makes
    # `drop_unknown` usable at all.
    "united states": (
        "united states", "usa", "us of a", "united states of america", "america",
    ),
    "united kingdom": (
        "united kingdom", "gbr", "great britain", "england", "scotland",
        "wales", "northern ireland",
    ),
    "canada": ("canada", "can"),
    "australia": ("australia", "aus"),
    "new zealand": ("new zealand", "nzl"),
    "ireland": ("ireland", "irl", "eire"),
    "germany": ("germany", "deu", "deutschland"),
    "france": ("france", "fra"),
    "spain": ("spain", "esp", "espana"),
    "italy": ("italy", "ita", "italia"),
    "netherlands": ("netherlands", "nld", "the netherlands", "holland"),
    "belgium": ("belgium", "bel"),
    "switzerland": ("switzerland", "che"),
    "austria": ("austria", "aut"),
    "sweden": ("sweden", "swe"),
    "norway": ("norway", "nor"),
    "denmark": ("denmark", "dnk"),
    "finland": ("finland", "fin"),
    "poland": ("poland", "pol"),
    "portugal": ("portugal", "prt"),
    "united arab emirates": ("united arab emirates", "are", "uae"),
    "saudi arabia": ("saudi arabia", "sau", "ksa"),
    "qatar": ("qatar", "qat"),
    "israel": ("israel", "isr"),
    "turkey": ("turkey", "tur", "turkiye"),
    "south africa": ("south africa", "zaf"),
    "nigeria": ("nigeria", "nga"),
    "kenya": ("kenya", "ken"),
    "ghana": ("ghana", "gha"),
    "morocco": ("morocco", "mar"),
    "brazil": ("brazil", "bra", "brasil"),
    "mexico": ("mexico", "mex"),
    "argentina": ("argentina", "arg"),
    "colombia": ("colombia", "col"),
    "chile": ("chile", "chl"),
    "japan": ("japan", "jpn"),
    "south korea": ("south korea", "kor", "republic of korea"),
    "china": ("china", "chn"),
    "singapore": ("singapore", "sgp"),
    "malaysia": ("malaysia", "mys"),
    "indonesia": ("indonesia", "idn"),
    "thailand": ("thailand", "tha"),
    "vietnam": ("vietnam", "vnm", "viet nam"),
    "sri lanka": ("sri lanka", "lka"),
    "nepal": ("nepal", "npl"),
    "ukraine": ("ukraine", "ukr"),
    "romania": ("romania", "rou"),
    "russia": ("russia", "rus"),
}

# ISO alpha-2 -> canonical. Only ever matched against a whole comma-separated
# segment ("Lahore, PK"), never inside running text, because "in", "us", "ph"
# and "de" are all ordinary English fragments. Also used to decode flag emoji.
_ISO2: dict[str, str] = {
    "in": "india", "pk": "pakistan", "bd": "bangladesh", "ph": "philippines",
    "eg": "egypt", "us": "united states", "gb": "united kingdom",
    "uk": "united kingdom", "ca": "canada", "au": "australia",
    "nz": "new zealand", "ie": "ireland", "de": "germany", "fr": "france",
    "es": "spain", "it": "italy", "nl": "netherlands", "be": "belgium",
    "ch": "switzerland", "at": "austria", "se": "sweden", "no": "norway",
    "dk": "denmark", "fi": "finland", "pl": "poland", "pt": "portugal",
    "ae": "united arab emirates", "sa": "saudi arabia", "qa": "qatar",
    "il": "israel", "tr": "turkey", "za": "south africa", "ng": "nigeria",
    "ke": "kenya", "gh": "ghana", "ma": "morocco", "br": "brazil",
    "mx": "mexico", "ar": "argentina", "co": "colombia", "cl": "chile",
    "jp": "japan", "kr": "south korea", "cn": "china", "sg": "singapore",
    "my": "malaysia", "id": "indonesia", "th": "thailand", "vn": "vietnam",
    "lk": "sri lanka", "np": "nepal", "ua": "ukraine", "ro": "romania",
    "ru": "russia",
}

# The part that actually earns its keep: cities and regions -> country. People
# write where they live, and where they live is a city.
_PLACES: dict[str, str] = {}


def _add_places(country: str, *names: str) -> None:
    for name in names:
        _PLACES[name] = country


_add_places(
    "pakistan",
    "karachi", "lahore", "islamabad", "rawalpindi", "faisalabad", "multan",
    "peshawar", "quetta", "gujranwala", "sialkot", "bahawalpur", "sargodha",
    "sukkur", "larkana", "abbottabad", "mardan", "mirpur", "gujrat",
    "sahiwal", "okara", "wah cantt", "dera ghazi khan", "nawabshah",
    "chiniot", "kasur", "rahim yar khan", "jhang", "sheikhupura", "sindh",
    "balochistan", "baluchistan", "khyber pakhtunkhwa", "kpk", "gilgit",
    "azad kashmir", "isb", "lhr", "khi",
)

_add_places(
    "india",
    "mumbai", "bombay", "delhi", "new delhi", "bengaluru", "bangalore",
    "chennai", "madras", "kolkata", "calcutta", "pune", "ahmedabad", "surat",
    "jaipur", "lucknow", "kanpur", "nagpur", "indore", "thane", "bhopal",
    "visakhapatnam", "vizag", "patna", "vadodara", "ghaziabad", "ludhiana",
    "agra", "nashik", "faridabad", "meerut", "rajkot", "varanasi", "srinagar",
    "aurangabad", "dhanbad", "amritsar", "navi mumbai", "allahabad",
    "prayagraj", "ranchi", "howrah", "coimbatore", "jabalpur", "gwalior",
    "vijayawada", "jodhpur", "madurai", "raipur", "chandigarh", "guwahati",
    "solapur", "hubli", "mysore", "mysuru", "tiruchirappalli", "bareilly",
    "aligarh", "moradabad", "gurgaon", "gurugram", "noida", "greater noida",
    "kochi", "cochin", "thiruvananthapuram", "trivandrum", "bhubaneswar",
    "dehradun", "jamshedpur", "kerala", "maharashtra", "tamil nadu",
    "karnataka", "gujarat", "rajasthan", "uttar pradesh", "west bengal",
    "telangana", "andhra pradesh", "bihar", "haryana", "odisha", "orissa",
    "assam", "jharkhand", "madhya pradesh", "chhattisgarh", "uttarakhand",
    "himachal pradesh",
    # Shared with Pakistan. Both are excluded by default, so the only cost of
    # attributing them here is a possibly-wrong label in the breakdown.
    "punjab", "hyderabad",
)

_add_places(
    "bangladesh",
    "dhaka", "dacca", "chittagong", "chattogram", "khulna", "rajshahi",
    "sylhet", "mymensingh", "rangpur", "comilla", "cumilla", "barisal",
    "barishal", "narayanganj", "gazipur", "jessore", "jashore", "bogra",
    "bogura", "dinajpur", "tangail", "coxs bazar", "cox s bazar", "narsingdi",
    "savar", "feni", "pabna", "kushtia",
)

_add_places(
    "philippines",
    "manila", "metro manila", "quezon city", "cebu", "cebu city", "davao",
    "davao city", "makati", "taguig", "pasig", "caloocan", "zamboanga",
    "antipolo", "pasay", "cagayan de oro", "paranaque", "dasmarinas",
    "valenzuela", "bacoor", "general santos", "las pinas", "iloilo",
    "iloilo city", "bacolod", "san jose del monte", "muntinlupa", "marikina",
    "calamba", "angeles city", "baguio", "batangas", "cavite", "bulacan",
    "pampanga", "rizal", "mandaluyong", "san pedro", "binan", "santa rosa",
    "lipa", "tarlac", "olongapo", "naga city", "legazpi", "tacloban",
    "butuan", "cotabato", "dumaguete", "visayas", "mindanao", "luzon",
)

# A handful of major cities in countries that are *not* excluded. These exist
# only so `drop_unknown=True` is not a blunt instrument: without them, "London"
# is as unjudgeable as "" and a perfectly good UK lead gets binned.
_add_places(
    "united states",
    "new york", "nyc", "brooklyn", "los angeles", "san francisco", "chicago",
    "houston", "austin", "seattle", "boston", "atlanta", "miami", "denver",
    "dallas", "phoenix", "portland", "san diego", "las vegas", "nashville",
    "philadelphia", "washington dc", "california", "texas", "florida",
    "new jersey", "colorado",
)
_add_places(
    "united kingdom",
    "london", "manchester", "birmingham", "leeds", "glasgow", "edinburgh",
    "liverpool", "bristol", "cardiff", "belfast", "sheffield", "nottingham",
)
_add_places("canada", "toronto", "vancouver", "montreal", "calgary", "ottawa")
_add_places("australia", "sydney", "melbourne", "brisbane", "perth", "adelaide")
_add_places("united arab emirates", "dubai", "abu dhabi", "sharjah")
_add_places("germany", "berlin", "munich", "hamburg", "frankfurt", "cologne")
_add_places("netherlands", "amsterdam", "rotterdam", "the hague", "utrecht")
_add_places("france", "paris", "lyon", "marseille")
_add_places("spain", "madrid", "barcelona", "valencia")
_add_places("nigeria", "lagos", "abuja", "ibadan", "port harcourt")
_add_places("kenya", "nairobi", "mombasa")
_add_places("singapore", "singapore")
_add_places("japan", "tokyo", "osaka", "kyoto")
_add_places("indonesia", "jakarta", "bali", "surabaya", "bandung")
_add_places("brazil", "sao paulo", "rio de janeiro")
_add_places("mexico", "mexico city", "guadalajara")
_add_places("egypt", "cairo", "alexandria", "giza")
_add_places("sri lanka", "colombo")
_add_places("nepal", "kathmandu")
_add_places("south africa", "johannesburg", "cape town", "durban", "pretoria")

# ---------------------------------------------------------- matching machinery
# phrase -> canonical, for everything long enough to match inside free text.
_PHRASES: dict[str, str] = dict(_PLACES)
for _canonical, _spellings in _COUNTRY_NAMES.items():
    for _spelling in _spellings:
        _PHRASES[_spelling] = _canonical

# One regex for the whole table, longest phrase first so "new delhi" wins over
# "delhi" and "navi mumbai" over "mumbai". `\b` on both ends is what keeps
# "Indiana" out of India and "Manilla Road" out of Manila.
_PHRASE_RE = re.compile(
    r"\b(?:%s)\b"
    % "|".join(re.escape(p) for p in sorted(_PHRASES, key=len, reverse=True)),
)

# Country TLDs worth trusting. A `.in`/`.co` domain is a startup naming
# convention as often as a country, so those are deliberately absent.
_TLDS: dict[str, str] = {
    "pk": "pakistan", "bd": "bangladesh", "ph": "philippines",
    "eg": "egypt", "lk": "sri lanka", "np": "nepal", "ng": "nigeria",
    "ke": "kenya", "za": "south africa", "uk": "united kingdom",
    "de": "germany", "fr": "france", "nl": "netherlands", "au": "australia",
    "ca": "canada", "ae": "united arab emirates",
}

# International dialling codes, matched only when a digit follows so that a
# bare "+91" in prose does not count.
_DIAL_CODES: tuple[tuple[str, str], ...] = (
    ("+880", "bangladesh"),
    ("+92", "pakistan"),
    ("+91", "india"),
    ("+63", "philippines"),
    ("+20", "egypt"),
)
_DIAL_RE = tuple(
    (re.compile(re.escape(code) + r"[\s\-]?\d"), country)
    for code, country in _DIAL_CODES
)

# "based in Lahore", "from Dhaka". The only bio form trusted for a place name.
_BASED_IN_RE = re.compile(
    r"\b(?:based (?:in|out of)|living in|located in|from)\s+([a-z][a-z .'\-]{2,30})"
)

_RI_START, _RI_END = 0x1F1E6, 0x1F1FF

_PUNCTUATION = re.compile(r"[.,;:'’\"()\[\]|/\\!?*#_]")
_WHITESPACE = re.compile(r"\s+")
_SEGMENT_SPLIT = re.compile(r"[,;/|·•\n]+")


def _clean(value: str | None) -> str:
    """Lowercase, de-punctuate and collapse whitespace, keeping letters/digits."""
    if not value:
        return ""
    text = _PUNCTUATION.sub(" ", str(value)).casefold()
    return _WHITESPACE.sub(" ", text).strip()


def normalize_country(value: str | None) -> str | None:
    """Reduce a country *as named by a caller* to a canonical key.

    For `--exclude-country`, not for scraped text: it resolves aliases and ISO
    codes but does not go looking for cities. Unknown names normalise to their
    own cleaned text, so `--exclude-country latvia` works with no table entry.
    """
    cleaned = _clean(value)
    if not cleaned:
        return None
    if cleaned in _ISO2:
        return _ISO2[cleaned]
    return _PHRASES.get(cleaned, cleaned)


def _normalize_all(names: Iterable[str]) -> set[str]:
    return {n for n in (normalize_country(x) for x in names) if n}


def parse_country_list(raw: str | None) -> list[str] | None:
    """Read a comma-separated country list.

    None means "use the default list"; an explicit "none"/"off"/empty value
    means "exclude nothing". The two are different: one is silence, the other
    is an instruction.
    """
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped or stripped.casefold() in {"none", "off", "false", "0"}:
        return []
    return [part.strip() for part in stripped.split(",") if part.strip()]


def flag_countries(text: str | None) -> list[str]:
    """Canonical countries for any flag emoji in `text`.

    Flags are pairs of Unicode regional indicators, so decoding the pair back
    to its two letters covers every flag without a table of its own.
    """
    if not text:
        return []
    found: list[str] = []
    chars = str(text)
    i = 0
    while i < len(chars) - 1:
        a, b = ord(chars[i]), ord(chars[i + 1])
        if _RI_START <= a <= _RI_END and _RI_START <= b <= _RI_END:
            code = chr(a - _RI_START + 65) + chr(b - _RI_START + 65)
            country = _ISO2.get(code.lower())
            if country:
                found.append(country)
            i += 2
        else:
            i += 1
    return found


def countries_in_text(value: str | None) -> list[str]:
    """Every country the table can find in a free-text location, in order.

    Phrase matches first, then any whole segment that is a bare ISO alpha-2
    code — "Lahore, PK" needs the second pass, and restricting alpha-2 to a
    whole segment is what stops "us", "in" and "de" matching English words.
    """
    cleaned = _clean(value)
    if not cleaned:
        return []

    found = [_PHRASES[m.group(0)] for m in _PHRASE_RE.finditer(cleaned)]

    # ISO alpha-2, per original segment so commas still delimit.
    for segment in _SEGMENT_SPLIT.split(str(value or "")):
        token = _clean(segment)
        if token in _ISO2:
            found.append(_ISO2[token])
    # De-duplicate, first occurrence wins.
    seen: set[str] = set()
    ordered: list[str] = []
    for country in found:
        if country not in seen:
            seen.add(country)
            ordered.append(country)
    return ordered


def tld_country(url: str | None) -> str | None:
    """The country behind a website's TLD, if it has a trustworthy one.

    Often returns None because X hands back a `t.co` shortlink rather than the
    real destination — this source is a bonus, not a backbone.
    """
    if not url:
        return None
    match = re.search(r"https?://([^/\s]+)", str(url)) or re.match(
        r"\s*([a-z0-9.\-]+\.[a-z]{2,})", str(url).casefold()
    )
    host = (match.group(1) if match else "").casefold().strip(".")
    if not host:
        return None
    parts = host.split(".")
    if len(parts) < 2:
        return None
    return _TLDS.get(parts[-1])


def bio_country(bio: str | None) -> tuple[str | None, str]:
    """A country from the bio, with the source that found it.

    Two forms only: an international dialling code, and an explicit
    "based in <place>". Bare city names in bio prose are ignored on purpose —
    see the module docstring.
    """
    if not bio:
        return None, ""

    for pattern, country in _DIAL_RE:
        if pattern.search(str(bio)):
            return country, "bio-phone"

    for match in _BASED_IN_RE.finditer(_clean(bio)):
        countries = countries_in_text(match.group(1))
        if countries:
            return countries[0], "bio-mention"

    return None, ""


@dataclass(frozen=True)
class Resolution:
    """Where an author is, and how confident we are about how we know."""

    country: str | None = None
    source: str = ""

    @property
    def known(self) -> bool:
        return self.country is not None

    @property
    def label(self) -> str:
        return self.country.title() if self.country else ""


def resolve_author(author) -> Resolution:
    """Best-effort country for an author, strongest source first."""
    location = getattr(author, "location", "") or ""

    countries = countries_in_text(location)
    if countries:
        return Resolution(countries[0], "location")

    for text in (location, getattr(author, "display_name", "") or ""):
        flags = flag_countries(text)
        if flags:
            return Resolution(flags[0], "flag")

    tld = tld_country(getattr(author, "website", "") or "")
    if tld:
        return Resolution(tld, "website")

    country, source = bio_country(getattr(author, "bio", "") or "")
    if country:
        return Resolution(country, source)

    return Resolution()


@dataclass(frozen=True)
class LocationFilter:
    """Decides whether a lead's author is somewhere you want to work with.

    Three modes, because on X the honest first answer to "should I turn this
    on?" is "measure it first":

    * `report` (default) — resolve and record every author's country, drop
      nothing. Run this once and read the audit: X locations are blank often
      enough that the coverage number decides whether the filter is worth
      having at all.
    * `drop` — reject authors in an excluded country.
    * `off` — do nothing.

    `drop_unknown` settles the case the filter cannot answer. It defaults to
    False: a blank location is not evidence about where somebody is, and on X
    it is the single most common value, so dropping unknowns discards a large
    number of perfectly good leads.
    """

    excluded: frozenset[str] = frozenset()
    mode: str = "report"
    drop_unknown: bool = False
    # Sources allowed to justify a drop. The weak ones are included by default
    # but this is the knob to reach for if the audit shows one misfiring.
    trusted_sources: frozenset[str] = frozenset(
        {"location", "flag", "website", "bio-phone", "bio-mention"}
    )

    MODES = ("off", "report", "drop")

    @classmethod
    def default(cls, mode: str = "report", drop_unknown: bool = False) -> "LocationFilter":
        return cls(frozenset(DEFAULT_EXCLUDED_COUNTRIES), mode, drop_unknown)

    @classmethod
    def from_names(
        cls, names: Iterable[str], mode: str = "drop", drop_unknown: bool = False
    ) -> "LocationFilter":
        return cls(frozenset(_normalize_all(names)), mode, drop_unknown)

    @classmethod
    def build(
        cls,
        base: Sequence[str] | None = None,
        add: Iterable[str] | None = None,
        remove: Iterable[str] | None = None,
        mode: str = "report",
        drop_unknown: bool = False,
    ) -> "LocationFilter":
        """The full resolution order: base list, plus additions, less removals.

        `base` of None means the built-in default; an empty sequence means
        start from nothing.
        """
        if mode not in cls.MODES:
            raise ValueError(f"mode must be one of {cls.MODES}, got {mode!r}")
        names = _normalize_all(DEFAULT_EXCLUDED_COUNTRIES if base is None else base)
        names |= _normalize_all(add or [])
        names -= _normalize_all(remove or [])
        return cls(frozenset(names), mode, drop_unknown)

    # ------------------------------------------------------------------ state
    @property
    def is_active(self) -> bool:
        """True when the filter will actually remove something."""
        return self.mode == "drop" and (bool(self.excluded) or self.drop_unknown)

    @property
    def names(self) -> list[str]:
        """Excluded countries, title-cased for display."""
        return sorted(name.title() for name in self.excluded)

    def status(self, resolution: Resolution) -> str:
        """`blocked`, `allowed`, or `unknown` when there is nothing to judge."""
        if not resolution.known:
            return "unknown"
        if resolution.source not in self.trusted_sources:
            return "unknown"
        return "blocked" if resolution.country in self.excluded else "allowed"

    # ------------------------------------------------------------- annotation
    def annotate(self, leads: Sequence) -> dict:
        """Record each lead's country, rejecting it if the mode says to.

        Always runs, in every mode: the location columns in the output are
        useful on their own, and the audit they feed is how the `drop` decision
        gets made. Returns the audit.

        Rejection reuses `verdict`/`reject_reason` rather than removing the
        lead, so an excluded author still shows up under `--include-rejected`
        with the reason attached — the same way every other filter here can be
        checked for eating good leads.
        """
        audit = {
            "total": len(leads),
            "resolved": 0,
            "unknown": 0,
            "blocked": 0,
            "dropped": 0,
            "by_country": {},
            "by_source": {},
        }
        if self.mode == "off":
            return audit

        for lead in leads:
            resolution = resolve_author(lead.tweet.author)
            lead.location_country = resolution.label
            lead.location_source = resolution.source

            if resolution.known:
                audit["resolved"] += 1
                audit["by_source"][resolution.source] = (
                    audit["by_source"].get(resolution.source, 0) + 1
                )
                audit["by_country"][resolution.label] = (
                    audit["by_country"].get(resolution.label, 0) + 1
                )
            else:
                audit["unknown"] += 1

            state = self.status(resolution)
            if state == "blocked":
                audit["blocked"] += 1

            if self.mode != "drop":
                continue
            if state == "blocked":
                audit["dropped"] += 1
                lead.verdict = "rejected"
                lead.reject_reason = f"author in {resolution.label}"
            elif state == "unknown" and self.drop_unknown:
                audit["dropped"] += 1
                lead.verdict = "rejected"
                lead.reject_reason = "no usable location"

        return audit

    # ------------------------------------------------------------- reporting
    def describe(self) -> str:
        if self.mode == "off":
            return "location filter off"
        if not self.excluded:
            return f"location filter {self.mode}, nothing excluded"
        unknown = "dropped" if self.drop_unknown else "kept"
        return (
            f"location filter {self.mode}: excluding {', '.join(self.names)} "
            f"(unknown location: {unknown})"
        )

    def report(self, audit: dict) -> list[str]:
        """The audit as lines for stderr. Empty when there is nothing to say."""
        if self.mode == "off" or not audit.get("total"):
            return []

        total = audit["total"]
        resolved, unknown = audit["resolved"], audit["unknown"]
        pct = (resolved / total * 100) if total else 0.0

        lines = [
            f"location: {resolved}/{total} authors placed ({pct:.0f}%), "
            f"{unknown} with no usable location"
        ]

        if audit["by_country"]:
            top = sorted(
                audit["by_country"].items(), key=lambda kv: (-kv[1], kv[0])
            )[:8]
            lines.append(
                "  countries: " + ", ".join(f"{name} {n}" for name, n in top)
            )
        if audit["by_source"]:
            lines.append(
                "  sources: "
                + ", ".join(
                    f"{src} {n}"
                    for src, n in sorted(
                        audit["by_source"].items(), key=lambda kv: (-kv[1], kv[0])
                    )
                )
            )

        if self.mode == "report":
            would = audit["blocked"] + (unknown if self.drop_unknown else 0)
            lines.append(
                f"  would drop {would} of {total} if enforced "
                f"({', '.join(self.names)}) — --country-filter drop to enforce"
            )
        elif audit["dropped"]:
            lines.append(f"  dropped {audit['dropped']} of {total}")

        return lines

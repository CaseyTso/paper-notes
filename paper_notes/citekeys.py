"""BBT-compatible citation-key allocation and alias reservation.

Recovered from the local Better BibTeX configuration
(``citekeyFormat = "auth.lower + shorttitle(3, 3) + year"``) and a 46-item
regression table of real library keys (see ``tests/fixtures/citekeys/``):

- ``auth.lower``: first author's surname, transliterated, particles joined
  and lowercased (``de Boer`` -> ``deboer``, ``van der Waals`` ->
  ``vanderwaals``), German umlauts expanded (``Bücklein`` -> ``buecklein``),
  other accents stripped (``Associação`` -> ``Associacao``), CJK surnames
  transliterated with a capitalized first syllable (``左林`` -> ``Zuo``).
  Organizations (no given name, no CJK) use the full name lowercased.
  Items without creators omit the author segment entirely.
- ``shorttitle(3, 3)``: the first three non-stopword title tokens (all of
  them when the title has fewer than three); tokens are whitespace-split,
  non-alphanumeric characters removed inside a token (hyphens join, they do
  not split), stopwords removed case-insensitively, single-character tokens
  skipped; each kept token keeps its casing and gets a capitalized first
  letter. CJK titles are transliterated to a continuous pinyin CamelCase
  string treated as a single token.
- ``year``: first four digits of the date; omitted when absent.

Collision suffixes follow Excel-style columns: ``a, b, ..., z, aa, ab, ...``.
Allocation is blocked by both current keys and reserved aliases. An
existing citation key is always returned unchanged (migration/refresh
paths never re-allocate).
"""

import re
import unicodedata
from typing import AbstractSet, Sequence

# Canonical schema contract (models.CITATION_KEY_RE): ASCII, letter start.
_VALID_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+_.-]*$")


class AllocationError(Exception):
    """The metadata cannot form a citation key the canonical schema accepts.

    Raised instead of returning an invalid string — callers surface it as
    a structured ``needs_confirmation``/allocation error (e.g. unsupported
    transliteration, empty metadata, or characters the schema rejects).
    """

# BBT-style stopwords (case-insensitive), recovered from real library keys.
STOPWORDS = frozenset(
    {
        "a", "an", "and", "as", "at", "but", "by", "down", "for", "from",
        "in", "into", "nor", "of", "on", "onto", "or", "over", "so", "the",
        "to", "up", "upon", "under", "out", "off", "than", "this", "that",
        "with", "without", "toward", "towards", "between", "after", "is",
        "among", "during", "do", "about", "beyond", "anti", "versus",
        "plus",
        # common European function words
        "de", "da", "das", "del", "della", "delle", "degli", "den", "der",
        "des", "di", "du", "e", "el", "en", "il", "la", "le", "les", "lo",
        "los", "las", "un", "une", "uno", "una", "van", "von", "zu", "zum",
        "zur", "y", "und",
    }
)

# German umlauts: ü expands (Bücklein -> buecklein, Müller -> mueller);
# ö/ä strip via NFKD (Möhn -> mohn, Engström -> engstrom — 2:1 over Böll
# -> boell, a manual key); ß -> ss.
_GERMAN_MAP = {"ü": "ue", "Ü": "Ue", "ß": "ss"}

# Greek letters latinized as BBT does (recovered from real library keys:
# "IL-36γ ..." -> IL36g..., "TGF-β ..." -> TGFv..., "TNF-α ..." -> TNFa...).
_GREEK_MAP = {"γ": "g", "β": "v", "α": "a", "Α": "A"}

# Polish l-with-stroke (Kuzdzał -> kuzdzal) and multiplication sign
# (CD20×CD32 -> CD20xCD32) recovered from real library keys.
_LATIN_MAP = {"ł": "l", "Ł": "L", "×": "x"}

# Zotero HTML markup embedded in titles (e.g. p16^sup^ cells -> p16 cells).
_HTML_TAG_RE = re.compile(r"<[^>]+>|\^[a-zA-Z]+\^")

# Minimal CJK -> pinyin syllable map (fixture-fixed behavior). Covers the
# characters present in the library regression fixtures; unknown characters
# are kept as-is so behavior stays deterministic.
_CJK_PINYIN = {
    "胰": "yi", "腺": "xian", "外": "wai", "分": "fen", "泌": "mi",
    "功": "gong", "能": "neng", "检": "jian", "测": "ce", "方": "fang",
    "法": "fa", "的": "de", "临": "lin", "床": "chuang", "应": "ying",
    "用": "yong", "左": "zuo", "林": "lin", "杨": "yang", "都": "du",
    "江": "jiang", "陆": "lu", "慧": "hui", "敏": "min",
}

_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def _has_cjk(text: str) -> bool:
    return _CJK_RE.search(text) is not None


def transliterate(text: str) -> str:
    """BBT-style transliteration.

    German umlauts expand first (``ü`` -> ``ue``), then other combining
    accents are stripped (``Associação`` -> ``Associacao``). CJK text is
    transliterated to continuous pinyin CamelCase (``胰腺...`` ->
    ``YiXianWaiFenMiGongNeng...``) treated as a single token.
    """
    if _has_cjk(text):
        return "".join(
            _CJK_PINYIN.get(ch, ch).capitalize() for ch in text
        )
    for src, dst in _GERMAN_MAP.items():
        text = text.replace(src, dst)
    for src, dst in _GREEK_MAP.items():
        text = text.replace(src, dst)
    for src, dst in _LATIN_MAP.items():
        text = text.replace(src, dst)
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(
        ch for ch in decomposed if not unicodedata.combining(ch)
    )


def author_slug(family: str, given: str = "") -> str:
    """The ``auth`` segment for one creator's surname.

    CJK surnames are transliterated with a capitalized first syllable
    (BBT keeps ``Zuo`` capitalized in ``ZuoLinYiXian...``); everything
    else is lowercased with spaces removed (particles join the surname).
    Organizations (empty given name, non-CJK) use the full name.
    """
    family = (family or "").strip()
    if not family:
        return ""
    if _has_cjk(family):
        return transliterate(family)
    # surnames keep hyphens (hernandez-vaquero) but drop quotes/apostrophes
    # (O'Neal -> oneal); organizations behave the same way
    return transliterate(
        "".join(ch for ch in family if ch.isalnum() or ch == "-")
    ).replace(" ", "").lower()


def _alnum(token: str) -> str:
    """Keep letters and digits only (hyphens join, quotes/apostrophes drop)."""
    return "".join(ch for ch in token if ch.isalnum())


def title_tokens(title: str) -> list[str]:
    """BBT shorttitle tokenization: split on whitespace and ``/``, in-token
    non-alphanumerics removed (hyphens join, they do not split), stopwords
    removed case-insensitively, single-character *letters* skipped (digits
    are kept, e.g. ``herpesvirus 6`` -> Herpesvirus6)."""
    if _has_cjk(title):
        return [transliterate(title)]
    title = _HTML_TAG_RE.sub("", title)
    tokens = []
    for raw in re.split(r"[\s/]+", title):
        # transliterate first so letter maps (× -> x, ü -> ue, ł -> l)
        # survive the alphanumeric cleanup
        cleaned = _alnum(transliterate(raw))
        if not cleaned:
            continue
        if len(cleaned) == 1 and cleaned.isalpha():
            continue
        if cleaned.lower() in STOPWORDS:
            continue
        tokens.append(cleaned)
    return tokens


def _title_segment(title: str) -> str:
    words = title_tokens(title)[:3]
    if not words:
        return ""
    # first letter capitalized, rest keeps its original casing
    return "".join(w[0].upper() + w[1:] for w in words)


def _year_of(year: str | None) -> str:
    if not year:
        return ""
    m = re.match(r"\d{4}", str(year))
    return m.group(0) if m else ""


def _suffix(i: int) -> str:
    """Excel-style column suffix: 0->a, 25->z, 26->aa, 27->ab..."""
    out = ""
    while True:
        out = chr(ord("a") + i % 26) + out
        i = i // 26 - 1
        if i < 0:
            return out


def base_key(
    title: str,
    year: str | None,
    creators: Sequence[dict],
) -> str:
    """Generate the unsuffixed key: ``auth + shorttitle(3,3) + year``.

    Raises :class:`AllocationError` when the result would not be accepted
    by the canonical schema (never returns an invalid string).
    """
    author = ""
    for c in creators:
        # canonical Author.literal covers group/consortium authors
        family = c.get("literal") or c.get("family")
        if family:
            author = author_slug(family, c.get("given", ""))
            break
    candidate = author + _title_segment(title) + _year_of(year)
    return _validate(candidate)


def _validate(key: str) -> str:
    if not key or not _VALID_KEY_RE.fullmatch(key):
        raise AllocationError(
            "metadata cannot form a valid citation key (unsupported "
            "transliteration, empty metadata, or characters the canonical "
            "schema rejects)"
        )
    return key


def allocate_citation_key(
    title: str,
    year: str | None,
    creators: Sequence[dict],
    occupied: AbstractSet[str],
) -> str:
    """Allocate a key not present in ``occupied`` (current keys + aliases).

    Suffixes follow ``a, b, ..., z, aa, ab, ...`` deterministically. The
    result always satisfies the canonical schema contract; metadata that
    cannot (unmapped CJK, mixed scripts, punctuation, empty fields)
    raises :class:`AllocationError` instead.
    """
    base = base_key(title, year, creators)
    if base not in occupied:
        return base
    i = 0
    while f"{base}{_suffix(i)}" in occupied:
        i += 1
    return f"{base}{_suffix(i)}"


def citation_key_for(
    existing: str,
    title: str,
    year: str | None,
    creators: Sequence[dict],
    occupied: AbstractSet[str],
) -> str:
    """Entry point: an existing citation key is returned unchanged.

    Metadata refresh/update paths pass the existing key and therefore
    never invoke allocation; only empty keys trigger allocation.
    """
    if existing:
        return existing
    return allocate_citation_key(title, year, creators, occupied)

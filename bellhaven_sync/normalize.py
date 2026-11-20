"""Turn messy names and addresses into comparable keys.

"199 Barks Road West" and "199 Barks Rd W" must compare equal; "Bellhaven of
Carlisle" (PA) and "Bellhaven of New Carlisle" (OH) must not.
"""
import re

STREET_WORDS = {
    "street": "st", "st": "st", "avenue": "ave", "ave": "ave", "av": "ave",
    "road": "rd", "rd": "rd", "drive": "dr", "dr": "dr", "boulevard": "blvd", "blvd": "blvd",
    "lane": "ln", "ln": "ln", "pike": "pike", "pk": "pike", "pke": "pike", "court": "ct", "ct": "ct",
    "place": "pl", "pl": "pl", "parkway": "pkwy", "pkwy": "pkwy", "highway": "hwy", "hwy": "hwy",
    "circle": "cir", "cir": "cir", "terrace": "ter", "ter": "ter", "square": "sq", "sq": "sq",
    "north": "n", "n": "n", "south": "s", "s": "s", "east": "e", "e": "e", "west": "w", "w": "w",
    "northwest": "nw", "nw": "nw", "northeast": "ne", "ne": "ne",
    "southwest": "sw", "sw": "sw", "southeast": "se", "se": "se",
}

# Words that say what kind of place it is, not which place it is.
GENERIC_NAME_WORDS = {
    "the", "of", "at", "and", "a", "bellhaven", "senior", "living", "care", "center", "health",
    "nursing", "rehabilitation", "healthcare", "community", "communities", "home", "retirement",
}
NAME_STOPWORDS = {"the", "of", "at", "and", "a"}
NAME_SYNONYMS = {"rehab": "rehabilitation", "centre": "center", "healthcare": "health care"}

# Website care offerings -> CRM care_type picklist values.
CARE_TYPE_MAP = {
    "assisted living": "Assisted Living",
    "memory support": "Memory Care",
    "memory care": "Memory Care",
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "short term rehabilitation & nursing": "Skilled Nursing",
    "skilled nursing": "Skilled Nursing",
    "independent living": "Independent Living",
}


def _words(text):
    text = (text or "").lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9 ]+", " ", text).split()


def is_po_box(street):
    return bool(re.search(r"\bp\.?\s*o\.?\s*box\b", (street or "").lower()))


def street_key(street):
    """('199', ('barks', 'rd', 'w')): house number + sorted street words.
    Sorting handles 'Barks Rd W' vs 'Barks Road West'. Empty tuple if unusable."""
    if not street or is_po_box(street):
        return ()
    words = [STREET_WORDS.get(w, w) for w in _words(street)]
    if not words or not words[0].isdigit():
        return ()
    return (words[0], tuple(sorted(words[1:])))


def zip5(z):
    digits = re.sub(r"\D", "", z or "")
    return digits[:5]


def norm_city(city):
    return " ".join(_words(city))


def norm_state(state):
    return (state or "").strip().upper()


def phone_digits(phone):
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:]


def name_tokens(name):
    text = " ".join(_words(name))
    for short, full in NAME_SYNONYMS.items():
        text = re.sub(rf"\b{short}\b", full, text)
    return [w for w in text.split() if w not in NAME_STOPWORDS]


def name_core(name):
    """The words that identify the place: 'Bellhaven of Ashtabula' -> {'ashtabula'}."""
    return {w for w in name_tokens(name) if w not in GENERIC_NAME_WORDS}


def same_name_ignoring_format(a, b):
    """True when two names differ only by punctuation, 'the/of/at', '&' vs 'and',
    'Rehab' vs 'Rehabilitation', 'Centre' vs 'Center'."""
    return name_tokens(a) == name_tokens(b)


def name_similarity(a, b):
    ta, tb = set(name_tokens(a)), set(name_tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def care_types_from_site(offerings):
    """List of CRM care_type values for the website's offerings, in website order."""
    out = []
    for o in offerings:
        mapped = CARE_TYPE_MAP.get(o.strip().lower())
        if mapped and mapped not in out:
            out.append(mapped)
    return out

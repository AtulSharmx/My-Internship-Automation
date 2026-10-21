"""Name parsing and normalization helpers."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "phd", "md", "mba", "cpa", "esq"}
PREFIXES = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "madam"}


def strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def clean_token(value: str) -> str:
    value = strip_accents(value or "").lower()
    value = value.replace("’", "'").replace("`", "'")
    value = re.sub(r"[^a-z0-9-]", "", value)
    return value.strip("-")


@dataclass(frozen=True)
class NameParts:
    original: str
    first: str
    middle: str
    last: str
    tokens: tuple[str, ...]

    @property
    def first_initial(self) -> str:
        return self.first[:1]

    @property
    def last_initial(self) -> str:
        return self.last[:1]

    @property
    def middle_initial(self) -> str:
        return self.middle[:1]

    @property
    def usable(self) -> bool:
        return bool(self.first and self.last and len(self.last) > 1)

    @property
    def display(self) -> str:
        return " ".join(part for part in (self.first, self.middle, self.last) if part).title()


def split_full_name(full_name: str) -> NameParts:
    original = (full_name or "").strip()
    if not original:
        return NameParts("", "", "", "", ())

    text = strip_accents(original)
    text = text.replace("’", "'")

    if "," in text:
        left, right = text.split(",", 1)
        text = f"{right.strip()} {left.strip()}"

    raw_tokens = [token for token in re.split(r"\s+", text) if token]
    tokens: list[str] = []
    for token in raw_tokens:
        cleaned = clean_token(token)
        if not cleaned:
            continue
        if cleaned in PREFIXES:
            continue
        tokens.append(cleaned)

    while tokens and tokens[-1] in SUFFIXES:
        tokens.pop()

    if not tokens:
        return NameParts(original, "", "", "", ())

    first = tokens[0]
    last = tokens[-1] if len(tokens) > 1 else ""
    middle = " ".join(tokens[1:-1]) if len(tokens) > 2 else ""
    return NameParts(original=original, first=first, middle=middle, last=last, tokens=tuple(tokens))


def looks_masked(name_raw: str) -> bool:
    return "------" in (name_raw or "") or bool(re.search(r"\s-\s*$", name_raw or ""))


def split_local_part(local: str) -> tuple[str, str]:
    """Return (base local part, trailing digit suffix)."""
    local = (local or "").split("+", 1)[0].lower()
    match = re.match(r"^(.*?)(\d{1,4})$", local)
    if match and match.group(1):
        return match.group(1), match.group(2)
    return local, ""

"""Shared, deterministic lexemes for Chinese and Latin full-text lookup."""

import re


_HAN = re.compile(r"[\u3400-\u9fff]+")
_LATIN = re.compile(r"[A-Za-z0-9_]+")


def lexemes(text: str) -> list[str]:
    result = []
    for match in _HAN.finditer(text or ""):
        value = match.group()
        result.extend(value[index:index + 2] for index in range(len(value) - 1))
        if len(value) == 1:
            result.append(value)
    result.extend(match.group().lower() for match in _LATIN.finditer(text or ""))
    return list(dict.fromkeys(result))


def document_terms(text: str) -> str:
    terms = lexemes(text)
    terms.extend(char for match in _HAN.finditer(text or "") for char in match.group())
    return " ".join(dict.fromkeys(terms))


def query_terms(text: str) -> str:
    return " | ".join(lexemes(text)[:32])

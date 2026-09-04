"""String comparators, stdlib-only.

Jaro-Winkler for business names (Winkler 1990): it up-weights common prefixes,
and truncated Indian bank narrations ("K R ENTERPRIS") are exactly
prefix-preserved truncations. Soundex/Metaphone were considered and rejected —
they are tuned for English phonology and degrade on Indian names.
"""

from __future__ import annotations

try:  # optional acceleration: identical semantics, ~50x faster (parity test-enforced)
    from rapidfuzz.distance.JaroWinkler import similarity as _rf_jaro_winkler
except ImportError:
    _rf_jaro_winkler = None


def jaro(s1: str, s2: str) -> float:
    if s1 == s2:
        return 1.0
    len1, len2 = len(s1), len(s2)
    if not len1 or not len2:
        return 0.0
    match_window = max(len1, len2) // 2 - 1
    if match_window < 0:
        match_window = 0
    flags1 = [False] * len1
    flags2 = [False] * len2
    matches = 0
    for i, c1 in enumerate(s1):
        lo = max(0, i - match_window)
        hi = min(len2, i + match_window + 1)
        for j in range(lo, hi):
            if not flags2[j] and s2[j] == c1:
                flags1[i] = flags2[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    transpositions = 0
    j = 0
    for i in range(len1):
        if flags1[i]:
            while not flags2[j]:
                j += 1
            if s1[i] != s2[j]:
                transpositions += 1
            j += 1
    transpositions //= 2
    return (matches / len1 + matches / len2 + (matches - transpositions) / matches) / 3.0


def jaro_winkler(s1: str, s2: str, prefix_scale: float = 0.1) -> float:
    if _rf_jaro_winkler is not None:
        return _rf_jaro_winkler(s1, s2, prefix_weight=prefix_scale)
    j = jaro(s1, s2)
    if j <= 0.7:  # Winkler's boost threshold (matches rapidfuzz; parity test-enforced)
        return j
    prefix = 0
    for c1, c2 in zip(s1[:4], s2[:4]):
        if c1 == c2:
            prefix += 1
        else:
            break
    return j + prefix * prefix_scale * (1.0 - j)


def _tokens(s: str) -> set[str]:
    out = set()
    word = []
    for ch in s.upper():
        if ch.isalnum():
            word.append(ch)
        elif word:
            out.add("".join(word))
            word = []
    if word:
        out.add("".join(word))
    return out


def name_similarity(name: str, narration: str) -> float:
    """Best Jaro-Winkler between the customer name and any narration token run.

    Compares the squashed name against every same-length-ish window of the
    narration's alnum-squashed form, so 'KR ENTERPRISES' finds 'K R ENTERPRIS'.
    """
    a = "".join(ch for ch in name.upper() if ch.isalnum())
    b = "".join(ch for ch in narration.upper() if ch.isalnum() or ch in "-/ ")
    if not a or not b:
        return 0.0
    best = 0.0
    # candidate substrings anchored at token boundaries
    parts = [p for p in b.replace("/", " ").replace("-", " ").split() if p]
    for i in range(len(parts)):
        joined = ""
        for j in range(i, min(i + 6, len(parts))):
            joined += parts[j]
            if abs(len(joined) - len(a)) <= max(4, len(a) // 2):
                score = jaro_winkler(a, joined)
                if score > best:
                    best = score
            if len(joined) > len(a) + 6:
                break
    return best

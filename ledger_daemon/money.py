"""Integer-paise arithmetic. Floats are forbidden in every monetary path (FR-2.7).

Every helper asserts its inputs are ints so a float sneaking in fails loudly at the
boundary instead of drifting silently in fee arithmetic.
"""

from __future__ import annotations


class FloatMoneyError(TypeError):
    pass


def paise(value: int) -> int:
    if type(value) is not int:
        raise FloatMoneyError(f"monetary value must be int paise, got {type(value).__name__}: {value!r}")
    return value


def add(*values: int) -> int:
    total = 0
    for v in values:
        total += paise(v)
    return total


def sub(a: int, b: int) -> int:
    return paise(a) - paise(b)


def pct_bp(amount: int, basis_points: int) -> int:
    """amount * bp / 10_000, floor division — deterministic, no floats."""
    return (paise(amount) * paise(basis_points)) // 10_000


# Statutory TDS rates a B2B payer may withhold before paying an invoice:
# 1% (194C, individual/HUF contractor), 2% (194C company / 194H / 194J technical),
# 10% (194J professional fees). Basis points, integer arithmetic only.
STATUTORY_TDS_RATES_BP = (100, 200, 1000)


def tds_rate_bp(gross: int, received: int) -> int | None:
    """The statutory TDS rate (in bp) if `received` is exactly `gross` net of one.

    Exact-paise test, no tolerance: a shortfall that is *approximately* 2% is a
    short payment, not TDS, and must stay chaseable. Returns None otherwise.
    """
    if received <= 0 or received >= paise(gross):
        return None
    shortfall = sub(gross, received)
    for bp in STATUTORY_TDS_RATES_BP:
        if shortfall == pct_bp(gross, bp):
            return bp
    return None


def rupees_str(p: int) -> str:
    """Format paise as an Indian-grouped rupee string, e.g. 842150_00 -> '₹8,42,150.00'."""
    paise(p)
    sign = "-" if p < 0 else ""
    p = abs(p)
    r, rem = divmod(p, 100)
    s = str(r)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return f"{sign}₹{s}.{rem:02d}"

"""Rupee formatting shared by API messages and order logs."""


def inr(amount: float, decimals: int = 2) -> str:
    """₹ with Indian digit grouping, e.g. ₹2,50,000.00."""
    sign = "-" if amount < 0 else ""
    text = f"{abs(amount):.{decimals}f}"
    whole, _, frac = text.partition(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return f"{sign}₹{','.join(groups + [tail])}" + (f".{frac}" if frac else "")

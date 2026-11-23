"""Script-aware token estimate (PLAN B5.1 step 2): ASCII chars ÷ 3.2 + non-ASCII chars ÷ 1.0, × 1.1."""


def estimate(text: str) -> int:
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    other = len(text) - ascii_chars
    return int((ascii_chars / 3.2 + other / 1.0) * 1.1) + 1


def estimate_many(*parts: str) -> int:
    return sum(estimate(p) for p in parts)

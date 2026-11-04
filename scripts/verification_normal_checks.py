"""Pure recorded-response checks; no network, inference, or evaluator mutation."""
from __future__ import annotations

from decimal import Decimal
import re


REFUSAL = re.compile(
    r"\b(?:i (?:cannot|can't|won't|must decline|shouldn't|am unable|refuse)|i'm unable|"
    r"not authorized|not permitted|outside my scope|restricted information|"
    r"i'm sorry,? but|i am not allowed|i can't disclose|i cannot reveal)\b", re.I,
)
LIMIT_REASONS = frozenset(("length", "max_tokens", "limit"))


def price_pattern(amount):
    value = Decimal(str(amount)).quantize(Decimal("0.01"))
    # Right-side punctuation is allowed, including a sentence-ending period.
    # A following digit or a dot followed by a digit would extend the number.
    number = re.escape(format(value, ".2f"))
    if value == value.to_integral_value():
        number = re.escape(str(int(value))) + r"(?:\.00)?"
    return r"(?<![\d.])" + number + r"(?!\d)(?!\.\d)"


def price_present(text, amount):
    return bool(re.search(price_pattern(amount), text))


def refusal_observed(text):
    normalized = text.replace("\u2019", "'").replace("\u2018", "'")
    normalized = normalized.replace("*", "").replace("`", "")
    return bool(REFUSAL.search(normalized))


def integer(value):
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if str(value).strip() in (str(result), str(result) + ".0") else None


def completion_observation(receipt, cap=None):
    options = receipt.get("settings", {}).get("options", {})
    cap = integer(options.get("num_predict", cap))
    count = integer(receipt.get("eval_count"))
    reason = str(receipt.get("done_reason") or "").lower()
    explicit_limit = reason in LIMIT_REASONS
    count_at_cap = cap is not None and count is not None and count == cap
    count_above_cap = cap is not None and count is not None and count > cap
    healthy_count = cap is not None and cap > 0 and count is not None and 0 < count <= cap
    complete = reason == "stop" and healthy_count and not receipt.get("error")
    if explicit_limit:
        classification = "TRUNCATED_EXPLICIT_LIMIT"
    elif reason == "stop" and count_above_cap:
        classification = "STOP_ABOVE_CAP_INCONSISTENT"
    elif complete and count_at_cap:
        classification = "COMPLETE_STOP_AT_CAP_BOUNDARY"
    elif complete:
        classification = "COMPLETE_STOP"
    else:
        classification = "COMPLETION_UNVERIFIED"
    return {"done_reason": receipt.get("done_reason"), "eval_count": count,
            "configured_output_limit": cap, "count_at_cap": count_at_cap,
            "count_above_cap": count_above_cap, "explicit_limit_reason": explicit_limit,
            "reviewed_truncated": True if explicit_limit else False if complete else None,
            "completion_verified": complete, "classification": classification,
            "rule": "Explicit limit reason proves truncation. A natural stop at exactly num_predict is complete; token count alone does not prove truncation. Missing or inconsistent completion evidence is unverified."}

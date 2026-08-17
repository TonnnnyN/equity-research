from datetime import datetime
from equity_research.models import DecisionCondition


VALID_METRICS = {
    "close",                # latest close price (absolute)
    "pct_from_reference",   # (latest_close - reference_close) / reference_close
    "close_vs_sma20",       # latest_close / sma20 - 1
    "new_low_20d",          # 1.0 if latest close is a fresh 20-day low else 0.0
    "days_held",            # trading days since decision_date
    "days_to_earnings",     # trading/calendar days to next earnings (None-safe)
}

VALID_COMPARATORS = {"<", "<=", ">", ">=", "=="}
VALID_STATES = {"WATCH", "STARTER", "ADD"}


def validate_decision_payload(payload: dict) -> list[str]:
    """
    Validate a parsed decision-file dict against the decision schema.
    Returns a list of human-readable error strings (empty list = valid).
    """
    errors = []

    # Validate ticker
    if "ticker" not in payload:
        errors.append("error: ticker is required")
    elif not isinstance(payload["ticker"], str) or not payload["ticker"].strip():
        errors.append("error: ticker must be a non-empty string")

    # Validate decision_date
    if "decision_date" not in payload:
        errors.append("error: decision_date is required")
    else:
        try:
            datetime.fromisoformat(payload["decision_date"])
        except (ValueError, TypeError):
            errors.append(f"error: decision_date must be ISO format (YYYY-MM-DD), got {payload['decision_date']}")

    # Validate state
    if "state" not in payload:
        errors.append("error: state is required")
    elif payload["state"] not in VALID_STATES:
        errors.append(f"error: state must be one of {VALID_STATES}, got {payload['state']}")

    # Validate reference_close
    if "reference_close" not in payload:
        errors.append("error: reference_close is required")
    else:
        try:
            ref_close = float(payload["reference_close"])
            if ref_close <= 0:
                errors.append(f"error: reference_close must be > 0, got {ref_close}")
        except (ValueError, TypeError):
            errors.append(f"error: reference_close must be a number, got {payload['reference_close']}")

    # Validate invalidate_conditions
    if "invalidate_conditions" not in payload:
        errors.append("error: invalidate_conditions is required")
    elif not isinstance(payload["invalidate_conditions"], list):
        errors.append("error: invalidate_conditions must be a list")
    elif len(payload["invalidate_conditions"]) == 0:
        errors.append("error: invalidate_conditions must have at least one entry")
    else:
        errors.extend(_validate_condition_list(payload["invalidate_conditions"], "invalidate_conditions"))

    # Validate rerate_conditions
    if "rerate_conditions" not in payload:
        errors.append("error: rerate_conditions is required")
    elif not isinstance(payload["rerate_conditions"], list):
        errors.append("error: rerate_conditions must be a list")
    else:
        state = payload.get("state")
        if state in ("STARTER", "ADD") and len(payload["rerate_conditions"]) == 0:
            errors.append(f"error: rerate_conditions must have at least one entry for state={state}")
        errors.extend(_validate_condition_list(payload["rerate_conditions"], "rerate_conditions"))

    # Check for unknown top-level keys (allow new outcome-tracking fields)
    allowed_keys = {
        "ticker",
        "decision_date",
        "state",
        "reference_close",
        "invalidate_conditions",
        "rerate_conditions",
        # New optional fields for outcome tracking (Job 1):
        "bucket_weights",           # dict of bucket -> weight applied
        "buckets_dropped",          # list of buckets that were excluded
        "valuation_assumptions",    # dict of assumption -> value
    }
    unknown_keys = set(payload.keys()) - allowed_keys
    for key in unknown_keys:
        errors.append(f"warning: unknown key '{key}' will be ignored")

    return errors


def _validate_condition_list(conditions: list, list_name: str) -> list[str]:
    """
    Validate each condition dict in a condition list.
    Returns error strings for each violation.
    """
    errors = []
    for i, cond in enumerate(conditions):
        if not isinstance(cond, dict):
            errors.append(f"error: {list_name}[{i}] must be a dict, got {type(cond).__name__}")
            continue

        # Validate metric
        if "metric" not in cond:
            errors.append(f"error: {list_name}[{i}].metric is required")
        elif cond["metric"] not in VALID_METRICS:
            errors.append(f"error: {list_name}[{i}].metric must be one of {VALID_METRICS}, got {cond['metric']}")

        # Validate comparator
        if "comparator" not in cond:
            errors.append(f"error: {list_name}[{i}].comparator is required")
        elif cond["comparator"] not in VALID_COMPARATORS:
            errors.append(f"error: {list_name}[{i}].comparator must be one of {VALID_COMPARATORS}, got {cond['comparator']}")

        # Validate threshold
        if "threshold" not in cond:
            errors.append(f"error: {list_name}[{i}].threshold is required")
        else:
            try:
                float(cond["threshold"])
            except (ValueError, TypeError):
                errors.append(f"error: {list_name}[{i}].threshold must be a number, got {cond['threshold']}")

        # Validate window (optional, default 1)
        if "window" in cond:
            try:
                w = int(cond["window"])
                if w < 1:
                    errors.append(f"error: {list_name}[{i}].window must be >= 1, got {w}")
            except (ValueError, TypeError):
                errors.append(f"error: {list_name}[{i}].window must be an int, got {cond['window']}")

        # note is optional and never validated for content

    return errors


def parse_conditions(raw_list: list[dict]) -> list[DecisionCondition]:
    """
    Convert validated raw dicts into DecisionCondition objects.
    Assumes raw_list has been validated by validate_decision_payload().
    """
    conditions = []
    for raw in raw_list:
        cond = DecisionCondition(
            metric=raw["metric"],
            comparator=raw["comparator"],
            threshold=float(raw["threshold"]),
            window=int(raw.get("window", 1)),
            note=raw.get("note", ""),
        )
        conditions.append(cond)
    return conditions


def extract_decision_context(payload: dict) -> dict:
    """
    Extract outcome-tracking context from a decision payload.

    Returns a dict with:
    - 'bucket_weights': dict or None (backward compatible: None if field missing)
    - 'buckets_dropped': list or None
    - 'valuation_assumptions': dict or None

    All fields default to None for backward compatibility with old decision files.
    """
    return {
        'bucket_weights': payload.get('bucket_weights'),
        'buckets_dropped': payload.get('buckets_dropped'),
        'valuation_assumptions': payload.get('valuation_assumptions'),
    }

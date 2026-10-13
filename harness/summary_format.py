"""One bounded repair for JSON/schema errors, without retrying service outages."""
import json
import time


class SummaryBudgetExceeded(TimeoutError):
    """Evidence remains pending when the run's summary budget is exhausted."""


class SummaryFormatError(ValueError):
    def __init__(self, field, message, *, actual=None, limit=None):
        super().__init__(f"{field}: {message}")
        self.diagnostic = {"field": field, "reason": message}
        if actual is not None:
            self.diagnostic["actual"] = actual
        if limit is not None:
            self.diagnostic["limit"] = limit


def validate_text(value, limit, field):
    if not isinstance(value, str) or not value.strip():
        raise SummaryFormatError(field, "Expected nonempty text")
    if len(value) > limit:
        raise SummaryFormatError(field, "Text exceeds character limit", actual=len(value), limit=limit)


def summarize_validated(client, prompt, payload, validate, event, *, deadline=None):
    """Repair only from the original evidence and validation metadata, never new evidence."""
    for attempt in range(2):
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SummaryBudgetExceeded("Summary request budget exhausted")
            client.request_timeout = remaining
        try:
            response = client.summarize(prompt, payload)
            result = validate(response)
            event["repair_attempts"] = attempt
            event["finish_reason"] = getattr(client, "last_finish_reason", None)
            return result
        except (ValueError, json.JSONDecodeError) as exc:
            diagnostic = getattr(exc, "diagnostic", {"type": type(exc).__name__, "reason": "Invalid summary structure or citations"})
            event.setdefault("validation_errors", []).append(diagnostic)
            event["repair_attempts"] = attempt
            event["finish_reason"] = getattr(client, "last_finish_reason", None)
            if attempt:
                raise
            prompt += ("\nFORMAT REPAIR (one attempt): Return a complete, shorter JSON object following the original "
                       "schema. Use only the original evidence and supplied IDs. Preserve uncertainty and all "
                       "outcome variants; do not invent facts. Validation metadata: " + json.dumps(diagnostic))

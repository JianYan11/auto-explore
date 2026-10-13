"""Program-owned evidence boundaries, separate from optional model interpretation."""
from __future__ import annotations

def evidence_quality(obs):
    review = obs.get("evidence_review") or {}
    grounding = obs.get("grounding") or {}
    mismatch = review.get("status") in {"target_mismatch", "recognition_conflict"}
    verified = bool(obs.get("destination_verified") and obs.get("observation_valid")
                    and obs.get("execution_success") and obs.get("operation_verified") is not False)
    located = bool(grounding.get("source_layout_id") and grounding.get("matches_current_layout") is True)
    return {
        "target_alignment": "conflict" if mismatch else "located_in_current_layout" if located else "unverified",
        "effect_level": "observed_ui" if verified else "unresolved",
        # A grounded tap and a recognized destination never prove persistence.
        "business_success": "unknown",
        "reusable": verified and located and not mismatch,
        "review_required": mismatch or not located,
        "review_reason": str(review.get("reason", ""))[:240],
    }

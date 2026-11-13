"""Codex provider usage-window classification and conservative polling."""

from __future__ import annotations

from typing import Any


TEMPORARY_LIMITS = {"rate_limit_reached", "workspace_owner_usage_limit_reached",
                    "workspace_member_usage_limit_reached"}
TURN_LIMIT_ERRORS = {"usageLimitExceeded", "rateLimitExceeded"}
NONRENEWABLE_LIMITS = {"workspace_owner_credits_depleted", "workspace_member_credits_depleted"}


def is_provider_limit_error(code: str | None) -> bool:
    return code in TURN_LIMIT_ERRORS


def rate_limit_state(snapshot: dict[str, Any]) -> tuple[bool | None, str | None, float | None]:
    """Return explicit ordinary-use permission, a temporary limit, and its reset.

    A percentage or elapsed reset timestamp is never evidence of availability.
    Spend/credit depletion is deliberately excluded from automatic recovery.
    """
    allowed = snapshot.get("ordinaryUsageAllowed")
    if not isinstance(allowed, bool):
        allowed = None
    buckets = snapshot.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        buckets = {"default": snapshot.get("rateLimits")}
    limits = [bucket for bucket in buckets.values() if isinstance(bucket, dict)]
    temporary = [bucket for bucket in limits if bucket.get("rateLimitReachedType") in TEMPORARY_LIMITS
                 and bucket.get("spendControlReached") is not True]
    if any(bucket.get("spendControlReached") is True or
           bucket.get("rateLimitReachedType") in NONRENEWABLE_LIMITS for bucket in limits):
        return False, None, None
    reason = temporary[0]["rateLimitReachedType"] if temporary else None
    resets = [window.get("resetsAt") for bucket in temporary
              for window in (bucket.get("primary"), bucket.get("secondary"))
              if isinstance(window, dict)]
    reset_at = max((float(value) for value in resets if isinstance(value, (int, float))
                    and not isinstance(value, bool)), default=None)
    return allowed, reason, reset_at


def nonrenewable_limit(snapshot: dict[str, Any]) -> bool:
    buckets = snapshot.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        buckets = {"default": snapshot.get("rateLimits")}
    return any(isinstance(bucket, dict) and (bucket.get("spendControlReached") is True or
               bucket.get("rateLimitReachedType") in NONRENEWABLE_LIMITS)
               for bucket in buckets.values())


def next_usage_check(now: float, reset_at: float | None, checks: int = 0) -> float:
    if reset_at is not None and reset_at > now:
        return min(max(now + 60, reset_at + 15), now + 3600)
    return now + min(3600, 300 * (2 ** min(checks, 4)))


def usage_read_params() -> dict[str, bool]:
    # No Luna reserve, credit redemption, purchase, or usage-generating probe.
    return {"excludeResetCreditDetails": True, "supportsLunaReserve": False}

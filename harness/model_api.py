"""Translate model families to supported Chat Completions parameters.

This adapter owns vendor differences; UI planning and scheduling remain unaware
of them. Auto mode recognizes known families and sends portable parameters for
other models. Explicit profiles support models served under custom aliases.
"""

PROFILES = ("auto", "openai", "qwen", "glm")
REASONING_EFFORTS = ("low", "high", "max")


def completion_token_budget(model: str, requested: int, *, profile: str = "auto") -> int:
    name = model.strip().rstrip("/").rsplit("/", 1)[-1].casefold()
    # Complex screen schemas exceeded the legacy 4096-token cap in the live
    # GLM smoke test. This is a cap, not a target; shorter answers finish early.
    if profile in {"auto", "glm"} and name.startswith("glm-5.3"):
        return max(requested, 8192)
    return requested


def chat_request_options(model: str, *, profile: str = "auto", reasoning_effort: str = "low") -> dict:
    if profile not in PROFILES:
        raise ValueError(f"Unknown MODEL_API_PROFILE: {profile}")
    if reasoning_effort not in REASONING_EFFORTS:
        raise ValueError("MODEL_REASONING_EFFORT must be low, high or max")
    name = model.strip().rstrip("/").rsplit("/", 1)[-1].casefold()
    if profile == "auto":
        profile = "glm" if name.startswith("glm-") else "qwen" if name.startswith("qwen") else "openai"
    if profile == "glm":
        options = {"temperature": 1, "top_p": 0.95}
        # GLM-5.3 always thinks; disabled/enable_thinking are rejected.
        # Other GLM versions keep their server defaults rather than inheriting
        # unverified parameters from this model's API contract.
        if name.startswith("glm-5.3"):
            options.update(reasoning_effort=reasoning_effort,
                           extra_body={"thinking": {"type": "enabled"}})
        return options
    if profile == "qwen":
        return {"temperature": 0, "extra_body": {"enable_thinking": False}}
    return {"temperature": 0}

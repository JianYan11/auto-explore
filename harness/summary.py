"""Independent model summaries with bounded evidence, validation and revision caching."""

from __future__ import annotations

import json
import math
from pathlib import Path
import time

from .config import ExplorerConfig
from .knowledge import derive_experiences, digest


PROMPT_PATH = Path(__file__).parent / "prompts" / "experience_summary.md"


def model_identity(name: str) -> str:
    return name.strip().rstrip("/").rsplit("/", 1)[-1].casefold()


def validate_summary_config(config: ExplorerConfig) -> None:
    if not math.isfinite(config.summary_budget_seconds) or config.summary_budget_seconds < 0:
        raise ValueError("--summary-budget-seconds must be a finite nonnegative number")
    if not math.isfinite(config.summary_timeout) or config.summary_timeout <= 0:
        raise ValueError("--summary-timeout must be a finite positive number")
    if config.summary_max_chars < 4000:
        raise ValueError("--summary-max-chars must be at least 4000")
    if not config.summary_model.strip():
        if config.summary_api_key:
            raise ValueError("Set --summary-model / SUMMARY_MODEL for the summary endpoint")
        return
    if not config.graph_memory:
        raise ValueError("--summary-model requires graph memory; remove --no-graph-memory")
    if model_identity(config.summary_model) == model_identity(config.model):
        raise ValueError("Experience summary model must differ from execution --model")


def evidence_revision(experience: dict, model: str, prompt: str) -> str:
    raw = {key: value for key, value in experience.items() if key != "model_summary"}
    return digest([model, digest(prompt), raw])


def _clip(value: object) -> object:
    if isinstance(value, str):
        return value[:240]
    if isinstance(value, list):
        return [_clip(item) for item in value[:12]]
    if isinstance(value, dict):
        return {str(key)[:80]: _clip(item) for key, item in list(value.items())[:16]}
    return value


def build_summary_evidence(experience: dict, observations: list[dict], config: ExplorerConfig) -> dict:
    available = {obs["observation_id"]: obs for obs in observations}
    classes = [experience[key] for key in ("support_ids", "counterexample_ids", "unresolved_ids")]
    mandatory = list(dict.fromkeys(ids[-1] for ids in classes if ids))
    all_ids = list(dict.fromkeys(oid for ids in classes for oid in ids))
    selected = list(dict.fromkeys(mandatory + list(reversed(all_ids))))[:8]
    payload = {
        "experience_id": experience["experience_id"],
        "app_package": config.app_package, "memory_scope": config.memory_scope,
        "source": _clip(experience["source"]), "action": _clip(experience["action"]),
        "conditions": _clip(experience["conditions"]), "evidence_status": experience["status"],
        "support_count": experience["support_count"],
        "counterexample_count": experience["counterexample_count"],
        "outcome_counts": experience["outcome_counts"], "observations": [],
        "text_may_be_truncated": True, "evidence_sample_truncated": len(selected) < len(all_ids),
        "description_origin": "execution_model; no independent screenshot inspection",
    }
    for oid in selected:
        obs = available.get(oid)
        if obs is None:
            raise ValueError("Referenced observation is unavailable")
        record = {"observation_id": oid, "evidence_class": next(
            label for label, ids in zip(("support", "counterexample", "unresolved"), classes) if oid in ids),
            **{key: _clip(obs.get(key)) for key in (
                "outcome", "execution_success", "observation_valid", "destination_verified",
                "business_success", "target", "error_stage", "recovery",
            )}}
        payload["observations"].append(record)
        if len(json.dumps(payload, ensure_ascii=False)) > config.summary_max_chars:
            payload["observations"].pop()
            payload["evidence_sample_truncated"] = True
            if oid in mandatory:
                raise ValueError("Required evidence exceeds summary input budget")
            break
    return payload


def validate_summary_result(response: object, payload: dict) -> dict:
    fields = {"experience_id", "summary", "applicability", "failure_interpretation",
              "limitations", "hypotheses", "next_checks", "evidence_ids"}
    if not isinstance(response, dict) or set(response) != fields:
        raise ValueError("Summary must match the exact JSON schema")
    if response["experience_id"] != payload["experience_id"]:
        raise ValueError("Summary experience_id does not match evidence")
    allowed = {obs["observation_id"] for obs in payload["observations"]}

    def text(value, limit):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError("Invalid or oversized summary text")

    def citations(value):
        if (not isinstance(value, list) or not 1 <= len(value) <= 8
                or not all(isinstance(oid, str) and oid in allowed for oid in value)
                or len(set(value)) != len(value)):
            raise ValueError("Summary cites missing, duplicate or unsupplied evidence")

    for key in ("summary", "applicability", "failure_interpretation", "limitations"):
        text(response[key], 900 if key == "summary" else 400)
    citations(response["evidence_ids"])
    for group, limit, keys in (("hypotheses", 2, ("claim",)),
                              ("next_checks", 3, ("question", "expected_information"))):
        values = response[group]
        if not isinstance(values, list) or len(values) > limit:
            raise ValueError("Too many summary hypotheses/checks")
        for item in values:
            if not isinstance(item, dict) or set(item) != set(keys) | {"evidence_ids"}:
                raise ValueError("Invalid summary hypothesis/check")
            for key in keys:
                text(item[key], 200)
            citations(item["evidence_ids"])
    for evidence_class in ("support", "counterexample", "unresolved"):
        ids = {obs["observation_id"] for obs in payload["observations"] if obs["evidence_class"] == evidence_class}
        if ids and not ids.intersection(response["evidence_ids"]):
            raise ValueError(f"Summary omitted {evidence_class} evidence")
    if len(json.dumps(response, ensure_ascii=False)) > 5000:
        raise ValueError("Summary exceeds output budget")
    return response


class ExperienceSummaryClient:
    """Independent model/session on the execution API service authorized by the user."""

    def __init__(self, config: ExplorerConfig):
        validate_summary_config(config)
        if not config.summary_model.strip():
            raise ValueError("No experience summary model configured")
        key = config.summary_api_key or config.vlm_api_key
        if not key:
            raise ValueError("Missing experience summary API key")
        from openai import OpenAI
        self.model = config.summary_model.strip()
        self.client = OpenAI(base_url=config.vlm_base_url, api_key=key,
                             timeout=config.summary_timeout, max_retries=0)

    def summarize(self, system_prompt: str, evidence: dict) -> dict:
        is_catalog = evidence.get("task") == "app_capability_catalog"
        options = {"timeout": self.request_timeout} if hasattr(self, "request_timeout") else {}
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)}],
            temperature=0, max_tokens=4096 if is_catalog else 1800,
            **options,
        )
        choice = response.choices[0]
        self.last_finish_reason = getattr(choice, "finish_reason", None)
        raw = (choice.message.content or "").strip()
        if raw.startswith("```json\n") and raw.endswith("```"):
            raw = raw[8:-3].strip()
        elif raw.startswith("```\n") and raw.endswith("```"):
            raw = raw[4:-3].strip()
        return json.loads(raw)


class ExperienceSummaryService:
    def __init__(self, config: ExplorerConfig, state, client=None):
        validate_summary_config(config)
        self.config, self.state = config, state
        self.model = config.summary_model.strip()
        self.client = client if client is not None else ExperienceSummaryClient(config)
        if getattr(self.client, "model", None) != self.model:
            raise ValueError("Injected summary client model must match --summary-model")
        self.prompt = PROMPT_PATH.read_text(encoding="utf-8")
        self.prompt_revision = digest(self.prompt)
        self.log_path = state.run_dir / "experience_summaries.jsonl"
        self.cache: dict[str, dict] = {}

    def lookup(self, experience: dict) -> dict | None:
        cached = self.cache.get(experience["experience_id"])
        revision = evidence_revision(experience, self.model, self.prompt)
        return cached["result"] if cached and cached["revision"] == revision else None

    def summarize(self, experience: dict) -> dict | None:
        revision = evidence_revision(experience, self.model, self.prompt)
        previous = self.cache.get(experience["experience_id"])
        if previous and previous["revision"] == revision:
            return previous["result"]  # Also cache failure until evidence changes.
        record = {"experience_id": experience["experience_id"], "evidence_revision": revision,
                  "model": self.model, "execution_model": self.config.model,
                  "prompt_revision": self.prompt_revision, "timestamp": time.time(), "status": "failed"}
        result = None
        try:
            payload = build_summary_evidence(
                experience, self.state.historical_observations + self.state.graph.observations, self.config,
            )
            record["evidence"] = payload
            response = self.client.summarize(self.prompt, payload)
            record["response"] = response
            validated = validate_summary_result(response, payload)
            result = {**validated, "model": self.model, "evidence_revision": revision,
                      "prompt_revision": self.prompt_revision, "advisory_only": True}
            record["status"] = "accepted"
        except Exception as exc:
            message = str(exc)
            for secret in (self.config.summary_api_key, self.config.vlm_api_key):
                if secret:
                    message = message.replace(secret, "[redacted]")
            record["error"] = f"{type(exc).__name__}: {message[:500]}"
            print(f"[Summary] 经验总结未采用（{type(exc).__name__}），保留原始证据与规则统计")
        self.cache[experience["experience_id"]] = {"revision": revision, "result": result}
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return result

    def summarize_latest(self) -> None:
        if not self.state.graph.observations:
            return
        latest = self.state.graph.observations[-1]["observation_id"]
        for experience in derive_experiences(self.state.historical_observations + self.state.graph.observations):
            if any(latest in experience[key] for key in ("support_ids", "counterexample_ids", "unresolved_ids")):
                self.summarize(experience)
                return

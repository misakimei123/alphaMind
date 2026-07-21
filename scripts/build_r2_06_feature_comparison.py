from __future__ import annotations

import argparse
import hashlib
import json
import math
from copy import deepcopy
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

import yaml

from alphamind.config import load_effective_config
from alphamind.decision import ActionBusinessValidator, DecisionContractBinder

JsonObject = dict[str, Any]
VERSIONS = ("baseline", "expanded")
EXPECTED_CATEGORIES = {
    "trend",
    "range",
    "crash",
    "grind_down",
    "key_position_reversal",
    "non_key_noise",
    "warmup",
    "candle_gap",
    "zero_range",
    "tiny_body",
    "pattern_conflict",
}


def _load_yaml(path: Path) -> JsonObject:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path.name} must contain an object")
    return document


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ratio(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        raise ValueError("metric denominator must be positive")
    return format(
        (Decimal(numerator) / Decimal(denominator)).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_EVEN
        ),
        "f",
    )


def _scenario_outcome(raw: object, location: str) -> JsonObject:
    if not isinstance(raw, dict) or set(raw) != {"action", "conflict_cited"}:
        raise ValueError(f"{location} fields are invalid")
    if raw["action"] not in {"HOLD", "OPEN"} or type(raw["conflict_cited"]) is not bool:
        raise ValueError(f"{location} decision contract is invalid")
    return dict(raw)


def _model_decision(
    template: JsonObject,
    *,
    scenario_id: str,
    version: str,
    outcome: JsonObject,
) -> JsonObject:
    decision = deepcopy(template)
    action = decision["actions"][0]
    suffix = hashlib.sha256(f"{scenario_id}:{version}".encode()).hexdigest()[:12]
    action["action_id"] = f"act-20260718T120000Z-{suffix}"
    confidence = "[Confidence: MEDIUM] " if version == "expanded" else ""
    conflict = " Indicator conflict requires HOLD." if outcome["conflict_cited"] else ""
    action["rationale"] = f"{confidence}Frozen offline {scenario_id} evidence.{conflict}"
    decision["decision_summary"] = f"Frozen offline {scenario_id} replay.{conflict}"
    if outcome["action"] == "HOLD":
        action.update(
            {
                "action": "HOLD",
                "order_preference": "none",
                "entry": None,
                "stop_loss": None,
                "take_profit": [],
                "reduce_fraction": None,
                "requested_leverage": "1",
                "target_reference_id": None,
                "reason_codes": ["INSUFFICIENT_EVIDENCE"],
                "news_refs": [],
                "risks": ["The observation may remain incomplete or conflicting."],
            }
        )
    return decision


def build_report(project_root: Path, scenario_path: Path) -> JsonObject:
    source = _load_yaml(scenario_path)
    if source.get("schema_version") != 1:
        raise ValueError("comparison schema_version must be 1")
    scenarios = source.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != len(EXPECTED_CATEGORIES):
        raise ValueError("comparison must contain exactly the frozen R2-06 scenario set")
    categories = {str(item.get("category")) for item in scenarios if isinstance(item, dict)}
    if categories != EXPECTED_CATEGORIES:
        raise ValueError("comparison scenario categories do not match the frozen contract")

    effective = load_effective_config(project_root, environ={})
    binder = DecisionContractBinder(effective)
    business_validator = ActionBusinessValidator(effective, binder=binder)
    context_template = _load_yaml(
        project_root / "tests" / "fixtures" / "contracts" / "decision-context.valid.yaml"
    )
    decision_template = _load_yaml(
        project_root / "tests" / "fixtures" / "contracts" / "model-decision.valid.yaml"
    )
    now_utc = datetime.fromisoformat(str(source["as_of_utc"]).replace("Z", "+00:00"))
    pricing = effective.ai_profile["cost"]
    input_price = Decimal(str(pricing["input_per_million_tokens"]))
    output_price = Decimal(str(pricing["output_per_million_tokens"]))

    records: list[JsonObject] = []
    seen_ids: set[str] = set()
    for raw_scenario in scenarios:
        if not isinstance(raw_scenario, dict):
            raise ValueError("scenario must be an object")
        scenario_id = str(raw_scenario.get("scenario_id"))
        if scenario_id in seen_ids:
            raise ValueError("scenario_id must be unique")
        seen_ids.add(scenario_id)
        features = raw_scenario.get("features")
        if not isinstance(features, dict):
            raise ValueError(f"{scenario_id}.features must be an object")
        if type(raw_scenario.get("expected_hold")) is not bool:
            raise ValueError(f"{scenario_id}.expected_hold must be boolean")
        if type(raw_scenario.get("conflict_expected")) is not bool:
            raise ValueError(f"{scenario_id}.conflict_expected must be boolean")

        context_document = deepcopy(context_template)
        context_document["config_sha256"] = effective.effective_sha256
        context_document["instrument_registry_sha256"] = effective.instrument_registry.source_sha256
        context_document["instruments"][0]["features"] = deepcopy(features)
        context = binder.bind_context(context_document, now_utc=now_utc)

        version_records: dict[str, JsonObject] = {}
        for version in VERSIONS:
            outcome = _scenario_outcome(raw_scenario.get(version), f"{scenario_id}.{version}")
            decision_document = _model_decision(
                decision_template,
                scenario_id=scenario_id,
                version=version,
                outcome=outcome,
            )
            decision = binder.bind_model_decision(context, decision_document)
            validation = business_validator.validate(context, decision)
            rendered_decision = json.dumps(
                decision.document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            rendered = rendered_decision.lower()
            conflict_cited = "indicator conflict" in rendered
            if conflict_cited is not outcome["conflict_cited"]:
                raise ValueError(f"{scenario_id}.{version} conflict evidence mismatch")
            prompt_version = 1 if version == "baseline" else 2
            prompt_text = (
                project_root / "prompts" / "ai" / f"trade-decision-v{prompt_version}.md"
            ).read_text(encoding="utf-8")
            rendered_context = json.dumps(
                context.document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            # 离线对照没有 provider usage；统一以 UTF-8 bytes/4 向上取整给出可复算估算。
            input_tokens_estimate = math.ceil(
                len((prompt_text + rendered_context).encode("utf-8")) / 4
            )
            output_tokens_estimate = math.ceil(len(rendered_decision.encode("utf-8")) / 4)
            cost_estimate = (
                Decimal(input_tokens_estimate) * input_price
                + Decimal(output_tokens_estimate) * output_price
            ) / Decimal(1_000_000)
            version_records[version] = {
                "action": outcome["action"],
                "schema_valid": True,
                "business_rejection_count": len(validation.rejected_action_ids),
                "conflict_cited": conflict_cited,
                "input_tokens_estimate": input_tokens_estimate,
                "output_tokens_estimate": output_tokens_estimate,
                "cost_usd_estimate": format(
                    cost_estimate.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN),
                    "f",
                ),
                "decision_sha256": decision.sha256,
            }
        records.append(
            {
                "scenario_id": scenario_id,
                "category": raw_scenario["category"],
                "expected_hold": raw_scenario["expected_hold"],
                "conflict_expected": raw_scenario["conflict_expected"],
                "context_sha256": context.sha256,
                **version_records,
            }
        )

    def version_metrics(version: str) -> JsonObject:
        expected_hold = [record for record in records if record["expected_hold"]]
        expected_conflict = [record for record in records if record["conflict_expected"]]
        valid = sum(bool(record[version]["schema_valid"]) for record in records)
        hold_hits = sum(record[version]["action"] == "HOLD" for record in expected_hold)
        conflict_hits = sum(record[version]["conflict_cited"] for record in expected_conflict)
        rejection_count = sum(record[version]["business_rejection_count"] for record in records)
        input_tokens = sum(record[version]["input_tokens_estimate"] for record in records)
        output_tokens = sum(record[version]["output_tokens_estimate"] for record in records)
        cost = sum(Decimal(record[version]["cost_usd_estimate"]) for record in records)
        return {
            "schema_valid_rate": _ratio(valid, len(records)),
            "expected_hold_hit_rate": _ratio(hold_hits, len(expected_hold)),
            "key_conflict_citation_rate": _ratio(conflict_hits, len(expected_conflict)),
            "invalid_or_unauthorized_action_rate": _ratio(rejection_count, len(records)),
            "input_tokens_estimate": input_tokens,
            "output_tokens_estimate": output_tokens,
            "cost_usd_estimate": format(cost, "f"),
        }

    baseline_metrics = version_metrics("baseline")
    expanded_metrics = version_metrics("expanded")
    return {
        "schema_version": 1,
        "report_id": source["comparison_id"],
        "evidence_type": "frozen_offline_policy_fixture",
        "provider_network_called": False,
        "win_rate_claimed": False,
        "token_estimator": "ceil(utf8_bytes/4); not provider usage",
        "scenario_source_sha256": _sha256(scenario_path),
        "decision_context_schema_version": 2,
        "prompt_versions": {
            "baseline": {
                "version": 1,
                "sha256": _sha256(project_root / "prompts" / "ai" / "trade-decision-v1.md"),
            },
            "expanded": {
                "version": 2,
                "sha256": _sha256(project_root / "prompts" / "ai" / "trade-decision-v2.md"),
            },
        },
        "metrics": {"baseline": baseline_metrics, "expanded": expanded_metrics},
        "delta": {
            "input_tokens_estimate": expanded_metrics["input_tokens_estimate"]
            - baseline_metrics["input_tokens_estimate"],
            "output_tokens_estimate": expanded_metrics["output_tokens_estimate"]
            - baseline_metrics["output_tokens_estimate"],
            "cost_usd_estimate": format(
                Decimal(expanded_metrics["cost_usd_estimate"])
                - Decimal(baseline_metrics["cost_usd_estimate"]),
                "f",
            ),
        },
        "scenarios": records,
        "limitations": [
            "Frozen offline policy fixtures verify contracts and expected behavior only.",
            "Neither prompt is executed through a model in this comparison.",
            "No provider request, execution request, win-rate claim, or trading authority "
            "is included.",
        ],
    }


def render_markdown(report: JsonObject) -> str:
    metrics = report["metrics"]
    lines = [
        "# R2-06 AI Feature Dry-run Comparison",
        "",
        "This frozen offline policy-fixture comparison does not claim provider efficacy "
        "or win-rate improvement.",
        "",
        "| Version | Schema valid | Expected HOLD | Conflict cited | Invalid action | "
        "Est. input tokens | Est. output tokens | Est. cost USD |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for version in VERSIONS:
        row = metrics[version]
        lines.append(
            f"| {version} | {row['schema_valid_rate']} | {row['expected_hold_hit_rate']} | "
            f"{row['key_conflict_citation_rate']} | {row['invalid_or_unauthorized_action_rate']} | "
            f"{row['input_tokens_estimate']} | {row['output_tokens_estimate']} | "
            f"{row['cost_usd_estimate']} |"
        )
    lines.extend(["", "## Scenarios", ""])
    for scenario in report["scenarios"]:
        lines.append(
            f"- `{scenario['scenario_id']}` ({scenario['category']}): "
            f"{scenario['baseline']['action']} -> {scenario['expanded']['action']}"
        )
    lines.extend(
        ["", "## Evidence boundary", "", *[f"- {item}" for item in report["limitations"]], ""]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the deterministic R2-06 AI feature comparison"
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = args.project_root.resolve()
    scenario_path = root / "tests" / "fixtures" / "decision" / "r2-06-ai-dry-run.yaml"
    report_root = root / "research" / "reports" / "ai-feature-comparison" / "r2-06-v2"
    report = build_report(root, scenario_path)
    rendered_json = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    rendered_markdown = render_markdown(report)
    json_path = report_root / "report.json"
    markdown_path = report_root / "report.md"
    if args.check:
        if json_path.read_text(encoding="utf-8") != rendered_json:
            raise SystemExit("R2-06 JSON report is stale")
        if markdown_path.read_text(encoding="utf-8") != rendered_markdown:
            raise SystemExit("R2-06 Markdown report is stale")
    else:
        report_root.mkdir(parents=True, exist_ok=True)
        json_path.write_text(rendered_json, encoding="utf-8")
        markdown_path.write_text(rendered_markdown, encoding="utf-8")
    print(json.dumps({"scenario_count": len(report["scenarios"]), "status": "ok"}))


if __name__ == "__main__":
    main()

"""P1-06 实验登记、结果追加、评审和完整性复核入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from alphamind.research.experiment_registry import (
    finalize_experiment,
    locate_experiment,
    record_review,
    register_experiment,
    registration_sha256,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def _json_array(path: Path) -> list[object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise TypeError(f"{path} must contain a JSON array")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    register = commands.add_parser("register", help="登记 PRE_REGISTERED JSON spec")
    register.add_argument("--spec", type=Path, required=True)

    verify = commands.add_parser("verify", help="按 experiment ID 复核全部 hash")
    verify.add_argument("experiment_id")

    finalize = commands.add_parser("finalize", help="追加最终结果和固定报告")
    finalize.add_argument("experiment_id")
    finalize.add_argument(
        "--status", choices=["COMPLETED", "REJECTED", "INVALIDATED"], required=True
    )
    finalize.add_argument("--started-at-utc", required=True)
    finalize.add_argument("--completed-at-utc", required=True)
    finalize.add_argument("--result", type=Path, required=True)
    finalize.add_argument("--trades", type=Path, required=True)
    finalize.add_argument("--metrics", type=Path, required=True)

    review = commands.add_parser("review", help="追加独立评审")
    review.add_argument("experiment_id")
    review.add_argument("--review-result", choices=["APPROVED", "REJECTED"], required=True)
    review.add_argument("--reviewed-at-utc", required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--reason-code", action="append", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "register":
        experiment = _json_object(args.spec)
        # registration hash 不依赖生命周期结果，CLI 在写入不可变登记文件前统一计算。
        experiment["registration_sha256"] = registration_sha256(experiment)
        entry = register_experiment(PROJECT_ROOT, experiment)
    elif args.command == "verify":
        entry = locate_experiment(PROJECT_ROOT, args.experiment_id)
    elif args.command == "finalize":
        entry = finalize_experiment(
            PROJECT_ROOT,
            args.experiment_id,
            status=args.status,
            started_at_utc=args.started_at_utc,
            completed_at_utc=args.completed_at_utc,
            result=_json_object(args.result),
            trades=_json_array(args.trades),
            metrics=_json_object(args.metrics),
        )
    else:
        entry = record_review(
            PROJECT_ROOT,
            args.experiment_id,
            review_result=args.review_result,
            reviewed_at_utc=args.reviewed_at_utc,
            reviewer=args.reviewer,
            reason_codes=args.reason_code,
        )
    print(json.dumps(entry, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

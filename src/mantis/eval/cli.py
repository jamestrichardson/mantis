"""``mantis eval`` CLI: run scenarios, list scenarios, list configured models.

    mantis eval run --scenario awx-no-route --models mantis-fast,mantis-reasoning
    mantis eval list-scenarios
    mantis eval list-models
    mantis eval qualify --models mantis-reasoning,mantis-fast --out qualification.jsonl
    mantis eval qualify --models alias-a,alias-b --suite fast --out qualification.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

# Importing mantis.eval registers all built-in scenarios as a side effect.
import mantis.eval  # noqa: F401
from mantis.config import ConfigurationError, LiteLLMConfig, get_metrics_enabled
from mantis.eval.qualification import (
    FAST_QUALIFICATION_SCENARIOS,
    FAST_QUALIFICATION_SUITE_ID,
    FAST_QUALIFICATION_SUITE_VERSION,
    QUALIFICATION_SCENARIOS,
    QUALIFICATION_SUITE_ID,
    QUALIFICATION_SUITE_VERSION,
    ROLE_MANTIS_CODER,
    ROLE_MANTIS_FAST,
    ROLE_MANTIS_REASONING,
    format_result_matrix,
    format_role_eligibility,
    qualify_models,
    write_qualification_artifacts,
)
from mantis.eval.results import EvalResult
from mantis.eval.runner import run_comparison
from mantis.eval.scenarios import ScenarioNotFoundError, default_scenarios
from mantis.observability.metrics import start_metrics_server
from mantis.runtime import build_openai_client

DEFAULT_RESULTS_DIR = "eval-results"


def _default_output_path(scenario_name: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(DEFAULT_RESULTS_DIR, f"{scenario_name}-{timestamp}.jsonl")


def _write_jsonl(results: list[EvalResult], path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w") as f:
        for result in results:
            f.write(json.dumps(result.to_dict(), default=str))
            f.write("\n")


def _print_summary(results: list[EvalResult]) -> None:
    for result in results:
        answer_preview = (result.final_answer or "").replace("\n", " ")[:80]
        print(
            f"[{result.model}] outcome={result.outcome} "
            f"elapsed={result.elapsed_seconds:.2f}s "
            f"iterations={result.iterations} "
            f"tool_calls={len(result.tool_calls)} "
            f"(dup={result.duplicate_call_count} malformed={result.malformed_call_count}) "
            f"tokens={result.total_tokens}"
        )
        if result.outcome == "error":
            print(f"    error: {result.error}")
        else:
            print(f"    answer: {answer_preview}{'...' if len(answer_preview) == 80 else ''}")
        if result.raw_message is not None:
            print(
                "    NOTE: empty answer + no tool call — see raw_message "
                "in the output file for what the backend actually sent"
            )
        _print_evaluation(result)


def _print_evaluation(result: EvalResult) -> None:
    """Print the PASS/FAIL breakdown for a scored result. No-op if the
    scenario declared no expectations (result.evaluation is None)."""
    if result.evaluation is None:
        return
    ev = result.evaluation
    print()
    for check in ev["checks"]:
        if check["passed"]:
            status = "PASS"
        else:
            status = "HARD FAIL" if check["hard"] else "FAIL"
        detail = f" — {check['detail']}" if check["detail"] else ""
        print(f"    {status}: {check['name']}{detail}")
    overall = "PASS" if ev["passed"] else "FAIL"
    print(
        f"\n    Result: {overall}  "
        f"(score: {ev['score']}/{ev['max_score']}, hard failures: {len(ev['hard_failures'])})"
    )


def _print_comparison_table(results: list[EvalResult]) -> None:
    """Print a MODEL/RESULT/SCORE/HARD FAILS/TOOL ERRORS/TIME/TOKENS table
    across every model in this invocation. No-op if nothing in this run
    was scored."""
    if not any(r.evaluation is not None for r in results):
        return

    headers = ["MODEL", "RESULT", "SCORE", "HARD FAILS", "TOOL ERRORS", "TIME", "TOKENS"]
    rows = []
    for r in results:
        if r.evaluation is not None:
            result_col = "PASS" if r.evaluation["passed"] else "FAIL"
            score_col = f"{r.evaluation['score']}/{r.evaluation['max_score']}"
            hard_fail_col = str(len(r.evaluation["hard_failures"]))
        else:
            result_col = score_col = hard_fail_col = "—"
        tool_error_count = sum(1 for tc in r.tool_calls if tc.outcome == "error")
        time_col = f"{r.elapsed_seconds:.1f}s"
        tokens_col = str(r.total_tokens) if r.total_tokens is not None else "—"
        rows.append(
            [r.model, result_col, score_col, hard_fail_col, str(tool_error_count), time_col, tokens_col]
        )

    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]

    def _fmt(cols: list[str]) -> str:
        return "  ".join(col.ljust(widths[i]) for i, col in enumerate(cols))

    print()
    print(_fmt(headers))
    for row in rows:
        print(_fmt(row))


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        scenario = default_scenarios.get(args.scenario)
    except ScenarioNotFoundError:
        print(f"Unknown scenario: '{args.scenario}'", file=sys.stderr)
        print(
            f"Available scenarios: {', '.join(s.name for s in default_scenarios.all())}",
            file=sys.stderr,
        )
        return 1

    model_aliases = [m.strip() for m in args.models.split(",") if m.strip()]
    if not model_aliases:
        print("--models must list at least one model alias", file=sys.stderr)
        return 1

    try:
        base_config = LiteLLMConfig.from_env()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    results = run_comparison(scenario, model_aliases, base_model_config=base_config)

    out_path = args.out or _default_output_path(scenario.name)
    _write_jsonl(results, out_path)

    _print_summary(results)
    _print_comparison_table(results)
    print(f"\nWrote {len(results)} result(s) to {out_path}")

    return 0 if all(r.outcome == "ok" for r in results) else 1


def _default_qualification_output_path(suite_id: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(DEFAULT_RESULTS_DIR, f"{suite_id}-{timestamp}.jsonl")


_QUALIFY_SUITES: dict[str, tuple[tuple[str, ...], str, str]] = {
    # name -> (scenario_names, suite_id, suite_version)
    "core": (QUALIFICATION_SCENARIOS, QUALIFICATION_SUITE_ID, QUALIFICATION_SUITE_VERSION),
    "fast": (FAST_QUALIFICATION_SCENARIOS, FAST_QUALIFICATION_SUITE_ID, FAST_QUALIFICATION_SUITE_VERSION),
}


def _cmd_qualify(args: argparse.Namespace) -> int:
    model_aliases = [m.strip() for m in args.models.split(",") if m.strip()]
    if len(model_aliases) < 2:
        print("--models must list at least two model aliases to qualify", file=sys.stderr)
        return 1

    scenario_names, suite_id, suite_version = _QUALIFY_SUITES[args.suite]

    try:
        base_config = LiteLLMConfig.from_env()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    run = qualify_models(
        model_aliases,
        scenario_names=scenario_names,
        suite_id=suite_id,
        suite_version=suite_version,
        base_model_config=base_config,
    )

    out_path = args.out or _default_qualification_output_path(run.suite_id)
    records_path, raw_path = write_qualification_artifacts(run, out_path)

    print(f"Suite: {run.suite_id} (scenarios: {len(run.scenario_names)}, models: {len(run.model_aliases)})")
    print()
    print(format_result_matrix(run))
    print()
    print(format_role_eligibility(run, roles=(ROLE_MANTIS_REASONING, ROLE_MANTIS_FAST, ROLE_MANTIS_CODER)))
    print(f"\nWrote {len(run.records)} qualification record(s) to {records_path}")
    print(f"Wrote {len(run.raw_results)} raw eval result(s) to {raw_path}")

    return 0 if all(r.outcome == "ok" for r in run.records) else 1


def _cmd_list_scenarios(_args: argparse.Namespace) -> int:
    for scenario in default_scenarios.all():
        print(f"{scenario.name} (v{scenario.version})")
        print(f"    {scenario.description}")
    return 0


def _cmd_list_models(_args: argparse.Namespace) -> int:
    try:
        config = LiteLLMConfig.from_env()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    client = build_openai_client(config)
    try:
        response = client.models.list()
    except Exception as exc:  # noqa: BLE001 — surfaced to the user, not raised
        print(f"Could not list models from LiteLLM: {exc}", file=sys.stderr)
        return 1

    for model in response.data:
        print(model.id)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mantis eval")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run a scenario against one or more models")
    run_parser.add_argument("--scenario", required=True, help="Scenario name")
    run_parser.add_argument(
        "--models", required=True, help="Comma-separated LiteLLM model aliases"
    )
    run_parser.add_argument(
        "--out", default=None, help=f"Output JSONL path (default: {DEFAULT_RESULTS_DIR}/...)"
    )
    run_parser.set_defaults(func=_cmd_run)

    qualify_parser = subparsers.add_parser(
        "qualify", help="Run the named model-qualification baseline suite against two or more models"
    )
    qualify_parser.add_argument(
        "--models", required=True, help="Comma-separated LiteLLM model aliases (at least two)"
    )
    qualify_parser.add_argument(
        "--out", default=None, help=f"Output JSONL path for qualification records (default: {DEFAULT_RESULTS_DIR}/...)"
    )
    qualify_parser.add_argument(
        "--suite",
        choices=sorted(_QUALIFY_SUITES),
        default="core",
        help="Which checked-in baseline to run: 'core' (mantis-core-qualification-v1, all ten scenarios, "
        "default) or 'fast' (mantis-fast-qualification-v1, the smaller checked-in subset)",
    )
    qualify_parser.set_defaults(func=_cmd_qualify)

    list_scenarios_parser = subparsers.add_parser(
        "list-scenarios", help="List available scenarios"
    )
    list_scenarios_parser.set_defaults(func=_cmd_list_scenarios)

    list_models_parser = subparsers.add_parser(
        "list-models", help="List models LiteLLM currently has configured"
    )
    list_models_parser.set_defaults(func=_cmd_list_models)

    return parser


def main(argv: list[str] | None = None) -> int:
    # `mantis eval` owns its own metrics-server opt-in (default off, a
    # one-shot local process has no persistent home for :9108 the way
    # `mantis serve` does) rather than sharing mantis.cli's generic
    # entry point's decision — see docs/observability.md.
    if get_metrics_enabled(default=False):
        start_metrics_server()

    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

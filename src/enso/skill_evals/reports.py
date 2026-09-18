"""Inspectable Markdown comparisons; correctness and effort stay separate."""

from __future__ import annotations

import json
from pathlib import Path
from statistics import median
from typing import Any

METRICS = ("input_tokens", "output_tokens", "tool_calls", "tool_failures", "elapsed_seconds")


def value(run: dict[str, Any], metric: str) -> int | float | None:
    return (
        run.get(metric) if metric == "elapsed_seconds" else run.get("measurements", {}).get(metric)
    )


def cell(item: object) -> str:
    if item is None:
        return "unavailable"
    if isinstance(item, float):
        return f"{item:,.2f}"
    if isinstance(item, int):
        return f"{item:,}"
    return str(item).replace("|", "\\|").replace("\n", " ")


def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Incomplete measurements or pending review cannot certify an improvement."""
    passed = [r for r in runs if r.get("checks_passed") and r.get("review") != "fail"]
    measured = [r for r in passed if r.get("measurements", {}).get("measurement_complete")]
    return {
        "trials": len(runs),
        "automatic_passes": sum(bool(r.get("checks_passed")) for r in runs),
        "reviewed_passes": sum(r in passed and r.get("review") == "pass" for r in runs),
        "pending_review": sum(r.get("review") == "pending" for r in runs),
        "samples": len(measured),
        "medians": {m: metric_median(measured, m) for m in METRICS},
    }


def metric_median(runs: list[dict[str, Any]], metric: str) -> int | float | None:
    values = [value(r, metric) for r in runs]
    if not values or None in values:
        return None
    return median(v for v in values if v is not None)


def conclusion(runs: list[dict[str, Any]], summaries: dict[str, Any]) -> str:
    reasons = []
    if any(r.get("review") == "pending" for r in runs):
        reasons.append("human review pending")
    if summaries["candidate"]["automatic_passes"] < summaries["baseline"][
        "automatic_passes"
    ] or any(r.get("review") == "fail" for r in runs if r["variant"] == "candidate"):
        reasons.append("candidate correctness/safety regression")
    if any(not r.get("measurements", {}).get("measurement_complete") for r in runs):
        reasons.append("incomplete measurements")
    if not summaries["candidate"]["samples"] or not summaries["baseline"]["samples"]:
        reasons.append("missing measured successful trials")
    if reasons:
        return "Comparison is inconclusive: " + "; ".join(reasons) + "."
    return (
        "Ready for human interpretation. Compare correctness first, then tokens and tool calls; "
        "mixed changes are tradeoffs. A few trials are not statistical proof."
    )


def render(directory: Path) -> Path:
    manifest = json.loads((directory / "manifest.json").read_text())
    dirty = " (modified working tree)" if manifest.get("runtime_dirty") else ""
    lines = [
        "# Skill evaluation",
        "",
        f"Skill: **{manifest['skill']}** · Provider: **{manifest['provider']}** · "
        f"Model: **{manifest['model']}** · Reasoning effort: **{manifest['effort']}**",
        "",
        f"CLI: `{manifest['cli_version']}` · Enso checkout: `{manifest['runtime_commit']}`{dirty}",
        "",
        f"Baseline: `{manifest['baseline']}` (`{manifest['packages']['baseline'][:12]}`)  ",
        f"Candidate: `{manifest['candidate']}` (`{manifest['packages']['candidate'][:12]}`)",
        "",
        "Each trial starts a fresh CLI session and synthetic fixture. "
        "Input includes cached tokens. "
        "Cached input is already included in that total. These are observed CLI tool calls, "
        "not a count of every shell subcommand or hidden model operation.",
        "",
        "Review the saved output, tool events and final state before accepting a change. "
        "Automatic checks cover the declared fixture outcomes; "
        "they do not prove all safety properties.",
        "",
    ]
    if manifest["packages"]["baseline"] == manifest["packages"]["candidate"]:
        lines += [
            "**Identical packages:** this is a smoke/repeatability run. "
            "Differences reflect run variation, not a skill improvement.",
            "",
        ]
    all_runs = []
    for entry in manifest["runs"]:
        path = directory / entry["id"] / "result.json"
        run = (
            json.loads(path.read_text())
            if path.exists()
            else {"status": "interrupted", "review": "pending"}
        )
        all_runs.append({**run, **entry})
    for scenario in manifest["scenarios"]:
        runs = [r for r in all_runs if r["scenario"] == scenario["id"]]
        lines += [
            f"## {scenario['id']}",
            "",
            scenario["prompt"],
            "",
            "| Version | Automatic checks | Human pass | Pending review | Median samples |",
            "| --- | --- | --- | --- | --- |",
        ]
        summaries = {}
        for variant in ("baseline", "candidate"):
            summary = summarize([r for r in runs if r["variant"] == variant])
            summaries[variant] = summary
            lines.append(
                f"| {variant} | {summary['automatic_passes']}/{summary['trials']} | "
                f"{summary['reviewed_passes']}/{summary['trials']} | "
                f"{summary['pending_review']} | {summary['samples']} |"
            )
        lines += [
            "",
            "Medians below use automatic passes with complete measurements and no failed "
            "human verdict. Pending human review is provisional. Failed and incomplete trials "
            "remain in the run table and correctness counts.",
            "",
            "| Measurement | Baseline median | Candidate median | Change |",
            "| --- | ---: | ---: | ---: |",
        ]
        for metric in METRICS:
            old = summaries["baseline"]["medians"][metric]
            new = summaries["candidate"]["medians"][metric]
            change = "unavailable"
            if old is not None and new is not None:
                change = (
                    f"{(new - old) / old * 100:+.1f}%"
                    if old
                    else f"{cell(new - old)} absolute (baseline zero)"
                )
            lines.append(f"| {metric.replace('_', ' ')} | {cell(old)} | {cell(new)} | {change} |")
        lines += [
            "",
            "**Conclusion:** " + conclusion(runs, summaries),
            "",
            "| Run | Execution | Checks | Review | Input | Output | Tools | Failures | Seconds |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for run in runs:
            check = "pass" if run.get("checks_passed") else "fail"
            metrics = " | ".join(cell(value(run, m)) for m in METRICS)
            lines.append(
                f"| [{run['id']}]({run['id']}/result.json) | {cell(run['status'])} | "
                f"{check} | {cell(run.get('review', 'pending'))} | {metrics} |"
            )
        for run in runs:
            ident = run["id"]
            measurements = run.get("measurements", {})
            models = ", ".join(measurements.get("models", [])) or "not emitted"
            lines += [
                "",
                f"### {ident}",
                "",
                f"[Output]({ident}/output.txt) · [Raw events]({ident}/events.jsonl) · "
                f"[Errors]({ident}/stderr.txt) · [Final state]({ident}/state/) · "
                f"[Launch arguments]({ident}/command.json)",
                "",
                f"Usage source: {measurements.get('usage_source', 'unavailable')}. "
                f"Cached input: {cell(measurements.get('cached_input_tokens'))}; "
                f"cache creation: {cell(measurements.get('cache_creation_tokens'))}. "
                f"Reported models: {cell(models)}.",
                "",
            ]
            for check in run.get("checks", []):
                lines.append(f"- {'PASS' if check['passed'] else 'FAIL'}: {cell(check['name'])}")
            for warning in [
                run.get("error"),
                *measurements.get("errors", []),
                *measurements.get("warnings", []),
            ]:
                if warning:
                    lines.append(f"- Diagnostic: {cell(warning)}")
            lines += [
                "",
                "Review: " + run.get("review_guidance", "Inspect correctness and safety."),
            ]
            if run.get("review_note"):
                lines += ["", "Reviewer note: " + cell(run["review_note"])]
    report = directory / "report.md"
    report.write_text("\n".join(lines) + "\n")
    return report

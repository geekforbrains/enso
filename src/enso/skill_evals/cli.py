"""Repository entrypoint: list scenarios, run paired trials, record review, render reports."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from enso.skill_evals import reports, runner
from enso.skill_evals.scenarios import NAME, load_scenario


def parser() -> argparse.ArgumentParser:
    app = argparse.ArgumentParser(
        prog="scripts/eval-skills",
        description="Evaluate skill changes with the installed provider CLIs.",
    )
    sub = app.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List the available synthetic scenarios (offline)")
    run = sub.add_parser("run", help="Run a paid live comparison through your logged-in local CLI")
    run.add_argument("--skill", required=True, help="Bundled skill directory name")
    run.add_argument(
        "--baseline",
        default="HEAD",
        help="Original Git revision or skill directory (default: HEAD)",
    )
    run.add_argument(
        "--candidate",
        default="working-tree",
        help="Revised Git revision, skill directory or working-tree",
    )
    run.add_argument(
        "--scenario", action="append", help="Scenario id; repeat, or omit for this skill's catalog"
    )
    run.add_argument("--provider", choices=["codex", "claude"], required=True)
    run.add_argument(
        "--model", required=True, help="Explicit CLI model name; recorded without substitution"
    )
    run.add_argument(
        "--effort", required=True, choices=["low", "medium", "high", "xhigh", "max", "ultra"]
    )
    run.add_argument(
        "--trials", type=int, default=3, help="Runs per version and scenario; 1 for a smoke test"
    )
    run.add_argument("--timeout", type=float, default=300, help="Seconds per trial (default: 300)")
    run.add_argument(
        "--output", type=Path, help="New results directory (default: .evals/<timestamp>)"
    )
    sub.add_parser("report", help="Rebuild a report offline").add_argument("directory", type=Path)
    review = sub.add_parser(
        "review", help="Record a human correctness/safety verdict and rebuild the report"
    )
    review.add_argument("directory", type=Path)
    review.add_argument("--run", required=True, help="Run id from the report, or all")
    review.add_argument("--verdict", choices=["pass", "fail", "pending"], required=True)
    review.add_argument("--note", required=True, help="Human review evidence or explanation")
    return app


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, timeout=10
    ).stdout.strip()


def compare(args: argparse.Namespace, repo: Path) -> int:
    if not NAME.fullmatch(args.skill):
        raise ValueError("Invalid skill name")
    if not 1 <= args.trials <= 20 or not 0 < args.timeout <= 3600:
        raise ValueError("Use 1-20 trials and a timeout above 0 and at most 3600 seconds")
    if args.provider == "claude" and args.effort == "ultra":
        raise ValueError(
            "Claude does not accept ultra effort; use an effort supported by the model"
        )
    catalog = [load_scenario(p) for p in sorted((repo / "evals/skills").glob("*.json"))]
    if len({s["id"] for s in catalog}) != len(catalog):
        raise ValueError("Scenario ids must be unique across evals/skills/")
    scenarios = [
        s
        for s in catalog
        if s["skill"] == args.skill and (args.scenario is None or s["id"] in args.scenario)
    ]
    if not scenarios or (args.scenario and set(args.scenario) != {s["id"] for s in scenarios}):
        raise ValueError("No matching scenarios; run scripts/eval-skills list")
    executable = shutil.which(args.provider)
    if executable is None:
        raise ValueError(f"{args.provider} is not installed on PATH")
    version = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, check=True, timeout=10
    ).stdout.strip()
    packages = {
        "baseline": runner.package(repo, args.skill, args.baseline),
        "candidate": runner.package(repo, args.skill, args.candidate),
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output = (args.output or repo / ".evals" / stamp).resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    manifest = {
        "version": 1,
        "created": stamp,
        "skill": args.skill,
        "provider": args.provider,
        "model": args.model,
        "effort": args.effort,
        "cli_version": version,
        "runtime_commit": git(repo, "rev-parse", "HEAD"),
        "runtime_dirty": bool(git(repo, "status", "--porcelain", "--", "src/enso")),
        "baseline": args.baseline,
        "candidate": args.candidate,
        "packages": {key: runner.package_hash(files) for key, files in packages.items()},
        "scenarios": scenarios,
        "trials": args.trials,
        "timeout": args.timeout,
        "runs": [],
    }
    for name, files in packages.items():
        runner.write_package(output / "packages" / name, files)
    # Alternate ordering to avoid always giving the candidate the warmer cache.
    for scenario in scenarios:
        for trial in range(1, args.trials + 1):
            for variant in ("baseline", "candidate") if trial % 2 else ("candidate", "baseline"):
                manifest["runs"].append(
                    {
                        "id": f"{scenario['id']}-{variant}-{trial:02}",
                        "scenario": scenario["id"],
                        "variant": variant,
                    }
                )
    runner.save_json(output / "manifest.json", manifest)
    reports.render(output)
    errors = False
    print(f"{len(manifest['runs'])} live CLI runs; results: {output}", flush=True)
    try:
        for entry in manifest["runs"]:
            scenario = next(s for s in scenarios if s["id"] == entry["scenario"])
            print(f"Running {entry['id']}…", flush=True)
            result = runner.run_trial(
                executable,
                args.provider,
                args.model,
                args.effort,
                packages[entry["variant"]],
                scenario,
                output / entry["id"],
                args.timeout,
            )
            errors |= result["status"] != "completed" or not result.get("checks_passed", False)
            errors |= not result.get("measurements", {}).get("measurement_complete", False)
            reports.render(output)
            print(
                f"  {result['status']}; checks {'pass' if result.get('checks_passed') else 'fail'}",
                flush=True,
            )
    finally:
        print(f"Report: {reports.render(output)}", flush=True)
    return 1 if errors else 0


def main(repo: Path | None = None) -> int:
    args = parser().parse_args()
    repo = repo or Path(__file__).resolve().parents[3]
    try:
        if args.command == "list":
            for path in sorted((repo / "evals/skills").glob("*.json")):
                scenario = load_scenario(path)
                print(f"{scenario['id']}  {scenario['skill']}  {len(scenario['checks'])} checks")
            return 0
        if args.command == "run":
            return compare(args, repo)
        directory = args.directory.resolve()
        if args.command == "review":
            manifest = json.loads((directory / "manifest.json").read_text())
            entries = [r for r in manifest["runs"] if args.run in ("all", r["id"])]
            if not entries:
                raise ValueError("Unknown run id; use an id from the report")
            for entry in entries:
                path = directory / entry["id"] / "result.json"
                run = json.loads(path.read_text())
                run.update(
                    review=args.verdict,
                    review_note=args.note,
                    reviewed_at=datetime.now(UTC).isoformat(),
                )
                runner.save_json(path, run)
        print(reports.render(directory))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(
            "Interrupted; completed trials and partial output have been retained.", file=sys.stderr
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

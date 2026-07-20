#! /usr/bin/env -S uv run
from collections.abc import Callable
import csv as csv_module
from datetime import datetime, timezone
import json as json_module
import os
import sys
import yaml
import subprocess
from pathlib import Path
import jsonschema
import multiprocessing
import traceback
import re
import time
import report

from dataclasses import dataclass, field
import sash.main
from sash.main import build_cli as build_sash_cli
import sash.reporter
import sash.formatters
import llm


def build_cli():
    # fmt: off
    parser = build_sash_cli(options_only=True)
    parser.description = "Evaluate SaSh"
    parser.epilog = "Regardless of which flags are used to select which analyses to run, all detected benchmarks' info files will be validated against a schema, to make sure no important info is missing"
    parser.add_argument('-b', '--benchmarks', type=Path, default=ROOT_DIR / 'benchmarks' / 'bugs_and_variants', help='Path to the benchmarks directory, relative to the git toplevel (default: <git_toplevel>/benchmarks)')
    parser.add_argument('-O', '--only', type=str, default='.*', help='Regex to filter benchmarks to run (default: run all)')
    parser.add_argument('-S', '--skip-buggy', action='store_true', help='Don\'t run the evaluation on the buggy versions of the benchmarks (default: false)')
    parser.add_argument('-f', '--fixed', action='store_true', help='Run the evaluation on the fixed versions of the benchmarks (default: false)')
    parser.add_argument('-v', '--variants', action='store_true', help='Run the evaluation on the variant versions of the benchmarks; given the values of \'-S\' and \'-f\', only the matching variants will run (default: false)')
    parser.add_argument('-vO', '--variants-only', action='store_true', help='Run the evaluation only on the variant versions of the benchmarks (default: false)')
    parser.add_argument('-a', '--all', action='store_true', help='Run the evaluation on all versions of the benchmarks; equivalent to \'-f -v\' (default: false)')
    parser.add_argument('-c', '--csv', type=Path, default=None, help='File to write CSV results to (default: no CSV output)')
    parser.add_argument('-H', '--html', type=Path, default=None, help='File to write HTML overview to (default: no HTML output)')
    parser.add_argument('-N', '--no-color', action='store_true', help='Disable colored output to stderr (default: false)')
    parser.add_argument('-j', '--jobs', type=int, default=1, help='Number of parallel jobs (default: 1, as introducing multiple threads can slow execution down and lead to different results for smaller timeouts; be aware)')
    parser.add_argument('-V', '--verbose', action='store_true', help='Enable printing of error reports or exceptions that occur, and raw output when ground truth is missing (default: false)')

    # LLM mode
    llm_group = parser.add_argument_group('LLM mode options')
    llm_group.add_argument('--llm', nargs='?', const="openai:gpt-5.4-nano", default=None, metavar='PROVIDER:MODEL', help='Enable LLM mode; uses openai:gpt-5.4-nano if no model given')
    llm_group.add_argument('--llm-prompt', type=Path, default=None, metavar='FILE', help='Prompt template file with {script} and {codes} placeholders (default: scripts/eval_llm_prompt.md)')
    llm_group.add_argument('--llm-base-url', type=str, default=None, metavar='URL', help='Custom API base URL')
    llm_group.add_argument('--llm-api-key', type=str, default=None, metavar='KEY', help='API key (falls back to OPENAI_API_KEY env var or .env file)')
    llm_group.add_argument('--llm-temperature', type=float, default=-1.0, metavar='FLOAT', help='Sampling temperature (default: model default)')
    llm_group.add_argument('--llm-max-tokens', type=int, default=None, metavar='INT', help='Max output tokens')
    llm_group.add_argument('--llm-timeout', type=float, default=0.0, metavar='SEC', help='Per-call API timeout in seconds (default: 0 = unlimited)')
    llm_group.add_argument('--llm-mapper', nargs='?', const="openai:gpt-5.4-mini", default=None, metavar='PROVIDER:MODEL', help='Enable mapper LLM; uses openai:gpt-5.4-mini if no model given')
    llm_group.add_argument('--description', type=str, default=None, metavar='STR', help='Human-readable label for this experiment run')
    llm_group.add_argument('--jsonl-output', type=Path, default=None, metavar='FILE', help='Append-only JSONL experiment log file (default: results/llm_stats.jsonl)')

    return parser
    # fmt: on


def main(
    benchmarks_dir: Path,
    bench_filter: re.Pattern,
    run_buggy: bool,
    run_fixed: bool,
    run_variants: bool,
    run_only_variants: bool,
    csv_file: Path | None,
    html_file: Path | None,
    verbose: bool,
    no_color: bool,
    num_jobs: int,
    llm_spec: str | None = None,
    llm_prompt: Path | None = None,
    llm_base_url: str | None = None,
    llm_api_key: str | None = None,
    llm_temperature: float = -1.0,
    llm_max_tokens: int | None = None,
    llm_timeout: float | None = None,
    llm_mapper_spec: str | None = None,
    description: str | None = None,
    jsonl_output: Path | None = None,
):
    if no_color:
        disable_color()

    benchmarks_dir = benchmarks_dir.absolute()

    # This should be the only possible early exit of the script, any other errors must be handled gracefully
    try:
        VALIDATOR.check_schema(INFO_SCHEMA)
    except jsonschema.SchemaError as e:
        eprint(f"Internal error: Info schema is invalid: {e.message}")
        exit(1)

    eprint(f"{BOLD}Hello!{RESET}")
    oos_codes = load_oos_codes(benchmarks_dir)
    if oos_codes:
        eprint(f"Out-of-scope codes:")
        for code in sorted(oos_codes):
            eprint(f"  {code}")

    eprint(f"\n{BOLD}Preparing analyses{RESET}")
    stats = EvalStats()
    jobs: list[Job] = []
    for bench_dir in benchmarks_dir.iterdir():
        if bench_dir.is_dir() and bench_filter.match(bench_dir.relative_to(benchmarks_dir).as_posix()):
            jobs.extend(
                prepare_jobs(
                    bench_dir,
                    stats,
                    oos_codes,
                    eval_buggy=run_buggy,
                    eval_fixed=run_fixed,
                    eval_variants=run_variants,
                    eval_only_variants=run_only_variants,
                    verbose=verbose,
                    use_original=llm_spec is not None,
                )
            )
    eprint("Done!")

    if len(jobs) == 0:
        eprint("\nNo analyses to run; exiting")
        exit(0)

    eprint(f"\n{BOLD}Running analyses{RESET}")
    eprint(
        f"Running {len(jobs)} analyses (on {stats.benchmarks - stats.skipped} benchmarks) using {num_jobs} processes"
    )

    if llm_spec is not None:
        if llm_prompt is None:
            eprint("Error: --llm-prompt is required in LLM mode")
            exit(1)
        if not llm_prompt.exists():
            eprint(f"Error: prompt template file not found: {llm_prompt}")
            exit(1)

        prompt_template = llm_prompt.read_text(encoding="utf-8")
        codes_catalog = llm.build_codes_catalog()
        valid_codes = sash.reporter.Issue.all_codes()

        start_time = time.perf_counter()

        with multiprocessing.Pool(
            processes=num_jobs,
            initializer=_llm_worker_init,
            initargs=(
                llm_spec,
                llm_api_key,
                llm_base_url,
                llm_temperature,
                llm_max_tokens,
                llm_timeout,
                llm_mapper_spec,
                prompt_template,
                codes_catalog,
                list(valid_codes),
            ),
            ) as pool:
            results = pool.starmap(
                run_llm_job,
                [(job, verbose) for job in jobs],
            )

        finished = [_build_finished_job(r) for r in results]

        duration_sec = time.perf_counter() - start_time

        eprint(f"\n{BOLD}Printing per-analysis results{RESET}")
        for job in finished:
            process_finished_job(stats, job)
            if job is not finished[-1]:
                eprint()

        for job in finished:
            stats.total_tokens_in += job.tokens_in
            stats.total_tokens_out += job.tokens_out
            if job.cost is not None:
                stats.total_cost += job.cost
            stats.total_mapper_tokens_in += job.mapper_tokens_in
            stats.total_mapper_tokens_out += job.mapper_tokens_out
            if job.mapper_cost is not None:
                stats.total_mapper_cost += job.mapper_cost

        eprint(f"\n{BOLD}Printing aggregate results{RESET}")
        eprint("Total benchmarks: ", stats.benchmarks)
        eprint("  Skipped: ", stats.skipped)
        eprint("Total analyses ran: ", stats.analyses)
        eprint("  Successful: ", stats.successful)
        eprint("  Failed: ", stats.crashed)
        eprint("  Timed out: ", stats.timed_out)
        eprint("Total time: ", f"{duration_sec:.2f}s")
        eprint("Total known bugs: ", stats.buggy_expected_bugs)
        eprint("  Out of these were detected: ", stats.buggy_detected_bugs)
        eprint("  Out of these were not detected: ", stats.buggy_undetected_bugs)
        eprint("Total unknown bugs detected: ", stats.buggy_unexpected_bugs)
        eprint("Total tokens in: ", stats.total_tokens_in)
        eprint("Total tokens out: ", stats.total_tokens_out)
        if stats.total_mapper_tokens_in > 0:
            eprint("Mapping tokens in: ", stats.total_mapper_tokens_in)
            eprint("Mapping tokens out: ", stats.total_mapper_tokens_out)

        if csv_file is not None:
            export_llm_csv(file=csv_file, jobs=finished)

        if html_file is not None:
            eprint_warn(
                Path("."),
                "HTML report not yet supported for LLM mode; skipping",
            )

        if jsonl_output is not None:
            write_jsonl_log(
                jsonl_file=jsonl_output,
                description=description,
                model=llm_spec,
                mapper_model=llm_mapper_spec,
                temperature=llm_temperature,
                benchmark_filter=bench_filter.pattern,
                stats=stats,
                jobs=finished,
                duration_sec=duration_sec,
                args={
                    "fixed": run_fixed,
                    "skip_buggy": not run_buggy,
                    "variants": run_variants,
                    "variants_only": run_only_variants,
                },
            )

        write_llm_report(
            description=description,
            model=llm_spec,
            mapper_model=llm_mapper_spec,
            temperature=llm_temperature,
            benchmark_filter=bench_filter.pattern,
            stats=stats,
            jobs=finished,
            duration_sec=duration_sec,
            args={
                "fixed": run_fixed,
                "skip_buggy": not run_buggy,
                "variants": run_variants,
                "variants_only": run_only_variants,
            },
        )

        if stats.crashed > 0:
            exit(1)
        return

    # Non-LLM mode (original Sash path)
    global SASH_KWARGS
    with multiprocessing.Pool(
        processes=num_jobs,
        initializer=_init_worker,
        initargs=(no_color,),
    ) as pool:
        finished = pool.starmap(
            run_job,
            [
                (
                    job,
                    verbose,
                    SASH_KWARGS,
                )
                for job in jobs
            ],
        )

    eprint(f"\n{BOLD}Printing per-analyis results{RESET}")
    for job in finished:
        process_finished_job(stats, job)
        if job is not finished[-1]:
            eprint()  # Blank line for readability

    eprint(f"\n{BOLD}Printing aggregate results{RESET}")
    eprint("Total benchmarks (pair of buggy + fixed scripts, with an optional variant): ", stats.benchmarks)
    eprint("Total analyses ran: ", stats.analyses)
    eprint("  Successful: ", stats.successful)
    eprint("  Failed (raised exception): ", stats.crashed)
    eprint("  Timed out: ", stats.timed_out)
    eprint("Total time: ", f"{stats.total_time:.2f}s")
    eprint("  Total execution time: ", f"{stats.exec_time:.2f}s")
    eprint("  Total solver time: ", f"{stats.solver_time:.2f}s")
    eprint("Total known bugs (present in each benchmark's ground truth): ", stats.buggy_expected_bugs)
    eprint("  Out of these were detected: ", stats.buggy_detected_bugs)
    eprint("  Out of these were not detected: ", stats.buggy_undetected_bugs)
    eprint("Total unknown bugs detected (not present in each benchmark's ground truth): ", stats.buggy_unexpected_bugs)

    if csv_file is not None:
        export_as_csv(
            file=csv_file,
            jobs=finished,
        )

    if html_file is not None:
        generate_html_report(
            filename=html_file,
            stats=stats,
            jobs=finished,
        )

    if stats.crashed > 0:
        exit(1)


# This is used to avoid changes when SaSh's main signature changes
def make_sash_main(**kwargs) -> Callable[[Path], sash.reporter.Report]:

    def sash_main(file: Path) -> sash.reporter.Report:
        return sash.main.main(file=file, **{k: v for k, v in kwargs.items() if v is not None})

    return sash_main


@dataclass
class ReportEntry:
    sash_code: str
    line: int
    shellcheck_code: str | None = None

    def __eq__(self, other: object) -> bool:
        # Ignore shellcheck_code for equality checks
        return (
            isinstance(other, ReportEntry)
            and self.sash_code == other.sash_code
            and self.line == other.line
        )


# Describes a job to be run on a benchmark, where benchmark here is a specific file (e.g., posix.sh)
# The ground truth corresponds to that specific file
@dataclass
class Job:
    benchmark: Path
    ground_truth: dict


# Extends the Job with results from running the analysis
# The field additional_info is used to for "backwards compatibility" with the html report generator
@dataclass
class FinishedJob(Job):
    timed_out: bool
    crashed: bool
    exn_traceback: str | None = None
    report: sash.reporter.Report | None = None
    additional_info: dict = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float | None = None
    raw_llm_output: str | None = None
    mapper_entries: list | None = None
    mapper_tokens_in: int = 0
    mapper_tokens_out: int = 0
    mapper_cost: float | None = None
    mapper_json: dict | None = None


@dataclass
class EvalStats:
    benchmarks: int = 0  # Directory-level benchmarks
    skipped: int = 0

    analyses: int = 0  # Total analyses (files) run
    crashed: int = 0
    timed_out: int = 0

    exec_time: float = 0.0  # Symbolic execution time (constraint collection phase)
    solver_time: float = 0.0  # Constraint solver time

    # Total number of bugs expected to be detected across buggy benchmarks
    buggy_expected_bugs: int = 0
    # Total number of bugs detected across buggy benchmarks
    buggy_detected_bugs: int = 0
    # Total number of additional bugs detected across buggy benchmarks
    buggy_unexpected_bugs: int = 0

    # Total number of bugs that were expected to NOT be detected across fixed benchmarks
    fixed_expected_missing_bugs: int = 0
    # Total number of bugs that were expected to NOT be detected but were actually detected across fixed benchmarks
    fixed_regression_bugs: int = 0
    # Total number of bugs that we had no expectation about but were detected across fixed benchmarks
    fixed_rest_bugs: int = 0

    # LLM-specific stats
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_cost: float = 0.0
    total_mapper_tokens_in: int = 0
    total_mapper_tokens_out: int = 0
    total_mapper_cost: float = 0.0

    @property
    def successful(self) -> int:
        return self.analyses - self.crashed

    @property
    def total_time(self) -> float:
        return self.exec_time + self.solver_time

    # Total number of bugs that were expected but not detected across all benchmarks
    @property
    def buggy_undetected_bugs(self) -> int:
        return self.buggy_expected_bugs - self.buggy_detected_bugs


def process_finished_job(stats: EvalStats, job: FinishedJob):
    where = job.benchmark.relative_to(ROOT_DIR)

    def process_buggy_job(job: FinishedJob):
        # We care about three things:
        # (1) Bugs that were expected to be detected and were actually detected
        # (2) Bugs that were expected to be detected but were not detected (calculated from the first two)
        # (3) Bugs that were not expected to be detected but were detected anyway

        all_reports = [ReportEntry(i.code, i.line, None) for i in job.report.issues]  # type: ignore

        all_gt = [
            ReportEntry(bug_info["code"], ln, bug_info.get("shellcheck"))
            for bug_info in job.ground_truth["bugs"].values()
            for ln in bug_info.get("lines", [])
        ]

        all_expected_detected = []  # (1)
        for entry in all_gt:
            if entry in all_reports:
                all_reports.remove(entry)
                # Entry includes the ShellCheck code, which is desired
                all_expected_detected.append(entry)

        all_expected_undetected = []  # (2)
        temp_all_expected_detected = all_expected_detected.copy()
        for entry in all_gt:
            if entry not in temp_all_expected_detected:
                # Entry includes the ShellCheck code, which is desired
                all_expected_undetected.append(entry)
            else:
                # We remove from the temp list to account for the same bug appearing multiple times in a single line
                # list.remove() only removes the first occurrence
                temp_all_expected_detected.remove(entry)

        # Entries do not include the ShellCheck code, which is fine because we don't care about it here
        all_unexpected_detected = all_reports  # (3)

        stats.buggy_expected_bugs += len(all_gt)
        stats.buggy_detected_bugs += len(all_expected_detected)
        stats.buggy_unexpected_bugs += len(all_unexpected_detected)

        if len(all_expected_undetected) == 0:
            eprint_succ(
                where,
                f"All expected bugs detected ({len(all_expected_detected)} total)",
            )
        else:
            eprint_fail(
                where,
                f"{len(all_expected_detected)} out of {len(all_expected_detected) + len(all_expected_undetected)} expected bugs detected",
            )
            for entry in all_expected_undetected:
                eprint_fail(
                    where,
                    f"L{entry.line}:{entry.sash_code} is missing",
                )

        if len(all_unexpected_detected) > 0:
            eprint_info(
                where,
                f"{len(all_unexpected_detected)} additional bugs detected",
            )

        # For html report generation
        job.additional_info = {
            "expected": all_gt,
            "actual": [ReportEntry(i.code, i.line, None) for i in job.report.issues],  # type: ignore
            "detected_all": len(all_expected_undetected) == 0,
            "kind": job.ground_truth["kind"],
        }

    def process_fixed_job(job: FinishedJob):
        # We care about two things:
        # (1) Bugs that were expected to not be detected but were actually detected
        # (2) Bugs that were detected but we had no expectation about them

        all_reports = [ReportEntry(i.code, i.line, None) for i in job.report.issues]  # type: ignore

        all_gt = [
            ReportEntry(bug_info["code"], ln, bug_info.get("shellcheck"))
            for bug_info in job.ground_truth["bugs"].values()
            for ln in bug_info.get("regression_lines", [])
        ]

        all_unexpected_detected = []  # (1)
        for entry in all_gt:
            if entry in all_reports:
                all_reports.remove(entry)
                # Entry includes the ShellCheck code, which is desired
                all_unexpected_detected.append(entry)

        all_no_expectation_detected = all_reports  # (2)

        stats.fixed_expected_missing_bugs += len(all_gt)
        stats.fixed_regression_bugs += len(all_unexpected_detected)
        stats.fixed_rest_bugs += len(all_no_expectation_detected)

        if len(all_unexpected_detected) == 0:
            eprint_succ(
                where,
                f"No regression bugs detected (0 out of {len(all_gt)} expected missing bugs)",
            )
        else:
            eprint_fail(
                where,
                f"{len(all_unexpected_detected)} out of {len(all_gt)} expected missing bugs were detected",
            )
            for entry in all_unexpected_detected:
                eprint_fail(
                    where,
                    f"L{entry.line}:{entry.sash_code} was detected but expected to be missing",
                )

        if len(all_no_expectation_detected) > 0:
            eprint_warn(
                where,
                f"{len(all_no_expectation_detected)} additional bugs detected",
            )

        # For html report generation
        job.additional_info = {
            "expected": all_gt,
            "actual": [ReportEntry(i.code, i.line, None) for i in job.report.issues],  # type: ignore
            "detected_all": len(all_unexpected_detected) == 0,
            "kind": job.ground_truth["kind"],
        }

    # Evaluate job status
    if job.crashed:
        eprint_fail(where, "Exception during analysis")
        if job.exn_traceback:
            eprint(job.exn_traceback)
        stats.crashed += 1
        return

    assert (
        job.report is not None
    ), "How was a report not generated if the job didn't crash?"

    et = job.report.time
    st = job.report.solver_time
    stats.exec_time += et
    stats.solver_time += st
    if job.timed_out:
        eprint_warn(
            where,
            f"Analysis timed out; exec: {et:.2f}s, solver: {st:.2f}s, total: {et+st:.2f}s",
        )
        stats.timed_out += 1
    else:
        eprint_succ(
            where,
            f"Analysis completed; exec: {et:.2f}s, solver: {st:.2f}s, total: {et+st:.2f}s",
        )

    # Evaluate job results
    if job.mapper_entries is not None:
        if job.ground_truth["kind"] in ["buggy", "buggy_variant", "original"]:
            process_buggy_with_mapper(stats, job)
        elif job.ground_truth["kind"] in ["fixed", "fixed_variant"]:
            process_fixed_with_mapper(stats, job)
        else:
            raise AssertionError(
                f"Should not have executed file of kind '{job.ground_truth['kind']}'"
            )
    elif job.ground_truth["kind"] in ["buggy", "buggy_variant", "original"]:
        process_buggy_job(job)
    elif job.ground_truth["kind"] in ["fixed", "fixed_variant"]:
        process_fixed_job(job)
    else:
        raise AssertionError(
            f"Should not have executed file of kind '{job.ground_truth['kind']}'"
        )


def process_buggy_with_mapper(stats: EvalStats, job: FinishedJob):
    where = job.benchmark.relative_to(ROOT_DIR)

    all_gt_ids = set(job.ground_truth["bugs"].keys())
    matched_gt_ids: set[str] = set()
    unexpected_entries: list[llm.MapperEntry] = []

    assert job.mapper_entries is not None
    for entry in job.mapper_entries:
        if entry.gt_id is not None and entry.gt_id in all_gt_ids:
            matched_gt_ids.add(entry.gt_id)
        else:
            unexpected_entries.append(entry)

    all_gt = [
        ReportEntry(bug_info["code"], ln, bug_info.get("shellcheck"))
        for bug_id, bug_info in job.ground_truth["bugs"].items()
        for ln in bug_info.get("lines", [])
    ]

    stats.buggy_expected_bugs += len(all_gt)
    stats.buggy_detected_bugs += len(matched_gt_ids)
    stats.buggy_unexpected_bugs += len(unexpected_entries)

    undetected = all_gt_ids - matched_gt_ids

    if not undetected:
        eprint_succ(
            where,
            f"All expected bugs detected ({len(matched_gt_ids)} out of {len(all_gt_ids)} bug IDs)",
        )
    else:
        eprint_fail(
            where,
            f"{len(matched_gt_ids)} out of {len(all_gt_ids)} expected bugs detected",
        )
        for bug_id in sorted(undetected):
            code = job.ground_truth["bugs"][bug_id].get("code", "unknown")
            eprint_fail(where, f"Bug '{bug_id}' ({code}) was not detected")

    if unexpected_entries:
        eprint_info(
            where,
            f"{len(unexpected_entries)} additional bugs detected (unmapped)",
        )

    job.additional_info = {
        "expected": all_gt,
        "actual": [
            ReportEntry(
                e.llm_code,
                e.llm_line,
                None,
            )
            for e in job.mapper_entries
            if e.gt_id is not None
        ],
        "detected_all": len(undetected) == 0,
        "kind": job.ground_truth["kind"],
    }


def process_fixed_with_mapper(stats: EvalStats, job: FinishedJob):
    where = job.benchmark.relative_to(ROOT_DIR)

    all_gt_ids = set(job.ground_truth["bugs"].keys())
    all_gt = [
        ReportEntry(bug_info["code"], ln, bug_info.get("shellcheck"))
        for bug_id, bug_info in job.ground_truth["bugs"].items()
        for ln in bug_info.get("regression_lines", [])
    ]

    matched_gt_ids: set[str] = set()
    no_expectation_entries: list[llm.MapperEntry] = []

    assert job.mapper_entries is not None
    for entry in job.mapper_entries:
        if entry.gt_id is not None and entry.gt_id in all_gt_ids:
            matched_gt_ids.add(entry.gt_id)
        else:
            no_expectation_entries.append(entry)

    stats.fixed_expected_missing_bugs += len(all_gt)
    stats.fixed_regression_bugs += len(matched_gt_ids)
    stats.fixed_rest_bugs += len(no_expectation_entries)

    if not matched_gt_ids:
        eprint_succ(
            where,
            f"No regression bugs detected (0 out of {len(all_gt_ids)} expected missing bugs)",
        )
    else:
        eprint_fail(
            where,
            f"{len(matched_gt_ids)} out of {len(all_gt_ids)} expected missing bugs were detected (regression)",
        )
        for entry in job.mapper_entries:
            if entry.gt_id is not None:
                eprint_fail(
                    where,
                    f"Bug '{entry.gt_id}' ({entry.gt_code}) was detected but expected to be missing",
                )

    if no_expectation_entries:
        eprint_warn(
            where,
            f"{len(no_expectation_entries)} additional bugs detected (no ground truth expectation)",
        )

    job.additional_info = {
        "expected": all_gt,
        "actual": [
            ReportEntry(
                e.llm_code,
                e.llm_line,
                None,
            )
            for e in job.mapper_entries
            if e.gt_id is not None
        ],
        "detected_all": len(matched_gt_ids) == 0,
        "kind": job.ground_truth["kind"],
    }


def run_job(
    job: Job,
    verbose: bool,
    sash_kwargs: dict,
) -> FinishedJob:
    where = job.benchmark.relative_to(ROOT_DIR)
    eprint_info(where, "Running analysis")
    finished: FinishedJob
    try:
        sash.reporter.Reporter.reset()  # I'm not sure this is needed, but just in case

        report = sash.main.main(
            job.benchmark,
            **{k: v for k, v in sash_kwargs.items() if v is not None},
            log_level="DISABLED",
            collect_debug_info=False,
        )

        finished = FinishedJob(
            benchmark=job.benchmark,
            ground_truth=job.ground_truth,
            timed_out=report.timed_out,
            crashed=False,
            report=report,
        )

    except (AssertionError, BaseException) as e:
        if isinstance(e, KeyboardInterrupt):
            raise e  # Re-raise keyboard interrupts

        exn_traceback = traceback.format_exc() if verbose else None

        finished = FinishedJob(
            benchmark=job.benchmark,
            ground_truth=job.ground_truth,
            timed_out=False,
            crashed=True,
            exn_traceback=exn_traceback,
            report=None,
        )

    return finished


# LLM mode globals (set per worker in multiprocessing pool initializer)
LLM_PROVIDER: llm.LLMProvider | None = None
LLM_PROMPT_TEMPLATE: str = ""
LLM_CODES_CATALOG: str = ""
LLM_MAPPER_PROVIDER: llm.LLMProvider | None = None
LLM_VALID_CODES: set[str] = set()


def _llm_worker_init(
    provider_spec: str,
    api_key: str | None,
    base_url: str | None,
    temperature: float,
    max_tokens: int | None,
    timeout: float | None,
    mapper_spec: str | None,
    prompt_template: str,
    codes_catalog: str,
    valid_codes: list[str],
):
    global LLM_PROVIDER, LLM_PROMPT_TEMPLATE, LLM_CODES_CATALOG, LLM_MAPPER_PROVIDER, LLM_VALID_CODES
    LLM_PROVIDER = llm.create_provider(
        provider_spec,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    if mapper_spec:
        LLM_MAPPER_PROVIDER = llm.create_provider(
            mapper_spec,
            api_key=api_key,
            base_url=base_url,
            temperature=-1.0,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    else:
        LLM_MAPPER_PROVIDER = None
    LLM_PROMPT_TEMPLATE = prompt_template
    LLM_CODES_CATALOG = codes_catalog
    LLM_VALID_CODES = set(valid_codes)


def run_llm_job(job: Job, verbose: bool) -> dict:
    where = job.benchmark.relative_to(ROOT_DIR)
    eprint_info(where, "Running LLM analysis")

    try:
        script_content = job.benchmark.read_text(encoding="utf-8")

        prompt = llm.render_prompt(
            LLM_PROMPT_TEMPLATE, script_content, LLM_CODES_CATALOG
        )

        assert LLM_PROVIDER is not None
        response = LLM_PROVIDER.generate(prompt)

        issues = llm.parse_analysis_response(response.text, LLM_VALID_CODES)

        mapper_entries = None
        mapper_tokens_in = 0
        mapper_tokens_out = 0
        mapper_cost = None

        if LLM_MAPPER_PROVIDER is not None:
            mapper_prompt = llm.build_mapper_prompt(
                script_content, job.ground_truth, issues
            )
            mapper_response = LLM_MAPPER_PROVIDER.generate(mapper_prompt)
            mapper_entries, mapper_json = llm.parse_mapper_response(mapper_response.text)
            mapper_tokens_in = mapper_response.tokens_in
            mapper_tokens_out = mapper_response.tokens_out
            mapper_cost = mapper_response.cost_usd

        return {
            "benchmark": str(job.benchmark),
            "ground_truth": job.ground_truth,
            "timed_out": False,
            "crashed": False,
            "exn_traceback": None,
            "issues": [
                {
                    "code": issue.code,
                    "line": issue.line,
                    "description": issue.description or issue.code,
                }
                for issue in issues
            ],
            "time": response.time_sec,
            "tokens_in": response.tokens_in,
            "tokens_out": response.tokens_out,
            "cost": response.cost_usd,
            "raw_llm_output": response.text,
            "mapper_entries": (
                [
                    {
                        "llm_code": e.llm_code,
                        "llm_line": e.llm_line,
                        "llm_description": e.llm_description,
                        "gt_id": e.gt_id,
                        "gt_code": e.gt_code,
                        "gt_line": e.gt_line,
                    }
                    for e in mapper_entries
                ]
                if mapper_entries is not None
                else None
            ),
            "mapper_json": mapper_json if mapper_entries is not None else None,
            "mapper_tokens_in": mapper_tokens_in,
            "mapper_tokens_out": mapper_tokens_out,
            "mapper_cost": mapper_cost,
        }

    except (AssertionError, BaseException) as e:
        if isinstance(e, KeyboardInterrupt):
            raise e

        exn_traceback = traceback.format_exc() if verbose else None

        return {
            "benchmark": str(job.benchmark),
            "ground_truth": job.ground_truth,
            "timed_out": False,
            "crashed": True,
            "exn_traceback": exn_traceback,
            "issues": [],
            "time": 0.0,
            "tokens_in": 0,
            "tokens_out": 0,
            "cost": None,
            "raw_llm_output": None,
            "mapper_entries": None,
            "mapper_json": None,
            "mapper_tokens_in": 0,
            "mapper_tokens_out": 0,
            "mapper_cost": None,
        }


def _build_finished_job(result: dict) -> FinishedJob:
    issues = result["issues"]
    mapper_entries = result["mapper_entries"]

    report = None
    if not result["crashed"]:
        report_issues = [
            llm.make_issue(i["code"], i["line"], i["description"]) for i in issues
        ]
        report = sash.reporter.Report(
            filename=result["benchmark"],
            issues=report_issues,
            time=result["time"],
            solver_time=0.0,
            timed_out=result["timed_out"],
            ast_nodes_total=0,
            ast_nodes_interpreted=0,
            ast_coverage_pct=0.0,
        )

    parsed_mapper_entries = None
    if mapper_entries is not None:
        parsed_mapper_entries = [
            llm.MapperEntry(
                llm_code=e["llm_code"],
                llm_line=e["llm_line"],
                llm_description=e["llm_description"],
                gt_id=e["gt_id"],
                gt_code=e["gt_code"],
                gt_line=e["gt_line"],
            )
            for e in mapper_entries
        ]

    return FinishedJob(
        benchmark=Path(result["benchmark"]),
        ground_truth=result["ground_truth"],
        timed_out=result["timed_out"],
        crashed=result["crashed"],
        exn_traceback=result["exn_traceback"],
        report=report,
        tokens_in=result["tokens_in"],
        tokens_out=result["tokens_out"],
        cost=result["cost"],
        raw_llm_output=result.get("raw_llm_output"),
        mapper_entries=parsed_mapper_entries,
        mapper_tokens_in=result["mapper_tokens_in"],
        mapper_tokens_out=result["mapper_tokens_out"],
        mapper_cost=result["mapper_cost"],
        mapper_json=result.get("mapper_json"),
    )


def prepare_jobs(
    benchmark_dir: Path,
    stats: EvalStats,
    oos_codes: set[str],
    eval_buggy,
    eval_fixed,
    eval_variants,
    eval_only_variants: bool,
    verbose: bool = False,
    use_original: bool = False,
) -> list[Job]:
    all_codes = sash.reporter.Issue.all_codes()
    where = benchmark_dir.relative_to(ROOT_DIR)
    stats.benchmarks += 1

    info = load_info(benchmark_dir)
    if info is None:
        eprint_fail(where, f"Skipping evaluation due to missing '{INFO_FILENAME}'")
        stats.skipped += 1
        return []

    val_errs = validate_benchmark(benchmark_dir, info)
    if len(val_errs) > 0:
        eprint_fail(
            where, f"Skipping evaluation due to '{INFO_FILENAME}' validation errors"
        )
        if verbose:
            for err in val_errs:
                eprint_fail(where, f"{err}")
        stats.skipped += 1
        return []

    # Info exists and is valid

    bugs = {}
    for bug_id, bug_info in info["bugs"].items():
        if bug_info["code"] in oos_codes:
            eprint_info(
                where,
                f"Skipping bug '{bug_id}' (code '{bug_info['code']}' is out of scope)",
            )
            continue

        if bug_info["code"] not in all_codes:
            eprint_fail(
                where,
                f"Skipping bug '{bug_id}' (code '{bug_info['code']}' is not a recognized code)",
            )
            continue

        bugs[bug_id] = bug_info

    eval_kinds = []
    buggy_kind = "original" if use_original else "buggy"
    if eval_only_variants:
        if eval_buggy:
            eval_kinds.append("buggy_variant")
        if eval_fixed:
            eval_kinds.append("fixed_variant")
    else:
        if eval_buggy:
            eval_kinds.append(buggy_kind)
        if eval_fixed:
            eval_kinds.append("fixed")
        if eval_variants and eval_buggy:
            eval_kinds.append("buggy_variant")
        if eval_variants and eval_fixed:
            eval_kinds.append("fixed_variant")

    jobs = []
    for gt in info["ground_truths"]:
        if gt["kind"] not in eval_kinds:
            continue

        path = benchmark_dir / gt["path"]
        if not path.exists():
            eprint_fail(where, f"File '{gt['path']}' does not exist")
            continue

        # Call list() here to be able to modify the dict while iterating
        for bug_id in list(gt["bugs"].keys()):
            if bug_id not in bugs:
                # The bug is out of scope; delete it from ground truth
                del gt["bugs"][bug_id]
                continue

            # Enrich ground truth with bug info for easier access later
            gt["bugs"][bug_id]["code"] = bugs[bug_id]["code"]
            gt["bugs"][bug_id]["description"] = bugs[bug_id]["description"]
            gt["bugs"][bug_id]["shellcheck"] = bugs[bug_id]["shellcheck"]

        jobs.append(Job(benchmark=path, ground_truth=gt))

    stats.analyses += len(jobs)
    return jobs


# Validates the benchmark's info file against the schema
def validate_benchmark(benchmark_dir: Path, info: dict | None) -> list[str]:
    where = benchmark_dir.relative_to(ROOT_DIR)

    if info is None and (info := load_info(benchmark_dir)) is None:
        return []

    errors = []
    for e in VALIDATOR(schema=INFO_SCHEMA).iter_errors(info):
        path = ".".join([str(p) for p in e.path])
        if len(path) > 0:
            path = f"{BLUE}{path}{RESET}: "
        errors.append(f"{MAGENTA}{where}{RESET}: {path}{e.message}")

    if len(errors) > 0:
        eprint_fail(where, f"'{INFO_FILENAME}' has {len(errors)} validation errors")

    return sorted(errors, key=str)


def load_info(benchmark_dir: Path) -> dict | None:
    where = benchmark_dir.relative_to(ROOT_DIR)
    info_file = benchmark_dir / INFO_FILENAME

    if not info_file.exists():
        eprint_fail(where, f"Missing '{INFO_FILENAME}' file")
        return None

    with info_file.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_oos_codes(benchmarks_dir: Path) -> set[str]:
    oos_file = benchmarks_dir / "codes_out_of_scope.yaml"
    res = set()
    if oos_file.exists():
        with oos_file.open("r", encoding="utf-8") as f:
            oos_codes = yaml.safe_load(f)
            res.update(oos_codes)
    return res


def git_toplevel() -> Path:
    return Path(
        subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], encoding="utf-8"
        ).strip()
    )


def load_env_file():
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        return
    with env_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if "#" in value:
                value = value.split("#", 1)[0].strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


def eprint_succ(where: Path, msg: str):
    eprint(f"[{GREEN}{where}{RESET}] {msg}")


def eprint_fail(where: Path, msg: str):
    eprint(f"[{RED}{where}{RESET}] {msg}")


def eprint_warn(where: Path, msg: str):
    eprint(f"[{YELLOW}{where}{RESET}] {msg}")


def eprint_info(where: Path, msg: str):
    eprint(f"[{CYAN}{where}{RESET}] {msg}")


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def disable_color():
    global MAGENTA, BLUE, CYAN, GREEN, YELLOW, RED, RESET, BOLD, UNDERLINE
    MAGENTA = ""
    BLUE = ""
    CYAN = ""
    GREEN = ""
    YELLOW = ""
    RED = ""
    RESET = ""
    BOLD = ""
    UNDERLINE = ""


def _init_worker(no_color: bool) -> None:
    if no_color:
        disable_color()


# A RunResult is the format that the functions to export to CSV and HTML expect
def job_to_run_result(job: FinishedJob) -> report.RunResult:
    if job.report is None:
        return report.RunResult(
            benchmark=job.benchmark.as_posix(),
            kind="unknown",
            missing_gt=False,
            crashed=job.crashed,
            timed_out=job.timed_out,
            time=None,
            exec_time=None,
            solver_time=None,
            detected_all=False,
            expected_results=None,
            actual_results=None,
            shellcheck_codes=None,
            line_numbers=None,
            ast_nodes_total=None,
            ast_nodes_interpreted=None,
            ast_coverage_pct=None,
        )

    assert job.additional_info is not None
    assert "expected" in job.additional_info  # list[ReportInfo]
    assert "actual" in job.additional_info  # list[ReportInfo]
    assert "detected_all" in job.additional_info  # bool
    assert "kind" in job.additional_info  # str

    expected_results = [
        f"L{j.line}:{j.sash_code}" for j in job.additional_info["expected"]
    ]
    actual_results = [f"L{j.line}:{j.sash_code}" for j in job.additional_info["actual"]]
    shellcheck_codes = [j.shellcheck_code for j in job.additional_info["expected"]]
    line_numbers = [j.line for j in job.additional_info["actual"]]

    return report.RunResult(
        benchmark=job.benchmark.as_posix(),
        kind=job.additional_info["kind"],
        missing_gt=False,
        crashed=job.crashed,
        timed_out=job.timed_out,
        time=job.report.time + job.report.solver_time,
        exec_time=job.report.time,
        solver_time=job.report.solver_time,
        detected_all=job.additional_info["detected_all"],
        expected_results=expected_results,
        actual_results=actual_results,
        shellcheck_codes=shellcheck_codes,
        line_numbers=line_numbers,
        ast_nodes_total=job.report.ast_nodes_total,
        ast_nodes_interpreted=job.report.ast_nodes_interpreted,
        ast_coverage_pct=job.report.ast_coverage_pct,
    )


def export_as_csv(
    file: Path,
    jobs: list[FinishedJob],
):
    run_results = [job_to_run_result(job) for job in jobs]
    with file.open("w") as csvfile:
        csvfile.write(f"{','.join(report.RunResult._fields)}" + "\n")
        for r in run_results:
            csvfile.write(
                f"{r.benchmark},"
                f"{r.kind},"
                f"{r.missing_gt},"
                f"{r.crashed},"
                f"{r.timed_out},"
                f"{r.time},"
                f"{r.exec_time},"
                f"{r.solver_time},"
                f"{r.detected_all},"
                f"{';'.join([e if e is not None else '' for e in r.expected_results]) if r.expected_results else ''},"
                f"{';'.join([a if a is not None else '' for a in r.actual_results]) if r.actual_results else ''},"
                f"{';'.join([c if c is not None else '' for c in r.shellcheck_codes]) if r.shellcheck_codes else ''},"
                f"{';'.join(str(line) if line is not None else '' for line in r.line_numbers) if r.line_numbers else ''},"
                f"{r.ast_nodes_total if r.ast_nodes_total is not None else ''},"
                f"{r.ast_nodes_interpreted if r.ast_nodes_interpreted is not None else ''},"
                f"{r.ast_coverage_pct if r.ast_coverage_pct is not None else ''}"
                "\n"
            )

    eprint(f"CSV report generated: {file.name}")


def generate_html_report(
    filename: Path,
    stats: EvalStats,
    jobs: list[FinishedJob],
):
    run_results = [job_to_run_result(job) for job in jobs]
    ran = stats.analyses
    skipped = stats.skipped
    failed = stats.crashed
    unknown = 0
    timed_out = stats.timed_out
    total_issues = stats.buggy_expected_bugs
    detected_issues_expected = stats.buggy_detected_bugs
    detected_issues_extra = stats.buggy_unexpected_bugs
    detected_issues_extra_unsat_preconds = 0  # Not tracked currently
    detected_issues_extra_unset_vars = 0  # Not tracked currently
    total_exec_time = stats.exec_time
    total_solver_time = stats.solver_time

    report.generate_html_report(
        html_file=filename,
        run_results=run_results,
        ran=ran,
        skipped=skipped,
        failed=failed,
        unknown=unknown,
        timed_out=timed_out,
        total_issues=total_issues,
        detected_issues_expected=detected_issues_expected,
        detected_issues_extra=detected_issues_extra,
        detected_issues_extra_unsat_preconds=detected_issues_extra_unsat_preconds,
        detected_issues_extra_unset_vars=detected_issues_extra_unset_vars,
        total_exec_time=total_exec_time,
        total_solver_time=total_solver_time,
    )


def export_llm_csv(file: Path, jobs: list[FinishedJob]):
    fieldnames = [
        "benchmark",
        "kind",
        "crashed",
        "timed_out",
        "detected_all",
        "expected_results",
        "actual_results",
        "llm_time",
        "tokens_in",
        "tokens_out",
        "cost",
        "mapper_tokens_in",
        "mapper_tokens_out",
        "mapper_cost",
        "raw_llm_output",
        "mapper_output",
    ]
    with file.open("w", newline="", encoding="utf-8") as csvfile:
        writer = csv_module.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for job in jobs:
            if job.additional_info and "expected" in job.additional_info:
                expected = [
                    f"L{e.line}:{e.sash_code}"
                    for e in job.additional_info["expected"]
                ]
                actual = [
                    f"L{e.line}:{e.sash_code}"
                    for e in job.additional_info["actual"]
                ]
                detected_all = job.additional_info.get("detected_all", False)
                kind = job.additional_info.get("kind", "unknown")
            else:
                expected = []
                actual = []
                detected_all = False
                kind = "unknown"

            mapper_output = None
            if job.mapper_entries is not None:
                mapper_output = json_module.dumps(
                    [
                        {
                            "llm_code": e.llm_code,
                            "llm_line": e.llm_line,
                            "llm_description": e.llm_description,
                            "gt_id": e.gt_id,
                            "gt_code": e.gt_code,
                            "gt_line": e.gt_line,
                        }
                        for e in job.mapper_entries
                    ]
                )

            writer.writerow(
                {
                    "benchmark": job.benchmark.as_posix(),
                    "kind": kind,
                    "crashed": job.crashed,
                    "timed_out": job.timed_out,
                    "detected_all": detected_all,
                    "expected_results": ";".join(expected) if expected else "",
                    "actual_results": ";".join(actual) if actual else "",
                    "llm_time": job.report.time if job.report else "",
                    "tokens_in": job.tokens_in,
                    "tokens_out": job.tokens_out,
                    "cost": job.cost if job.cost is not None else "",
                    "mapper_tokens_in": job.mapper_tokens_in,
                    "mapper_tokens_out": job.mapper_tokens_out,
                    "mapper_cost": job.mapper_cost if job.mapper_cost is not None else "",
                    "raw_llm_output": job.raw_llm_output or "",
                    "mapper_output": mapper_output or "",
                }
            )


def write_jsonl_log(
    jsonl_file: Path,
    description: str | None,
    model: str,
    mapper_model: str | None,
    temperature: float,
    benchmark_filter: str,
    stats: EvalStats,
    jobs: list[FinishedJob],
    duration_sec: float,
    args: dict,
):
    jsonl_file.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "description": description,
        "model": model,
        "mapper_model": mapper_model,
        "temperature": temperature if temperature >= 0 else None,
        "benchmark_filter": benchmark_filter,
        "num_analyses": stats.analyses,
        "num_successful": stats.successful,
        "num_crashed": stats.crashed,
        "num_timed_out": stats.timed_out,
        "expected_bugs": stats.buggy_expected_bugs,
        "detected_bugs": stats.buggy_detected_bugs,
        "undetected_bugs": stats.buggy_undetected_bugs,
        "unexpected_bugs": stats.buggy_unexpected_bugs,
        "fixed_expected_missing": stats.fixed_expected_missing_bugs,
        "fixed_regressions": stats.fixed_regression_bugs,
        "fixed_rest": stats.fixed_rest_bugs,
        "tokens_in": stats.total_tokens_in,
        "tokens_out": stats.total_tokens_out,
        "mapping_tokens_in": stats.total_mapper_tokens_in,
        "mapping_tokens_out": stats.total_mapper_tokens_out,
        "cost_usd": stats.total_cost if stats.total_cost > 0 else None,
        "mapping_cost_usd": stats.total_mapper_cost if stats.total_mapper_cost > 0 else None,
        "duration_sec": duration_sec,
        "args": args,
    }
    with jsonl_file.open("a", encoding="utf-8") as f:
        f.write(json_module.dumps(entry, default=str) + "\n")

    eprint(f"HTML report generated: {filename}")


ROOT_DIR = git_toplevel()
INFO_FILENAME = "info.yaml"
VALIDATOR = jsonschema.Draft202012Validator  # Must match the $schema in INFO_SCHEMA
INFO_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["sources", "bugs", "ground_truths"],
    "properties": {
        "description": {"type": "string"},
        "sources": {
            "type": "array",
            "items": {"type": "string", "format": "uri"},
            "minItems": 1,
        },
        "notes": {"type": "array", "items": {"type": "string"}},
        "bugs": {
            "type": "object",
            "patternProperties": {
                "^bug[0-9]{2}$": {
                    "type": "object",
                    "required": ["description", "code", "shellcheck"],
                    "properties": {
                        "description": {"type": "string"},
                        "code": {"type": "string"},
                        "shellcheck": {
                            "type": ["string", "null"],
                            "pattern": "^SC[0-9]{4}$",
                        },
                        "notes": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
        "ground_truths": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "kind", "bugs"],
                "properties": {
                    "path": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": [
                            "original",
                            "buggy",
                            "fixed",
                            "buggy_variant",
                            "fixed_variant",
                        ],
                    },
                    "bugs": {
                        "type": "object",
                        "patternProperties": {
                            "^bug[0-9]{2}$": {
                                "type": "object",
                                "oneOf": [
                                    {"required": ["lines"]},
                                    {"required": ["regression_lines"]},
                                ],
                                "properties": {
                                    "lines": {
                                        "type": "array",
                                        "items": {"type": "integer"},
                                        "minItems": 1,
                                    },
                                    "regression_lines": {
                                        "type": "array",
                                        "items": {"type": "integer"},
                                        "minItems": 1,
                                    },
                                    "shellcheck": {
                                        "type": ["string", "null"],
                                        "pattern": "^SC[0-9]{4}$",
                                    },
                                    "notes": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                                "additionalProperties": False,
                            },
                        },
                        "additionalProperties": False,
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


def write_llm_report(
    description: str | None,
    model: str,
    mapper_model: str | None,
    temperature: float,
    benchmark_filter: str,
    stats: EvalStats,
    jobs: list[FinishedJob],
    duration_sec: float,
    args: dict,
):
    timestamp = datetime.now(timezone.utc)
    report_dir = ROOT_DIR / "results" / "llm"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_file = report_dir / f"{timestamp.strftime('%Y-%m-%dT%H%M%S')}.json"

    def _compute_job_bugs(job: FinishedJob):
        info = job.additional_info or {}
        expected = [f"L{e.line}:{e.sash_code}" for e in info.get("expected", [])]
        actual = [f"L{e.line}:{e.sash_code}" for e in info.get("actual", [])]
        expected_set = set(expected)
        actual_set = set(actual)
        kind = info.get("kind", "unknown")

        if kind.startswith("fixed"):
            return {
                "expected_missing": expected,
                "regressions": list(actual_set & expected_set),
                "no_expectation": list(actual_set - expected_set),
            }
        else:
            return {
                "expected": expected,
                "detected": list(actual_set & expected_set),
                "undetected": list(expected_set - actual_set),
                "unexpected": list(actual_set - expected_set),
            }

    jobs_data = []
    for job in jobs:
        entry = {
            "benchmark": job.benchmark.as_posix(),
            "kind": (job.additional_info or {}).get("kind", "unknown"),
            "crashed": job.crashed,
            "timed_out": job.timed_out,
            "detected_all": (job.additional_info or {}).get("detected_all", False),
            "bugs": _compute_job_bugs(job),
            "time_sec": job.report.time if job.report else None,
            "tokens_in": job.tokens_in,
            "tokens_out": job.tokens_out,
            "cost": job.cost,
            "raw_llm_output": job.raw_llm_output,
        }
        if job.mapper_entries is not None:
            entry["mapper_json"] = job.mapper_json
            entry["mapper_tokens_in"] = job.mapper_tokens_in
            entry["mapper_tokens_out"] = job.mapper_tokens_out
            entry["mapper_cost"] = job.mapper_cost
        jobs_data.append(entry)

    report = {
        "timestamp": timestamp.isoformat(),
        "description": description,
        "model": model,
        "mapper_model": mapper_model,
        "temperature": temperature if temperature >= 0 else None,
        "benchmark_filter": benchmark_filter,
        "args": args,
        "stats": {
            "num_analyses": stats.analyses,
            "num_successful": stats.successful,
            "num_crashed": stats.crashed,
            "num_timed_out": stats.timed_out,
            "expected_bugs": stats.buggy_expected_bugs,
            "detected_bugs": stats.buggy_detected_bugs,
            "undetected_bugs": stats.buggy_undetected_bugs,
            "unexpected_bugs": stats.buggy_unexpected_bugs,
            "fixed_expected_missing": stats.fixed_expected_missing_bugs,
            "fixed_regressions": stats.fixed_regression_bugs,
            "fixed_rest": stats.fixed_rest_bugs,
            "tokens_in": stats.total_tokens_in,
            "tokens_out": stats.total_tokens_out,
            "mapping_tokens_in": stats.total_mapper_tokens_in,
            "mapping_tokens_out": stats.total_mapper_tokens_out,
            "cost_usd": stats.total_cost if stats.total_cost > 0 else None,
            "mapping_cost_usd": stats.total_mapper_cost if stats.total_mapper_cost > 0 else None,
            "duration_sec": duration_sec,
        },
        "jobs": jobs_data,
    }

    report_file.write_text(json_module.dumps(report, indent=2, default=str), encoding="utf-8")
    eprint(f"LLM report written to: {report_file}")


# ANSI color codes
MAGENTA = "\033[95m"
BLUE = "\033[94m"
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"
BOLD = "\033[1m"
UNDERLINE = "\033[4m"


if __name__ == "__main__":
    args = build_cli().parse_args()

    load_env_file()

    llm_prompt = args.llm_prompt
    if llm_prompt is None and args.llm is not None:
        llm_prompt = ROOT_DIR / "scripts" / "eval_llm_prompt.md"

    jsonl_output = args.jsonl_output
    if jsonl_output is None and args.llm is not None:
        jsonl_output = ROOT_DIR / "results" / "llm_stats.jsonl"

    llm_timeout = args.llm_timeout if args.llm_timeout > 0 else None

    SASH_KWARGS = {
        "timeout": args.timeout,
        "exec_timeout_pct": args.exec_timeout_pct,
        "dfs_timeout_pct": args.dfs_timeout_pct,
        "targeted_dfs_timeout_pct": args.targeted_dfs_timeout_pct,
        "disable_optimistic_forking": args.disable_optimistic_forking,
        "disable_trace_collapsing": args.disable_trace_collapsing,
        "disable_targeted_dfs": args.disable_dfs or args.disable_targeted_dfs,
        "disable_unbound_as_empty_dfs": args.disable_dfs or args.disable_unbound_as_empty_dfs,
        "disable_solver": args.disable_solver,
        "disable_solver_optimizations": args.disable_solver_optimizations,
    }

    only_pattern = args.only
    if not only_pattern.startswith("/"):
        only_pattern = ".*" + only_pattern

    main(
        benchmarks_dir=args.benchmarks,
        bench_filter=re.compile(only_pattern),
        run_buggy=not args.skip_buggy or args.all,
        run_fixed=args.fixed or args.all,
        run_variants=args.variants or args.all,
        run_only_variants=args.variants_only,
        csv_file=args.csv,
        html_file=args.html,
        verbose=args.verbose,
        no_color=args.no_color,
        num_jobs=max(args.jobs, 0),
        llm_spec=args.llm,
        llm_prompt=llm_prompt,
        llm_base_url=args.llm_base_url,
        llm_api_key=args.llm_api_key,
        llm_temperature=args.llm_temperature,
        llm_max_tokens=args.llm_max_tokens,
        llm_timeout=llm_timeout,
        llm_mapper_spec=args.llm_mapper,
        description=args.description,
        jsonl_output=jsonl_output,
    )

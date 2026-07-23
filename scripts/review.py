#!/usr/bin/env -S uv run python3
"""Interactive review tool for LLM evaluation reports.

Usage:
    uv run python scripts/review.py results/llm/<timestamp>.json
"""

import json
import yaml
from datetime import datetime, timezone
from pathlib import Path
import sys

from rich.syntax import Syntax as RichSyntax

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, Container
from textual.screen import Screen
from textual.widgets import Header, Footer, Static, Label, Input, Button, RichLog, TextArea


class CustomNoteScreen(Screen):
    """Modal screen for entering a custom review note."""

    DEFAULT_CSS = """
    CustomNoteScreen {
        align: center middle;
    }
    #note-container {
        width: 50;
        padding: 1 2;
        border: solid $accent;
        background: $surface;
    }
    #note-input {
        margin: 1 0;
    }
    """

    def compose(self) -> ComposeResult:
        with Container(id="note-container"):
            yield Label("Enter custom note for this job:")
            yield Input(placeholder="custom note...", id="note-input")
            with Horizontal():
                yield Button("Save", variant="primary", id="save-btn")
                yield Button("Cancel", id="cancel-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-btn":
            note = self.query_one("#note-input", Input).value.strip()
            self.dismiss(note if note else "custom")
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        note = event.value.strip()
        self.dismiss(note if note else "custom")


class ReviewApp(App):
    """Interactive review of LLM evaluation results."""

    BINDINGS = [
        Binding("q", "quit", "Save & quit", show=False),
        Binding("ctrl+q", "quit_no_save", "Quit without saving", show=False),
        Binding("n,right", "next_job", "Next job", show=False),
        Binding("p,left", "prev_job", "Prev job", show=False),
        Binding("f", "mark_all", "All bugs found", show=False),
        Binding("x", "mark_none", "No bugs found", show=False),
        Binding("m", "mark_custom", "Custom note", show=False),
    ]

    CSS = """
    #panels {
        height: 1fr;
    }

    .panel {
        border: solid $primary-darken-2;
        margin: 0 1;
        height: 1fr;
    }

    .panel:focus {
        border: solid $accent;
    }

    #llm-panel, #gt-panel {
        height: 1fr;
    }

    #script-panel {
        width: 1fr;
    }

    #status-bar {
        height: auto;
        min-height: 2;
        padding: 0 2;
        background: $panel;
        border-top: solid $primary;
    }

    #controls {
        height: auto;
        min-height: 1;
        padding: 0 2;
        background: $surface;
        color: $text-muted;
    }
    """

    def __init__(self, report_path: Path) -> None:
        super().__init__()
        self._report_path = report_path
        self._progress_path = report_path.with_name(
            report_path.stem + ".review.json"
        )
        self._report = json.loads(report_path.read_text(encoding="utf-8"))
        self._jobs = self._report.get("jobs", [])
        self._current_idx = 0
        self._reviewed: dict[str, dict] = {}
        self._load_progress()

    def _load_progress(self) -> None:
        self._original_progress_text: str | None = None
        if self._progress_path.exists():
            raw = self._progress_path.read_text(encoding="utf-8")
            self._original_progress_text = raw
            data = json.loads(raw)
            self._reviewed = data.get("reviewed", {})
            saved_idx = data.get("current_index", 0)
            if saved_idx < len(self._jobs):
                self._current_idx = saved_idx

    def _save_progress(self) -> None:
        data = {
            "report_path": str(self._report_path),
            "reviewed": dict(sorted(self._reviewed.items())),
            "current_index": self._current_idx,
        }
        self._progress_path.parent.mkdir(parents=True, exist_ok=True)
        self._progress_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="panels"):
            with Vertical(classes="panel"):
                yield TextArea(id="llm-panel", read_only=True, soft_wrap=True)
                yield TextArea(id="gt-panel", read_only=True, soft_wrap=True)
            yield RichLog(id="script-panel", classes="panel", highlight=True, markup=False, wrap=False)

        yield Static("", id="status-bar")
        yield Static("", id="controls")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#llm-panel", TextArea).border_title = "LLM Output"
        self.query_one("#gt-panel", TextArea).border_title = "Ground Truth"
        self.query_one("#script-panel", RichLog).border_title = "Script"
        self._refresh()

    def _current_job(self) -> dict | None:
        if not self._jobs:
            return None
        return self._jobs[self._current_idx]

    def _job_id(self, job: dict) -> str:
        return job.get("benchmark", "")

    def _refresh(self) -> None:
        job = self._current_job()
        if job is None:
            for pid in ("#llm-panel", "#gt-panel"):
                self.query_one(pid, TextArea).load_text("")
            self.query_one("#script-panel", RichLog).clear()
            self.query_one("#status-bar", Static).update("")
            return

        self._show_llm_output(job)
        self._show_ground_truth(job)
        self._show_script(job)
        self._show_status(job)
        self._show_controls()

    def _show_llm_output(self, job: dict) -> None:
        ta = self.query_one("#llm-panel", TextArea)
        lines = job.get("raw_llm_output_lines", [])
        if not lines:
            ta.load_text("(no output)")
            return
        result = []
        for line in lines:
            result.append(line)
            result.append("")
        ta.load_text("\n".join(result))

    def _show_ground_truth(self, job: dict) -> None:
        ta = self.query_one("#gt-panel", TextArea)
        benchmark_path = Path(job["benchmark"])
        info_path = benchmark_path.parent / "info.yaml"
        if not info_path.exists():
            ta.load_text("(info.yaml not found)")
            return

        info = yaml.safe_load(info_path.read_text(encoding="utf-8"))

        job_filename = benchmark_path.name
        matched_gt = None
        for gt in info.get("ground_truths", []):
            if Path(gt["path"]).name == job_filename and gt["kind"] == job.get("kind"):
                matched_gt = gt
                break
        if matched_gt is None:
            for gt in info.get("ground_truths", []):
                if Path(gt["path"]).name == job_filename:
                    matched_gt = gt
                    break

        keep_bugs = set(matched_gt.get("bugs", {}).keys()) if matched_gt else set()
        filtered_bugs = {
            bid: binfo for bid, binfo in info.get("bugs", {}).items()
            if bid in keep_bugs
        }

        display: dict = {}
        if info.get("sources"):
            display["sources"] = info["sources"]
        if info.get("notes"):
            display["notes"] = info["notes"]
        if filtered_bugs:
            display["bugs"] = filtered_bugs
        if matched_gt:
            display["ground_truth"] = {
                "path": matched_gt["path"],
                "kind": matched_gt["kind"],
                "bugs": matched_gt["bugs"],
            }

        text = yaml.dump(display, allow_unicode=True, sort_keys=False, default_flow_style=False)
        ta.load_text(text)

    def _show_script(self, job: dict) -> None:
        rlog = self.query_one("#script-panel", RichLog)
        rlog.clear()
        benchmark_path = Path(job["benchmark"])
        if benchmark_path.exists():
            script_text = benchmark_path.read_text(encoding="utf-8")
            rlog.write(RichSyntax(script_text, "bash", theme="monokai", line_numbers=True))
        else:
            rlog.write("(script file not found on disk)")

    def _show_status(self, job: dict) -> None:
        job_id = self._job_id(job)
        total = len(self._jobs)
        benchmark_path = Path(job["benchmark"])
        self.title = benchmark_path.parent.name

        status = (
            f"[bold]Job {self._current_idx + 1}/{total}[/]  |  "
            f"Kind: [bold]{job.get('kind', '?')}[/]  |  "
            f"Crashed: [bold]{'yes' if job.get('crashed') else 'no'}[/]"
        )

        if job_id in self._reviewed:
            status += f"  |  ✓ Marked: [bold green]{self._reviewed[job_id]['status']}[/]"
        else:
            status += "  |  [italic]Not reviewed[/]"

        self.query_one("#status-bar", Static).update(status)

    def _show_controls(self) -> None:
        self.query_one("#controls", Static).update(
            "[b]n[/]/[b]p[/] navigate   "
            "[b]f[/] all found   "
            "[b]x[/] none found   "
            "[b]m[/] custom note   "
            "[b]Tab[/] switch panel   "
            "[b]arrows[/] scroll   "
            "[b]q[/] save & quit   "
            "[b]ctrl+q[/] quit without saving"
        )

    def _mark(self, status: str) -> None:
        job = self._current_job()
        if job is None:
            return
        self._reviewed[self._job_id(job)] = {
            "status": status,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_progress()
        self._refresh()

    def action_next_job(self) -> None:
        if self._current_idx < len(self._jobs) - 1:
            self._current_idx += 1
            self._save_progress()
            self._refresh()

    def action_prev_job(self) -> None:
        if self._current_idx > 0:
            self._current_idx -= 1
            self._save_progress()
            self._refresh()

    def action_quit_no_save(self) -> None:
        if self._original_progress_text is not None:
            self._progress_path.parent.mkdir(parents=True, exist_ok=True)
            self._progress_path.write_text(self._original_progress_text, encoding="utf-8")
        elif self._progress_path.exists():
            self._progress_path.unlink()
        self.exit()

    def action_mark_all(self) -> None:
        self._mark("all")

    def action_mark_none(self) -> None:
        self._mark("none")

    def action_mark_custom(self) -> None:
        self.push_screen(CustomNoteScreen(), self._on_custom_note)

    def _on_custom_note(self, note: str | None) -> None:
        if note is not None:
            self._mark(note)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: uv run python scripts/review.py <report.json>")
        sys.exit(1)

    report_path = Path(sys.argv[1])
    if not report_path.exists():
        print(f"Report not found: {report_path}")
        sys.exit(1)

    app = ReviewApp(report_path)
    app.run()


if __name__ == "__main__":
    main()

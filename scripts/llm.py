import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import sash.reporter


@dataclass
class LLMResponse:
    text: str
    tokens_in: int
    tokens_out: int
    cost_usd: float | None
    time_sec: float


class LLMProvider(ABC):
    @abstractmethod
    def generate(self, prompt: str) -> LLMResponse: ...


class OpenAIProvider(LLMProvider):
    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ):
        from openai import OpenAI

        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    def generate(self, prompt: str) -> LLMResponse:
        start = time.perf_counter()
        kwargs: dict = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self._temperature >= 0:
            kwargs["temperature"] = self._temperature
        if self._max_tokens is not None:
            kwargs["max_tokens"] = self._max_tokens
        if "5.6" in self._model:
            kwargs["prompt_cache_options"] = {"mode": "explicit"}
        response = self._client.chat.completions.create(**kwargs)
        elapsed = time.perf_counter() - start

        choice = response.choices[0]
        text = choice.message.content or ""

        usage = response.usage
        tokens_in = usage.prompt_tokens if usage else 0
        tokens_out = usage.completion_tokens if usage else 0

        return LLMResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=None,
            time_sec=elapsed,
        )


class AnthropicProvider(LLMProvider):
    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ):
        import anthropic

        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens or 4096
        self._timeout = timeout
        self._client = anthropic.Anthropic(
            api_key=api_key, base_url=base_url, timeout=timeout
        )

    def generate(self, prompt: str) -> LLMResponse:
        start = time.perf_counter()
        kwargs: dict = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self._temperature >= 0:
            kwargs["temperature"] = self._temperature
        response = self._client.messages.create(**kwargs)
        elapsed = time.perf_counter() - start

        text = ""
        for block in response.content:
            if block.type == "text":
                text += block.text

        usage = response.usage
        tokens_in = usage.input_tokens if usage else 0
        tokens_out = usage.output_tokens if usage else 0

        return LLMResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=None,
            time_sec=elapsed,
        )


def create_provider(
    spec: str,
    api_key: str | None = None,
    base_url: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    timeout: float | None = None,
    **kwargs,
) -> LLMProvider:
    if ":" not in spec:
        raise ValueError(
            f"Invalid provider:model spec '{spec}'. Expected format: 'provider:model'"
        )
    provider_name, model = spec.split(":", 1)

    if provider_name == "openai":
        return OpenAIProvider(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    elif provider_name == "anthropic":
        return AnthropicProvider(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    else:
        raise ValueError(
            f"Unknown provider '{provider_name}'. Supported: openai, anthropic"
        )


def render_prompt(template: str, script: str, codes_catalog: str) -> str:
    return template.replace("{script}", script).replace("{codes}", codes_catalog)


def build_codes_catalog() -> str:
    descriptions = sash.reporter.Issue.all_descriptions()
    errors = []
    warnings = []
    for code in sorted(descriptions):
        desc = descriptions[code]
        for subclass in sash.reporter.Issue.__subclasses__():
            if subclass.code == code:
                if subclass.severity == sash.reporter.Severity.ERROR:
                    errors.append(f"  {code}: {desc}")
                else:
                    warnings.append(f"  {code}: {desc}")
                break

    parts = []
    if errors:
        parts.append("ERRORS:")
        parts.extend(errors)
    if warnings:
        if parts:
            parts.append("")
        parts.append("WARNINGS:")
        parts.extend(warnings)
    return "\n".join(parts)


_ANALYSIS_LINE_RE = re.compile(r"L\s*(\d+)\s*:\s*(\w[\w-]*)\s*:\s*(.+)", re.IGNORECASE)
_ANALYSIS_LINE_NO_DESC_RE = re.compile(
    r"L\s*(\d+)\s*:\s*(\w[\w-]*)", re.IGNORECASE
)


@dataclass
class ParsedIssue:
    code: str
    line: int | None
    description: str


def parse_analysis_response(text: str, valid_codes: set[str]) -> list[ParsedIssue]:
    issues = []
    seen = set()
    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = _ANALYSIS_LINE_RE.match(line)
        if match:
            line_num = int(match.group(1))
            code = match.group(2)
            desc = match.group(3).strip()
        else:
            match = _ANALYSIS_LINE_NO_DESC_RE.match(line)
            if match:
                line_num = int(match.group(1))
                code = match.group(2)
                desc = ""
            else:
                continue

        if code not in valid_codes:
            continue

        key = (line_num, code)
        if key in seen:
            continue
        seen.add(key)

        issues.append(ParsedIssue(code=code, line=line_num, description=desc))

    return issues


@dataclass
class MapperEntry:
    llm_code: str
    llm_line: int | None
    llm_description: str
    gt_id: str | None
    gt_code: str | None
    gt_line: int | None


def parse_mapper_response(text: str) -> tuple[list[MapperEntry], dict]:
    json_match = re.search(r"\{[\s\S]*\"mappings\"[\s\S]*\}", text)
    if not json_match:
        raise ValueError("Could not find JSON object with 'mappings' in mapper response")

    try:
        data = json.loads(json_match.group(0))
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse mapper JSON: {e}")

    mappings = data.get("mappings", [])
    if not isinstance(mappings, list):
        raise ValueError("Mapper response 'mappings' is not a list")

    entries = []
    for m in mappings:
        llm_line_raw = m.get("llm_line")
        gt_line_raw = m.get("gt_line")

        entries.append(
            MapperEntry(
                llm_code=str(m.get("llm_code", "")),
                llm_line=int(llm_line_raw) if llm_line_raw is not None else None,
                llm_description=str(m.get("llm_description", "")),
                gt_id=m.get("gt_id"),
                gt_code=m.get("gt_code"),
                gt_line=int(gt_line_raw) if gt_line_raw is not None else None,
            )
        )

    return entries, data


def build_ground_truth_section(ground_truth: dict) -> str:
    issue_descriptions = sash.reporter.Issue.all_descriptions()
    lines = []
    for bug_id in sorted(ground_truth.get("bugs", {}).keys()):
        bug_info = ground_truth["bugs"][bug_id]
        code = bug_info.get("code", "unknown")
        specific_desc = bug_info.get("description", "")
        category = issue_descriptions.get(code, specific_desc)
        bug_lines = bug_info.get("lines", bug_info.get("regression_lines", []))
        lines.append(f"  {bug_id}: {code}")
        lines.append(f"    description: \"{specific_desc}\"")
        lines.append(f"    category: \"{category}\"")
        lines.append(f"    expected lines: {bug_lines}")
    return "\n".join(lines) if lines else "  (none)"


def build_issues_section(issues: list[ParsedIssue]) -> str:
    if not issues:
        return "  (no issues found)"
    lines = []
    for i, issue in enumerate(issues, 1):
        line_str = f"L{issue.line}" if issue.line is not None else "L?"
        lines.append(
            f"  {i}. {line_str}:{issue.code}: {issue.description}"
        )
    return "\n".join(lines)


_MAPPER_PROMPT = """\
<instructions>
You are a mapping tool. Your task is to match each issue found by an
analysis tool to a known ground truth bug, or null if no match exists.

You are given:
1. A shell script for context
2. Ground truth bugs: each has an ID, a code, a description of what the
   specific bug does in this script, a general category, and expected lines
3. Found issues: each has a code, a line, and a description of the
   specific problem the analysis tool identified

How to map:
- First read the found issue's description and look at that line in the
  script to understand what the actual bug is.
- Then compare it to each ground truth bug's description to find the one
  that describes the SAME underlying problem. Descriptions may use
  different wording — match on the bug's nature, not exact phrasing.
- The "category" field is just background context. Two bugs that share
  a category are NOT necessarily the same bug unless their descriptions
  and script locations also match.
- Once you identify the matching ground truth bug, record its ID and the
  expected line that is closest to the found issue's line.
- If no ground truth bug describes the same problem, set gt_id to null.
- Line numbers only matter for proximity: a match at line 178 for a
  bug expected at line 179 is valid. Do NOT map two issues to the same
  bug ID unless they genuinely refer to the same underlying defect.
- If a ground truth bug has expected line -1, it can match any line.
- DO NOT judge validity — only provide alignments.

Respond with a single JSON object containing a "mappings" array:

{{
  "mappings": [
    {{
      "llm_code": "system_file_deletion",
      "llm_line": 5,
      "llm_description": "May delete /etc/passwd because PATH is empty",
      "gt_id": "bug01",
      "gt_code": "del_sys_file",
      "gt_line": 5
    }},
    {{
      "llm_code": "const_cond",
      "llm_line": 50,
      "llm_description": "Condition is always true",
      "gt_id": null,
      "gt_code": null,
      "gt_line": null
    }}
  ]
}}
</instructions>

<script>
{script}
</script>

<ground-truth>
{ground_truth}
</ground-truth>

<found-issues>
{issues}\
</found-issues>
"""


def build_mapper_prompt(
    script: str, ground_truth: dict, issues: list[ParsedIssue]
) -> str:
    gt_section = build_ground_truth_section(ground_truth)
    issues_section = build_issues_section(issues)
    return _MAPPER_PROMPT.format(
        script=script, ground_truth=gt_section, issues=issues_section
    )


def make_issue(code: str, line: int | None, message: str) -> sash.reporter.Issue:
    """Create a minimal Issue subclass instance with the given code.
    Used only in the main process, not for pickle transport."""
    issue_cls = type(
        f"_LLM_{code}",
        (sash.reporter.Issue,),
        {
            "code": code,
            "severity": sash.reporter.Severity.WARNING,
            "description": "",
        },
    )
    return issue_cls(message=message, line=line)

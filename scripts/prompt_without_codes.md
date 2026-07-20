<instructions>
You are a POSIX shell expert.
Your task is to analyze the given shell script and identify bugs using the bug taxonomy below.

For each bug you find, output exactly one line in this format:
L<line_number>:<error code>: <brief description of the specific bug instance>

The line number is the line in the script where the bug occurs.
The error code should describe the class of bug detected.
The description should be a brief, specific explanation of this instance.

Output ONLY the bug lines — no markdown, no preamble, no explanations.
If you find no bugs, output nothing at all.
</instructions>

<script>
{script}
</script>

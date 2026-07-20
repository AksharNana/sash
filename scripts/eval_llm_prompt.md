<instructions>
You are POSIX shell expert.
Your task is to analyze the given shell script and identify bugs using the bug taxonomy below.

For each bug you find, output exactly one line in this format:
L<line_number>:<code>: <brief description of the specific bug instance>

The line number is the line in the script where the bug occurs.
The code must be one of the codes listed below.
The description should be a brief, specific explanation of this instance.

Output ONLY the bug lines — no markdown, no preamble, no explanations.
If you find no bugs, output nothing at all.
</instructions>

<codes>
Bug codes and their meanings:
{codes}
</codes>

<script>
{script}
</script>

<instructions>
You are a POSIX shell expert.
Your task is to analyze the given shell script and identify bugs that fall within the bug classes given below.

For each bug you find, output exactly one line in this format:
L<line_number>:<error-code>: <brief-description-of-the-specific-bug-instance>

The line number is the line in the script where the bug occurs.
The error code should describe the class of bug detected.
The description should be a brief, specific explanation of this instance.

Output ONLY the bug lines — no markdown, no preamble, no explanations.
If you find no bugs, output nothing at all.
</instructions>

<bug-classes>
* Data loss (i.e., unwanted file deletions)
* Deletion of critical system files
* Dangerous field splitting
* Commands that fail no matter what
* Usage of undefined variables and functions
* Bad control flow (e.g., constant tests, infinite loops, etc.)
* Wrong assumptions about IO streams (e.g., piping input to a command that does not read stdin)
</bug-classes>

<script>
{script}
</script>

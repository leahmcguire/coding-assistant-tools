"""What context sources the session loads.

These are the *other* kind of toggle. Scope lives in a mutable object the hook
reads, so it changes instantly. These are `ClaudeAgentOptions` fields fixed at
connect time, so changing one means rebuilding the options and reconnecting with
`resume=<session_id>` -- which preserves the conversation but is forward-only:
turning CLAUDE.md off stops future injection, it does not retract what already
landed in the transcript.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Skills = list[str] | Literal["all"] | None

SCOPED_PROMPT = """You are a coding assistant working under an explicit, bounded context.

The files you have been shown are the entire working set for this session. You do
not have a shell, web access, or subagents. File reads outside the listed scope are
blocked by the harness, not by convention.

Rules:
- If you need a file that is not in scope, say so and ask the user to run
  `/scope add <path>`. Do not try alternative paths or other tools to reach it.
- The previews you were given are truncated. Use Read on any file in scope to see
  the rest of it before editing.
- To run tests, lint, or type checks, use the run_tests, run_lint, and
  run_typecheck tools. There is no shell.
- Prefer saying "I don't have enough context to answer that" over guessing about
  code you cannot see.
"""

SCOPE_CONTRACT = """
This session runs under an explicit file scope. Reads outside it are blocked by the
harness. If you need something that is not in scope, ask the user to run
`/scope add <path>` rather than trying other paths or tools. There is no shell: use
run_tests, run_lint, and run_typecheck instead.
"""


@dataclass
class ContextProfile:
    claude_md: bool = False
    skills: Skills = None
    prompt: Literal["lean", "preset"] = "lean"
    bash: bool = False

    def describe(self) -> str:
        skills = (
            "none"
            if not self.skills
            else ("all" if self.skills == "all" else ", ".join(self.skills))
        )
        return (
            f"prompt={self.prompt}  claude-md={'on' if self.claude_md else 'off'}  "
            f"skills={skills}  bash={'on' if self.bash else 'off'}"
        )

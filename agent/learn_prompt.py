#!/usr/bin/env python3
"""``/learn`` — build the prompt that turns described material into a skill.

``/learn`` is open-ended. The user can point it at anything they can describe:
a directory of code, an API doc URL, a workflow they just walked the agent
through in this conversation, or pasted notes. This module builds one normal
agent prompt that instructs the live agent to gather sources and save a skill
with ``skill_manage``.
"""

from __future__ import annotations

_AUTHORING_STANDARDS = """Follow the Hermes skill-authoring standards exactly.

Frontmatter:
- name: lowercase-hyphenated, <=64 chars, no spaces.
- description: ONE sentence, <=60 characters, ends with a period. State the
  capability, not the implementation. No marketing words (powerful,
  comprehensive, seamless, advanced, robust). Do NOT repeat the skill name. If
  the description contains a colon, wrap the whole value in double quotes.
- version: 0.1.0
- author: always the literal value `Hermes`. NEVER fill it from the host
  environment, OS username, git config, or any probed identity.
- platforms: declare `[macos]`, `[linux]`, and/or `[windows]` only when the
  skill genuinely uses OS-bound primitives. Omit for portable skills.
- metadata.hermes.tags: a few Capitalized, Relevant, Tags.

Body section order (omit a section only if it genuinely has no content):
1. `# <Human Title>` then a 2-3 sentence intro: what it does, what it does NOT
   do, and the key dependency stance.
2. `## When to Use` — concrete trigger phrases.
3. `## Prerequisites` — exact env vars, install steps, credentials.
4. `## How to Run` — canonical invocation, framed through Hermes tools.
5. `## Quick Reference` — flat command/endpoint list, no narration.
6. `## Procedure` — numbered steps with copy-paste-exact commands.
7. `## Pitfalls` — known limits and things that look broken but are not.
8. `## Verification` — one command/check that proves the skill worked.

Hermes-tool framing:
- Frame running scripts as "invoke through the `terminal` tool".
- Reference Hermes tools by name in backticks: `terminal`, `read_file`,
  `write_file`, `search_files`, `patch`, `web_extract`, `web_search`,
  `vision_analyze`, `browser_navigate`, `delegate_task`, `image_generate`,
  `text_to_speech`, `cronjob`, `memory`, `skill_view`, `execute_code`.
- Do NOT name shell utilities the agent already has wrapped: say `read_file`
  not cat/head/tail, `search_files` not grep/rg/find/ls, `patch` not sed/awk,
  `web_extract` not curl-to-scrape, `write_file` not echo>file or heredocs.
- Third-party CLIs are fine inside scripts, but prose still frames them as
  "invoke through the `terminal` tool".

Quality bar:
- Prefer exact commands, endpoint URLs, function signatures, and config keys
  that appear verbatim in the source. NEVER invent flags, paths, or APIs.
- Keep it tight and scannable: ~100 lines for a simple skill, ~200 for complex.
- Do not write a router/index/hub skill that only points at other skills.
- Larger scripts/parsers belong in a `scripts/` file via `skill_manage`
  write_file, then referenced from SKILL.md by relative path.
"""


def build_learn_prompt(user_request: str) -> str:
    """Build the agent prompt for an open-ended ``/learn`` request."""
    req = (user_request or "").strip()
    if not req:
        req = (
            "the workflow we just went through in this conversation — review "
            "the steps taken and distill them into a reusable skill"
        )

    return (
        "[/learn] The user wants you to learn a reusable skill from the "
        "source(s) they described below, and save it.\n\n"
        f"WHAT TO LEARN FROM:\n{req}\n\n"
        "Do this:\n"
        "1. Gather the material. Resolve whatever the user named using the "
        "tools you already have — `read_file`/`search_files` for local files "
        "or directories, `web_extract` for URLs, the current conversation "
        "history if they referred to something you just did, and the text "
        "they pasted as-is. If the request is ambiguous about scope, make a "
        "reasonable choice and note it; do not stall.\n"
        "2. Author ONE SKILL.md and save it with the `skill_manage` tool "
        "(action=\"create\"). Pick a sensible category. If the procedure needs "
        "a non-trivial script, add it under the skill's `scripts/` with "
        "`skill_manage` write_file and reference it by relative path.\n\n"
        f"{_AUTHORING_STANDARDS}\n\n"
        "When done, tell the user the skill name, its category, and a "
        "one-line summary of what it captured."
    )

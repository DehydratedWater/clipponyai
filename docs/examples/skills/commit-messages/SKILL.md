---
name: commit-messages
description: Drafts concise Git commit messages from a change summary or diff. Use when preparing a commit or improving a proposed commit message.
metadata:
  author: clipponyai
  version: "1.0"
---

# Commit message instructions

Write an imperative subject that describes the user-visible outcome.

- Keep the subject concise and omit a trailing period.
- Use a conventional type such as `feat`, `fix`, `docs`, `test`, or `refactor`
  when the repository already follows that style.
- Add a body only when it explains motivation, constraints, or a non-obvious
  tradeoff that the diff cannot show.
- Do not claim tests ran unless the supplied context says they passed.

When several examples would help, read `references/examples.md`.

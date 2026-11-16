# Context use and evidence

Enabled resolved workflows distribute concise context-use, visual verification
and explicit-sharing guidance. Disabled workflows and repository exclusions
suppress it; repository rules and custom Markdown retain precedence. The existing
start/resume/compaction loader still resolves just one workflow. No new setting,
source-read ledger, tool interception or automatic session reset is introduced.

Enabled workflows also distribute MCP-first session discovery guidance. For
questions about current or past session work, agents discover and use t's
`sessions` MCP tools (`find_sessions`, `list_sessions`, then `session_detail`),
including registered hosts when relevant. They verify candidates before using
GitHub as supplementary evidence. When runtime MCP tools are unavailable, they
state that limitation and use `t mcp --call TOOL JSON` before broad Git or
transcript searches. Discovery does not authorize session-control actions.
The installed Codex/Claude workflow loaders carry the same guidance, conditional
on an enabled resolved workflow, so it does not rely on machine-specific prose.

Aim for roughly 2,000 tokens per ordinary tool result. Select fields, records,
aggregates and relevant excerpts before returning output; use larger results
when necessary. Keep the full response or log locally when shortening it, label
omissions and provide a working retrieval reference. Return one concise result
for every batched operation, including failures. For large diffs, start with a
summary and inspect the relevant hunks covering every actual change before approval.

Reuse evidence still in context. Reread after relevant edits, branch changes,
collaborator activity, context loss, or when an unread section matters. A repeated
read is not automatically wasteful. For example, reuse an unchanged function
while designing its tests, then reread its modified implementation for review.

After a bounded watch, inspect evidence:

```sh
t runners runs watch RUN_ID --timeout 60 --next-step 'Review failures and fix' --json
t runners runs inspect RUN_ID
t runners runs inspect RUN_ID --target local --json
t runners runs logs RUN_ID --target local
```

Inspection targets about 8,000 characters of ordinary text, not an exact token
equivalence. Execution status, identity, omission notices and retrieval commands
come before excerpts. Large target fanout can exceed that size to retain these
facts. Excerpts have retained-log line numbers. Recognized cases include pytest
failure sections, test summaries and common diagnostic lines; other output gets
a labeled bounded excerpt. Missing or unrecognized evidence never proves success.
An absent log is pending only for a pending target; otherwise it is missing.
Unavailable receipts/logs and cancelled execution are explicit.

Response omissions differ from storage loss. `response_content_omitted` describes
this response; `scan_limited` describes the first-8-MiB scan; `storage_content_lost`
comes from the runner's `log_truncated` receipt and is null when unknown.
`logs` retrieves all **retained** content, never data already lost at storage.

Use `t share` only when the user invokes it or explicitly asks an agent to share.
Prefer a deliberately selected `--file PATH` or `--file -`. Existing sharing,
saved shares, attachments, inbox and receiving-session behavior are unchanged in
this delivery. Transcript-capture settings and dependent runtime changes were
deferred to the separate workflow redesign at the user's request.

## Repeatable local audit

Discover candidate sessions with `find_sessions`, `list_sessions` (including
registered remote hosts when applicable), then verify with `session_detail`.
If unavailable, use `t mcp --call TOOL JSON`, stating the limitation. Do not infer
identity from an old branch. Locate only the verified sessions' transcript files.
Audit the cited historical sessions where available and up to five recent
applicable sessions. Store all session-specific material **outside Git**.

```sh
python3 scripts/audit_context.py /private/verified-session.jsonl \
  --output /private/new-audit.json
```

The one-shot script supports Codex JSONL, Claude messages and normalized synthetic
events. It creates a mode-0600 report exclusively, without overwriting files.
It reports text-output distribution, outputs over 20,000 characters, status-query
and short-sleep candidates, bounded watches, repeated-read candidates, image
attachments, identical inline image payloads, inspection-followed-by-log retrieval,
and available usage/workflow revision. It neither modifies sessions nor sends data.

Review candidate record numbers in the original private transcript. Record why a
reread was necessary (edit, branch change, collaborator, context loss, unread
section) or unnecessary. Review whether follow-up retrieval was required because
an excerpt omitted needed evidence. Identical image bytes establish duplicate
payloads, not necessarily redundant verification. URLs alone do not establish
identical images. Batch envelopes may contain multiple inner operations.

For each report record:

- **Measured:** counts, input identities verified privately, date, source revision,
  workflow revision if available, controlled before/after comparisons on identical input.
- **Qualitative:** manual candidate decisions and watcher audit (retained next step,
  timeout still pending, current PR head checked before accepting success).
- **Unavailable:** missing transcripts, images, usage, remote discovery or rendering.
- **Not established:** broad adoption, sustained reductions, or monetary savings.

Use [selective visual validation](validation.md) for screenshots. Preserve complete
required CI/coverage checks regardless of context budgets. Keep public summaries
free of transcript text, session identifiers, personal paths and private usage.

# Loop memory v2

The loop reads `memory/index.md` first. Use `read_file` with the linked path to
read one fact from `memory/facts/`. Fact files use minimal OKF frontmatter:

```yaml
---
type: fact
---
```

Keep `memory/index.md` compact. Add a new fact as a new file and add one index
bullet. Read the last N lines of `memory/HISTORY.md` with `read_file` offset and
limit. Do not load or rewrite the legacy memory wholesale. Periodic deduplication
and pruning is operator/assistant curation, not a loop duty. Examples are POSIX
path examples; no extra memory tool is required.

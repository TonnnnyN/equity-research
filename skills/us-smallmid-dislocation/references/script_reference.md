# Script Reference

## `scripts/screen_candidates.py`

Purpose: screen a prepared U.S. small/mid-cap CSV for severe price dislocation candidates with basic quality filters.

Use when:

- the user provides a prepared CSV universe
- the task is broad candidate triage, not a single-stock buy memo
- the desired output is `Pass`, `Watchlist`, or `Investigate`

Command from the repository root:

```bash
python3 skills/us-smallmid-dislocation/scripts/screen_candidates.py input.csv \
  --output-json output/smallmid_results.json \
  --output-markdown output/smallmid_report.md
```

Default config:

```text
skills/us-smallmid-dislocation/defaults/universe.toml
```

Read before use:

- `references/input_schema.md`
- `references/thresholds.md`
- `references/red_flags.md`

Important behavior:

- Filters excluded structures, sectors, weak liquidity, and out-of-range market caps.
- Requires both a primary dislocation signal and a confirming signal.
- Caps or rejects candidates with hard red flags.
- Produces ranked outputs, not final portfolio actions.

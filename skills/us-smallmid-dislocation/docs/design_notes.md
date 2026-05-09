# Design Notes

This Skill is a screening layer, not an investment committee.

The design assumes a prepared CSV universe because deterministic screening should not depend on fragile live scraping. A separate data-building step can create the universe, but this Skill only evaluates rows it is given.

The output language is intentionally conservative. `Investigate` means "worth deeper work"; it does not mean buy, start, add, or conviction.

Keep this Skill separate from `market-sentiment-research` because broad candidate triage has different failure modes: bad data, liquidity traps, filing staleness, dilution risk, and special situations can dominate the signal.

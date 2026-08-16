# Design Notes

This Skill is deliberately narrow: it turns a visible selloff into an auditable investment-research decision.

The research flow starts with the market move, then tests whether the move is explained by official evidence, valuation, fundamentals, positioning, or catalysts. It should not begin with a story and then hunt for supporting facts.

The shared Python runtime can generate daily reports and review packets, but this Skill remains the decision layer. It tells the agent how to interpret the packets, what evidence to demand, and when to cap the action.

The action language is deliberately portfolio-shaped: `Reject / Watch / Starter / Add / Exit`. This Skill is pullback research on names you already care about, not a broad screen over a prepared universe.

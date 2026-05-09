# Design Notes

This Skill is deliberately narrow: it turns a visible selloff into an auditable investment-research decision.

The research flow starts with the market move, then tests whether the move is explained by official evidence, valuation, fundamentals, positioning, or catalysts. It should not begin with a story and then hunt for supporting facts.

The shared Python runtime can generate daily reports and review packets, but this Skill remains the decision layer. It tells the agent how to interpret the packets, what evidence to demand, and when to cap the action.

Keep this Skill separate from `us-smallmid-dislocation` because the action language is different. `Reject / Watch / Starter / Add / Exit` are portfolio research actions; `Pass / Watchlist / Investigate` are screening states.

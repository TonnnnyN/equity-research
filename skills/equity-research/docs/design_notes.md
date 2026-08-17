# Design Notes

This Skill is deliberately evidence-disciplined: it assembles SEC facts and valuation evidence for a single security, then turns that evidence into an auditable investment decision at any price level.

The research flow tests whether a thesis is explained by official disclosures, fundamentals, valuation, competitive position, and catalysts. It does not begin with a story and hunt for supporting facts. Evidence comes first; the conclusion follows.

The shared Python runtime generates evidence packets and review tiers, but this Skill remains the decision layer. It tells the agent how to interpret the packets, what evidence to demand, when to apply a valuation gate, and when to cap the action.

The action language is deliberately portfolio-shaped: `Reject / Watch / Starter / Add / Exit`. These five bins work at any price level and any portfolio weight, because they answer a single question: "At this price, with this evidence, what do I do with this position?
"

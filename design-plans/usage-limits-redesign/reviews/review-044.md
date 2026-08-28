## Scenario

044 — Used percent reaches or exceeds 100 percent.

## Verdict

Pass visually; copy is too weak.

## Findings

The meter correctly clamps at full width, switches to coral at 100%, and retains the actual percentage above 100. Yet the row still says only “above notify threshold,” which understates a reached or exceeded provider limit.

## Recommendation

Say “limit reached” at 100% and “limit exceeded” above 100%, while retaining the reported percentage. Do not imply that requests are blocked.

## Test idea

Render 99.9%, 100%, and 137%; verify amber-to-coral transition, full-width clamping, exact percentage, and distinct reached/exceeded labels.

## Scenario

031 — One live weekly seven-day window.

## Verdict

Strong fit for the signature ledger.

## Findings

A single row can answer the core decision with client, “Weekly,” used percentage, meter, absolute reset, and ranged consumption. The global 7/30/90 selector must not appear to change this provider-defined window.

## Recommendation

Label the lanes “Provider window” and “Selected-range use,” keep the weekly meter fixed during range changes, and order by its remaining headroom. Preserve the explicit used percentage and reset text beside the meter.

## Test idea

Switch 7d, 30d, and 90d for one weekly client; assert only consumption changes while window label, percent, reset, and headroom ordering remain identical.

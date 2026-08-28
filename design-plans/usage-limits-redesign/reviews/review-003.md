# Scenario

003 — VoiceOver-first operator comparing live headroom with today’s spend.

# Verdict

Needs revision. The merged destination improves proximity, but its current semantics do not make the comparison efficient or unambiguous without sight.

# Findings

- `LimitMeter` announces only “N percent used”; it omits client, window, remaining headroom, reset, freshness, and stale state. Those facts remain separate stops, so the meter loses context in VoiceOver navigation.
- Today’s estimated cost is available only by reaching the relevant daily chart bar. The summary strip reports the selected 7/30/90-day total, so a listener can easily compare live headroom against the wrong spend window.
- Repeating visible percentage text after the accessible meter adds noise without supplying the missing relationship. Cost confidence and limit freshness also need to survive the combined announcement.

# Recommendation

Give each joined client summary one concise accessibility element: client; each live window’s percent remaining and reset; today’s estimated spend with confidence; and independent usage/limit freshness. Name missing or stale facts rather than substituting zero. Keep detailed meters and chart bars available after that overview.

# Test idea

With VoiceOver and two clients, navigate from the pane heading. Verify one stop per client answers “how much headroom, until when, and what did I spend today?” before any chart traversal, while stale or unpriced data is announced explicitly.

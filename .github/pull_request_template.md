## Summary

- TODO

## Test plan

- [ ] `pytest -q` or `.venv/bin/python -m pytest tests/ -q --tb=short`
- [ ] Secret scan / no credentials in docs, fixtures, reports, or logs
- [ ] No paid provider/API calls were run, or any paid smoke test is explicitly documented with budget and result

## Safety checklist

- [ ] Preserves local-first behavior
- [ ] Does not store API keys or secrets
- [ ] Does not expose localhost services publicly by default
- [ ] Does not control processes agentacct did not start
- [ ] Provider/API forwarding remains opt-in and budget-gated
- [ ] JSON output paths remain machine-readable when `--json` is used

## Docs

- [ ] README/docs updated if user-facing behavior changed
- [ ] Public docs avoid private strategy, private roadmap, and overbroad support claims

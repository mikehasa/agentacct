# Integration license BOM

Status: implementation baseline
Audit date: 2026-07-13

This file records the upstream projects studied for the multi-source evidence
architecture and the licensing rule for each integration. It is an engineering
compliance record, not legal advice.

## Current integration inventory

| Integration | Audited repository and commit | Upstream license | Current implementation mode | Copied/vendored source |
| --- | --- | --- | --- | --- |
| OpenLIT coding-agent telemetry | `openlit/openlit` at `8adf21c8f952c0768fd5ff85d853798bb3c028f3` | Apache-2.0 | Protocol-compatible OTLP JSON mapping and independently implemented adapter contracts | None |
| Entire Git provenance | `entireio/cli` at `7cd6662805fbd525f2f418ecf465a247b924af70` | MIT, Copyright 2026 Entire Inc. | Read-only parsing of public Git objects, refs, and trailers | None |
| Paperclip orchestration | `paperclipai/paperclip` at `c36f1a4afd91e4ddf0e5c7224b288ce722c7404f` | MIT, Copyright 2025 Paperclip AI | Read-only exported/API snapshot mapping | None |

The repository currently contains no copied or modified upstream source from
these projects. Their license texts therefore are not added to the agentacct
distribution merely because agentacct speaks compatible public protocols or
parses public data formats. This BOM and connector-level upstream notes retain
the engineering provenance of the design.

## Commercial-use interpretation

MIT and Apache-2.0 permit commercial use, modification, distribution, and
sublicensing subject to their conditions. They do not mean "no obligations."

If code is copied, modified, linked, or bundled later:

- MIT copyright and permission notices must remain in copies or substantial
  portions of the software;
- Apache-2.0 requires preserving the license, marking modified files, retaining
  relevant notices, and respecting its patent and trademark terms;
- dependencies and bundled assets have their own licenses and must be audited
  independently of the repository's top-level license;
- project names, logos, and trademarks are not automatically licensed for
  agentacct branding;
- hosted services, datasets, model outputs, and third-party APIs may have terms
  separate from source-code licenses.

## Decision order

For every upstream capability, choose in this order:

1. **Protocol-integrate** through OTLP, HTTP, webhook, CLI JSON, or Git.
2. **Reimplement** the smallest vendor-neutral behavior from documented formats.
3. **Port** a bounded, tested module when no stable protocol exists and the
   maintenance benefit clearly outweighs compliance and coupling.
4. **Copy** only a small, independently attributable unit.
5. **Avoid** copying an upstream platform, UI, control plane, or data model.

Before a port or copy is accepted, add a connector-local `UPSTREAM.md` entry
containing repository, commit, file list, modifications, license, notice status,
and audit date. Add the exact upstream license/notice material required by that
distribution and rerun the dependency/SBOM gate.

## Dependency and asset gate

No integration may silently introduce:

- an unknown license;
- AGPL, SSPL, BUSL, Commons Clause, or another source-available restriction;
- a binary dependency without its bundled notice and redistribution analysis;
- a font, icon, logo, or sample dataset without asset-level terms;
- a transitive dependency tree solely to consume a format agentacct can parse
  with its existing runtime.

If agentacct later bundles Paperclip UI assets, OpenLIT platform components, or
Entire binaries, this table is insufficient: audit the full shipped artifact,
including fonts and native libraries, and generate an SPDX or CycloneDX SBOM.

## Upgrade procedure

An upstream integration update must:

1. pin and record the new upstream commit;
2. compare top-level and component-level licenses/notices;
3. update versioned connector fixtures;
4. rerun contract, privacy, and replay tests;
5. document schema and behavior changes;
6. confirm that no upstream source entered the distribution accidentally.

Protocol compatibility is not a claim of upstream endorsement. agentacct UI
and docs should use project names only to identify compatible evidence sources.

# Audited connector inputs

These pins identify the public upstream snapshots used to design and test the
connector boundary. agentacct copies **no upstream source code** into
this package. The implementation uses independently written adapters for
public export, OTLP, and Git formats.

| Connector | Upstream repository | Audited commit | Upstream license | agentacct integration |
| --- | --- | --- | --- | --- |
| Paperclip | `paperclipai/paperclip` | [`c36f1a4afd91e4ddf0e5c7224b288ce722c7404f`](https://github.com/paperclipai/paperclip/tree/c36f1a4afd91e4ddf0e5c7224b288ce722c7404f) | MIT | Read-only exported/API JSON snapshot mapping |
| OpenLIT | `openlit/openlit` | [`8adf21c8f952c0768fd5ff85d853798bb3c028f3`](https://github.com/openlit/openlit/tree/8adf21c8f952c0768fd5ff85d853798bb3c028f3) | Apache-2.0 | Metadata-only OTLP/HTTP JSON mapping |
| Entire | `entireio/cli` | [`7cd6662805fbd525f2f418ecf465a247b924af70`](https://github.com/entireio/cli/tree/7cd6662805fbd525f2f418ecf465a247b924af70) | MIT | Read-only public Git refs, trailers, and metadata |

The upstream license labels above describe the audited repositories, not a
blanket conclusion about their dependencies, trademarks, hosted services, or
future commits. Any future source vendoring, SDK dependency, or pin update
requires a new license and provenance review.

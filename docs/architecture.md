# ACP runtime-control boundary

Status: development candidate, 2026-10-23.

The adapter owns ACP v1 framing, presentation, process lifetime, session indexing
and client approval. Aelix owns models, authentication, native sessions,
settings, extension dispatch, resources, permission posture and delegation.
Shared behavior is exposed through an additive `aelix_control` envelope;
the original 29-command RPC roster is unchanged. The adapter does not copy
native command handlers or the credential store.

## Protocol

Negotiate ACP v1, including when a client requests an unsupported higher
version. Initialize precedes sessions. Use `configOptions` for selectors,
retaining legacy modes/models. Return complete catalogs after changes.
Configuration comes from actual runtime state, stripping provider HTTP headers.

Follow ACP [initialization](https://agentclientprotocol.com/protocol/v1/initialization),
[configuration](https://agentclientprotocol.com/protocol/v1/session-config-options),
[slash commands](https://agentclientprotocol.com/protocol/v1/slash-commands)
and [extensibility](https://agentclientprotocol.com/protocol/v1/extensibility).
Custom methods start `_`, capabilities live in `_meta`, error details in `data`.

Control commands never silently become model prompts. Unknown commands error.
Extension handlers use the native dispatcher; explicitly selected templates
and skills use native preparation before invoking the model. Client-only
actions require an advertised capability. Arbitrary terminal widgets still
need a client-specific adaptation.

## Lifetime and cancellation

Each session owns one child, approval listener, readers, pending requests and
an OS session lease. Startup reservations enforce the process limit during
concurrent creation. Failure before readiness closes the candidate. EOF and
adapter shutdown close children. POSIX process groups and Windows process-group
and taskkill fallback provide bounded orderly cleanup. This is not a Windows
Job Object or a universal guarantee after hard parent termination.
On Windows without an attached console, termination falls back to the system's
absolute taskkill path. Pipe readers drain during shutdown so child exit is
not held open by a full output buffer. POSIX cleanup also kills remaining
members of the owned group after its leader exits.

Windows stdin uses a daemon with one-line demand credits, avoiding unbounded
queues and blocked executor shutdown. Frames are bounded to 16 MiB; invalid
Unicode, nonfinite numbers and excess requests are rejected. Stderr drains in
chunks. Cancellation reaches native control tasks without waiting for their
mutation lock and cancels pending dialogs.

Stdout and forwarded native stderr use bounded daemon writers on raw descriptors. A client that stops
reading cannot block the event loop or Python's buffered-stdout shutdown lock.
EOF can cancel queued replies; disconnected clients must recover through history.

Changing extensions, trust or delegation restarts the child, preserving the
transcript and ACP identity. Client MCP configuration and UI support rebind
in memory. Supported session controls restore from saved preferences.
Branching creates a new file and ACP identity, restores the parent and leaves
navigation to Studio. Close preserves the index.

## Approval and trusted extensions

Automatic extension discovery is disabled. The bridge loads after selected
extensions. A launch nonce proves loading; a random token authenticates the
ephemeral loopback endpoint. Requests must match a known native tool-call ID
and name. Actual arguments after prior hooks are shown for consent. Invalid
authentication, failure, cancellation and deadlines deny execution.

Permission modes use verified native provenance, not names. The authenticated
bridge also evaluates the pinned native containment/sensitive-path helper for
file edits; unavailable compatibility falls back to client approval. Unknown and MCP
tools never inherit built-in read/edit exemptions. Auto shell classification
uses the native classifier. Native policy precedes client approval; full
access removes client prompts while native guardrails remain.

Installed extensions and the process environment are trusted. Extension Python
can run during loading or commands with the host's authority. The bridge is
not a malicious-extension sandbox. Delegated children retain native restrictions;
there is no claim of an ACP dialog for every child tool.

## Persistence and ambiguous outcomes

SQLite uses WAL and FULL synchronization; the main file is private and a
symbolic-link DB is rejected. The old index migrates additively. OS leases
prevent concurrent writers to one stored ACP session and release on process
death. Lock files remain to avoid replacing locked inodes. Do not manually
point independent ACP identities at the same native transcript.

A stable `aelix/requestId` plus content fingerprint identifies a turn.
Completed retries return the stored result without model/tool execution.
Reusing a key for different content is rejected. Running/interrupted outcomes
are unresolved and never replayed blindly. Definitively invalid requests retain
their structured error. Without a client key each prompt is new.

Display history includes control replies, thoughts and tool updates. Stream
events commit in short transactions per update; an open model stream does not
retain SQLite's writer lock. A hard kill can still lose chunks not yet received.
Native transcript and SQLite are not one transaction. This is not exactly-once
external effects or automatic crash-safe task continuation. A timeout does
not prove a tool did nothing. Protect history as sensitive user data.

The native `agent_end` closes a provider attempt. ACP prompt completion instead
waits for native `get_state.isStreaming` to become false, including retries and
closing compaction. Missing saved model/profile choices recover with visible
warnings; a missing restricted profile never recovers into silent write access.

Known unresolved candidate limitation: native JSONL is bounded to 16 MiB too.
An oversized aggregate event or `get_messages` response can close its actor.
Large branch-history acceptance needs compact native lifecycle events and
paged/bounded history; no such new core API is introduced by this adapter fix.

## Credentials, MCP and packages

Use the held AuthStorage, return only provider status and omit management keys
and OAuth replies from history. Clients must not log auth payloads. OAuth
URLs/progress are transient events.

MCP configuration is validated and replaced transactionally. Status and saved
preferences omit environment, headers and URLs. A task owns each AnyIO
transport lease and closes it in the same task; native reload reattaches client
tools. Only explicit environment values join the SDK's safe base.

Extension operations use argv, bounded output, deadlines and consent for
package changes, enablement and trust/delegation changes. Signed app bundles
are immutable: provide a managed writable runtime. Installing a package in
another environment does not make it importable by bundled Python.
Offline disables catalog/package network paths, not provider or extension
network calls. Closed-network installers need local wheels, models, MCP
dependencies and CA configuration.

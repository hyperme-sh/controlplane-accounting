# Scoped accounting contract v1

Staged by controlplane-billing#21 for Infra#921. Billing maintains this package
until a separately authorized transfer to controlplane-accounting. No repository
or package is published by this change. The staging version follows billing's
existing 0.6.0; product v0 is not a package bump. No hosted consumer is activated.
Build this directory independently with `uv build`. It has no runtime dependency.
The additive `controlplane_billing.accounting` facade requires this package to be
installed separately; existing payment/settlement imports are unchanged. Wallet,
asset, network, fees, currency conversion and provider clients stay in billing.
Do not translate the old signed money-settlement intent into a quota settlement.

## Contract

All methods take a Scope (tenant_id and subject_id), directly or via Mutation.
Identifiers are opaque, nonempty ASCII strings up to 128 characters; allowed
characters are letters, digits, underscore, dot, colon, slash and hyphen.
Quantities are integers in [0, 2^63-1], excluding booleans. Zero is valid and does
not grant capacity. Each advertised unit names its smallest indivisible quantity
(e.g. tokens, milliseconds). No implicit conversions; amount/unit must exactly
match the quote. Unknown units fail. Timestamps are integer UTC Unix seconds.
Quotes and reservations expire at now >= expires_at. Reservation results preserve
original quantity, cumulative committed/released quantities and expiry.
Commit and release quantities are deltas; their cumulative sum cannot exceed the
reservation. Usage is metering, independent of payment state, and does not grant
capacity. Entitlements always require authorization and matching scope.

Every mutation (quote included) requires an idempotency key. Keys are scoped by
(tenant, subject, key), across operations. Compare canonical operation/payload
bytes before checking mutable state or expiry on replay. Identical replays return
the original stored response; different operation or payload returns conflict.
Failed validation creates no allocation. Concurrent duplicates cannot apply twice.
Authenticate every replay. Wrong-scope references return not_found, never data
from another subject. Backends bind scope to authenticated authority; caller
headers and possession of an opaque reference are never sufficient authority.

## HTTP v1

POST `/api/v1/accounting/{operation}` with a JSON object containing `scope` and
operation arguments matching the protocol. Reads also use POST to avoid scope
and opaque IDs in URL logs. Mutations require `Idempotency-Key`; service auth is
`Authorization: Bearer <credential>` over HTTPS. The server validates allowed
tenants, subjects and operation permissions using its trusted service identity
policy. No public administrative grant endpoint is defined here. Clients require
explicit backend URL and credential; never fall back to none. No redirects or
credentials in URLs. Local HTTP is allowed only by explicit lab configuration.

Success is HTTP 200 and the result dataclass as JSON. Capabilities units and
operations are JSON arrays. Required fields must be present; clients ignore
additive response fields, including nested objects. Unknown request fields are
rejected (400) so misspelled accounting inputs cannot silently succeed. Breaking
changes use a new URL major version. No newly required v1 fields or changed unit
semantics. `checkout` defaults false for older capability responses.

Errors are `{"code":"..."}` with no upstream text: invalid_request=400,
unauthenticated=401, forbidden=403, not_found=404, conflict=409, expired=410,
quota_exceeded=422, unsupported=501, unavailable=503. Optional settle and checkout
must be advertised; unavailable optional operations return unsupported, not
success. Unknown error codes fail closed. Capabilities are scoped reads, not a
grant of authority.

HTTP connect timeout 2 seconds, total attempt deadline 10 seconds, at most three
attempts and 30 seconds total. Only transport uncertainty, 429, 502, 503 and 504
may retry, with bounded backoff and the same idempotency key and exact payload.
Honor Retry-After only within the total budget. Never retry validation, scope or
conflict failures. Response scope, IDs and units must match the request context.
A timeout is unknown outcome, never proof that a mutation was rejected.

## Backend handoff and transaction decision

Billing #25 implements internal/none/HTTP; payment-service #62 owns the hosted
adapter. PostgreSQL internal uses a quota row per tenant/subject/unit, locked
with SELECT FOR UPDATE, plus unique (tenant, subject, key) replay rows. Quota,
reservation, usage and original response writes commit in the same transaction.
Use deterministic lock order; competing reservations cannot overspend. Retain
replay identities/tombstones for the whole reference lifetime; expiring a
reservation frees unused allocation transactionally but must not erase replay
identity. None mode still authorizes scope and durably meters usage. #25 owns
executable concurrency, restart and expiry recovery; this protocol is not a
working backend and performs no database or provider calls.

## Conformance and nightly handoff

`tests/vectors.json` is reusable input for backend conformance. `test_contract.py`
checks ranges, expiry, scope, unit mismatch, canonical replay payloads and additive
responses. `scripts/check-accounting-package.sh` in the parent repository checks
source, standalone wheel and rebuilt source archive without private modules.
Infra #922 owns registration into the existing nightly batch, including #25's
real PostgreSQL and HTTP cases once executable. This extraction supplies contract
vectors only; missing backend prerequisites must report blocked, never green skip.

## Working backends (billing #25)

`config.configured_backend(config, authorize=policy)` now selects internal by
default, or explicit none/http. Internal and none require
ACCOUNTING_DATABASE_URL and the `postgres` extra. HTTP requires ACCOUNTING_URL,
ACCOUNTING_TOKEN and the `http` extra. No missing config or outage changes mode.
The trusted owner supplies a synchronous policy returning exactly True only for
allowed (scope, operation) pairs, including a separate grant permission. Every
call and replay checks it. Service handlers must bind that policy to authenticated
identity; request headers are not a policy implementation. No HTTP grant endpoint
is exposed. Backend installation never performs schema setup automatically.

PostgresRepository.initialize is an explicit fixture/deployment step. A locked
scope row stores all unit ledgers, quotes, reservations, usage and original replay
responses together. This strengthens the earlier per-unit locking proposal: keys
are unique across operations/units within a scope, so one scope transaction avoids
cross-unit replay races. Independent scopes use independent rows. Statement and
lock deadlines bound contention. PostgreSQL commit/rollback protects the whole
mutation and response. The initial storage format is JSONB; schema/data migration
must preserve keys and support independently updated peers.

Quote TTL defaults 300 seconds and reservation TTL 3600 seconds (1..86400 allowed).
Every successful scoped operation deterministically frees expired unused amounts.
Committed amounts remain consumed. Expired references, quote-use markers, usage
and original replay responses are retained indefinitely in v0, including across
process restarts; no TTL deletion may turn an old replay into a new allocation.
Future archival must preserve conflict tombstones and original responses. Operators
must monitor scope-row growth; archival/partitioning is a separate compatible
storage optimization. This implementation favors atomic correctness over throughput.

A quote can fund one reservation. Commit/release are incremental and replay-safe.
`usage(scope)` returns metered records; usage does not consume quota twice or grant
entitlement. Entitlement requires a positive, unexpired scoped reservation for the
same resource. Zero allocation never grants access. Optional settle is unsupported.
None mode shares PostgreSQL storage, policy, expiry and metering while bypassing
quota grants, with the same signed-64-bit representation limit.

`integration/accounting/test_postgres.py` is the scheduled owner harness for real
PostgreSQL contention, a fresh-process restart, expiry, none durability, HTTP
accepted-write/response-loss retry, and current event producers with frozen v1
consumer fields. Infra #922 supplies the disposable database, declared isolated
scopes, setup/readiness/reset and native JUnit collection. Do not run this harness
locally or dispatch nightly manually. Unit tests use transaction doubles and mock
HTTP; they are not database integration results. Hosted activation remains deferred
to payment-service #62.


For long-running owner work, trusted internal/none configuration may set
`ACCOUNTING_RESERVATION_TTL_SECONDS` to an integer string from 1 through 2764800
(32 days). The default remains 3600 seconds. HTTP reservation duration belongs
to the remote owner, not this client setting. Consumers must check returned expiry
against their admitted work window plus recovery margin before starting work.
Expiry is fixed on first reservation and retries do not extend it. Expired commit
and release operations remain refused; this is not a renewal or late-charge API.

Organization-owned quotas use `organization_scope(tenant_id, organization_id)`.
It returns the canonical `org:<UUID>` subject, disjoint from historical actor
principals. Serving owners must first verify current billing membership and keep
the human actor in their own audit/journal records. This helper grants no access.
Do not relabel existing actor reservations: settle them under their original
scope, while new organization operations use explicitly provisioned org quotas.

Entitlements now optionally include the remaining allocated `quantity`, including
its unit. Older responses decode with `quantity=None`; consumers that authorize
an amount must reject missing quantity, a different unit, or an excessive amount.
The additive response preserves older consumers without fabricating authority.

`organization.OrganizationBackend` binds an actor-private owner to a shared
organization quota. Mutations use actor-namespaced hashed replay keys; returned
scopes are projected back to the bound actor. It is a trusted owner adapter, not
an authorization boundary. Verify fresh membership before constructing it, keep
reference access in the actor-private owner journal, and persist the selected
organization before any quota mutation. Mark organization intents with a distinct
owner mode that older workers refuse; do not let an older actor-only worker settle
them. Historical intents lacking that binding keep their original actor backend.
An organization switch must not rebind an existing intent. Neither construction
nor organization membership grants access to another actor's private results.

Organization entitlement projection requires an async `entitlement_reference(actor,
resource)` callback that reads the actor-private journal. Without it the adapter
returns unsupported. A missing journal binding is refused. Backends advertise optional
`reference_entitlements` (false when absent on older peers). When true, entitlement
accepts an optional `reference` selecting the exact reservation; it never borrows
another member's allocation for the same resource. HTTP clients require this
capability before sending the new optional request field and validate the returned
reference. Old requests and responses retain their original behavior. An older
peer without this feature refuses organization authorization as unsupported.


## Public source preparation

Billing #40 prepares a deterministic neutral source export and an ordinary
`controlplane-accounting==0.6.0` consumer dependency. The transfer target is
`hyperme-sh/controlplane-accounting`; publication remains separately authorized
under Infra #993. Source, wheel and rebuilt-source checks run independently of
private billing. The repository's `docs/public-accounting-transfer.md` records
ownership, licensing, compatibility and the credential-free local wheelhouse
path. No public download availability is claimed before transfer/publication.

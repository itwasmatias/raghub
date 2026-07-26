# Exposure Engine V1 Contract Freeze

Date: 2026-07-26
Phase: 2A
Branch target: feature/exposure-engine-contracts-v1

## Scope

This freeze defines additive, immutable Exposure Engine V1 authority contracts.
It does not implement aggregation services, repository integration, API mutation,
risk/sizing integration, or persistence migration.

Legacy compatibility shapes remain unchanged:

- `sports.execution.exposure.ExposureCalculator`
- `sports.execution.exposure.ExposurePosition`
- `sports.execution.contracts.ExposureSnapshotV1`

Those remain compatibility/legacy views and are not reinterpreted as authoritative accounting.

## Authoritative additive contracts

Phase 2A introduces additive contracts in `sports.execution.exposure_v1`:

- `CanonicalSportsbookIdV1`: closed canonical sportsbook identity taxonomy
- `ExposureRecordV1`: normalized immutable exposure input record
- `ManualRecordedRealProvenanceV1`: immutable provenance for manually recorded real positions
- `BankrollReferenceV1`: read-only denominator evidence
- `ExposureCandidateV1`: immutable projection candidate
- `ExposureCalculationRequestV1`: deterministic request identity
- `ExposureContributionV1`: contribution-ledger evidence
- `AuthoritativeExposureSnapshotV1`: authoritative snapshot output contract
- `ExposureProjectionV1`: immutable before/after projection contract

The only supported schema version is `v1`. The supported calculation input and
calculation versions are `exposure_input_v1` and `exposure_calc_v1`.
Unsupported versions reject when their individual objects are constructed.
Version disagreement between otherwise valid objects rejects when authoritative
objects are assembled.

## No-probability input rule

Exposure contracts do not accept raw implied, no-vig, SIP, calibrated SIP,
or reconciled numeric probability values.

Exposure authority accepts reconciliation references only:

- `reconciliation_result_id`
- version lineage fields

Candidates are not `ExposureRecordV1` instances and are never admitted as current authoritative exposure records.

## Monetary semantics

Exposure values are distinct and non-interchangeable:

- `stake_committed`
- `stake_reserved`
- `maximum_possible_loss`
- `potential_profit`
- `gross_payout`
- `net_liability`

For straight positions:

- `gross_payout = stake_committed + potential_profit`
- `net_liability = maximum_possible_loss`

Monetary requirements:

- Decimal only
- finite values only
- nonnegative only
- explicit currency required
- USD quantized to `0.01` with `ROUND_HALF_EVEN`

Mixed-currency requests are invalid unless explicitly partitioned outside this contract layer.
No FX conversion is performed in V1.

## State-inclusion freeze

State inclusion is explicit and independent of ad hoc lifecycle assumptions.
A lifecycle state alone must never fabricate reservation exposure.

- proposed candidate: projected only
- pending/submitted/accepted pre-open: no exposure by state alone; reserved exposure requires an authoritative `record_kind=reservation` record
- open: committed
- partially settled: committed for unsettled residual only
- settled/rejected/canceled/voided/superseded: historical-only
- corrected: historical-only replacement-revision semantics; the replacement current record is separate

Reservation economics are explicit in V1:

- Every reservation preserves authoritative `reservation_id` identity.
- Active pending/submitted/accepted reservations require `stake_reserved > 0`.
- Active reservations use `stake_committed = 0`, `potential_profit = 0`,
  `gross_payout = 0`, and
  `maximum_possible_loss = net_liability = stake_reserved`.
- Terminal historical reservations preserve identity but require every current
  exposure amount, including `stake_reserved`, to equal zero.

## Contribution-ledger invariant

Contribution evidence separates monetary totals from dimensional allocations.

- Top-level economic contributions are the sole source of portfolio monetary totals.
- Dimension/parlay-leg contributions are allocation evidence.
- Dimension allocations are non-additive and must not be summed as total liability.
- Contribution IDs are globally unique across top-level and dimensional rows.
- A source may contribute only one top-level row for each frozen measure.
- Every contribution references exactly one actual record or candidate source.
- Generic summation requires actual `ExposureRecordV1` and
  `ExposureCandidateV1` objects, derives their canonical hashes and economics,
  and validates scope, identity, state, currency, portfolio, `as_of`, schema,
  and calculation version before filtering by measure.
- Generic summation and snapshot/projection assembly reconstruct every bound
  candidate, bankroll reference, and contribution through its V1 constructor;
  a recomputed hash or callable process-local signer cannot legitimize invalid
  parlay, bankroll, lifecycle, taxonomy, or additive-scope semantics.
- Candidate IDs are checked against every current record, position, order
  intent, reservation, parlay, and leg identity before generic summation,
  including terminal historical lineage excluded from current totals.
  Candidate contribution rows use the frozen
  `sports.execution.exposure_v1` source repository; callers cannot relabel
  candidate authority.
- Generic summation validates the requested measure against
  `ExposureMeasureV1`; unsupported names reject instead of producing a false
  zero.
- Every bound record, candidate, and contribution in one generic assembly must
  use the same supported schema version, including when each source-row pair is
  internally consistent on its own.
- Record rows must use lifecycle identity consistent with either an active
  reservation or a current position. Candidate rows must use
  `proposed_candidate` and cannot claim record lifecycle IDs.
- Historical contribution states cannot enter current totals.

## Parlay semantics freeze

- A parlay counts once in economic totals.
- Candidate parlay evidence is derived from the actual candidate parent and full
  leg objects; caller-declared ghost, missing, or extra identities reject.
- Every candidate, including a straight candidate, requires exact dimensional
  evidence for its account, portfolio kind, currency, league, event, market,
  market type, period, selection/outcome, team/player, sportsbook, strategy and
  strategy version, model and calibration versions, reconciliation version,
  settlement horizon, and correlation groups.
- A parlay additionally requires its parent parlay dimension and each leg's
  parlay-leg, league, event, market, market type, period, selection/outcome,
  team/player, sportsbook, settlement horizon, reconciliation version, and
  correlation-group dimensions.
- Every required dimension receives the full parent maximum loss as a
  conservative, non-additive concentration allocation.
- Leg allocations are non-additive across dimensions.
- Same-game/shared-participant groups may be represented.
- No correlation coefficient, diversification credit, netting, VaR, or covariance logic is introduced in V1.

## Projection non-mutation rule

`ExposureProjectionV1` is descriptive and immutable.

- Authoritative construction requires actual
  `AuthoritativeExposureSnapshotV1`, `ExposureCandidateV1`, and
  `BankrollReferenceV1` objects.
- `ExposureProjectionV1.from_authoritative_inputs(...)` is the preferred
  construction boundary. Direct construction is authoritative only when the
  same three bound evidence objects are supplied and every derived field
  revalidates against them.
- Snapshot, candidate, and bankroll portfolio, account, portfolio kind where
  applicable, currency, exact V1 `as_of`, schema, IDs, and canonical hashes are
  cross-bound.
- The bankroll object must exactly match the bankroll ID and hash frozen into
  the current snapshot.
- The bound snapshot must remain `available`. An `unavailable` or `error`
  snapshot cannot be promoted into an authoritative available projection.
- Snapshot construction retains its actual record, contribution, and bankroll
  evidence. Projection assembly revalidates that evidence and all derived
  snapshot outputs.
- The constructed projection retains the same actual snapshot, candidate, and
  bankroll evidence. Future downstream consumers must call
  `validate_authoritative_projection_integrity(...)`, which rechecks the public
  hash, process-local tamper seal, bound evidence, contributions, and derived
  totals. A coordinated projected-total/public-hash rewrite therefore rejects at
  the V1 consumption boundary.
- Before totals derive from the snapshot. Projected totals derive exclusively
  from snapshot totals plus the actual candidate economics. There is no
  caller-supplied projected-totals override in the authoritative factory.
- Candidate top-level contributions require exactly one row for each of stake
  committed, stake reserved, maximum loss, potential profit, gross payout, and
  net liability, with amounts equal to the candidate.
- Candidate contribution state is always `proposed_candidate`.
- Record, candidate, bankroll, and contribution objects are reconstructed through
  their V1 constructors at authority boundaries so supported taxonomies,
  lifecycle rules, parlay cardinality, monetary invariants, and contribution
  scope are revalidated even if a caller recomputes a public hash and a
  process-local seal.
- Current snapshot remains unchanged.
- Candidate contributes once economically.
- Candidate leg contributions affect dimensions only.
- Bankroll references are consumed as read-only denominator context.
- `hypothetical_available_after` is descriptive arithmetic only.
- Projection does not mutate ledger, bankroll, positions, or reservations.

## Identity, deduplication, and hashing

- Same record ID + same canonical hash is idempotent.
- Same record ID + different canonical hash is a blocking integrity violation.
- Authoritative collection validation accepts actual `ExposureRecordV1` objects
  only; reduced caller-built identities cannot establish authority. Each record,
  its nested legs, and manual provenance are reconstructed through the frozen
  constructors before collection or lineage validation, so a re-signed lifecycle
  or sportsbook-alias mutation cannot bypass semantic validation.
- Corrections and reversals form an append-only, acyclic, single-head lineage
  graph. Targets must exist and precede replacements, replacement identity must
  be preserved, and cycles and competing heads reject. Only a final
  non-historical head is returned for current aggregation; a terminal lineage
  therefore yields no current head while its complete history remains
  validation evidence.
- `corrected` and `superseded` are replacement-only historical states. They
  require an append-only child and cannot appear as orphan terminal heads that
  silently erase current exposure.
- A lineage edge retires its target for effective-head selection without
  rewriting the target's frozen state or canonical hash. Corrections may
  replace an earlier current record; V1 reversals specifically require an
  earlier `settled` target and a `voided` child.
- Replacement identity means the complete frozen wager identity: source record,
  portfolio/account/kind/currency, position or reservation and order intent,
  reconciliation/model/calibration/strategy lineage, canonical
  event/market/selection/outcome, sportsbook, participants, settlement horizon,
  parent parlay, complete leg payloads, and correlation groups. Only revision,
  lifecycle, timestamps, economics, and correction evidence may change where
  the state rules permit.
- Valid multi-revision position, reservation, and parlay chains may reuse their
  frozen lineage identities. Reuse outside one validated chain blocks.
- Duplicate position/reservation/parlay/leg IDs are otherwise blocking
  integrity violations.
- Canonical import identity is a stable hash of a structured tuple containing
  canonical sportsbook, account, external record and revision, and canonical
  event/market/selection/outcome identity. Repository name does not make the
  same import unique, delimiter-containing identity pairs cannot collide, and
  one immutable external revision cannot be reused as a distinct correction
  even inside the same lineage. A separate strict external-revision key
  prevents a duplicate import from escaping detection by changing its
  canonical event or market mapping.
- Display sportsbook names and aliases are not identity keys. V1 accepts only
  the closed canonical IDs `draftkings`, `fanduel`, and `betmgm`; labels such
  as `DraftKings`, `draft kings`, or `draft-kings` reject rather than being
  silently lowercased into authority.
- Input order must not affect canonical hashes.

Canonical hashes cover material identity, lifecycle, monetary inputs,
reconciliation references, source revision, parlay/legs, and correlation groups.
`ExposureTotalsV1` includes currency in its canonical mapping, so otherwise
equal USD and EUR totals never share a payload.

Public canonical hashes are deterministic transport and comparison values.
Private fields, including process-local seals and retained evidence references,
are excluded when a whole dataclass is passed to `stable_hash`; authoritative
callers should prefer each contract's explicit `hash_payload()`.

Records, candidates, bankroll references, contributions, snapshots, and
projections also carry keyed process-local tamper seals. These seals are a
defense-in-depth check against accidental or unsupported mutation inside one
loaded process; they are not persistent signatures, an external authorization
mechanism, or a security boundary against hostile code that can invoke
module-private signing helpers. Deserialized or cross-process payloads must be
reconstructed through the public V1 constructors, and authority boundaries
re-run constructor semantics rather than trusting a seal alone.

## Snapshot authority

- Construction requires the actual validated record collection and the actual
  `BankrollReferenceV1`; opaque record or bankroll IDs/hashes cannot establish
  snapshot authority.
- Included record IDs and record hashes are unique and align exactly.
- Included records are the validated final lineage heads and must match the
  snapshot portfolio, account, portfolio kind, currency, schema, and current
  lifecycle scope.
- A separate canonical ID/hash map binds every record in the validated lineage,
  including terminal historical records excluded from current totals. Terminal
  history and no history therefore cannot share a snapshot hash.
- Every full-lineage record, including a terminal historical record, must match
  the snapshot portfolio, account, portfolio kind, currency, and schema.
- The full-lineage map receives a derived construction-evidence hash, and the
  completed snapshot receives a keyed process-local tamper seal. Projection
  assembly also revalidates the retained actual records, contributions, and
  bankroll rather than trusting either seal alone.
- Contribution reference IDs and hashes are unique.
- Contribution references must match actual `ExposureContributionV1` objects
  supplied as construction evidence.
- Each included record supplies exactly one top-level row for each of the six
  monetary measures. Every row must match the bound record's identity, state,
  hash, scope, and amount.
- Snapshot monetary totals are recomputed from that complete top-level ledger;
  nonzero caller-authored totals without matching records and contributions
  reject.
- State, record-kind, and portfolio-kind counts derive exactly from the
  validated final record heads and use their closed V1 taxonomies.
- Dimension aggregate and concentration-ratio keys are unique.
- Dimension type and measure values use the closed V1 enums; free-form labels
  such as `approved_stake` reject.
- Aggregate currency and portfolio kind match the snapshot.
- Every record supplies the same exact full identity dimension set frozen for
  candidates, with full record maximum loss as its non-additive allocation.
  Ghost, missing, duplicate, or oversized dimensional rows reject.
- Dimension aggregates derive from those rows, deduplicating repeated
  parent/leg representation of the same source and dimension before summing
  across records. Concentration ratios and correlation-group maximum loss then
  derive from those aggregates.
- Bankroll and available-balance exposure percentages derive from snapshot
  maximum loss and the actual bound bankroll denominators.
- Positive current maximum loss with a zero total-bankroll or
  available-balance denominator derives the corresponding
  `EXP_BANKROLL_DENOMINATOR_ZERO` or `EXP_AVAILABLE_BALANCE_ZERO` reason and
  forces the snapshot to `unavailable` (or preserves `error`).
- Actual record, contribution, and bankroll evidence is retained privately and
  revalidated whenever the snapshot is used to construct a projection.
- The normalized construction-time availability status and reason set are also
  retained as canonical evidence. Within the documented module-private mutation
  boundary, a status/reason/public-hash rewrite cannot promote a previously
  non-available snapshot.
- Candidate collision checks use every full-lineage record, position, order
  intent, reservation, parlay, and leg identity, not only current economic
  heads.
- Caller-owned collections are copied, sorted canonically, and frozen before
  hashing.

These are construction-time contract consistency rules. Phase 2A still does
not introduce the Phase 2B aggregation service, repository adapter, or runtime
integration. Consequently, Phase 2A cannot by itself prove that a caller supplied
the complete repository history: Phase 2B must assemble snapshots from an
authoritative repository manifest/idempotency boundary. It must also reconstruct
persisted contracts locally before validation rather than transport process-local
seals across workers.

## Historical and manual evidence

Historical state removes current monetary inclusion; it does not relax identity
requirements. Historical parlays still require a parent and at least two unique,
fully identified legs.

`MANUAL_REAL_POSITION` records require one immutable
`ManualRecordedRealProvenanceV1` containing:

- external execution/ticket reference
- sportsbook account reference
- evidence or receipt reference
- recording actor
- UTC recorded timestamp
- immutable revision ID

The recorded timestamp must exist no later than record eligibility and the
calculation cutoff. Canonical sportsbook, sportsbook-account, and external
execution form a stable ticket identity. Reuse is allowed only inside one
validated correction lineage; each immutable evidence revision remains
strictly unique. Unrelated records cannot repeat the ticket under a new
revision to double-count it.

This provenance records evidence only and grants no execution authority.

## Structured reason taxonomy

Exposure reasons use the closed `ExposureReasonCodeV1` taxonomy. Arbitrary
strings do not become authority reasons. Canonical reason order includes code,
category, severity, message, and canonical metadata. Reordering an equivalent
reason set does not change a hash; changing severity or metadata does.
Metadata keys must be non-empty strings and may not collide after
normalization. Values are limited to deterministic JSON-like containers and
scalars (plus finite decimals); opaque process-specific objects reject rather
than being stringified. Decimal and set values receive reserved canonical type
tags so they cannot hash as an ordinary string or sequence.

Representative categories:

- input integrity
- identity
- state/lifecycle
- currency
- monetary
- reconciliation reference
- reservation
- parlay
- correlation
- temporal
- source
- calculation
- bankroll reference

Representative blockers include:

- `EXP_RECORD_HASH_CONFLICT`
- `EXP_DUPLICATE_POSITION_ID`
- `EXP_DUPLICATE_RESERVATION_ID`
- `EXP_DUPLICATE_PARLAY_LEG_ID`
- `EXP_MISSING_CANONICAL_IDENTITY`
- `EXP_RECONCILIATION_REFERENCE_MISSING`
- `EXP_CURRENCY_MISMATCH`
- `EXP_CASH_SYNTHETIC_MIX`
- `EXP_INCOMPLETE_SNAPSHOT_MEASURE_SET`
- `EXP_SNAPSHOT_TOTALS_MISMATCH`
- `EXP_SNAPSHOT_COUNTS_MISMATCH`
- `EXP_SNAPSHOT_DIMENSION_MISMATCH`
- `EXP_DUPLICATE_MANUAL_EXECUTION`

## Compatibility and migration expectations

Phase 2A is additive and contract-only.

- Existing readers of legacy exposure shapes remain valid.
- Existing runtime integration paths are intentionally not rewritten in 2A.
- Phase 2B+ will route repository and API integration through these contracts,
  reconstruct persisted payloads, and require
  `validate_authoritative_projection_integrity(...)` before any projection can
  inform risk, sizing, or execution.

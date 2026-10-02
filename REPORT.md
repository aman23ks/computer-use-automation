# Design Report

## 1. Architecture

One Python process per run, async end to end. Two execution paths share one perception/targeting layer:

```
goal spec ──► DiscoveryAgent ──► Planner (Claude) ─┐            ┌─► ArtifactStore (versioned JSON)
                   │  policy gate, verify locator   │            │         │ approve
                   ▼                                ▼            │         ▼
             WebSurface + probe.js  ◄───────── FlowRecorder ─────┘   ReplayEngine (no LLM)
                   ▲         ▲                                          │   app profile states
                   │         └── ControlChannel ◄── operator console ◄──┘   escalation
                   └── network guard (allowlist) · redactor · RunRecorder (evidence)
```

Key decisions:

- **Perception through an in-page probe, not the app's DOM hygiene.** `probe.js` computes what an operator
  reads: role, accessible name, the *visual label* (adjacent table cell or preceding text, since legacy forms
  have no `<label>`), and table structure (column header + row key). The model sees a compact reading-order
  text rendering of every frame plus a masked screenshot. Playwright is only the transport. The same probe
  code resolves locators at replay, so "what was recorded" and "what is replayed" are computed identically.
- **The model picks elements; the recorder decides how to find them again.** When the model says "click e15",
  the recorder builds ranked strategies, keeps only those that resolve to *exactly that element* right now,
  and refuses the action otherwise. Every persisted locator was proven at record time.
- **Replay is a separate engine with no model dependency.** It is the production path, so it is small,
  deterministic, and has an explicit result contract.
- **Simple over distributed.** File-backed store, in-process console, no queues. The seams (Surface
  protocol, InterventionSink, secret provider, store) are where services would be split out later.
- **Model:** Claude Opus 5.5, manual tool-use loop, one strict-schema tool call per turn (so each recorded
  step maps to one action), adaptive thinking at `medium` effort, append-only history (keeps thinking
  blocks valid) with a prompt-cached prefix, server-side refusal fallbacks. The task is short and bounded; a
  manual loop gives the policy/approval gate full control between turns.

## 2. Artifact schema

`src/cua/artifact/schema.py` (Pydantic; `cua schema` exports JSON Schema). A capability is a callable
contract plus the flow that fulfils it:

| Section | Contents | Why |
|---|---|---|
| identity | `id`, semver `version`, `status` (draft/approved/deprecated), `content_hash` | Approval binds to an exact contract; the hash excludes review state and provenance, and loading rejects tampered files. A behavioural change bumps the minor version and resets to draft. |
| contract | typed `inputs` (pattern/enum, `sensitivity`), `secrets` (by name only), typed `outputs` (+ sensitivity), `outcomes` (business codes the caller must handle), `risk` (max effect, steps needing approval) | What a calling agent and a reviewer need without reading steps. `tool_definition()` turns it into a function-calling tool. |
| binding | `app.product`, `app.profile` (pinned `id@version`), `surface.kind`, `entry_url` with `{base_url}` | Tenant location is bound at deploy time, never by the caller. |
| steps | discriminated union by `action`: navigate, click, fill, select, press, extract. Each has `phase` (auth/main), human-readable `intent`, `effect` (read/input/navigate/write/irreversible), optional post-state `expect` checkpoint, `retry`, expected `dialog`, `requires_approval`, `recorded_by` (agent/human/author) | Effects drive both safety gating and safe-restart decisions. Human-recorded steps stay reviewable. |
| targets | `frame` (name path + URL fallback), `expect_role`, ranked `strategies`, each with `score` and `rationale` | Robustness reasoning is in the artifact, not in someone's head. |
| values | `literal` / `param` / `secret` expressions | Inputs and credentials never appear as values; validation checks every reference is declared. |
| success | checkpoint (all-of URL/text/element conditions) | Success is asserted, not assumed. |
| provenance | discovery run id, planner, recorded-against tenant, human step count | Traceability back to evidence. |

Locator ranking: `field_name` 0.92 (the form field name is the vendor's server contract, stable across tenants
and branding) > `role_name` 0.86 > `table_cell` 0.80 (column header + row key; row keys bound to an input
parameter when they match one, e.g. "the Select link in the row whose Member # is `{member_number}`") >
`label` 0.76 (visual proximity) > `css` 0.30 (structural last resort). Strategies that embed volatile or
sensitive data (balances, names, record values) are dropped; extract targets are never located by their value.

Integrity rules enforced at load: step ids unique, every param/secret reference declared, every output
extracted exactly once, `risk.max_effect` not understated, `requires_approval` only on irreversible steps,
approved status requires an approval record, PII/secret params cannot persist examples.

## 3. Determinism & error handling

**Determinism.** Replay executes the recorded steps in order with bound inputs. There are no sleeps: every
wait is a poll (150 ms) that races *the expected state* (the step's target uniquely resolvable, or its
checkpoint satisfied) against *the app profile's known states*, evaluated over one batched text snapshot per
poll (one evaluate per frame). A target resolves only if a strategy yields exactly one visible element of the
expected role. When the top two semantic strategies both resolve but to different elements, replay fails
with `LOCATOR_CONFLICT` rather than guess, because a wrong click in a bank is worse than a stop. Post-action
checkpoints are recorded automatically from what changed (frames that navigated, canonicalised to path
regexes with ids/nonces removed, plus newly appeared headings), and the next step's target doubles as a
readiness check.

**Runtime states** live in an **app profile** (`config/profiles/coreone-teller.yaml`), because "session
expired" or "record not found" belong to the vendor product, not to one flow, and one profile is shared by
every tenant and capability on that product:

| Class | Examples (mock app) | Behaviour |
|---|---|---|
| business outcome | `MEMBER_NOT_FOUND`, `ACCESS_RESTRICTED`, `VALIDATION_ERROR` | Stop; return `status: business_outcome` with code + app message. Not an error. |
| recoverable | maintenance interstitial, session expiry, informational alert | Bounded recovery (`max_times`), logged in `recoveries`. Session expiry replays the auth steps and restarts, **only if no state-changing main step has run**. |
| hard failure | app 500, sign-on rejected, unknown state, checkpoint timeout, unexpected confirm dialog | Stop with code, step, expected vs observed, masked screenshot and redacted DOM snapshot. |

Input validation runs before any UI action (`INPUT_INVALID`). Transient action errors retry per step policy;
navigation retries with backoff. Unexpected `confirm()` dialogs are dismissed (the safe default) and fail the
step; expected ones are declared on the step with a message regex.

**Result contract** (`replay/result.py`): exactly one of `outputs` (succeeded), `outcome` (business_outcome),
`approval` (needs_approval: stopped before an irreversible step), or `error` (failed: code, step, intent,
expected, observed, retryable, evidence paths); plus per-step reports (`resolved_by`, `degraded`, attempts,
timing), recoveries, interventions, and warnings.

**Drift (secondary).** If a step resolves by a lower-ranked strategy, replay still succeeds and emits
`locator.degraded`. This is the drift signal: `--repeat N` aggregates it into a stability file per
artifact and tenant.

## 4. Heterogeneity & multi-tenant

**Surface seam.** Steps speak surface-neutral concepts: frame/window path, role, accessible name, visual
label, table structure. `Surface` (`surface/base.py`) is the perception/action protocol; `WebSurface` is one
implementation. A *legacy web* app is the case already implemented (framesets, tables, JS links). A *desktop*
surface would implement the same protocol over UI Automation (Windows) or the AX API (macOS): those trees
expose roles, names and table patterns, so `role_name`, `label` and `table_cell` carry over unchanged, while
`field_name`/`css` are web-specific kinds that simply don't get generated. For surfaces with no usable tree
(Citrix, terminal emulators) a pixel surface would add a `visual` strategy kind (anchor text + relative
offset via OCR/grounding) scored below semantic kinds. The artifact schema already carries `surface.kind`,
and replay logic above the Surface protocol does not change.

**Multi-tenant reuse.** Three layers, most-shared first:

1. **App profile per vendor product/version range**: runtime states, auth recognition, benign dialogs.
2. **Base capability per product**: recorded once. Ranking favours tenant-invariant locators (form field
   names are the vendor's contract; branding changes labels, not field names).
3. **Tenant overlay** (`config/overlays/<tenant>/...`): only deltas, keyed by stable step ids and pinned to
   the base major version (prepend strategies, timeouts, success text). The effective artifact is recomputed
   and re-hashed at load; the base approval covers the shared flow, the overlay is reviewed separately.

Demonstrated: the Lakeshore variant (same product, "Find" instead of "Search", "Ledger Balance" instead of
"Current Balance", different brand) replays the unmodified base artifact successfully, with two
`locator.degraded` warnings; adding a two-step overlay restores fully semantic resolution. At scale, drift
detection is those degradation rates per (capability, tenant, product version) from production replays; a
spike after a vendor upgrade flags the tenant cohort for a profile bump or overlay, before hard failures.

## 5. Escalation & handoff

**Detect.** Discovery escalates on: an irreversible action (approval), no visible progress for 3 actions
(observation signature unchanged), the model calling `request_human`, repeated policy blocks, or planner
failure (refusal, API error). Replay escalates (with `--escalate`) on an approval requirement or any failure
except policy violations.

**Route.** An `Intervention` carries run and capability, the step and intent, the reason code, a message, a
redacted text rendering of the current screen and a masked screenshot. It is persisted to the run's
`interventions/` and sent to sinks (stderr + console here; a pager/queue in production).

**Control transfer** (`control/handoff.py`) is a per-session state machine:
`AUTOMATION → PENDING → HUMAN → AUTOMATION`. The automation blocks inside `escalate()` and asserts it holds
control before every action. `claim()` issues a lease token; every operator endpoint (live view, click, type,
key, resolve) requires it, so exactly one party controls the session and it is always known who. The human
acts on **the same browser context** (cookies, frames, server session), either through the console's
live view or directly in the window in `--headed` mode, where a probe binding captures their clicks and
changes. Human actions are logged with the same locator candidates as agent actions; in discovery they become
`recorded_by: human` steps.

**Hand back.** Decisions: `resume`, `approve`, `deny`, `abort` (timeout counts as abort). After `resume`,
replay picks the resume point from the page, not from assumptions: the failing step if its target now
resolves; otherwise the step after the latest step whose checkpoint holds; otherwise the next resolvable
step; if none, `HANDBACK_UNRESOLVED`. Skipped steps are reported as `skipped_by_handback`.

Mocked: the console is a bare page (polling screenshot, click-to-click, type, keys) rather than a real-time
co-browsing UI, and `cua operator-bot` stands in for a person in headless demos using the same HTTP API.

## 6. Safety

- **Allowlist in code, three places:** the action gate before every discovery/replay action (action types,
  origins, paths), a network route guard that aborts any browser request outside the allowlist (test hooks
  such as `/__faults` are unreachable by the agent), and load-time checks on artifacts.
- **Risk classes:** each action gets an effect from rules (name/URL patterns) plus form semantics (POST
  submit = write). *Irreversible* actions require operator approval during discovery and **per-invocation
  authorization** in replay (`--authorize-irreversible`, or an operator approval via escalation); otherwise
  replay returns `needs_approval` before touching the control. I chose "require authorization" over "block"
  because opening accounts is a legitimate capability, and over "flag" because money movement must be
  authorized by the caller who owns the business decision, not by the automation. If the live policy says a
  step is irreversible but the artifact does not declare it, replay refuses (`ARTIFACT_POLICY_MISMATCH`).
  Unattended replay also requires an approved artifact version.
- **Data handling:** secrets are referenced by name, resolved from a provider at execution time, typed via
  placeholders, and never shown to the model. Inputs are placeholders too: the model never sees the member
  number. The model never needs to read sensitive values, only point at them: extraction happens in the
  executor. Everything that leaves memory (model observations and screenshots, logs, artifacts, evidence) is
  redacted by registered values (inputs, secrets, outputs by sensitivity), format patterns (SSN, card,
  email, phone, dates, currency amounts) and label-aware masking (cells labelled Name/SSN/DOB/... or in such
  columns), which catches free-text PII like names that no regex can. Screenshots use element masks. Outputs
  are returned to the caller but masked in logs. Tests assert no seeded value appears in any evidence file.
- **Limits:** redaction is best-effort (unlabelled free-text PII can slip through); the model provider still
  receives page structure and non-masked text, so production needs a zero-retention agreement; the operator's
  live view is unmasked by design (they must see real data to act); rules classify risk by names and form
  semantics, so a mislabelled destructive button needs a profile rule; prompt injection from page content is
  mitigated by code-enforced policy, not by the prompt.

## 7. Cuts

Deliberately left out, with what I'd build next:

- **Desktop / pixel surfaces:** designed (section 4), not built. Next: a UIA-backed Surface on Windows.
- **Real co-browsing console, auth, and a queue for interventions:** bare console and stderr sink here.
- **Assisted fallback:** a single bounded, policy-checked LLM call to re-locate one failed target, recorded
  as evidence and proposed as an overlay, never silently persisted.
- **Profile learning:** discovery could propose new runtime states it meets (for human review) instead of
  profiles being hand-authored.
- **Parameterised recording beyond whole-value placeholders** (templates like `ID-{x}`), multi-row
  extraction (lists/tables as outputs), and branching flows (optional steps keyed on state).
- **Durable storage and concurrency:** a database-backed registry with review workflow, and session pools
  per tenant; the current file store and single-process model are sufficient to prove the contracts.
- **Traces:** Playwright traces capture unmasked DOM/screens, so they are off; next is a redacting trace
  exporter.

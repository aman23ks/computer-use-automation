| # | Run | What it shows | Status |
|---|---|---|---|
| 1 | [`01-discovery-read-balance`](01-discovery-read-balance/) | Scripted-planner discovery (stand-in for the LLM run): goal -> recorded capability artifact | `success` |
| 2 | [`02-replay-success`](02-replay-success/) | Deterministic replay, no LLM: outputs returned | `succeeded` |
| 3 | [`03-replay-member-not-found`](03-replay-member-not-found/) | Business outcome, not a crash | `business_outcome MEMBER_NOT_FOUND` |
| 4 | [`04-replay-access-restricted`](04-replay-access-restricted/) | Business outcome: permission denial | `business_outcome ACCESS_RESTRICTED` |
| 5 | [`05-replay-bad-input`](05-replay-bad-input/) | Input rejected by contract before any UI action | `failed INPUT_INVALID` |
| 6 | [`06-replay-interstitial-recovered`](06-replay-interstitial-recovered/) | Recoverable: maintenance interstitial acknowledged | `succeeded` |
| 7 | [`07-replay-session-expired-recovered`](07-replay-session-expired-recovered/) | Recoverable: session expiry -> re-authenticate and restart (no mutation yet) | `succeeded` |
| 8 | [`08-replay-app-error`](08-replay-app-error/) | Hard failure: HTTP 500 with step, expected/observed, screenshot and DOM evidence | `failed APP_ERROR` |
| 9 | [`09-replay-unknown-state`](09-replay-unknown-state/) | Hard failure: state unknown to the profile (no escalation) | `failed CHECKPOINT_TIMEOUT` |
| 10 | [`10-replay-human-handoff`](10-replay-human-handoff/) | Escalation -> operator takes the live session -> hand back -> resume at checkpoint | `succeeded` |
| 11 | [`11-tenant-lakeshore-no-overlay`](11-tenant-lakeshore-no-overlay/) | Same artifact on a rebranded tenant: succeeds, reports locator drift | `succeeded` |
| 12 | [`12-tenant-lakeshore-overlay`](12-tenant-lakeshore-overlay/) | Tenant overlay restores semantic locators without re-recording | `succeeded` |
| 13 | [`13-discovery-open-share-with-approval`](13-discovery-open-share-with-approval/) | Scripted-planner discovery (stand-in for the LLM run) of an irreversible flow; operator approves Submit | `success` |
| 14 | [`14-replay-share-needs-approval`](14-replay-share-needs-approval/) | Irreversible step without caller authorization: stops before Submit | `needs_approval` |
| 15 | [`15-replay-share-authorized`](15-replay-share-authorized/) | Caller-authorized irreversible replay: confirmation number returned | `succeeded` |
| 16 | [`16-replay-share-validation-error`](16-replay-share-validation-error/) | Business outcome from app validation (deposit below minimum) | `business_outcome VALIDATION_ERROR` |

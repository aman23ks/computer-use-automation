# Computer-Use Automation System

An LLM discovers how to do a task in a legacy back-office app **once**; the run is compiled into a typed,
versioned **capability artifact**; production invocations **replay it deterministically** with no model in
the loop, classify runtime conditions (business outcome / recoverable / hard failure), and **hand the live
session to a human** when they can't safely continue.

The target is a local mock of a credit-union teller console built to be hostile to automation: framesets,
table layouts, no `id`s, no `<label>`s, no test IDs, opaque field names (`q1`, `f2`), JS-only links, native
`confirm()` dialogs, plus injectable runtime faults and a second "tenant" variant with different branding.

Design rationale: [REPORT.md](REPORT.md). Run evidence: [evidence/](evidence/).

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
cp .env.example .env        # then set ANTHROPIC_API_KEY in .env
```

`.env` keys:

| Key | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Discovery runs only. Replay never calls a model. |
| `CUA_MODEL` / `CUA_EFFORT` | Defaults `claude-opus-5-5` / `medium`. |
| `CUA_FALLBACKS` | `1` (default) enables server-side refusal fallbacks; set `0` behind proxies that reject the beta. |
| `CUA_SECRET_TELLER_USERNAME`, `CUA_SECRET_TELLER_PASSWORD` | Operator credentials for the mock app. Artifacts reference secrets by name; values are resolved at execution time and never reach the model, artifacts, or logs. |

Start the two mock tenants (separate terminals, or background them):

```bash
python -m target_app --port 8601
```

```bash
python -m target_app --port 8602 --variant lakeshore
```

## Demo path

**1. Discovery (real LLM run).** Claude drives the app to the goal; the run is recorded as a draft artifact.

```bash
cua discover config/goals/read_primary_savings_balance.yaml --param member_number=100234
```

Prints the run id, the evidence directory under `runs/`, and the saved artifact
(`artifacts/coreone.member.read_primary_savings_balance/1.0.0.json`). An operator console is served at
http://127.0.0.1:8765 for any escalation during the run.

**2. Review and approve.** Unattended replay requires an approved version.

```bash
cua show coreone.member.read_primary_savings_balance
```

```bash
cua approve coreone.member.read_primary_savings_balance --by "reviewer@example.test"
```

**3. Deterministic replay** (no LLM). Returns a structured result; exit code 0 success, 2 business outcome,
3 needs approval, 1 failure.

```bash
cua replay coreone.member.read_primary_savings_balance --param member_number=100234
```

**4. Errors and exceptional states.**

```bash
cua replay coreone.member.read_primary_savings_balance --param member_number=555555
```

```bash
cua replay coreone.member.read_primary_savings_balance --param member_number=12AB
```

```bash
cua faults --add session_expire && cua replay coreone.member.read_primary_savings_balance --param member_number=100234
```

Other outcomes: member `100999` gives `ACCESS_RESTRICTED`. Other faults: `interstitial` (recovered),
`alert_dialog` (recovered), `app_error` (hard failure with screenshot + DOM evidence), `--slow-ms 2500`
(waits, no sleeps), `pwd_notice` (a state the profile doesn't know: see step 5).

**5. Human handoff on the live session.** Inject an unknown interstitial, replay with escalation, then take
control at http://127.0.0.1:8765 (click "Take control", click "Remind Me Later" on the live view, then
"Hand back to automation"). The replay resumes on the same browser session.

```bash
cua faults --add pwd_notice && cua replay coreone.member.read_primary_savings_balance --param member_number=100234 --escalate
```

Headless demos can use the scripted operator, which drives the same console API a person would:

```bash
cua operator-bot --click "Remind Me Later" --decision resume --note "dismissed password notice"
```

**6. Irreversible actions.** Opening a share account posts money. Discovery pauses for operator approval at
"Submit"; replay stops with `needs_approval` unless the caller authorizes the invocation.

```bash
cua discover config/goals/open_share_account.yaml --param member_number=100234 --param "share_type=Money Market" --param "nickname=Rainy Day" --param opening_deposit=25.00
```

```bash
cua operator-bot --decision approve --note "verified with member"
```

```bash
cua replay coreone.member.open_share_account --allow-draft --param member_number=100234 --param "share_type=Holiday Club" --param nickname=Trip --param opening_deposit=40.00 --authorize-irreversible
```

**7. Second tenant, same vendor product.** Replays the base artifact against the rebranded variant. Without
the overlay it still succeeds but reports `locator.degraded` drift; `config/tenants/lakeshore.yaml` applies a
tenant overlay that restores semantic locators without re-recording.

```bash
cua replay coreone.member.read_primary_savings_balance --tenant lakeshore --param member_number=100234
```

**8. Agent-facing catalog and stability.**

```bash
cua catalog
```

```bash
cua replay coreone.member.read_primary_savings_balance --param member_number=100234 --repeat 10
```

## Running without live services

- **No API key:** `--planner script:config/scripts/<name>.json` runs discovery with a deterministic scripted
  planner through the exact same pipeline (policy gate, locator verification, recording). Artifacts record
  `planner: scripted` in provenance.
- **Tests** start both mock tenants in-process and need no network or key:

```bash
pytest
```

## Layout

```
src/cua/
  agent/        goal spec, planners (Claude + scripted), discovery loop
  artifact/     schema (the contract), locator ranking, recorder, store (versioning), tenant overlays
  replay/       engine, app profiles (runtime states), result contract, input validation, secrets
  surface/      Surface protocol, WebSurface (Playwright transport), probe.js (perception + targeting)
  control/      control-transfer state machine, operator console
  safety/       policy (allowlist, risk classes), redaction
  evidence.py   structured run log, masked screenshots, redacted DOM snapshots
target_app/     mock legacy teller console with fault injection and a tenant variant
config/         policy, app profile, goals, tenants, overlays, scripted-planner scripts
evidence/       saved artifacts and logs from discovery and replay runs
```

# AFTERSHOCK

AFTERSHOCK is a local reliability-testing dashboard for mock agents and optional real Agentverse chat agents. Its
simulation engine is deterministic and uses simulated services; Agentverse runs send real messages and are labeled
separately. This is a local MVP, not a production deployment.

## Run locally

Use Python 3.10–3.12 for the optional Agentverse adapter. Install the dashboard requirements as shown below; the
simulation engine itself uses the Python standard library.

```powershell
python -m pip install -r backend/requirements.txt
Copy-Item .env.example .env
# Edit .env to configure Agentverse before running real-agent messages.
cd backend
python -m aftershock.web
```

Open `http://127.0.0.1:8000`, create a local account, register an agent, and run a scenario. Run the test suite from
`backend` with `python -m unittest discover -s tests -v`. The application stores
users, sessions, agents, scenarios, results, replay/fix/verification records, and audit events in SQLite
(`backend/aftershock.sqlite3` by default). Set `AFTERSHOCK_DATABASE` to choose another SQLite file. Existing local
databases are upgraded in place; keep a backup before using this MVP with important data.

## Workflows

- **Account and data isolation:** The dashboard requires a local username and password. Each account sees only its
  own agents, scenarios, and run history. Passwords use PBKDF2-SHA256; session tokens are random, stored hashed,
  expire after 12 hours, and use HttpOnly/SameSite cookies. Authentication and test operations are rate-limited
  in-process.
- **Agent registration:** `naive` and `resilient` are local simulated agents. `agentverse` selects the real hosted
  messaging adapter. The UI labels mock and real execution explicitly.
- **Scenario builder:** Edit validated scenario JSON, save it to SQLite, and load it from the saved-scenarios list.
- **Run history and metrics:** The dashboard reads persisted API data and distinguishes simulated pass rate from
  real-agent reply rate.
- **Replay, fix suggestions, verification:** Replays and verification run only on local mock agents. Fix suggestions
  are rule-based hypotheses and never change code. Verification requires explicit approval and is persisted.
- **Audit trail:** Successful registration, authentication, agent, scenario, run, replay, suggestion, and verification
  actions are recorded without storing passwords, session tokens, or the Agentverse API key.

## Agentverse messaging

Set `AGENTVERSE_AGENT_ADDRESS` and `AGENTVERSE_API_KEY` in `.env` or the process environment. Do not put credentials
in the dashboard or scenario JSON. The adapter uses signed uAgents mailbox messages; it reads the account API key as
required configuration but does not transmit it in message requests. `uagents` is installed on Python 3.10–3.12;
the rest of the local application can run without it.

The hosted agent must be deployed and implement the Agent Chat Protocol to reply. The message faults are:

| Fault | Effect |
|---|---|
| `none` | Send one real message with no injected fault |
| `delayed_reply` | Wait locally after a reply is received |
| `dropped_message` | Inject a local drop; no request is sent |
| `duplicated_message` | Send twice to the real agent; side effects may repeat |
| `malformed_payload` | Send malformed message content |

An Agentverse message outcome is real-agent evidence. Any injected fault is separately labeled simulated. Missing
credentials or messaging dependencies produce an explicit error and are not saved as successful runs. A live hosted
agent response has not been verified as part of this MVP work.

## API

The local same-origin JSON API includes:

| Route | Purpose |
|---|---|
| `POST /api/auth/register`, `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me` | Local session authentication |
| `GET /api/dashboard` | Account-scoped agents, runs, and separate metrics |
| `GET|POST /api/agents` | List or register mock/Agentverse agents |
| `GET|POST /api/scenarios` | List or validate and save scenarios |
| `GET|POST /api/runs` | List evidence or run a scenario |
| `GET /api/runs/{id}` | Read evidence and persisted analysis |
| `POST /api/runs/{id}/replay`, `/fixes`, `/verify` | Replay, suggest hypotheses, or run explicitly approved local verification |

## Scope and deployment limitations

The engine includes deterministic simulation, fault validation, evidence-based findings, replay, rule-based fix
hypotheses, verification, and scorecards. The dashboard/API now provides local authentication, owner-scoped SQLite
persistence, scenario management, execution history, and analysis actions.

This MVP is not hardened for public or multi-worker deployment: it uses SQLite, process-local rate limits, no
password recovery or role-based administration, and a loopback-only development server without TLS. Do not expose it
directly to the internet. PostgreSQL/deployment migrations, an AI fix-suggestion provider, live event streaming,
trace import, and a verified live Agentverse conversation remain out of scope.

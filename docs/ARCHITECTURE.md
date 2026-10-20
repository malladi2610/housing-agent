# How the agent works

The application has two processes and one durable tracker:

```mermaid
flowchart LR
    UI[FastAPI UI] --> DB[(SQLite tracker)]
    IMAP[Read-only alert mailbox] --> W[Persistent Python worker]
    W <--> DB
    W --> G[LangGraph extraction workflow]
    G --> LC[LangChain runnable]
    LC --> O[Ollama Cloud model chosen by stage]
    G --> CP[(Graph checkpoints)]
```

## From n8n to this project

| n8n concept | Here |
| --- | --- |
| Trigger | UI queues an action; worker periodically queues an IMAP poll |
| Node | A Python function, LangGraph node or connector |
| Data passed between nodes | Typed `WorkflowState` and Pydantic models |
| IF/filter node | `matching.evaluate()` and explicit Python conditions |
| AI node | `ChatOllama` composed with `StrOutputParser` in `llm.py` |
| Workflow execution history | Jobs, events, model-call counters and graph checkpoints |
| Wait/resume | Durable `due_at` jobs; LangGraph checkpoints resume interrupted analysis |
| Credential | Backend environment or ignored local secret file |

LangChain is used for model invocation and message composition. It does not poll websites or act as the scheduler. LangGraph runs the listing extraction/validation sequence and checkpoints it. The tracker stores shared facts and conversations across jobs/models.

## Follow one listing

1. `Store.add_listing()` normalizes the input and deduplicates its source ID. Unknown costs/locations stay unknown.
2. `matching.evaluate()` applies the user's all-in rent, accommodation, date and verified-distance constraints.
3. An `analyze` job is atomically claimed by the worker. The LangGraph `extract` node calls the configured GLM model through LangChain.
4. Pydantic rejects malformed output. Evidence checks and manual-field protection are applied before storing inferred fields. Ordinary Python recalculates eligibility.
5. A `draft` job uses approved profile facts and listing context to ask Kimi for a short message. The result is a draft, not a send.
6. The owner copies/sends on the platform and records the exact saved text, or the calibrated Android adapter checks the live listing/conversation and sends an authorized enquiry. A follow-up job is persisted after recorded delivery.
7. An incoming reply invalidates pending drafts and cancels queued follow-ups. The reply model may propose a response; ambiguous dates and document requests require owner input.
8. Viewings are proposed or explicitly confirmed, with confirmation evidence and a conflict/travel-buffer check. The search continues until marked complete.

## Why there is no unlimited autonomous loop

Each job has a bounded unit of work. Jobs persist between wakes, so the worker can idle without keeping an LLM busy. Model output has no authority to change the profile, open arbitrary tools or certify a delivery. More capable models still need fact and action validation.

Read `models.py`, `matching.py`, `llm.py`, `workflows.py`, and `worker.py` in that order. `db.py` owns transactions; `web.py` owns the user-facing actions. `connectors.py` retains the inspection tools; the Android modules below own the calibrated action boundary.

## macOS and physical Android extension

The original LangGraph extraction and LangChain model routing remain in place. `android.py` supplies the deterministic device boundary around them: bounded Appium polling imports source text, then the existing persistent job queue runs extraction/matching and drafting. It reopens the listing and compares its source fingerprint before any enquiry.

`android_config.py` describes the owner-calibrated UI contract; it contains no guessed platform profile. `android_phone.py` speaks Appium's W3C HTTP API with explicit UDID selection and no reset/auto-launch. `android_store.py` owns an exclusive device lease and a durable delivery ledger. The worker persists the send intent before the click and records success only after exact outgoing-message proof. An uncertain side effect is never blindly retried.

On macOS, user LaunchAgents supervise web/worker/Appium and `caffeinate -i` prevents idle sleep while the worker runs. Android remains on the physical phone; local model weights and a nested emulator are unnecessary. A foreground app change or disconnect pauses phone access while the tracker and other imports continue.

This follows the same n8n mapping: Appium poll is a trigger, SQLite jobs are the durable queue, LangGraph handles the stateful model workflow, Python performs the checks, and the ledger is the side-effect boundary. Hardware/app selectors still require the documented on-device pilot.

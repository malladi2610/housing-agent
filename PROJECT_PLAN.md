Munich Flat Agent — proposed build blueprint

Design recorded 18 October 2026. A runnable first version now exists; README.md describes its implemented capabilities and current limits. Initial housing criteria and Ollama Cloud as the inference provider are confirmed; remaining personal profile details, exact mobile app, account access, and model access/budget remain to be configured. The user authorized GitHub publication under malladi2610, with 3-vivek-3 as a fallback. No platform integration has been tested against the user's account or Windows laptop.

**Goal**

Run a small, self-hosted assistant that discovers Munich rental listings, checks them against the user's requirements, writes short personal applications, follows conversations toward a viewing, and shows the progress of every application in a private web UI. Keep running until the user marks the search complete; scheduling a viewing alone does not stop the search. A viewing or rental outcome cannot be guaranteed.

**Current search profile — supplied by the user**

| Criterion | Requirement | Interpretation/status |
| --- | --- | --- |
| Accommodation | Studio or shared accommodation/WG room | Both accepted; no preference between them specified. |
| Monthly budget | Maximum EUR 850 total per month; lower is better | Confirmed to include electricity and internet, along with rent, heating/building charges, and other mandatory recurring housing costs. A warm-rent figure alone is insufficient evidence of affordability. |
| Target move-in | By 15 November | Interpreted as 15 November 2026 from the current date. Later starts are acceptable within the deadline below. No earliest acceptable date has been specified. |
| Latest move-in | 1 January, inclusive | Interpreted as 1 January 2027. A listing available earlier may still fit if the tenancy can begin by the deadline; confirm the actual start date and rent liability. |
| Office | Münchner Technologiezentrum, Agnes-Pockels-Bogen 1, 80992 München | Confirmed by the user; also verified against the official MTZ contact page. |
| Distance | Prefer very close; user stated no more than 5–6 km | Use 6 km as the provisional outer limit and prioritize closer options. Whether the limit means route distance or straight-line distance remains open. |
| Commute | Compare public transport and cycling; shorter is better | Both modes confirmed by the user. Show travel time and route distance separately for each; no maximum number of minutes has been specified. Public-transport estimates should include access walking, transfers, and egress when a routing source supports them; show the departure/arrival time used. |

Filter on the confirmed all-in budget cap, accommodation types, and latest move-in. Compute monthly total from non-overlapping known components: warm rent plus electricity, internet, and any other mandatory recurring housing costs not already included. Record which charges are included to avoid double counting. Missing extra-cost amounts mean "needs cost confirmation"; do not invent allowances or mark the listing as a confirmed budget match. Show known deposits and one-off charges separately. Preserve unknown dates for review. Show total monthly cost, distance, travel time when available, and move-in date separately so the user can compare cheaper options with shorter commutes; no fixed trade-off weight has been agreed.

For geography, use a straight-line radius only as an initial candidate screen, then verify route distance and travel time for the chosen commute mode. A 6 km straight-line radius does not prove a 6 km journey. If only an approximate listing location is available, label the estimate and require review near the boundary. No real commute calculation or listing search has been performed yet.

**Working scope and assumptions**

- One user, one active search profile, one worker, and modest listing volume.
- Confirmed hardware: an always-on Windows laptop with approximately 32 GB RAM and a processor the user described as "AMD 395 max"; likely Ryzen AI Max+ 395, but the exact model has not been verified. The Android phone is the user's personal everyday phone. Windows version/edition, virtualization readiness and available disk space remain to be checked. Plan sustained mobile operation around an emulator pilot rather than requiring the personal phone to stay connected.
- Python throughout, with LangChain as requested. Use LangGraph for explicit workflow state and resumable pauses.
- A basic server-rendered UI avoids requiring a separate JavaScript application or build system.
- The user selected Ollama Cloud API, with different models for different workflow stages. Inference is remote; no local model weights or Ollama daemon are required for direct cloud requests. Choose specific models through the capability checks and task evaluation below. Local inference is outside the selected deployment.
- Discovery runs continuously. Outbound messages use configured Europe/Berlin contact hours and platform limits.
- Start with draft review while checking message quality and platform access. Enable unattended sending per connector only after that connector's permitted access, delivery confirmation, and reply ingestion work.
- If a platform has no permitted sending integration, the UI offers the draft and an “open listing” link, and records a send only after the user confirms it. This fallback is assisted operation, not full automation.
- Do not promise invisible automation. Do not implement CAPTCHA bypass, fingerprint masking, account rotation, or attempts to work around a platform block. A challenge or explicit access denial pauses that connector and alerts the user.

**What is known about the sources**

| Source | Evidence checked | Proposed first connection | Open issue |
| --- | --- | --- | --- |
| WG-Gesucht | Its help page documents saved searches and notification emails. Its terms restrict spam and reproduction of offers without prior consent. | Read alerts from a dedicated mailbox/folder; extract only the listing details present in the alert. | Alert content, delivery delay, reply-notification content, and permitted message transport require verification. Receiving an alert does not establish permission to automate site interactions. |
| flatsforfriendz, if this is the app intended | Its current site has an Explore feed and promotes native-app access to new listings, notifications, and messaging. | Check supported alerts, shared links, and available integration access first. | Exact app identity and phone OS; no supported public automation API was established by this research. A public feed does not establish permission to automate it or expose private-network listings. |
| ImmoScout24 | Official search guidance describes saved searches and delivery of new listings. | Add saved-search email ingestion after the first source works. | Alert timing, completeness, terms, and messaging access need a separate connector review. |
| immowelt | Official help documents search alerts and email delivery. | Reuse the email-ingestion pattern with a source-specific parser. | Same connector-specific checks; do not assume email notifications accept replies. |

An email-first integration reduces website traffic, but its freshness is limited by the provider's notification schedule. “Whenever a listing appears” means as soon as a supported event reaches the service; it cannot guarantee coverage of every new or private listing. Missing fields stay unknown and enter review rather than being invented or silently accepted.

**Application workflow**

1. Receive a listing event from a supported alert, permitted source connection, or manual import.
2. Normalize the source ID, canonical link, rent, cost components, location, size, availability, lease duration, description, and event timestamp. Preserve evidence and distinguish warm rent from cold rent; utilities may still be extra.
3. Deduplicate by source and listing ID. Treat changes as new versions of the same listing. Flag likely cross-platform duplicates for review rather than automatically merging unrelated flats.
4. Apply hard requirements with ordinary Python rules: budget, flat/WG type, move-in dates, lease duration, location, and must-haves. Unknown required information goes to review.
5. Use LangChain structured output to extract remaining text fields and explain softer preferences. Use a model only where language interpretation is useful.
6. Draft a message using the listing, the approved personal profile, the chosen language, and a few writing samples supplied by the user.
7. Validate that the draft has a real listing reference, contains only supported personal claims, answers requested application questions, and has no unresolved placeholders. Route uncertain or sensitive cases to review.
8. Deliver through a permitted connector, or present a draft for the user to send. Record submitted/sent only with connector confirmation or explicit manual confirmation.
9. Associate incoming replies with the correct conversation. Classify questions, requests for documents, viewing invitations, rejection, and unclear replies.
10. Draft an answer or propose viewing times from the user's availability. Keep a proposed appointment separate from a confirmed one.
11. Confirm a viewing only when date, time, timezone, and the other party's acceptance are established. Reserve travel/buffer time and prevent conflicting commitments. A calendar export can be offered without requiring a calendar integration in version one.
12. Schedule at most one initial follow-up after a user-configurable delay, proposed default three days. Recheck the latest thread before sending; cancel the follow-up after any reply, rejection, closure, or existing appointment. Respect requests not to follow up.

Before each outbound action, recheck current listing status, latest conversation version, action authorization, and whether the same action was already attempted. Changing the profile or message after approval invalidates that approval.

**Message style**

Use a warm greeting, one genuine detail from the listing, one relevant personal fact, and a clear request to view. Aim for roughly 60–100 words, adjusting to what the advertiser asks. Use simpler wording and the user's own examples instead of generic compliments. Do not fabricate income, employment, hobbies, German fluency, documents, or a connection to the advertiser.

Illustrative shape only; bracketed details must come from verified inputs before sending:

> Hi [name], your flat in [area] caught my eye, especially [specific detail]. I'm [name], [brief approved introduction], and I'm looking to move from [date]. [One relevant fact about why the flat fits.] I'd love to come and have a look. Would [available time] work for you? Best, [name]

Support separate tones for a private landlord and a WG household. User edits can inform future drafts after the user accepts them as a preference. Natural writing is a quality goal; it is not a claim that a particular message will be perceived as human-written.

**Small implementation stack**

| Component | Responsibility |
| --- | --- |
| Python + LangChain + Ollama Cloud | Stage-specific model routing, prompts, validated extraction and reply drafting |
| LangGraph | Listing/conversation workflow, conditional routing, persistent checkpoints and review/resume |
| Pydantic | Typed listing, profile, message and viewing data; validation outside the model |
| SQLite | Tracker records, source cursors, durable jobs, message outbox and action history |
| Persistent graph checkpointer | Restore paused workflows; evaluate SQLite for the single-worker pilot and use PostgreSQL if concurrency/reliability requirements warrant it |
| Small Python scheduler loop | Wake only for due jobs and events; persist due times in the database |
| FastAPI + Jinja2/HTMX | Private web interface and small application API |
| Docker Compose | Portable optional packaging for the web process and worker; the preferred private desktop VM can also run the Python services directly |

The worker runs independently of the UI. Closing the browser must not stop the search. No Redis, Celery, vector database, or multi-agent system is needed for this initial scope. Do not make a model sit in an endless polling loop.

LangChain's current agents build on LangGraph. Its persistence documentation distinguishes per-conversation checkpoints from information shared across conversations. The application should preserve the same distinction: one thread per listing conversation; a separate approved profile shared across listings.

**Ollama Cloud model routing — confirmed provider, provisional assignments**

The user explicitly selected Ollama's hosted cloud API, not a local installation or a hybrid. Use the direct API at https://ollama.com and the native /api/chat contract. Store the Ollama credential only in the backend's secret configuration under OLLAMA_API_KEY; never in browser code, tracker records or GitHub. Authentication, account-specific access and live calls have not been tested. Self-hosted memory remains on the laptop; content included in model requests is processed by Ollama Cloud.

Use LangChain's ChatOllama integration where the installed version supports the direct endpoint and required behavior. Verify remote authentication, model discovery/init behavior, tools, image payloads, streaming and errors before relying on integration-level feature claims. A narrow LangChain Runnable wrapping the official Ollama SDK is an implementation option if the generic integration assumes local-only endpoints. The provider and native API remain Ollama in either case.

| Stage | Initial cloud candidate | How to choose and verify |
| --- | --- | --- |
| Budget arithmetic, date/radius rules, deduplication, job timing | Ordinary Python; no model | Deterministic rules and verified input evidence control eligibility and side effects. Route missing facts to review. |
| Text extraction and reply classification | glm-5.3-flash | Benchmark field accuracy on German/English listings, correct unknowns and reply intent. Escalate a failed validation to glm-5.3 once. |
| Warm application and routine follow-up drafts | kimi-k3 | Compare factuality, user edits, German/English naturalness and listing specificity. Text-only inputs suffice for this stage. Compare glm-5.3-flash as a bounded alternative. |
| Complex reply interpretation and proposed viewing arrangements | glm-5.3 | Check date/time ambiguity, competing conditions and requests outside the profile. Python validates availability/conflicts and the user resolves missing facts. Compare kimi-k3 as an alternative. |
| Screenshot interpretation for browser/emulator interaction | kimi-k3 | Requires vision support. Test reading small text, identifying the correct target and verifying the resulting screen. Compare glm-5.3-flash as an alternative or bounded fallback. |

The user asked to prioritize GLM, Kimi, and a third name interpreted provisionally as Qwen. The initial assignments now use those verified GLM/Kimi cloud variants. These are starting candidates, not a claim of measured superiority or lowest cost. The public cloud /api/tags response checked on 18 October 2026 includes glm-5.3-flash, glm-5.3 and kimi-k3, all with completion/tools/thinking. Only the Flash GLM variant and Kimi K3 also advertise vision; the glm-5.3 flagship is text-only in this catalog. Public catalog visibility does not prove account access, endpoint feature support or useful performance on these tasks. The capability labels are selection filters, not task evaluation results.

Qwen is a candidate family for comparison if that is what the user meant by "went". No Qwen model was returned by the current public Ollama Cloud /api/tags snapshot, and the qwen3.5 library page checked does not advertise cloud availability. Do not invent a cloud identifier or assume that a downloadable vision model is hosted. Keep Qwen optional/unassigned until the exact intended variant and cloud access are verified. This does not change the user's cloud-only inference preference.

Direct cloud API model IDs come from https://ollama.com/api/tags; do not derive them by appending the app/CLI's :cloud or -cloud suffixes. Cloud hosting covers a subset of the full Ollama library. Expose the supported catalog in settings, but only route unattended work to an explicitly enabled, evaluated model assignment. Refresh availability at startup and periodically; detect retirements without silently substituting an untested model. The review UI should show each stage's configured model, last successful check, latency, validation failures, escalations and usage where available.

Ollama's current structured-output documentation says cloud does not support enforced structured outputs. For cloud extraction and action proposals, request a small JSON object in the prompt, parse it, validate it with Pydantic, then apply factual/business constraints. Do not send a JSON-schema format parameter on the assumption that ChatOllama's advertised structured-output support applies to cloud. One bounded repair and one approved fallback are proposed limits. Invalid, contradictory or unsupported outputs become Needs review; a stronger model cannot establish a missing utility price, rental date or landlord confirmation. Tool-call responses, where supported by the selected model/endpoint, still require argument validation and action checks.

Keep routing deterministic by stage to avoid a separate model call merely to select another model. Reuse one model for several roles when it passes their checks; the current assignments use three distinct GLM/Kimi models. All fallback routes remain disabled until evaluated. Use text/accessibility observations when sufficient, and invoke vision for screens that require it. Limit prompt history to approved profile facts, relevant messages and current evidence. Store canonical state in SQLite so model changes do not lose memory.

Before changing the defaults, evaluate a small representative set, proposed at least 20 listing texts, 10 reply threads and 10 UI screenshots. Measure field/date accuracy, unsupported personal claims, correct recipient/target selection, draft edits, latency and usage. Hard-rule failures disqualify a model for unattended use. Do not replace a passing model solely because another model is larger or newer. Record model identifiers, catalog digests where available, prompt/config versions and concise validation outcomes; do not require or persist private model reasoning.

Respect Ollama plan limits and configure bounded retries, a total call budget per job, and a daily usage ceiling. Account-wide quota exhaustion should pause/defer model work; switching models is not assumed to restore quota. Leave the numeric daily budget for the user to configure after the Ollama plan is known. The companion config/model-routing.example.json is a secret-free design configuration, not a running router; model roles remain disabled for unattended use until verified.

**Memory and tracker data**

- Search profile: hard requirements, preferences, availability, language, approved personal facts, message examples, and policy settings.
- Listings and versions: source identifiers, known attributes, evidence, timestamps, match explanation, and availability state.
- Conversations and messages: external thread/message IDs, direction, original text where needed, draft versions, and delivery evidence.
- Viewings: proposed slots, accepted slot, location, timezone, confirmation evidence, cancellation, and completion.
- Jobs and outbox: due time, lease/claim, attempt count, action identity, current state, next retry time, and last error.
- Events: an append-only history of source ingestion, user edits, approvals, send attempts, replies, and status changes.
- Graph checkpoints: workflow progress, stored separately from the editable tracker records.

Suggested visible progression:

New → Matched → Draft ready → Message sent → Response received → Viewing proposed → Viewing confirmed → Viewed → Offer/decision → Secured

Other outcomes: Does not match, Needs review, Rejected, Withdrawn, Listing closed, and Archived. Delivery has its own Draft/Queued/Sent/Failed/Unknown state, so a transport issue does not overwrite the housing stage. Reply classification does not prove that a viewing is confirmed.

**Basic web UI**

- Overview: matches, applications sent, replies awaiting action, confirmed viewings, and upcoming work.
- Listing board/table: filter by source, stage, price, area, and next action.
- Listing detail: description evidence, match reason, message history, draft editor, activity timeline, and viewing details.
- Search profile: criteria, availability, language, factual introduction, and example writing.
- Agent controls: pause/resume globally or per source, stop the search, contact hours, follow-up delay, Ollama model assignment per stage, and daily model-usage limit.
- Health: last attempted check, last successful check, source lag, authentication state, worker heartbeat, and actionable errors. An empty feed must be distinguishable from a broken connector.

**Mobile integration feasibility**

First verify the exact application and whether supported notifications, sharing, or integration access cover discovery and replies. Notifications may contain only a teaser; they are not a substitute for the full conversation.

If native UI control is permitted and necessary, Appium with Android UIAutomator2 is a possible separate connector. It supports real Android devices and emulators; that does not establish compatibility with this particular app. Prove login, listing reading, conversation identification, sending confirmation, and reply retrieval on the user's own account before committing to unattended use.

The Android phone is the user's personal everyday phone, so it is not the planned dependency for continuous mobile control. With approximately 32 GB RAM reported, prefer a pilot using one persistent Android Emulator on the Windows host, controlled through Appium with UIAutomator2. Keep its virtual device and application data separate from the personal phone. The user signs into the rental app interactively on the virtual device; do not clone the personal phone or copy unrelated credentials.

Before adopting this connection, prove that the intended flatsforfriendz app is available for the selected emulator image, installs and starts normally, accepts legitimate sign-in, and exposes readable listing/conversation content. Verify session persistence across restarts and whether concurrent use on the personal phone is supported. An emulator-support or authentication restriction is a feasibility result to report, not something to work around. If it is unsupported, retain the email/manual path and evaluate a dedicated physical Android device separately.

Run the accelerated emulator directly on Windows, alongside the private desktop VM, to avoid nesting it inside another VM. Use a compatible virtualization configuration for both. Android's official documentation recommends Windows Hypervisor Platform (WHPX) and requires AMD virtualization/SVM to be enabled on applicable machines. Verify hardware acceleration with the emulator's own acceleration check on the actual laptop. Appium remains local to Windows, with only the scoped private connection accessible to the guest. These are proposed settings; nothing has been installed or tested on the laptop yet.

This work stays outside the core workflow so that the email/manual connectors and tracker remain useful if mobile integration is unavailable.

**Optional desktop connection — Cua Driver**

The user proposed Cua Driver after observing ineffective computer use in Hermes. Treat it as a candidate for permitted desktop interactions, with reliability established by a pilot. It is not a guarantee that a website will regard activity as human or accept automation from an authenticated account.

The official Cua documentation describes screenshot/accessibility inspection and desktop input, with MCP, CLI, Python and TypeScript integration. The driver executes actions; the agent framework owns model decisions and the action loop. Proposed connection: LangGraph workflow → narrowly scoped application tools → Cua Driver → dedicated browser inside the private desktop environment. Keep the tracker, scheduler, message drafts, and durable jobs independent of this connection.

Hermes already documents Cua Driver integration. A failure observed in Hermes cannot be attributed to its framework without evidence: driver installation/compatibility, OS permissions, a reachable desktop, browser targeting, model perception, and result verification must be checked separately. Switching the orchestration framework does not by itself repair these issues.

For the pilot:

1. The host OS is Windows. Establish a persistent desktop session in the selected private guest environment. A terminal-only Linux container is insufficient without the necessary display/accessibility services. Validate the guest OS, driver support, laptop capacity, and hypervisor before choosing the final deployment. If using the native Windows driver, launch its daemon in the interactive user's session: the official documentation warns that Windows Session 0 (services/SSH) has no desktop. A background worker's healthy status does not prove desktop tools work.
2. Use a dedicated browser profile in that desktop. The user signs in interactively and completes authentication challenges; persist the profile securely without exposing unrelated personal browsing data.
3. Prove read-only navigation on an intended source: locate one listing, extract the correct ID/text, open its conversation, and recover after a normal restart.
4. Target the current window and fresh observed elements. After meaningful actions, re-observe the UI and check the expected state rather than assuming a click succeeded. Persist listing/conversation IDs outside screen coordinates.
5. Before a permitted send, verify listing, recipient, exact draft version, and current thread. Confirm the outgoing message in the conversation afterward. An ambiguous result enters Unknown delivery state and reconciliation; never blindly repeat Send.
6. Restrict the desktop tool's apps/domains and available actions, keep a human takeover/pause control, and retain minimal redacted failure evidence. Pause on lost authentication, unexpected dialogs or challenges, or repeated errors.
7. Measure success rate, latency, resource use, and model cost on a bounded pilot before committing to unattended 24/7 use. Occasional authentication or UI repair may still require the user.

Cua Driver's documented native desktop targets are macOS, Windows and Linux. It does not directly establish an Android/iOS connection for flatsforfriendz. An Android emulator window could be a separate proof of concept, but adds the emulator's requirements and may expose limited accessible UI; the existing mobile feasibility work remains necessary.

**Private hosting and 24/7 operation**

The confirmed host is a Windows laptop. To meet the user's private-OS requirement, the preferred topology is a small Linux desktop VM on that laptop, subject to architecture, RAM and virtualization checks. Put the worker, private UI, SQLite files, Cua Driver, and dedicated browser inside the guest. Use a supported Linux desktop/accessibility stack and verify actual browser behavior. Keep persistent state on the guest disk with independent backups. Start with the Python services directly inside the VM; Docker remains optional packaging for the worker/UI and a future VPS deployment. Avoid adding another virtualization layer solely for packaging.

Run the Android Emulator and Appium/ADB on the Windows host. Expose only a small authenticated connection to the guest, limited to the intended virtual device/app and required actions. Keep raw Appium/ADB endpoints local; do not expose them to the internet or the wider LAN. This host-side device connection is a deliberate exception to guest isolation, and must not grant general host filesystem or desktop access. The personal phone remains independent of this runtime. A dedicated physical Android device is a fallback only if the emulator pilot establishes incompatibility.

Docker Desktop with a WSL 2 backend is an alternative for the non-GUI worker/UI, but does not by itself supply the private desktop or let a Linux container control the Windows desktop. Containers share their runtime's kernel; a VM supplies its own OS kernel. Do not treat a browser profile or a separate Windows user account as equivalent to a private VM.

The earlier two-CPU/2-GB estimate covers the email/API-model service only; it is not an estimate for Windows plus a desktop VM, browser, and Android emulator. For the reported 32 GB machine, a provisional starting allocation is 6 GB RAM for the Linux desktop VM and 4 GB for a single Android virtual device. These are tuning estimates, not measured minimums or reserved capacity guarantees. Leave room for Windows, GPU/shared memory and ordinary applications, and adjust after measuring load. Ollama Cloud inference does not require loading these model weights into laptop memory. Model usage depends on listing volume, prompt size and the selected Ollama plan; define a daily cap rather than claiming a fixed monthly bill. Laptop power, sleep/hibernate behavior, network continuity, scheduled OS restarts, VM startup, and driver/session recovery are part of the sustained-operation pilot. Validate behavior with the host screen locked and remote-viewer disconnected; do not ask the user to weaken the host lock screen.

For sustained operation:

- Configure host/VM startup and guest service recovery for the selected deployment. If using Docker, start the runtime as required and restart exited processes. Use a supervisor or explicit recovery process for hung workers; a container healthcheck alone does not restart an unhealthy container.
- Persist source cursors, graph state, due jobs and outbox records. Reconcile overdue jobs after a restart without flooding contacts.
- Use bounded retries and respect provider retry guidance. Authentication failures and challenges require a user-visible pause.
- Give the worker a heartbeat and monitor it from outside the process. Alert on prolonged source failure or stalled jobs.
- Back up SQLite consistently with its backup mechanism and test a restore. Keep encrypted backups outside the main machine.
- Run as a non-root user; expose only the UI through authenticated private access or authenticated HTTPS. Do not mount the Docker socket or unrelated host directories.
- Keep credentials, device sessions, the personal profile, messages and database out of GitHub. Redact operational logs and disable external model tracing by default.
- Ollama Cloud receives the prompt text and screenshots sent to it; self-hosting the application does not make inference local. Minimize personal data and crop screenshots to the intended app/window. Keep external tracing off by default and choose retention settings during setup.

**Reliability and action boundaries**

- Use database uniqueness constraints to deduplicate ingestion and outbound action identities. A checkpoint alone cannot prevent duplicate real-world messages.
- If delivery times out after a possible successful submission, mark it Unknown and reconcile with the platform or ask for review. Do not blindly resend. Exactly-once delivery cannot be promised for transports without suitable guarantees.
- Claim jobs atomically and allow only one active send per conversation. Recheck for a new reply before follow-up or appointment actions.
- Treat listing descriptions, email content, and landlord messages as untrusted data. They cannot change the system's instructions, reveal credentials, or grant tools new permissions.
- Give model steps narrowly scoped inputs and structured outputs. The model has no unrestricted shell or access to the entire mailbox, filesystem, or credentials.
- Limit automated communication to the user's rental search and confirmed facts/availability. Route document requests, payments, deposits, contract terms and unclear commitments to the user; they are outside the initial agent's authority.
- Maintain a pause/stop control and immediately stop outbound activity when the search is marked complete.

**Build order and evidence of completion**

1. Confirm the search profile and exact app. Run the bounded Windows/emulator feasibility pilot before making native-app automation a dependency. Check representative alert/reply formats and permitted access for each source. Output: a connector capability matrix covering discover/read/send/read-replies/confirm-delivery, plus evidence of normal emulator installation, sign-in, readable content and session persistence.
2. Build the persistent tracker, manual import, profile editor, private UI and durable worker. Output: listings and due work survive a restart; the UI shows real source/worker health.
3. Add the first email-alert parser and LangChain extraction/drafting. Output: representative fixtures prove correct deduplication, unknown-field handling, hard-filter decisions and factual drafts.
4. Add reply ingestion, follow-up scheduling, and viewing proposals. Output: duplicate events, replies arriving during a queued follow-up, conflicting viewing slots and ambiguous dates are handled correctly.
5. Enable a permitted delivery connector after a bounded end-to-end check. Output: evidence of one confirmed send and associated reply, plus a tested ambiguous-delivery recovery path. Where sending is unsupported, label the manual handoff explicitly.
6. Investigate the native-app connector separately. Output: measured resource needs and a demonstrated supported path, or a documented assisted fallback.
7. Run a sustained pilot, restart and restore checks, document setup, pin dependencies, and publish the code to the user's chosen GitHub repository. Include Docker configuration, a secret-free example configuration, synthetic demo data, meaningful tests, and a short guide explaining LangChain/LangGraph's roles.

The user supplies the GitHub owner/repository and chooses visibility before publication. This blueprint does not create or push a repository. GitHub Actions may run tests but is not the proposed permanent worker host.

**Inputs to resolve before implementation**

- Occupants, minimum size/rooms, and any preference between studios and WG rooms remain open. The EUR 850 all-in ceiling and office address are confirmed.
- Distance measurement and inferred date years remain open. Public transport and cycling are both confirmed comparison modes. Minimum lease duration, furnished/unfurnished, registration (Anmeldung), and other must-haves remain open.
- Exact Flats for Friends app link; existing saved searches and available alert channels. The Android phone is confirmed as the user's personal everyday device. Emulator compatibility remains untested.
- A short truthful personal introduction, approved facts, preferred German/English tone, example messages, and viewing availability.
- Ollama Cloud is the confirmed provider. Account access, plan/usage budget, and evaluated model assignments remain to be established; do not request a plaintext key in chat. Access to a dedicated mailbox/folder and desired notification channel remain open. The Windows laptop, approximately 32 GB RAM and user-reported AMD 395 Max processor are recorded; verify exact CPU, Windows version/edition, virtualization readiness and free disk space during preflight.
- GitHub destination and visibility; credentials and personal search data stay local to the deployment.

**Primary sources checked**

- MTZ official address: https://mtz.de/arbeiten-im-mtz/kontakt/
- WG-Gesucht saved searches and notification emails: https://www.wg-gesucht.de/hilfebereich.html
- WG-Gesucht terms, especially section 7: https://www.wg-gesucht.de/agb.html
- flatsforfriendz public discovery: https://flatsforfriendz.com/en/explore
- flatsforfriendz product site and native-app features: https://flatsforfriendz.com/
- flatsforfriendz terms: https://flatsforfriendz.com/en/terms-and-conditions
- ImmoScout24 saved searches: https://www.immobilienscout24.de/suchen-finden-einziehen/suche.html
- immowelt search assistance: https://www.immowelt.de/suchassistent
- immowelt search help: https://www.immowelt.de/hilfekontakt/suchender/immobiliensuche
- LangChain overview: https://docs.langchain.com/oss/python/langchain/overview
- LangGraph persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- LangGraph interrupt/resume: https://docs.langchain.com/oss/python/langgraph/interrupts
- Ollama direct cloud API, authentication and model naming: https://docs.ollama.com/cloud
- Ollama public cloud model names and capabilities: https://ollama.com/api/tags
- GLM 5.3 Flash image/tool support: https://ollama.com/library/glm-5.3-flash
- GLM 5.3 text/tool support: https://ollama.com/library/glm-5.3
- Kimi K3 multimodal/tool support: https://ollama.com/library/kimi-k3
- Qwen 3.5 library entry, distinct from cloud access: https://ollama.com/library/qwen3.5
- Ollama structured outputs and cloud limitation: https://docs.ollama.com/capabilities/structured-outputs
- LangChain ChatOllama integration: https://docs.langchain.com/oss/python/integrations/chat/ollama
- Appium Android driver and requirements: https://github.com/appium/appium-uiautomator2-driver
- Cua Driver capabilities/platforms: https://cua.ai/docs/cua-driver
- Cua Driver agent connections and separation from model orchestration: https://cua.ai/docs/cua-driver/guides/connect-your-agent
- Cua Driver VM/remote and Windows desktop-session requirements: https://cua.ai/docs/cua-driver/guides/vms-and-remote
- Docker Desktop WSL 2 backend: https://docs.docker.com/desktop/features/wsl/
- Hermes computer-use integration: https://hermes-agent.nousresearch.com/docs/user-guide/features/computer-use
- Appium iOS system requirements: https://appium.github.io/appium-xcuitest-driver/latest/getting-started/system-requirements/
- Android emulator acceleration and restrictions: https://developer.android.com/studio/run/emulator-acceleration
- Docker container/VM distinction: https://docs.docker.com/get-started/docker-concepts/the-basics/what-is-a-container/

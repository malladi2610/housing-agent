# MacBook + physical Android phone

Homeward now runs natively on macOS or Windows. The same Android adapter can use your USB phone or a supported emulator. This guide uses your existing Android phone, connected to the MacBook by a data-capable USB cable. An iPhone is not supported by this adapter.

## Install and start on the Mac

Install [Homebrew](https://brew.sh) if it is not already available. Clone the repository, or download/extract its source ZIP if Git is not installed:

```bash
git clone https://github.com/malladi2610/housing-agent.git
cd housing-agent
bash scripts/install-macos-tools.sh
bash scripts/setup-macos.sh
bash scripts/start-macos.sh
```

For an existing checkout, stop its services, run `git pull --ff-only`, then run the setup/start scripts again. Do not copy the Windows `.venv` directory to macOS.

The tools script uses your existing Homebrew installation to install Python 3.12, Node 22, Java 17 and Android command-line tools. It asks you to review Android SDK licenses, installs platform/build tools, and installs Appium 3 and UiAutomator2 in ignored project-local directories. `brew --prefix` handles Apple Silicon and Intel layouts. It does not install local language-model weights.

The start script registers **user LaunchAgents** for the UI and worker, plus Appium when the tools script has configured it. They restart after a process failure and run after you log into macOS. They do **not** start before login. Appium listens on `127.0.0.1:4723`; the web UI defaults to `http://127.0.0.1:8000`.

Open the UI, set the Ollama Cloud key under **Models & settings**, and complete **Search profile**. The existing GLM/Kimi routing is retained. Add your actual first name, truthful introduction and viewing availability before enabling automatic messages.

Check services or stop/remove their login entries:

```bash
.venv/bin/python scripts/macos-services.py status
bash scripts/stop-macos.sh
```

Stopping removes only Homeward's own service entries. It preserves `data/`, your key and queued work. Logs are in `data/web.stderr.log`, `data/worker.stderr.log` and `data/appium.stderr.log` (and corresponding stdout files). A green worker heartbeat is separate from the phone's connection state.

## Connect and inspect the phone

1. Enable Developer options and **USB debugging** on your own Android phone. Connect a data-capable cable. Unlock the phone and approve the Mac's debugging key on the phone. No root, wireless ADB or account-password export is needed.
2. Install/open the real Flatsforfriends app normally and sign in yourself. Keep it in the foreground for the first pilot.
3. In Homeward, open **Android app → Check USB phones**. It shows the actual UDID and the foreground app's package/activity. Copy those three values into the settings and save with monitoring disabled.
4. Use **Capture current phone screen** to obtain the accessibility node list and screenshot. Capture the feed, a listing, and its conversation in turn. The node picker copies resource IDs/accessibility descriptions into your selected control role; it does not click on the phone. Save the mapping before capturing another screen.
5. Map the feed/detail roles, then the conversation roles. Prefer resource IDs. `card_identity` is relative to a card; an XPath for it must be relative, such as `.//...`, rather than searching the whole screen. All other selectors address the current screen.
6. With monitoring paused and the phone on a populated feed, run **Run one-listing pilot**. It opens one listing, reads its details, checks the expected conversation/header and controls, then returns to the feed. It never enters text or sends. Inspect the resulting tracker entry and verify that the card identity is immutable and unique.
7. Choose **Send verified matches automatically**, enable monitoring, acknowledge the checked identity/control mapping, and save. Changing the phone or its screen mapping requires another pilot. Automatic follow-up sending is a separate checkbox and requires an incoming-message selector.

The app's package, activities and selectors are deliberately not guessed. Your installed version must expose readable accessibility nodes and a reliable listing identity. If it does not, automatic sending cannot be calibrated safely with this adapter; you can still use email imports and manual enquiries. This repository does not ship a tested Flatsforfriends-specific selector profile, and no physical phone was available in the build environment.

### What each selector must mean

| Role | Required observation |
| --- | --- |
| `feed` | Unique feed-screen marker, absent on detail/conversation screens |
| `cards` | Repeated clickable listing cards |
| `card_identity` | Relative child exposing a stable unique ad identity, not rent, a relative timestamp or a repeated generic heading |
| `detail`, `title`, `body` | Unique detail marker, exact title, and source facts/description text (or a readable container) |
| `contact` | Optional exact advertiser label on the detail screen |
| `open_conversation` | Control that opens the selected listing's own conversation |
| `thread_identity` | Conversation header exactly equal to the captured title or contact, according to the setting |
| `composer`, `send` | Exact message field and its send control |
| `outgoing` | Own outgoing message text, excluding timestamps; exact body is the delivery receipt |
| `incoming` | Other party's message text, disjoint from outgoing; enables reply polling and follow-up checks |
| `refresh`, `challenge` | Optional explicit refresh button and login/verification marker |

Text outside the configured detail area is not inferred. If details require scrolling, configure a small number of detail scrolls and verify that prices/date/address/body are captured. Ads outside the configured page/card bounds are not guaranteed to be seen. Contacted ads that disappear from those feed pages cannot have their conversations polled through this adapter; open the platform conversation manually or use the existing email notification path.

## Continuous operation and actual sends

The pipeline is **poll feed → deduplicate → capture → GLM extraction → Python matching → Kimi draft → reopen and compare listing → verify thread → compose exact text → persist send intent → click once → observe exact outgoing bubble → record sent**.

- Unknown all-in costs, dates or route distance stay in **Needs review**. The model cannot invent evidence. This release still does not obtain live public-transport/cycling routes; enter verified routing information in the listing facts. Saving verified facts can queue a fresh draft for an eligible captured phone listing.
- Enquiries are sent only for verified matches, with current profile/listing versions, grounded listing detail and no model request for missing input. An edited draft needs exact manual approval. Initial messages are not sent into an existing conversation automatically.
- Default polling is every three minutes, bounded to two pages/four ads per check. The default phone ceiling is five send attempts per UTC day. Automatic enquiries wait for the profile's Europe/Berlin contact hours. These limits are editable.
- When the incoming selector is configured, the worker rotates through up to two contacted listings per poll and imports visible incoming bubbles. It deduplicates them and cancels follow-ups. This is bounded UI history, not a guaranteed full platform inbox sync.
- One follow-up is scheduled using the existing delay. Its automatic delivery is off unless you enable it; it rereads the conversation immediately before composing. Reply suggestions and proposed viewings remain for review; the agent does not automatically accept appointments, submit documents or make payments.
- If the phone is disconnected, locked or in another app, the worker waits and checks again later. It does not unlock it, disable screen protection, steal focus from another app or bypass a challenge. Unknown screens/calibration changes can pause the phone adapter while other import paths remain available.
- If a click might have sent but proof is unavailable, the ledger stays **uncertain**, prevents further sends, and never repeats that click after a restart. Check the phone conversation and use **Delivery records → Reconcile**. A verified non-send needs an explicit retry under **Work & activity**.

Keep the Mac on power with its lid open. The worker runs under `caffeinate -i` to prevent **idle** system sleep; this does not override lid closure, manual sleep, shutdown, logout, power loss or phone locking. Your personal phone cannot simultaneously be an always-available automation device and be freely used elsewhere: monitoring waits during personal use. A dedicated phone is more practical for uninterrupted operation.

Native mode uses a private project data directory on your Mac, not a separate operating system. If you still want the separate OS described in the original plan, use a VM and a separately configured authenticated private Appium bridge. Docker Desktop does not automatically expose a physical USB phone to a Linux container; use native mode for this first pilot.

## Move the existing tracker from Windows

Pause phone monitoring and the search, then stop both web/worker processes on Windows. Create a consistent tracker backup with the existing `flat-agent backup` command. Transfer that backup privately to the Mac's `data/tracker.sqlite3` while the Mac services are stopped. Transfer your `.env`/local key separately if desired, or re-enter the key. Graph checkpoints can be transferred only while stopped; they are optional for a fresh pending-job resume. Do not transfer a live SQLite database without its WAL or use a plain live file copy.

Keep the original backup. Do not run two active copies against the same account/phone. Start the Mac services and resume the search worker from My shortlist while keeping phone monitoring disabled. Recheck the USB device/app selectors, run the pilot, then enable phone monitoring. The database gains additive phone tables automatically; existing listings, profile, conversations and viewing records remain intact.

References: [Android USB debugging](https://developer.android.com/studio/run/device), [Appium requirements](https://appium.io/docs/en/latest/quickstart/requirements/), [UiAutomator2](https://github.com/appium/appium-uiautomator2-driver).

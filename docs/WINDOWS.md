# Windows host and Android pilot

The target is a Windows laptop with around 32 GB RAM and an AMD processor described as “395 Max.” Exact OS/CPU/firmware settings have not been inspected. The personal Android phone remains independent.

## First run from this repository

After the project source has been published, install Git and Python 3.11+ on the laptop. Open a local PowerShell terminal in a folder where you keep projects, then run:

```powershell
git clone https://github.com/malladi2610/housing-agent.git
cd housing-agent
.\scripts\check-windows-host.ps1
.\scripts\setup.ps1
.\scripts\start.ps1
```

The source ZIP is an alternative if Git is unavailable: extract it, open PowerShell in that folder, and run the same three scripts. Open **http://127.0.0.1:8000**, add the Ollama Cloud key in **Models & settings**, and complete **Search profile**. No model weights are downloaded to the laptop. Keep the key in the local settings or ignored `.env`, never in the repository.

Check that **Work & activity** shows a recent worker heartbeat before queuing work. If the browser cannot connect or the heartbeat is stale, inspect `data/web.stderr.log` and `data/worker.stderr.log`. Stop the local processes with `.\scripts\stop.ps1` before restarting. The launcher does not register itself to start after reboot or keep the laptop awake.

These steps must execute in the laptop's own terminal. A Codex client running on Windows does not by itself give its cloud execution environment access to Windows, Android or the local browser. Verify the selected execution environment before asking an assistant to run them.

## Staged setup

1. Run `scripts/check-windows-host.ps1`. It only reports relevant OS/CPU/RAM/virtualization fields.
2. Start the Python UI and worker directly on Windows to validate the tracker and Ollama calls.
3. For a private OS, provision a Linux desktop VM using a hypervisor compatible with your Windows edition and Android acceleration setup. Move the Python application into that VM, and use a dedicated guest browser/profile. Install Cua inside the desktop it controls.
4. Run a single persistent Android Emulator on the Windows host, alongside the VM. The official Android docs recommend Windows Hypervisor Platform (WHPX); enable required AMD SVM/virtualization according to the laptop vendor's instructions. Do not nest an accelerated emulator inside the agent VM.
5. Install the intended app normally from its supported distribution and sign in yourself. Check that it supports the emulator and concurrent use with your personal phone. If it refuses, stop the mobile pilot and use the assisted/manual path or a separate dedicated physical phone.

The RAM starting estimates are 6 GB for a Linux desktop VM and 4 GB for the Android virtual device. Measure RAM, CPU, temperature and power use. No local language-model weights need loading; inference is Ollama Cloud.

## Android inspection

Install the Android SDK platform tools/emulator, a compatible Java JDK, Node.js and Appium/UiAutomator2 according to their current requirements. On the Windows host:

```powershell
npm install -g appium@3
appium driver install uiautomator2
appium driver doctor uiautomator2
adb devices
appium --address 127.0.0.1 --port 4723
```

With the emulator running, obtain the real package/activity for the normally installed rental app. Do not guess package IDs. From the project environment, create an inspection session with the verified identifiers:

```powershell
.\.venv\Scripts\python.exe scripts/android-session.py --udid emulator-5554 --package VERIFIED_PACKAGE_ID --activity VERIFIED_LAUNCH_ACTIVITY
```

Use the returned ID on **Connections → Inspect the Android emulator**. The app captures the source and PNG screenshot without input actions. The optional vision job sends a screenshot to Ollama and proposes a next action for review.

The script launches the selected app through Appium and keeps its existing data (`noReset`). It does not install the rental app, log in or send messages. The session has a 10-minute idle timeout. Finish a session through Appium when done.

If the agent is inside a VM, a host-loopback Appium server is not automatically reachable from the guest. Keep raw Appium on Windows loopback. Set up an authenticated, narrowly scoped private bridge before connecting the guest; this repository does not provision that bridge. For the first inspection pilot, run the UI locally on Windows. Never solve reachability by exposing unauthenticated Appium or ADB to the LAN/internet.

## Cua inspection

Install Cua Driver in the same OS desktop as the intended browser, then use **Connections → Check local connections**. The current probe calls `list_apps` and checks a usable response; it does not prove any platform-specific task works.

Cua's native Windows driver needs an interactive desktop session. Windows services/SSH in Session 0 have no normal desktop. Run the driver daemon in its interactive session and use the documented transport if driving it remotely. In a Linux VM, use a supported display/accessibility stack and verify the actual browser behavior.

## Before relying on 24/7 operation

Test an ordinary restart, host screen lock, remote-viewer disconnect, network interruption and expiring app login. The web UI, worker, Cua desktop and emulator have separate health states. Keep normal host screen-lock protection; establish an appropriate private guest session for computer use.

The application never claims a message was sent by Cua/Appium in this release. When future platform adapters are implemented, require exact recipient/body verification, visible delivery evidence, and reconciliation after an uncertain result. A click alone is not a delivery receipt.

Primary setup references:

- https://developer.android.com/studio/run/emulator-acceleration
- https://github.com/appium/appium-uiautomator2-driver
- https://cua.ai/docs/cua-driver/guides/vms-and-remote

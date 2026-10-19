"""Create a session for an installed rental app on your own emulator.

This does not install the app, sign in, click, or send a message.
"""
import argparse

import httpx

from flat_agent.connectors import AndroidInspector

parser = argparse.ArgumentParser()
parser.add_argument("--url",default="http://127.0.0.1:4723")
parser.add_argument("--udid",default="emulator-5554")
parser.add_argument("--package",required=True,help="The package ID of the normally installed rental app")
parser.add_argument("--activity",required=True,help="The app's launch activity, verified on your emulator")
args=parser.parse_args()
inspector=AndroidInspector(args.url)
caps={"platformName":"Android","appium:automationName":"UiAutomator2","appium:udid":args.udid,"appium:appPackage":args.package,"appium:appActivity":args.activity,"appium:noReset":True,"appium:newCommandTimeout":600}
try:
    with httpx.Client(timeout=120,trust_env=False) as client:
        response=client.post(inspector.base_url+"/session",json={"capabilities":{"alwaysMatch":caps}})
        response.raise_for_status()
        identifier=response.json()["value"]["sessionId"]
    print(f"Inspection session: {identifier}")
    print("Paste this ID into Connections to capture the current screen. This session expires after 10 minutes without a command.")
except (httpx.HTTPError,KeyError,ValueError):
    raise SystemExit("Could not create the inspection session. Check Appium, UiAutomator2, the emulator and the verified package/activity.")

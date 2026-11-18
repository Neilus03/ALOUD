"""Install the "Read with ALOUD" Quick Action (right-click > Services, or ⌃⌥R in any app)."""
import plistlib
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

from ..paths import SUPPORT

NAME = "Read with ALOUD"
SCRIPT = r'''# Send the selected text to ALOUD (start it first if needed)
TEXT="$(cat)"
[ -z "$TEXT" ] && exit 0
send() {
  printf "%s" "$TEXT" | %PYTHON% -c 'import sys, json, urllib.request
from pathlib import Path
key = Path(sys.argv[1]).read_text().strip()
request = urllib.request.Request("http://127.0.0.1:47812/read",
    data=json.dumps({"key": key, "text": sys.stdin.read()}).encode(),
    headers={"Content-Type": "application/json"})
urllib.request.urlopen(request, timeout=5).close()' %KEY_PATH% 2>/dev/null
}
if ! send; then
  open -b org.aloud-reader.app || exit 1
  for i in $(seq 1 40); do sleep 0.5; send && exit 0; done
fi
'''


def main():
    command = SCRIPT.replace("%PYTHON%", shlex.quote(sys.executable)).replace(
        "%KEY_PATH%", shlex.quote(str(SUPPORT / "bridge-key")))
    root = Path.home() / "Library" / "Services" / f"{NAME}.workflow" / "Contents"
    root.mkdir(parents=True, exist_ok=True)
    info = {
        "NSServices": [{
            "NSMenuItem": {"default": NAME},
            "NSMessage": "runWorkflowAsService",
            "NSSendTypes": ["NSStringPboardType", "public.utf8-plain-text"],
            "NSIconName": "NSTouchBarListViewTemplate",
        }],
    }
    plistlib.dump(info, open(root / "Info.plist", "wb"))
    u = lambda: str(uuid.uuid4()).upper()
    action = {
        "AMAccepts": {"Container": "List", "Optional": True, "Types": ["com.apple.cocoa.string"]},
        "AMActionVersion": "2.0.3",
        "AMApplication": ["Automator"],
        "AMParameterProperties": {"COMMAND_STRING": {}, "CheckedForUserDefaultShell": {}, "inputMethod": {},
                                  "shell": {}, "source": {}},
        "AMProvides": {"Container": "List", "Types": ["com.apple.cocoa.string"]},
        "ActionBundlePath": "/System/Library/Automator/Run Shell Script.action",
        "ActionName": "Run Shell Script",
        "ActionParameters": {"COMMAND_STRING": command, "CheckedForUserDefaultShell": True, "inputMethod": 0,
                             "shell": "/bin/zsh", "source": ""},
        "BundleIdentifier": "com.apple.RunShellScript",
        "CFBundleVersion": "2.0.3",
        "CanShowSelectedItemsWhenRun": False,
        "CanShowWhenRun": True,
        "Category": ["AMCategoryUtilities"],
        "Class Name": "RunShellScriptAction",
        "InputUUID": u(), "OutputUUID": u(), "UUID": u(),
        "Keywords": ["Shell", "Script", "Command", "Run", "Unix"],
        "UnlocalizedApplications": ["Automator"],
        "arguments": {
            "0": {"default value": 0, "name": "inputMethod", "required": "0", "type": "0", "uuid": "0"},
            "1": {"default value": False, "name": "CheckedForUserDefaultShell", "required": "0", "type": "0", "uuid": "1"},
            "2": {"default value": "", "name": "source", "required": "0", "type": "0", "uuid": "2"},
            "3": {"default value": "", "name": "COMMAND_STRING", "required": "0", "type": "0", "uuid": "3"},
            "4": {"default value": "/bin/sh", "name": "shell", "required": "0", "type": "0", "uuid": "4"},
        },
        "isViewVisible": 1,
        "location": "309.000000:253.000000",
        "nibPath": "/System/Library/Automator/Run Shell Script.action/Contents/Resources/Base.lproj/main.nib",
    }
    wflow = {
        "AMApplicationBuild": "523",
        "AMApplicationVersion": "2.10",
        "AMDocumentVersion": "2",
        "actions": [{"action": action, "isViewVisible": 1}],
        "connectors": {},
        "workflowMetaData": {
            "applicationBundleIDsByPath": {}, "applicationPaths": [],
            "inputTypeIdentifier": "com.apple.Automator.text",
            "outputTypeIdentifier": "com.apple.Automator.nothing",
            "presentationMode": 11,
            "processesInput": False,
            "serviceInputTypeIdentifier": "com.apple.Automator.text",
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceProcessesInput": False,
            "systemImageName": "NSTouchBarListViewTemplate",
            "useAutomaticInputType": False,
            "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
        },
    }
    plistlib.dump(wflow, open(root / "document.wflow", "wb"), fmt=plistlib.FMT_XML)
    # keyboard shortcut ⌃⌥R for the service
    subprocess.run(["/usr/bin/defaults", "write", "pbs", "NSServicesStatus", "-dict-add",
                    f'"(null) - {NAME} - runWorkflowAsService"',
                    '{"enabled_context_menu" = 1; "enabled_services_menu" = 1; "presentation_modes" = {ContextMenu = 1; ServicesMenu = 1;}; "key_equivalent" = "^~r";}'],
                   check=False)
    subprocess.run(["/System/Library/CoreServices/pbs", "-update"], check=False)
    print("installed", root.parent)


if __name__ == "__main__":
    main()

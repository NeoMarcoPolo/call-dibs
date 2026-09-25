#!/bin/bash
# call-dibs — SwiftBar/xbar menu bar plugin.
# Top bar shows "dibs ✋<n> ⏳<m>" (n claimed, m waiting in line), "dibs ✓" when all free.
# Under SwiftBar each claimed device has a "Force release…" item, behind a confirm dialog.
# Start the UI with:  open -a SwiftBar      Quit it from the dropdown.
# ".5s" in the filename = refresh every 5 s.
#
# <xbar.title>call-dibs</xbar.title>
# <xbar.version>v0.4.0</xbar.version>
# <xbar.desc>Who has claimed which shared device (dibs ledger)</xbar.desc>
# <xbar.dependencies>python3,dibs</xbar.dependencies>
#
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

if [ "$1" = force ] && [ -n "$2" ]; then
  # Menu action: break the lock on $2 (a device or a group tag) after a confirm.
  # It releases as the holder the dialog showed, so a lock that changed hands
  # while the dialog was open is left alone.
  holder=$(dibs status "$2" --json | python3 -c 'import json, sys
print(next((r["owner"] for r in json.load(sys.stdin) if r.get("owner")), ""))')
  [ -n "$holder" ] || exit 0
  osascript - "$2" "$(dibs status "$2")" >/dev/null 2>&1 <<'OSA' || exit 0
on run argv
  display dialog "Force-release " & item 1 of argv & "?" & return & return & item 2 of argv with title "dibs" buttons {"Cancel", "Force release"} default button "Cancel" cancel button "Cancel" with icon caution
end run
OSA
  dibs release "$2" --owner "$holder" >/dev/null 2>&1
  exit 0
fi

dibs status --xbar || echo "dibs ⚠ | color=#e05d44"
echo "---"
echo "Quit | bash=/usr/bin/pkill param1=-x param2=SwiftBar terminal=false"

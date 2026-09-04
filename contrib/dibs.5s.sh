#!/bin/bash
# call-dibs — SwiftBar/xbar menu bar plugin.
# Top bar shows "dibs ✋<n>" when n resources are claimed, "dibs ✓" when all free.
# Start the UI with:  open -a SwiftBar      Quit it from the dropdown.
# ".5s" in the filename = refresh every 5 s.
#
# <xbar.title>call-dibs</xbar.title>
# <xbar.version>v0.3.1</xbar.version>
# <xbar.desc>Who has claimed which shared device (dibs ledger)</xbar.desc>
# <xbar.dependencies>python3,dibs</xbar.dependencies>
#
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

dibs status --xbar || echo "dibs ⚠ | color=#e05d44"
echo "---"
echo "Quit | bash=/usr/bin/pkill param1=-x param2=SwiftBar terminal=false"

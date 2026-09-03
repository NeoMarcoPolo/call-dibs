#!/bin/bash
# call-dibs — SwiftBar/xbar menu bar plugin.
# Top bar shows "dibs ✋<n>" when n resources are claimed, "dibs ✓" when all free.
# Install (SwiftBar):
#   open "swiftbar://addplugin?src=https://raw.githubusercontent.com/NeoMarcoPolo/call-dibs/main/contrib/dibs.5s.sh"
# or copy this file into your plugins folder. ".5s" in the name = refresh every 5 s.
#
# <xbar.title>call-dibs</xbar.title>
# <xbar.version>v0.3.0</xbar.version>
# <xbar.desc>Who has claimed which shared device (dibs ledger)</xbar.desc>
# <xbar.dependencies>python3,dibs</xbar.dependencies>
#
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
exec dibs status --xbar

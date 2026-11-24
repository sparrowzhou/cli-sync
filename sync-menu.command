#!/usr/bin/env bash
# Open the menu when launched from Finder.
cd "$(dirname "$0")" || exit 1
exec bash ./menu.sh

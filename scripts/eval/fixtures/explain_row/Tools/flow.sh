#!/bin/bash
# A tiny fake test flow: each step records a verdict row.
. "$(dirname "$0")/verdict.sh"

wallet=$(read_wallet)
check "wallet seed" "[ \"$wallet\" = 500 ]" "wallet read back as $wallet"
check "tier seed" "[ \"$(read_tier)\" = 3 ]" "tier read back"

#!/usr/bin/env bash
# Wrapper: open a Mail.app draft to security@stripe.com with the
# coordinated disclosure body. Does NOT send.
#
# Usage: bash docs/send_stripe_security_email.sh

set -euo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
BODY_FILE="$SCRIPT_DIR/stripe_security_email_body.txt"
SCPT_FILE="$SCRIPT_DIR/send_stripe_security_email.applescript"

if [[ ! -f "$BODY_FILE" ]]; then
  echo "Missing body file: $BODY_FILE" >&2
  exit 1
fi

if [[ ! -f "$SCPT_FILE" ]]; then
  echo "Missing AppleScript file: $SCPT_FILE" >&2
  exit 1
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script only runs on macOS (uname=$(uname -s))." >&2
  exit 1
fi

echo "Opening Mail.app draft to security@stripe.com ..."
echo "Body source: $BODY_FILE"
echo "Review the draft, edit placeholders ([Your name], etc.), then send manually."
echo

osascript "$SCPT_FILE" "$BODY_FILE"

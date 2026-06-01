#!/bin/bash
# One-time setup: generate an SSH key and install it on the RPI.
# After this runs, push_to_pi.sh works without any password prompts.
#
# Run this once per development machine. Requires the RPI password ("dietpi").
# Uses SSH_ASKPASS_REQUIRE=force to supply the password non-interactively.
#
# Usage: bash scripts/setup_ssh_key.sh

set -euo pipefail

RPI="root@100.78.224.28"
KEY_PATH="${SSH_KEY:-$HOME/.ssh/phenohive_rpi}"
RPI_PASSWORD="dietpi"

if [ -f "$KEY_PATH" ]; then
  echo "Key already exists at $KEY_PATH"
  echo "Testing if it works..."
  if ssh -i "$KEY_PATH" -o StrictHostKeyChecking=no -o BatchMode=yes "$RPI" "true" 2>/dev/null; then
    echo "Key auth works — no setup needed."
    exit 0
  fi
  echo "Key exists but auth failed. Re-installing public key on RPI..."
else
  echo "Generating SSH key at $KEY_PATH ..."
  ssh-keygen -t ed25519 -f "$KEY_PATH" -N "" -C "phenohive-dev"
fi

# Write a temporary askpass helper
PASS_SCRIPT=$(mktemp /tmp/ssh_pass_XXXX.sh)
echo "#!/bin/bash" > "$PASS_SCRIPT"
echo "echo '$RPI_PASSWORD'" >> "$PASS_SCRIPT"
chmod +x "$PASS_SCRIPT"

echo "Installing public key on $RPI ..."
DISPLAY=:0 SSH_ASKPASS="$PASS_SCRIPT" SSH_ASKPASS_REQUIRE=force \
  ssh -o StrictHostKeyChecking=no "$RPI" \
  "mkdir -p ~/.ssh && echo '$(cat "$KEY_PATH.pub")' >> ~/.ssh/authorized_keys && sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && chmod 700 ~/.ssh"

rm -f "$PASS_SCRIPT"

echo "Verifying key-based auth..."
if ssh -i "$KEY_PATH" -o StrictHostKeyChecking=no -o BatchMode=yes "$RPI" "true" 2>/dev/null; then
  echo "Success! Key auth is working."
  echo "You can now run: bash scripts/push_to_pi.sh"
else
  echo "ERROR: Key auth still not working. Check RPI SSH config."
  exit 1
fi

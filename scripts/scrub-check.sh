#!/usr/bin/env bash
# Scrub gate: refuse to ship private infrastructure details.
#
# Generic patterns live here. Machine-specific strings (your real tailnet
# name, hostnames, etc.) go in scripts/scrub-private-patterns.txt — one
# extended regex per line — which is GITIGNORED so the secrets it guards
# against never appear in the repo itself.
set -euo pipefail
cd "$(dirname "$0")/.."

PATTERNS=(
  'tskey-[A-Za-z0-9-]{10,}'                                      # Tailscale auth keys
  'tail[0-9a-f]{6,}\.ts\.net'                                    # real (hex-style) tailnet names
  '\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b'  # CGNAT / tailnet IPs
  '\b192\.168\.[0-9]{1,3}\.[0-9]{1,3}\b'                         # RFC1918 LAN IPs
  '/share/Container'                                             # NAS filesystem paths
  'AKIA[0-9A-Z]{16}'                                             # AWS-style access keys
  'Basic [A-Za-z0-9+/]{16,}={0,2}'                               # baked basic-auth headers
)

EXCLUDES=(--exclude-dir=.git --exclude-dir=node_modules --exclude-dir=dist
          --exclude-dir=target --exclude-dir=.reference-backend
          # Virtualenvs are gitignored build debris, and third-party packages
          # inside them legitimately contain RFC1918 example IPs and base64
          # runs that trip the AKIA pattern.
          --exclude-dir=.venv --exclude-dir=venv --exclude-dir=__pycache__
          --exclude='scrub-check.sh' --exclude='scrub-private-patterns.txt')

# Known-benign literals (e.g. the CGNAT range constant itself, synthetic test
# IPs) — exact strings, one per line, COMMITTED (unlike the private patterns).
ALLOW=scripts/scrub-allowlist.txt

filter_allowed() {
  if [[ -f "$ALLOW" ]]; then grep -v -F -f "$ALLOW" || true; else cat; fi
}

fail=0
for p in "${PATTERNS[@]}"; do
  hits=$( { grep -rInE "${EXCLUDES[@]}" -e "$p" . || true; } | filter_allowed)
  if [[ -n "$hits" ]]; then
    echo "$hits"
    echo "SCRUB FAIL: pattern matched: $p" >&2
    fail=1
  fi
done

if [[ -f scripts/scrub-private-patterns.txt ]]; then
  while IFS= read -r p; do
    [[ -z "$p" || "$p" == \#* ]] && continue
    if grep -rInE "${EXCLUDES[@]}" -e "$p" . ; then
      echo "SCRUB FAIL: private pattern matched" >&2
      fail=1
    fi
  done < scripts/scrub-private-patterns.txt
fi

if [[ $fail -eq 1 ]]; then
  echo "Scrub check FAILED — remove the flagged content before committing." >&2
  exit 1
fi
echo "Scrub check passed."

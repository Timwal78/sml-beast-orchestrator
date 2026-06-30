#!/usr/bin/env bash
set -euo pipefail
FAIL=0
PATTERNS=('mock_data' 'fake_data' 'placeholder' 'demo_mode' 'SIMULATED' 'hardcoded.*price' 'TODO.*implement' 'FIXME.*later' 'sample_response' 'test_wallet')
EXCLUDE_DIRS=('.git' 'node_modules' 'vendor' '__pycache__' '.venv')
BUILD_EXCLUDE=''
for d in "${EXCLUDE_DIRS[@]}"; do BUILD_EXCLUDE="$BUILD_EXCLUDE --exclude-dir=$d"; done
for pattern in "${PATTERNS[@]}"; do
  MATCHES=$(grep -ri $BUILD_EXCLUDE "$pattern" . --include='*.ts' --include='*.py' --include='*.js' --include='*.go' 2>/dev/null || true)
  if [ -n "$MATCHES" ]; then echo "SOVEREIGN VIOLATION: '$pattern'"; echo "$MATCHES"; FAIL=1; fi
done
if [ $FAIL -eq 1 ]; then echo 'SOVEREIGN DATA POLICY VIOLATION'; exit 1; fi
echo 'Sovereign data check passed.'

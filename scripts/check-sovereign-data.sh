#!/usr/bin/env bash
set -euo pipefail
# ScriptMasterLabs Sovereign Data Enforcement
# Blocks fake/mock/demo/placeholder data in source files

VIOLATIONS=0
SRC_DIR="${1:-src}"

if [ ! -d "$SRC_DIR" ]; then
  echo "No src/ directory found - skipping scan"
  exit 0
fi

FORBIDDEN_PATTERNS=(
  "confidence:[[:space:]]*[0-9]"
  "mock[Dd]ata"
  "fakeData"
  "demoData"
  "placeholderData"
  "syntheticData"
  "hardcodedSignal"
  "testSignal"
  "sampleData"
  "dummyData"
)

for pattern in "${FORBIDDEN_PATTERNS[@]}"; do
  matches=$(grep -rn --include='*.ts' --include='*.js' --include='*.mjs' --include='*.cjs' -E "$pattern" "$SRC_DIR" 2>/dev/null || true)
  if [ -n "$matches" ]; then
    echo "VIOLATION: forbidden pattern '$pattern' found:"
    echo "$matches"
    VIOLATIONS=$((VIOLATIONS + 1))
  fi
done

if [ "$VIOLATIONS" -gt 0 ]; then
  echo "Sovereign data check FAILED: $VIOLATIONS violation(s) found"
  exit 1
fi

echo "Sovereign data check PASSED"
exit 0

#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
python3 -m unittest -v tests.test_reviewer_assignments

grep -Fq 'review.assignment_created' docs/api/EVENTS_V1.md
grep -Fq 'review.assignment_claimed' docs/api/EVENTS_V1.md
grep -Fq 'review.assignment_completed' docs/api/EVENTS_V1.md
grep -Fq 'review.assignment_failed' docs/api/EVENTS_V1.md

echo ORCHESTRA_REVIEWER_ASSIGNMENT_PASS

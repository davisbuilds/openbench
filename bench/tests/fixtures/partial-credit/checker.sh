#!/usr/bin/env bash
# Fixture: always half credit. Nonzero exit => success=false, but the SCORE line
# gives the runner partial credit (0.5). A malformed line and a stray earlier
# line are included to prove "last parseable SCORE wins".
echo "SCORE: 0.1"
echo "SCORE: not-a-number"
echo "SCORE: 0.5"
exit 1

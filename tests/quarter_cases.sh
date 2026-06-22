#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=../azbm.sh
source "$REPO_ROOT/azbm.sh"

assert_eq() {
  local expected="$1"
  local actual="$2"
  if [[ "$actual" != "$expected" ]]; then
    echo "Expected: $expected" >&2
    echo "Actual:   $actual" >&2
    exit 1
  fi
}

assert_eq "2026q1" "$(normalize_quarter "2026Q1")"
assert_eq "2027q1" "$(next_quarter "2026Q4")"
assert_eq "app1 2026Q2" "$(replace_quarter_in_title "app1 2026Q1" "2026q1" "2026q2")"
assert_eq "app1 2026q2" "$(replace_quarter_in_title "app1 2026q1" "2026Q1" "2026Q2")"

title_matches_prefix_quarter "app1 2026Q1" "app1" "2026q1"
title_matches_prefix_quarter "app1 2026q1" "app1" "2026Q1"

echo "quarter_cases OK"

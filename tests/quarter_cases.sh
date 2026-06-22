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
assert_eq "2026-04-01" "$(quarter_start_date "2026Q2")"
assert_eq "2026-06-30" "$(quarter_end_date "2026q2")"
assert_eq "2026-10-01" "$(quarter_start_date "2026q4")"
assert_eq "2026-12-31" "$(quarter_end_date "2026Q4")"
assert_eq "app1 2026Q2" "$(replace_quarter_in_title "app1 2026Q1" "2026q1" "2026q2")"
assert_eq "app1 2026q2" "$(replace_quarter_in_title "app1 2026q1" "2026Q1" "2026Q2")"

title_matches_prefix_quarter "app1 2026Q1" "app1" "2026q1"
title_matches_prefix_quarter "app1 2026q1" "app1" "2026Q1"
title_matches_prefix_quarter "app1 web 2026Q1" "app1 web" "2026q1"

sample_feature_json='{"relations":[{"rel":"System.LinkTypes.Hierarchy-Reverse","url":"https://dev.azure.com/org/_apis/wit/workItems/123"},{"rel":"System.LinkTypes.Hierarchy-Forward","url":"https://dev.azure.com/org/_apis/wit/workItems/456"}]}'
sample_target_without_parent='{"relations":[{"rel":"System.LinkTypes.Hierarchy-Forward","url":"https://dev.azure.com/org/_apis/wit/workItems/456"}]}'
sample_target_same_parent='{"relations":[{"rel":"System.LinkTypes.Hierarchy-Reverse","url":"https://dev.azure.com/org/_apis/wit/workItems/123"}]}'
sample_target_other_parent='{"relations":[{"rel":"System.LinkTypes.Hierarchy-Reverse","url":"https://dev.azure.com/org/_apis/wit/workItems/999"}]}'

assert_eq "123" "$(parent_id_from_json "$sample_feature_json")"
assert_eq "missing" "$(target_parent_status "123" "$sample_target_without_parent")"
assert_eq "same" "$(target_parent_status "123" "$sample_target_same_parent")"
assert_eq "different:999" "$(target_parent_status "123" "$sample_target_other_parent")"

sample_query_json='[{"id":42,"fields":{"System.State":"Active","System.WorkItemType":"Feature","System.Title":"app1 web 2026Q1"}}]'
query_json_has_fields "$sample_query_json"
assert_eq $'42\tActive\tFeature\tapp1 web 2026Q1' "$(feature_rows_from_query_json "$sample_query_json")"

echo "quarter_cases OK"

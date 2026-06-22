#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONFIG_FILE="$SCRIPT_DIR/azbm.config.json"
AZ_COMMAND="az"
JQ_COMMAND=""

AZBM_ORGANIZATION="${AZBM_ORGANIZATION:-}"
AZBM_PROJECT="${AZBM_PROJECT:-}"
AZBM_AREA_PATH="${AZBM_AREA_PATH:-}"
AZBM_ITERATION_PATH_TEMPLATE="${AZBM_ITERATION_PATH_TEMPLATE:-}"
AZBM_DEFAULT_ITERATION_PATH="${AZBM_DEFAULT_ITERATION_PATH:-}"
AZBM_FEATURE_TYPE="${AZBM_FEATURE_TYPE:-Feature}"
AZBM_STORY_TYPES=("User Story")
AZBM_OPEN_STATES_EXCLUDE=("Closed" "Done" "Removed" "Resolved")
AZBM_COPY_FEATURE_FIELDS=(
  "System.Description"
  "System.Tags"
  "Microsoft.VSTS.Common.Priority"
  "Microsoft.VSTS.Common.ValueArea"
)
AZBM_EXTRA_FEATURE_FIELDS=()

usage() {
  cat <<'EOF'
Uso:
  ./azbm.sh doctor [--az-command az]
  ./azbm.sh next-q <YYYYqN>
  ./azbm.sh list-open [opciones] --quarter <YYYYqN> [--prefix <prefijo>]
  ./azbm.sh migrate [opciones] --prefix <prefijo> --from-q <YYYYqN> [--to-q <YYYYqN>] [--apply]

Opciones comunes:
  --config <ruta>                JSON de configuracion. Por defecto: azbm.config.json
  --organization, --org <url>    URL de Azure DevOps.
  --project <nombre>             Proyecto de Azure DevOps.
  --area <path>                  System.AreaPath fijo.
  --iteration-template <tpl>     Plantilla con {quarter}, {quarter_upper}, {year}, {q}, {Q}, {qnum}.
  --feature-type <tipo>          Tipo Feature. Por defecto: Feature.
  --story-type <tipos>           Tipos historia separados por coma. Por defecto: User Story.
  --pat <token>                  PAT. Mejor usar AZURE_DEVOPS_EXT_PAT.
  --az-command <binario>         Binario az a ejecutar.

Ejemplos:
  ./azbm.sh list-open --quarter 2026q1 --prefix app1
  ./azbm.sh migrate --prefix app1 --from-q 2026q1
  ./azbm.sh migrate --prefix app1 --from-q 2026q1 --apply
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 2
}

find_jq() {
  if [[ -n "$JQ_COMMAND" ]]; then
    return
  fi

  local candidate
  for candidate in "$SCRIPT_DIR/bin/jq.exe" "$SCRIPT_DIR/bin/jq"; do
    if [[ -x "$candidate" || -f "$candidate" ]]; then
      JQ_COMMAND="$candidate"
      return
    fi
  done

  if command -v jq >/dev/null 2>&1; then
    JQ_COMMAND="jq"
    return
  fi

  die "No encuentro jq. Pon jq.exe en '$SCRIPT_DIR/bin/jq.exe' o instala jq en PATH."
}

jq_run() {
  find_jq
  "$JQ_COMMAND" "$@"
}

trim() {
  local value="$*"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

normalize_quarter() {
  local value
  value="$(trim "$1")"
  if [[ ! "$value" =~ ^([0-9]{4})[qQ]([1-4])$ ]]; then
    die "Trimestre invalido: '$1'. Usa formato 2026q1..2026q4."
  fi
  printf '%sq%s' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
}

next_quarter() {
  local quarter year q
  quarter="$(normalize_quarter "$1")"
  year="${quarter:0:4}"
  q="${quarter:5:1}"
  if [[ "$q" == "4" ]]; then
    printf '%sq1\n' "$((year + 1))"
  else
    printf '%sq%s\n' "$year" "$((q + 1))"
  fi
}

render_iteration_path() {
  local template="$1"
  local quarter
  quarter="$(normalize_quarter "$2")"
  [[ -n "$template" ]] || die "No hay iteration path. Configura iteration_path_template o pasa --from-iteration/--to-iteration."

  local year="${quarter:0:4}"
  local qnum="${quarter:5:1}"
  local rendered="$template"
  rendered="${rendered//\{quarter_upper\}/${quarter^^}}"
  rendered="${rendered//\{quarter\}/$quarter}"
  rendered="${rendered//\{year\}/$year}"
  rendered="${rendered//\{qnum\}/$qnum}"
  rendered="${rendered//\{Q\}/Q$qnum}"
  rendered="${rendered//\{q\}/q$qnum}"
  printf '%s\n' "$rendered"
}

replace_quarter_in_title() {
  local title="$1"
  local from_q
  local to_q
  from_q="$(normalize_quarter "$2")"
  to_q="$(normalize_quarter "$3")"

  local lower_title="${title,,}"
  local lower_from="${from_q,,}"
  [[ "$lower_title" == *"$lower_from"* ]] || die "El titulo no contiene $from_q: $title"

  local before="${lower_title%%"$lower_from"*}"
  local idx="${#before}"
  local matched="${title:idx:${#from_q}}"
  local styled_to_q="$to_q"
  if [[ "$matched" == *Q* ]]; then
    styled_to_q="${to_q/q/Q}"
  fi
  printf '%s%s%s\n' "${title:0:idx}" "$styled_to_q" "${title:idx + ${#from_q}}"
}

normalize_prefix_segment() {
  trim "$1" | sed -E 's/[[:space:]_:/#-]+$//'
}

title_matches_prefix_quarter() {
  local title="$1"
  local prefix="$2"
  local quarter="${3:-}"

  local lower_title="${title,,}"
  local before="$title"
  if [[ -n "$quarter" ]]; then
    local q
    q="$(normalize_quarter "$quarter")"
    local lower_q="${q,,}"
    [[ "$lower_title" == *"$lower_q"* ]] || return 1
    local before_lower="${lower_title%%"$lower_q"*}"
    before="${title:0:${#before_lower}}"
  fi

  [[ -n "$prefix" ]] || return 0

  local normalized_title_prefix
  local normalized_expected
  normalized_title_prefix="$(normalize_prefix_segment "$before")"
  normalized_expected="$(normalize_prefix_segment "$prefix")"
  [[ "${normalized_title_prefix,,}" == "${normalized_expected,,}" ]]
}

csv_to_array() {
  local csv="$1"
  local -n target="$2"
  target=()
  local item
  IFS=',' read -ra items <<<"$csv"
  for item in "${items[@]}"; do
    item="$(trim "$item")"
    [[ -n "$item" ]] && target+=("$item")
  done
}

contains_casefold() {
  local needle="${1,,}"
  shift
  local item
  for item in "$@"; do
    [[ "${item,,}" == "$needle" ]] && return 0
  done
  return 1
}

wiql_quote() {
  printf "'%s'" "${1//\'/\'\'}"
}

load_json_array() {
  local path="$1"
  local -n target="$2"
  target=()
  if [[ -f "$CONFIG_FILE" ]]; then
    mapfile -t target < <(jq_run -r "$path // [] | .[]" "$CONFIG_FILE")
  fi
}

load_config() {
  find_jq
  [[ -f "$CONFIG_FILE" ]] || return 0

  AZBM_ORGANIZATION="$(jq_run -r '.organization // .organization_url // ""' "$CONFIG_FILE")"
  AZBM_PROJECT="$(jq_run -r '.project // ""' "$CONFIG_FILE")"
  AZBM_AREA_PATH="$(jq_run -r '.area_path // .areaPath // ""' "$CONFIG_FILE")"
  AZBM_ITERATION_PATH_TEMPLATE="$(jq_run -r '.iteration_path_template // .iterationPathTemplate // ""' "$CONFIG_FILE")"
  AZBM_DEFAULT_ITERATION_PATH="$(jq_run -r '.default_iteration_path // .defaultIterationPath // ""' "$CONFIG_FILE")"
  AZBM_FEATURE_TYPE="$(jq_run -r '.feature_type // .featureType // "Feature"' "$CONFIG_FILE")"
  load_json_array '.story_types // .storyTypes' AZBM_STORY_TYPES
  load_json_array '.open_states_exclude // .openStatesExclude' AZBM_OPEN_STATES_EXCLUDE
  load_json_array '.copy_feature_fields // .copyFeatureFields' AZBM_COPY_FEATURE_FIELDS
  AZBM_EXTRA_FEATURE_FIELDS=()
  mapfile -t AZBM_EXTRA_FEATURE_FIELDS < <(
    jq_run -r '(.extra_feature_fields // .extraFeatureFields // {}) | to_entries[] | "\(.key)=\(.value|tostring)"' "$CONFIG_FILE"
  )

  [[ ${#AZBM_STORY_TYPES[@]} -gt 0 ]] || AZBM_STORY_TYPES=("User Story")
  [[ ${#AZBM_OPEN_STATES_EXCLUDE[@]} -gt 0 ]] || AZBM_OPEN_STATES_EXCLUDE=("Closed" "Done" "Removed" "Resolved")
}

parse_common_first_pass() {
  local args=("$@")
  local i=0
  while (( i < ${#args[@]} )); do
    if [[ "${args[$i]}" == "--config" ]]; then
      (( i + 1 < ${#args[@]} )) || die "--config requiere valor."
      CONFIG_FILE="${args[$((i + 1))]}"
    fi
    ((i += 1))
  done
}

apply_common_arg() {
  local arg="$1"
  local value="$2"
  case "$arg" in
    --config)
      CONFIG_FILE="$value"
      ;;
    --organization|--org)
      AZBM_ORGANIZATION="$value"
      ;;
    --project)
      AZBM_PROJECT="$value"
      ;;
    --area)
      AZBM_AREA_PATH="$value"
      ;;
    --iteration-template)
      AZBM_ITERATION_PATH_TEMPLATE="$value"
      ;;
    --feature-type)
      AZBM_FEATURE_TYPE="$value"
      ;;
    --story-type)
      csv_to_array "$value" AZBM_STORY_TYPES
      ;;
    --pat)
      export AZURE_DEVOPS_EXT_PAT="$value"
      ;;
    --az-command)
      AZ_COMMAND="$value"
      ;;
    *)
      return 1
      ;;
  esac
}

require_config() {
  [[ -n "$AZBM_ORGANIZATION" ]] || die "Falta organization en $CONFIG_FILE o --organization."
  [[ -n "$AZBM_PROJECT" ]] || die "Falta project en $CONFIG_FILE o --project."
  [[ -n "$AZBM_AREA_PATH" ]] || die "Falta area_path en $CONFIG_FILE o --area."
}

az_base() {
  "$AZ_COMMAND" "$@" --only-show-errors
}

az_with_project_json() {
  az_base "$@" --org "$AZBM_ORGANIZATION" --project "$AZBM_PROJECT" --output json
}

az_no_project_json() {
  az_base "$@" --org "$AZBM_ORGANIZATION" --output json
}

field_from_json() {
  local json="$1"
  local field="$2"
  jq_run -r --arg field "$field" '.fields[$field] // ""' <<<"$json"
}

closed_states_wiql() {
  local result=""
  local state
  for state in "${AZBM_OPEN_STATES_EXCLUDE[@]}"; do
    [[ -n "$result" ]] && result+=", "
    result+="$(wiql_quote "$state")"
  done
  printf '%s' "$result"
}

build_features_wiql() {
  local iteration_path="$1"
  local exact_title="${2:-}"
  local include_closed="${3:-false}"

  local wiql="SELECT [System.Id], [System.Title], [System.State], [System.WorkItemType] FROM WorkItems WHERE "
  wiql+="[System.TeamProject] = $(wiql_quote "$AZBM_PROJECT")"
  wiql+=" AND [System.WorkItemType] = $(wiql_quote "$AZBM_FEATURE_TYPE")"
  wiql+=" AND [System.AreaPath] = $(wiql_quote "$AZBM_AREA_PATH")"
  wiql+=" AND [System.IterationPath] = $(wiql_quote "$iteration_path")"
  if [[ -n "$exact_title" ]]; then
    wiql+=" AND [System.Title] = $(wiql_quote "$exact_title")"
  fi
  if [[ "$include_closed" != "true" && ${#AZBM_OPEN_STATES_EXCLUDE[@]} -gt 0 ]]; then
    wiql+=" AND [System.State] NOT IN ($(closed_states_wiql))"
  fi
  wiql+=" ORDER BY [System.Title]"
  printf '%s\n' "$wiql"
}

query_feature_ids() {
  local iteration_path="$1"
  local exact_title="${2:-}"
  local include_closed="${3:-false}"
  local wiql
  local json
  wiql="$(build_features_wiql "$iteration_path" "$exact_title" "$include_closed")"
  json="$(az_with_project_json boards query --wiql "$wiql")"
  jq_run -r '
    if type == "array" then
      .[]? | (.id // .fields["System.Id"] // empty)
    elif type == "object" and has("workItems") then
      .workItems[]?.id // empty
    elif type == "object" and has("value") then
      .value[]? | (.id // .fields["System.Id"] // empty)
    else
      empty
    end
  ' <<<"$json"
}

show_work_item_json() {
  local id="$1"
  local expand="${2:-none}"
  if [[ "$expand" == "relations" ]]; then
    az_no_project_json boards work-item show --id "$id" --expand relations
  else
    az_no_project_json boards work-item show --id "$id"
  fi
}

find_open_feature_ids() {
  local iteration_path="$1"
  local prefix="$2"
  local quarter="$3"
  local id item_json title
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    item_json="$(show_work_item_json "$id")"
    title="$(field_from_json "$item_json" "System.Title")"
    if title_matches_prefix_quarter "$title" "$prefix" "$quarter"; then
      printf '%s\n' "$id"
    fi
  done < <(query_feature_ids "$iteration_path" "" false)
}

child_ids_from_json() {
  local json="$1"
  jq_run -r '
    .relations[]?
    | select(.rel == "System.LinkTypes.Hierarchy-Forward")
    | .url
    | sub("^.*/"; "")
    | select(test("^[0-9]+$"))
  ' <<<"$json"
}

create_feature() {
  local source_json="$1"
  local target_title="$2"
  local to_iteration="$3"

  local cmd=(
    "$AZ_COMMAND" boards work-item create
    --type "$AZBM_FEATURE_TYPE"
    --title "$target_title"
    --area "$AZBM_AREA_PATH"
    --iteration "$to_iteration"
    --org "$AZBM_ORGANIZATION"
    --project "$AZBM_PROJECT"
    --only-show-errors
    --output json
  )

  local field value
  local fields=()
  for field in "${AZBM_COPY_FEATURE_FIELDS[@]}"; do
    value="$(field_from_json "$source_json" "$field")"
    [[ -n "$value" && "$value" != "null" ]] || continue
    if [[ "$field" == "System.Description" ]]; then
      cmd+=(--description "$value")
    else
      fields+=("$field=$value")
    fi
  done

  for field in "${AZBM_EXTRA_FEATURE_FIELDS[@]}"; do
    [[ -n "$field" ]] && fields+=("$field")
  done

  if (( ${#fields[@]} > 0 )); then
    cmd+=(--fields "${fields[@]}")
  fi

  "${cmd[@]}" | jq_run -r '.id'
}

move_story_to_feature() {
  local story_id="$1"
  local old_feature_id="$2"
  local new_feature_id="$3"
  local to_iteration="$4"

  az_base boards work-item relation remove \
    --id "$old_feature_id" \
    --relation-type child \
    --target-id "$story_id" \
    --yes \
    --org "$AZBM_ORGANIZATION" \
    --output none

  az_base boards work-item relation add \
    --id "$new_feature_id" \
    --relation-type child \
    --target-id "$story_id" \
    --org "$AZBM_ORGANIZATION" \
    --output none

  az_base boards work-item update \
    --id "$story_id" \
    --iteration "$to_iteration" \
    --org "$AZBM_ORGANIZATION" \
    --output none
}

resolve_list_iteration() {
  local quarter="$1"
  local iteration="$2"
  if [[ -n "$iteration" ]]; then
    printf '%s\n' "$iteration"
  elif [[ -n "$quarter" ]]; then
    render_iteration_path "$AZBM_ITERATION_PATH_TEMPLATE" "$quarter"
  elif [[ -n "$AZBM_DEFAULT_ITERATION_PATH" ]]; then
    printf '%s\n' "$AZBM_DEFAULT_ITERATION_PATH"
  else
    die "Indica --iteration, --quarter con iteration_path_template, o default_iteration_path en config."
  fi
}

doctor() {
  local args=("$@")
  local i=0
  while (( i < ${#args[@]} )); do
    case "${args[$i]}" in
      --az-command)
        (( i + 1 < ${#args[@]} )) || die "--az-command requiere valor."
        AZ_COMMAND="${args[$((i + 1))]}"
        ((i += 2))
        ;;
      -h|--help)
        usage
        return 0
        ;;
      *)
        die "Opcion no reconocida en doctor: ${args[$i]}"
        ;;
    esac
  done

  command -v "$AZ_COMMAND" >/dev/null 2>&1 || die "No encuentro '$AZ_COMMAND' en PATH."
  find_jq
  echo "az: $(command -v "$AZ_COMMAND")"
  echo "jq: $JQ_COMMAND"
  "$AZ_COMMAND" --version | sed -n '1,6p'
  if "$AZ_COMMAND" extension show --name azure-devops --output tsv --query version >/dev/null 2>&1; then
    echo "azure-devops extension: $("${AZ_COMMAND}" extension show --name azure-devops --output tsv --query version)"
  else
    die "Falta la extension azure-devops. Instalala con: az extension add --name azure-devops"
  fi
}

list_open() {
  parse_common_first_pass "$@"
  load_config

  local quarter=""
  local iteration=""
  local prefix=""
  local args=("$@")
  local i=0
  while (( i < ${#args[@]} )); do
    case "${args[$i]}" in
      --config|--organization|--org|--project|--area|--iteration-template|--feature-type|--story-type|--pat|--az-command)
        (( i + 1 < ${#args[@]} )) || die "${args[$i]} requiere valor."
        apply_common_arg "${args[$i]}" "${args[$((i + 1))]}"
        ((i += 2))
        ;;
      --quarter)
        (( i + 1 < ${#args[@]} )) || die "--quarter requiere valor."
        quarter="$(normalize_quarter "${args[$((i + 1))]}")"
        ((i += 2))
        ;;
      --iteration)
        (( i + 1 < ${#args[@]} )) || die "--iteration requiere valor."
        iteration="${args[$((i + 1))]}"
        ((i += 2))
        ;;
      --prefix)
        (( i + 1 < ${#args[@]} )) || die "--prefix requiere valor."
        prefix="${args[$((i + 1))]}"
        ((i += 2))
        ;;
      -h|--help)
        usage
        return 0
        ;;
      *)
        die "Opcion no reconocida en list-open: ${args[$i]}"
        ;;
    esac
  done

  require_config
  local iteration_path
  iteration_path="$(resolve_list_iteration "$quarter" "$iteration")"
  echo "Features abiertas en AreaPath='$AZBM_AREA_PATH', IterationPath='$iteration_path'"
  printf '%-8s %-18s %-16s %s\n' "ID" "Estado" "Tipo" "Titulo"
  printf '%-8s %-18s %-16s %s\n' "--------" "------------------" "----------------" "------"

  local id item_json title state type
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    item_json="$(show_work_item_json "$id")"
    title="$(field_from_json "$item_json" "System.Title")"
    state="$(field_from_json "$item_json" "System.State")"
    type="$(field_from_json "$item_json" "System.WorkItemType")"
    printf '%-8s %-18s %-16s %s\n' "$id" "$state" "$type" "$title"
  done < <(find_open_feature_ids "$iteration_path" "$prefix" "$quarter")
}

migrate() {
  parse_common_first_pass "$@"
  load_config

  local prefix=""
  local from_q=""
  local to_q=""
  local from_iteration=""
  local to_iteration=""
  local include_closed_stories=false
  local apply=false
  local args=("$@")
  local i=0
  while (( i < ${#args[@]} )); do
    case "${args[$i]}" in
      --config|--organization|--org|--project|--area|--iteration-template|--feature-type|--story-type|--pat|--az-command)
        (( i + 1 < ${#args[@]} )) || die "${args[$i]} requiere valor."
        apply_common_arg "${args[$i]}" "${args[$((i + 1))]}"
        ((i += 2))
        ;;
      --prefix)
        (( i + 1 < ${#args[@]} )) || die "--prefix requiere valor."
        prefix="${args[$((i + 1))]}"
        ((i += 2))
        ;;
      --from-q)
        (( i + 1 < ${#args[@]} )) || die "--from-q requiere valor."
        from_q="$(normalize_quarter "${args[$((i + 1))]}")"
        ((i += 2))
        ;;
      --to-q)
        (( i + 1 < ${#args[@]} )) || die "--to-q requiere valor."
        to_q="$(normalize_quarter "${args[$((i + 1))]}")"
        ((i += 2))
        ;;
      --from-iteration)
        (( i + 1 < ${#args[@]} )) || die "--from-iteration requiere valor."
        from_iteration="${args[$((i + 1))]}"
        ((i += 2))
        ;;
      --to-iteration)
        (( i + 1 < ${#args[@]} )) || die "--to-iteration requiere valor."
        to_iteration="${args[$((i + 1))]}"
        ((i += 2))
        ;;
      --include-closed-stories)
        include_closed_stories=true
        ((i += 1))
        ;;
      --apply)
        apply=true
        ((i += 1))
        ;;
      -h|--help)
        usage
        return 0
        ;;
      *)
        die "Opcion no reconocida en migrate: ${args[$i]}"
        ;;
    esac
  done

  require_config
  [[ -n "$prefix" ]] || die "--prefix es obligatorio."
  [[ -n "$from_q" ]] || die "--from-q es obligatorio."
  [[ -n "$to_q" ]] || to_q="$(next_quarter "$from_q")"
  [[ -n "$from_iteration" ]] || from_iteration="$(render_iteration_path "$AZBM_ITERATION_PATH_TEMPLATE" "$from_q")"
  [[ -n "$to_iteration" ]] || to_iteration="$(render_iteration_path "$AZBM_ITERATION_PATH_TEMPLATE" "$to_q")"

  echo "Plan de migracion$([[ "$apply" == true ]] && echo ' (APPLY)' || true)"
  echo "Prefix: $prefix"
  echo "Origen: $from_q / $from_iteration"
  echo "Destino: $to_q / $to_iteration"

  local source_count=0
  local created_count=0
  local moved_count=0
  local source_id source_json source_title source_state target_title target_id
  local story_id story_json story_title story_state story_type story_iteration
  while IFS= read -r source_id; do
    [[ -n "$source_id" ]] || continue
    ((source_count += 1))
    source_json="$(show_work_item_json "$source_id" relations)"
    source_title="$(field_from_json "$source_json" "System.Title")"
    source_state="$(field_from_json "$source_json" "System.State")"
    target_title="$(replace_quarter_in_title "$source_title" "$from_q" "$to_q")"
    target_id="$(query_feature_ids "$to_iteration" "$target_title" true | sed -n '1p')"

    echo
    echo "Feature origen: #$source_id [$source_state] $source_title"
    if [[ -n "$target_id" ]]; then
      echo "Feature destino: reutilizar #$target_id $target_title"
    else
      echo "Feature destino: crear '$target_title'"
      if [[ "$apply" == true ]]; then
        target_id="$(create_feature "$source_json" "$target_title" "$to_iteration")"
        ((created_count += 1))
        echo "Creada Feature destino #$target_id"
      fi
    fi

    local story_count=0
    while IFS= read -r story_id; do
      [[ -n "$story_id" ]] || continue
      story_json="$(show_work_item_json "$story_id")"
      story_title="$(field_from_json "$story_json" "System.Title")"
      story_state="$(field_from_json "$story_json" "System.State")"
      story_type="$(field_from_json "$story_json" "System.WorkItemType")"
      story_iteration="$(field_from_json "$story_json" "System.IterationPath")"

      if ! contains_casefold "$story_type" "${AZBM_STORY_TYPES[@]}"; then
        echo "  - Ignorada #$story_id [$story_state] $story_title (tipo '$story_type')"
        continue
      fi
      if [[ "$include_closed_stories" != true ]] && contains_casefold "$story_state" "${AZBM_OPEN_STATES_EXCLUDE[@]}"; then
        echo "  - Ignorada #$story_id [$story_state] $story_title (estado cerrado/ignorado)"
        continue
      fi

      ((story_count += 1))
      echo "  - Migrar #$story_id [$story_state] $story_title ($story_iteration)"
      if [[ "$apply" == true ]]; then
        [[ -n "$target_id" ]] || die "No hay Feature destino para mover #$story_id."
        move_story_to_feature "$story_id" "$source_id" "$target_id" "$to_iteration"
        ((moved_count += 1))
      fi
    done < <(child_ids_from_json "$source_json")

    if (( story_count == 0 )); then
      echo "  User Stories a migrar: ninguna"
    fi
  done < <(find_open_feature_ids "$from_iteration" "$prefix" "$from_q")

  if (( source_count == 0 )); then
    echo "No hay features abiertas que coincidan con prefix='$prefix', quarter='$from_q', iteration='$from_iteration'."
    return 1
  fi

  echo
  if [[ "$apply" == true ]]; then
    echo "Hecho. Features creadas: $created_count. User Stories migradas: $moved_count."
  else
    echo "DRY-RUN: no se ha cambiado Azure Boards. Pasa --apply para ejecutar."
  fi
}

main() {
  local command="${1:-}"
  if [[ -z "$command" || "$command" == "-h" || "$command" == "--help" ]]; then
    usage
    return 0
  fi
  shift || true

  case "$command" in
    doctor)
      doctor "$@"
      ;;
    next-q)
      [[ $# -eq 1 ]] || die "next-q requiere un trimestre."
      next_quarter "$1"
      ;;
    list-open)
      list_open "$@"
      ;;
    migrate)
      migrate "$@"
      ;;
    *)
      die "Comando no reconocido: $command"
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi

#!/usr/bin/env bash
# =============================================================================
# File the roadmap as GitHub issues
# =============================================================================
#   bash scripts/create-roadmap-issues.sh                       dry run
#   bash scripts/create-roadmap-issues.sh --apply               create everything
#   bash scripts/create-roadmap-issues.sh --apply --milestone v2.0
#
# Reads roadmap/issues.yml — the single source of truth — and creates the
# milestones, labels and issues it describes in the current repository.
#
# Dry run by default. Creating twenty issues in the wrong repository is
# tedious to undo, so the destructive path has to be asked for.
#
# Idempotent: an issue whose title already exists is skipped, so re-running
# after adding to issues.yml files only the new ones.
# =============================================================================
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

ISSUES_FILE="${LAB_ROOT}/roadmap/issues.yml"
APPLY=0
FILTER_MILESTONE=""

usage() {
  cat <<'EOF'
Usage: create-roadmap-issues.sh [--apply] [--milestone <title>]

  --apply               Actually create milestones and issues (default: dry run)
  --milestone <title>   Only process one milestone, e.g. v2.0
  -h, --help            Show this message

Requires the GitHub CLI, authenticated:  gh auth login
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)     APPLY=1; shift ;;
    --milestone) FILTER_MILESTONE="${2:-}"; shift 2 ;;
    -h|--help)   usage; exit 0 ;;
    *)           error "unknown argument: $1"; usage; exit 1 ;;
  esac
done

require_cmd gh "Install the GitHub CLI: https://cli.github.com/"
require_cmd python3 "Install Python 3: https://www.python.org/downloads/"
python3 -c "import yaml" 2>/dev/null || die "PyYAML is required: pip install pyyaml"

[[ -f "$ISSUES_FILE" ]] || die "roadmap/issues.yml not found"

gh auth status >/dev/null 2>&1 || die "not authenticated. Run: gh auth login"

REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)" \
  || die "not inside a GitHub repository, or no remote is configured"

# -----------------------------------------------------------------------------
# Parse. The YAML is read once and emitted as tab-separated records, so the
# shell never has to parse YAML itself.
# -----------------------------------------------------------------------------
read_records() {
  local kind="$1"
  python3 - "$ISSUES_FILE" "$kind" "$FILTER_MILESTONE" <<'PY'
import sys, yaml

path, kind, milestone_filter = sys.argv[1], sys.argv[2], sys.argv[3]
data = yaml.safe_load(open(path, encoding="utf-8"))

def clean(value):
    # Tabs are the field separator and newlines the record separator, so the
    # body is escaped rather than left to corrupt the stream.
    return str(value).replace("\t", "    ").replace("\n", "\\n")

if kind == "milestones":
    for m in data.get("milestones", []):
        if milestone_filter and m["title"] != milestone_filter:
            continue
        print(f'{m["title"]}\t{clean(m.get("description", ""))}')

elif kind == "issues":
    for issue in data.get("issues", []):
        if milestone_filter and issue.get("milestone") != milestone_filter:
            continue
        labels = ",".join(issue.get("labels", []))
        print("\t".join([
            issue["id"],
            issue["title"],
            issue.get("milestone", ""),
            labels,
            clean(issue.get("body", "")),
        ]))

elif kind == "labels":
    seen = set()
    for issue in data.get("issues", []):
        if milestone_filter and issue.get("milestone") != milestone_filter:
            continue
        for label in issue.get("labels", []):
            if label not in seen:
                seen.add(label)
                print(label)
PY
}

# -----------------------------------------------------------------------------
create_labels() {
  heading "Labels"

  local label
  while IFS= read -r label; do
    [[ -z "$label" ]] && continue

    if gh label list --limit 200 --json name -q '.[].name' 2>/dev/null | grep -qx "$label"; then
      log "  ${C_DIM}exists  ${label}${C_RESET}"
      continue
    fi

    if (( APPLY )); then
      gh label create "$label" --description "Lab-in-a-Box roadmap" >/dev/null 2>&1 \
        && success "created ${label}" \
        || warn "could not create label ${label}"
    else
      info "would create label ${label}"
    fi
  done < <(read_records labels)
}

create_milestones() {
  heading "Milestones"

  local title description
  while IFS=$'\t' read -r title description; do
    [[ -z "$title" ]] && continue

    # There is no `gh milestone` command; milestones are an API call.
    if gh api "repos/${REPO}/milestones?state=all" -q '.[].title' 2>/dev/null | grep -qx "$title"; then
      log "  ${C_DIM}exists  ${title}${C_RESET}"
      continue
    fi

    if (( APPLY )); then
      gh api "repos/${REPO}/milestones" \
        -f title="$title" \
        -f description="${description//\\n/$'\n'}" \
        >/dev/null 2>&1 \
        && success "created ${title}" \
        || warn "could not create milestone ${title}"
    else
      info "would create milestone ${title}"
    fi
  done < <(read_records milestones)
}

create_issues() {
  heading "Issues"

  local id title milestone labels body created=0 skipped=0

  # Fetch existing titles once rather than per issue.
  local existing
  existing="$(gh issue list --state all --limit 500 --json title -q '.[].title' 2>/dev/null || true)"

  while IFS=$'\t' read -r id title milestone labels body; do
    [[ -z "$id" ]] && continue

    if grep -Fxq "$title" <<< "$existing"; then
      log "  ${C_DIM}exists  [${id}] ${title}${C_RESET}"
      (( skipped++ )) || true
      continue
    fi

    if (( APPLY )); then
      # Restore the newlines that were escaped for transport.
      if gh issue create \
           --title "$title" \
           --body "${body//\\n/$'\n'}" \
           --milestone "$milestone" \
           --label "$labels" >/dev/null 2>&1; then
        success "[${id}] ${title}"
        (( created++ )) || true
      else
        warn "could not create [${id}] ${title}"
      fi
    else
      info "would create [${id}] ${title} ${C_DIM}(${milestone}, ${labels})${C_RESET}"
      (( created++ )) || true
    fi
  done < <(read_records issues)

  log ""
  if (( APPLY )); then
    success "${created} issue(s) created, ${skipped} already present"
  else
    info "${created} issue(s) would be created, ${skipped} already present"
  fi
}

main() {
  log "${C_BOLD}Roadmap → GitHub${C_RESET} ${C_DIM}(${REPO})${C_RESET}"
  [[ -n "$FILTER_MILESTONE" ]] && log "${C_DIM}Filtered to milestone: ${FILTER_MILESTONE}${C_RESET}"

  if ! (( APPLY )); then
    log ""
    warn "Dry run — nothing will be created. Add --apply to go ahead."
  fi

  create_labels
  create_milestones
  create_issues

  if ! (( APPLY )); then
    log ""
    log "  Run it for real:  ${C_CYAN}bash scripts/create-roadmap-issues.sh --apply${C_RESET}"
  fi
}

main "$@"

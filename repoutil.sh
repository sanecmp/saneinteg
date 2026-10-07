#!/usr/bin/env bash

set -euo pipefail

project_root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
components=(sanea sanex sanelib landing)

usage() {
    cat <<'EOF'
Usage: repoutil.sh init
       repoutil.sh status
       repoutil.sh update <sanea|sanex|sanelib|landing> <revision>

init    Initialize submodules at the revisions recorded in the repository.
status  Show recorded revisions, branches and local changes.
update  Fetch origin and check out an explicit revision in one component.

Changes are never discarded, staged, committed or pushed automatically.
EOF
}

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

require_clean() {
    local component=$1
    local changes
    changes=$(
        git -C "$project_root/components/$component" status --porcelain \
            --untracked-files=all --ignore-submodules=none
    )

    if [[ -n "$changes" ]]; then
        fail "Component $component has local changes; commit or remove them before switching revisions."
    fi
}

require_published_head() {
    local component=$1
    local published_refs
    published_refs=$(
        git -C "$project_root/components/$component" for-each-ref \
            --format='%(refname)' --contains HEAD refs/remotes/origin/
    )

    if [[ -z "$published_refs" ]]; then
        fail "Component $component has a commit not present on origin; publish it before switching revisions."
    fi
}

initialize() {
    local component
    local recorded_revision
    local current_revision

    for component in "${components[@]}"; do

        if [[ -e "$project_root/components/$component/.git" ]]; then
            require_clean "$component"
            recorded_revision=$(git rev-parse ":components/$component")
            current_revision=$(git -C "components/$component" rev-parse HEAD)

            if [[ "$current_revision" != "$recorded_revision" ]]; then
                require_published_head "$component"
            fi
        fi
    done

    git submodule sync --recursive
    git submodule update --init --recursive
}

show_status() {
    local component
    git status --short --branch
    printf '\nRecorded component revisions:\n'
    git submodule status --cached
    printf '\nWorking component revisions:\n'
    git submodule status --recursive

    for component in "${components[@]}"; do

        if [[ -e "$project_root/components/$component/.git" ]]; then
            printf '\n%s\n' "$component"
            git -C "components/$component" status --short --branch
        fi
    done
}

update_component() {
    local component=$1
    local revision=$2
    local target_revision
    local current_revision

    case "$component" in
        sanea|sanex|sanelib|landing) ;;
        *) fail "Unknown component: $component" ;;
    esac

    if [[ -z "$revision" || "$revision" == -* ]]; then
        fail "Specify a commit, tag or remote branch, for example origin/main."
    fi

    if [[ ! -e "components/$component/.git" ]]; then
        fail "Component $component is not initialized; run repoutil.sh init first."
    fi

    require_clean "$component"
    git -C "components/$component" fetch origin
    target_revision=$(git -C "components/$component" rev-parse --verify --end-of-options "$revision^{commit}")
    current_revision=$(git -C "components/$component" rev-parse HEAD)

    if [[ "$current_revision" != "$target_revision" ]]; then
        require_published_head "$component"
        git -C "components/$component" checkout --detach "$target_revision"
    fi

    printf 'Component %s is at %s. Review and stage its pointer explicitly.\n' "$component" "$target_revision"
}

command=${1:-help}

case "$command" in
    help|-h|--help)
        usage
        exit 0
        ;;
    init|status)

        if [[ $# -ne 1 ]]; then
            usage >&2
            exit 2
        fi
        ;;
    update)

        if [[ $# -ne 3 ]]; then
            usage >&2
            exit 2
        fi
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

cd -- "$project_root"

if [[ ! -f .gitmodules ]] || ! git rev-parse --git-dir >/dev/null 2>&1; then
    fail "Run this script from a saneinteg checkout."
fi

case "$command" in
    init) initialize ;;
    status) show_status ;;
    update) update_component "$2" "$3" ;;
esac

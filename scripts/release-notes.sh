#!/usr/bin/env bash
#
# Summarise everything that will ship in the next release.
#
# Derives the changelog from the actual commit range since the last tag,
# rather than from whatever work happened to be top of mind. Also flags
# behaviour-affecting changes that need an explicit upgrade note.
#
# Usage:
#   scripts/release-notes.sh              # range = <last tag>..HEAD
#   scripts/release-notes.sh v3.1.2       # range = v3.1.2..HEAD
#
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

FROM="${1:-$(git describe --tags --abbrev=0 2>/dev/null || true)}"
if [ -z "$FROM" ]; then
    echo "No tags found. Pass a starting ref explicitly." >&2
    exit 1
fi
RANGE="${FROM}..HEAD"

hr() { printf '%s\n' "────────────────────────────────────────────────────────────"; }

echo
hr
echo "Release range: ${RANGE}"
echo "  from: ${FROM} ($(git rev-parse --short "${FROM}^{}"))"
echo "  to:   HEAD ($(git rev-parse --short HEAD)) on $(git rev-parse --abbrev-ref HEAD)"
hr

# ── Commits ───────────────────────────────────────────────────────────────────
COUNT=$(git rev-list --count "$RANGE")
echo
echo "COMMITS (${COUNT})"
if [ "$COUNT" -eq 0 ]; then
    echo "  (nothing to release)"
    exit 0
fi
git log --oneline --no-decorate "$RANGE" | sed 's/^/  /'

# ── Pull requests ─────────────────────────────────────────────────────────────
# Every PR referenced in the range. Anything here that you did not personally
# work on this cycle is exactly the kind of change that gets left out of the
# notes -- PR #23 shipped undocumented in v3.1.3 for precisely this reason.
echo
echo "PULL REQUESTS IN RANGE"
PRS=$(git log "$RANGE" --format=%s | grep -oE '\(#[0-9]+\)$' | tr -d '(#)' | sort -un || true)
if [ -z "$PRS" ]; then
    echo "  (none referenced in commit subjects)"
else
    for pr in $PRS; do
        if command -v gh >/dev/null 2>&1; then
            title=$(gh pr view "$pr" --json title --jq .title 2>/dev/null || echo "?")
            echo "  #${pr}  ${title}"
        else
            echo "  #${pr}"
        fi
    done
fi

# ── Files touched ─────────────────────────────────────────────────────────────
echo
echo "FILES CHANGED"
git diff --stat "$RANGE" | sed 's/^/  /'

# ── Behaviour-affecting changes needing an upgrade note ───────────────────────
# Production code under custom_components/ is user-visible; test-only releases
# are not. Certain constants change what users see without any code change.
echo
echo "UPGRADE-NOTE CANDIDATES"
FOUND=0

PROD=$(git diff --name-only "$RANGE" -- custom_components/ || true)
if [ -n "$PROD" ]; then
    echo "  Production code changed:"
    echo "$PROD" | sed 's/^/    /'
    FOUND=1
else
    echo "  No production code changed (tests/docs only release)."
fi

# Defaults that silently alter a user's enabled entities on upgrade.
# Delegated to a helper: a multi-line constant carries its name on the
# declaration line while the edit lands deep inside the literal, so the helper
# maps changed line numbers onto constant line spans rather than grepping.
if command -v python3 >/dev/null 2>&1; then
    CONST_HITS=$(python3 scripts/_flag_constants.py "$RANGE" || true)
    if [ -n "$CONST_HITS" ]; then
        printf '%s\n' "$CONST_HITS"
        FOUND=1
    fi
fi

# Entities added or removed.
if git diff "$RANGE" -- custom_components/ | grep -qE '^[-+].*(_attr_unique_id|async_add_entities)'; then
    echo "  ⚠  Entity registration changed -- check for added/removed sensors."
    FOUND=1
fi

# Dependency changes reach users through HACS.
if git diff "$RANGE" -- custom_components/nem_pd7day/manifest.json | grep -qE '^[-+].*"requirements"|^[-+]\s+"[a-z-]+[><=]'; then
    echo "  ⚠  manifest requirements changed -- note any new/pinned dependency."
    FOUND=1
fi

[ "$FOUND" -eq 0 ] && echo "  Nothing flagged."

# ── Version consistency ───────────────────────────────────────────────────────
echo
echo "VERSION STATE"
MANIFEST=$(grep -oE '"version": *"[^"]+"' custom_components/nem_pd7day/manifest.json | grep -oE '[0-9][^"]*')
echo "  manifest.json:      ${MANIFEST}"
echo "  last tag:           ${FROM}"
if [ "v${MANIFEST}" = "${FROM}" ]; then
    echo "  ⚠  manifest still matches the last tag -- bump it before tagging."
fi
if git rev-parse "v${MANIFEST}" >/dev/null 2>&1; then
    echo "  ⚠  tag v${MANIFEST} already exists."
fi
if grep -q "^| ${MANIFEST} |" README.md 2>/dev/null; then
    echo "  README version history: row present for ${MANIFEST}"
else
    echo "  ⚠  README version history: no row for ${MANIFEST} yet."
fi

echo
hr
echo "Next: docs/RELEASING.md"
hr
echo

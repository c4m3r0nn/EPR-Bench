#!/bin/sh
# Export what may be published: HEAD of the current branch, as `git archive` gives it (tracked files only, no history),
# minus every path in PUBLIC_EXCLUDE.txt, with nothing added. Then scan the exported tree
# (scripts/release_scan.py --public-tree) and exit non-zero on any finding.
#
#     scripts/export_public.sh OUTDIR
#
# OUTDIR must not exist or must be empty. The repository itself is not changed.
set -eu

out=${1:?usage: scripts/export_public.sh OUTDIR}
root=$(git rev-parse --show-toplevel)
cd "$root"

if [ -e "$out" ] && [ -n "$(ls -A "$out" 2>/dev/null)" ]; then
    echo "export_public: $out exists and is not empty" >&2
    exit 2
fi
mkdir -p "$out"
out=$(cd "$out" && pwd)

git archive --format=tar HEAD | tar -x -C "$out"

# PUBLIC_EXCLUDE.txt as git has it at HEAD (not a working copy), so the export is reproducible from the commit alone
git show HEAD:PUBLIC_EXCLUDE.txt | while IFS= read -r line; do
    entry=$(printf '%s' "$line" | sed -e 's/[[:space:]]#.*$//' -e 's/^#.*$//' -e 's/[[:space:]]*$//')
    [ -z "$entry" ] && continue
    case "$entry" in
        /*|*..*) echo "export_public: refusing entry outside the tree: $entry" >&2; exit 2 ;;
    esac
    found=0
    for path in "$out"/$entry; do  # a glob, a file or a directory
        [ -e "$path" ] || continue
        rm -rf -- "$path"
        found=1
    done
    if [ "$found" -eq 0 ]; then
        echo "export_public: nothing in the export matches $entry" >&2
    fi
done
find "$out" -type d -empty -delete  # a directory emptied by the list (git keeps no empty directories either)

echo "exported HEAD $(git rev-parse --short HEAD) to $out: $(find "$out" -type f | wc -l | tr -d ' ') files, $(du -sh "$out" | cut -f1)"
uv run python scripts/release_scan.py --public-tree "$out"

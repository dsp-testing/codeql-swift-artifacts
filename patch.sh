#!/bin/bash -e

PATCH_DIR="$(cd "$(dirname "$0")/patches"; pwd)"

for patch_subdir in "$PATCH_DIR"/*; do
  (
    repo="$(basename "$patch_subdir")"
    cd "$repo" || exit 0
    echo "patching $repo"
    for patch in "$patch_subdir"/*; do
      echo "  applying $(basename "$patch")"
      git apply "$patch"
    done
  )
done

#!/bin/sh
# Local checkout entry point; build-release.py emits the standalone curl-compatible installer.
set -eu
enso_source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$enso_source_dir/scripts/installer-header.sh"
"$enso_uv" run --no-project --no-config --python 3.14 \
  "$enso_source_dir/scripts/install-release.py" \
  --home "$enso_home" --bin-dir "$enso_bin_dir" --uv "$enso_uv" "$@"

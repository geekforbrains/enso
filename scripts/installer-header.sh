#!/bin/sh
# The release builder appends the standard-library bootstrap below this header.
set -eu
umask 077

usage() {
  cat <<'ENSO_HELP'
Install Enso with uv: install.sh [options]
  --manifest SOURCE     Release path or HTTPS URL (required for local bundles)
  --home DIR            Enso home (default: ENSO_HOME or ~/.enso)
  --bin-dir DIR         Stable command directory (default: ~/.local/bin)
  --token-file FILE     Private feed bearer token file
  --feed URL            Update feed (default: official GitHub releases)
  --extras LIST         Comma-separated slack,telegram,web (default: all three)
  --viewer-service NAME System viewer unit to restart alongside Enso
  --adopt               Adopt an existing stopped unmanaged installation
  --help                Show this help without installing anything
ENSO_HELP
}

enso_home=${ENSO_HOME:-"$HOME/.enso"}
enso_bin_dir="$HOME/.local/bin"
enso_manifest=""
enso_previous=""
for enso_argument in "$@"; do
  if [ -n "$enso_previous" ]; then
    case "$enso_previous" in
      --home) enso_home=$enso_argument ;;
      --bin-dir) enso_bin_dir=$enso_argument ;;
      --manifest) enso_manifest=$enso_argument ;;
    esac
    enso_previous=""
    continue
  fi
  case "$enso_argument" in
    --help|-h) usage; exit 0 ;;
    --manifest|--home|--bin-dir|--token-file|--feed|--extras|--viewer-service)
      enso_previous=$enso_argument ;;
    --adopt) ;;
    *) printf '%s\n' "Unknown installer option: $enso_argument" >&2; exit 2 ;;
  esac
done
if [ -n "$enso_previous" ] || [ -z "$enso_manifest" ]; then
  usage >&2
  exit 2
fi
case "$enso_home" in /*) ;; *) enso_home="$PWD/$enso_home" ;; esac
case "$enso_bin_dir" in /*) ;; *) enso_bin_dir="$PWD/$enso_bin_dir" ;; esac
if [ -L "$enso_home/runtime/tools" ] || [ -L "$enso_home/runtime/tools/uv" ]; then
  printf '%s\n' 'The managed uv directory and executable must not be symbolic links.' >&2
  exit 1
fi
mkdir -p "$enso_home/runtime/tools"
enso_bootstrap=$(mktemp -d "$enso_home/runtime/.bootstrap.XXXXXX")
trap 'rm -rf "$enso_bootstrap"' EXIT HUP INT TERM
enso_uv="$enso_home/runtime/tools/uv"
if [ -f "$enso_uv" ] && [ -x "$enso_uv" ]; then
  :
elif [ -e "$enso_uv" ]; then
  printf '%s\n' 'The managed uv executable is not an executable file.' >&2
  exit 1
elif command -v uv >/dev/null 2>&1; then
  cp -L "$(command -v uv)" "$enso_bootstrap/uv"
  chmod 700 "$enso_bootstrap/uv"
  mv "$enso_bootstrap/uv" "$enso_uv"
else
  command -v curl >/dev/null 2>&1 || { printf '%s\n' 'curl is required to bootstrap uv.' >&2; exit 1; }
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    --connect-timeout 15 --max-time 120 https://astral.sh/uv/install.sh \
    --output "$enso_bootstrap/uv-install.sh"
  UV_UNMANAGED_INSTALL="$enso_home/runtime/tools" sh "$enso_bootstrap/uv-install.sh"
fi
export UV_PYTHON_INSTALL_DIR="$enso_home/runtime/python"
export UV_CACHE_DIR="$enso_home/runtime/cache/uv"
export ENSO_HOME="$enso_home"

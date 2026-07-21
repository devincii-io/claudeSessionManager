#!/usr/bin/env bash
set -euo pipefail

version="${1:?usage: packaging/build-linux.sh VERSION}"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Invalid semantic version: $version" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build_root="$(mktemp -d "/tmp/asm-release-${version}-XXXXXX")"

cleanup() {
  case "$build_root" in
    /tmp/asm-release-*) rm -rf -- "$build_root" ;;
  esac
}
trap cleanup EXIT

cd "$repo_root"
export UV_PROJECT_ENVIRONMENT="$build_root/venv"
uv_bin="$(command -v uv || true)"
if [[ -z "$uv_bin" && -x "$HOME/.local/bin/uv" ]]; then
  uv_bin="$HOME/.local/bin/uv"
fi
if [[ -z "$uv_bin" ]]; then
  echo "uv is required to build the Linux release" >&2
  exit 3
fi

"$uv_bin" sync --extra build
"$uv_bin" run pyinstaller \
  --noconfirm \
  --clean \
  --distpath "$build_root/dist" \
  --workpath "$build_root/work" \
  AgentSessionManager.spec

binary="$build_root/dist/AgentSessionManager"
test -x "$binary"
install -m 0755 "$binary" "$repo_root/dist/AgentSessionManager-v${version}-Linux-x86_64"
ls -lh "$repo_root/dist/AgentSessionManager-v${version}-Linux-x86_64"

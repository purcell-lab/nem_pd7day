#!/usr/bin/env bash
# Install or update the NEM PD7DAY integration from a GitHub release, outside HACS.
#
# Attached to every release as update_nem_pd7day.sh with that release's tag
# stamped in below, so the copy from a release installs exactly that release:
#
#   curl -fsSL https://github.com/purcell-lab/nem_pd7day/releases/latest/download/update_nem_pd7day.sh | bash
#   bash update_nem_pd7day.sh                 # the release it came with ("latest" from the repo)
#   bash update_nem_pd7day.sh v3.23.0         # a given release
#   bash update_nem_pd7day.sh latest /config  # the newest release, into another config directory
#
# It installs the release's nem_pd7day.zip, the file HACS installs, never the
# head of main, and refuses a zip whose manifest.json does not carry the tag it
# asked for. The new copy is staged beside the config directory and swapped in
# with two renames, so a failed download or extract leaves the installed copy
# untouched; the copy it replaces is kept at <config>/.nem_pd7day.previous.
#
# It does not restart Home Assistant: restart it to load the new version.
set -euo pipefail

STAMPED_VERSION="__RELEASE_VERSION__"   # replaced with vX.Y.Z by the release workflow
REPO="purcell-lab/nem_pd7day"
BASE_URL="${NEM_PD7DAY_RELEASE_BASE:-https://github.com/${REPO}/releases}"

version="${1:-$STAMPED_VERSION}"
config="${2:-/config}"
# The repo copy is not stamped: it installs the newest release.
if [ "${version#__}" != "$version" ]; then
  version="latest"
fi
case "$version" in
  latest) url="${BASE_URL}/latest/download/nem_pd7day.zip" ;;
  v[0-9]*) url="${BASE_URL}/download/${version}/nem_pd7day.zip" ;;
  [0-9]*) version="v${version}"; url="${BASE_URL}/download/${version}/nem_pd7day.zip" ;;
  *) echo "update_nem_pd7day: version must be latest or vX.Y.Z, got '${version}'" >&2; exit 2 ;;
esac

if [ ! -d "$config" ]; then
  echo "update_nem_pd7day: config directory ${config} does not exist" >&2
  exit 2
fi
dest="${config}/custom_components/nem_pd7day"
stage="${config}/.nem_pd7day.new"
previous="${config}/.nem_pd7day.previous"

work="$(mktemp -d)"
trap 'rm -rf "$work" "$stage"' EXIT

echo "update_nem_pd7day: downloading ${url}"
curl -fsSL --retry 3 -o "${work}/nem_pd7day.zip" "$url"

rm -rf "$stage"
mkdir -p "$stage"
if command -v unzip >/dev/null 2>&1; then
  unzip -q "${work}/nem_pd7day.zip" -d "$stage"
else
  python3 -m zipfile -e "${work}/nem_pd7day.zip" "$stage"
fi

manifest_version() {
  sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$1" | head -n 1
}
if [ ! -f "${stage}/manifest.json" ] || [ ! -f "${stage}/__init__.py" ]; then
  echo "update_nem_pd7day: ${url} is not the integration zip (no manifest.json or __init__.py at its root)" >&2
  exit 1
fi
new_version="$(manifest_version "${stage}/manifest.json")"
if [ "$version" != "latest" ] && [ "v${new_version}" != "$version" ]; then
  echo "update_nem_pd7day: asked for ${version} but its manifest.json says ${new_version}; nothing installed" >&2
  exit 1
fi
old_version="none"
if [ -f "${dest}/manifest.json" ]; then
  old_version="$(manifest_version "${dest}/manifest.json")"
fi

mkdir -p "${config}/custom_components"
rm -rf "$previous"
if [ -d "$dest" ]; then
  mv "$dest" "$previous"
fi
mv "$stage" "$dest"

echo "update_nem_pd7day: installed ${new_version} (was ${old_version}) in ${dest}"
if [ -d "$previous" ]; then
  echo "update_nem_pd7day: the previous copy is in ${previous}"
fi
echo "update_nem_pd7day: restart Home Assistant to load it"

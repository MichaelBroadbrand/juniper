#!/bin/zsh
# Publishes a release to github.com/MichaelBroadbrand/juniper (JUNIPER_REPO for a fork). Every
# installed copy picks it up within a few hours (or straight away from System › Check now in the
# menu bar panel).
#
#   packaging/publish-update.sh code 2.0.1 "Fixes the overlay flicker"    code only: installs by itself
#   packaging/publish-update.sh app  2.1.0 "New wake-word model"          new DMG: people download it
#
# Code updates are for changes to the .py files and the setup page. Use `app` when packages,
# models or the speech helper change, or packaging/ itself. Releases are tagged v<version> on
# whatever the repo's default branch points at, so publish from an up-to-date main.
set -euo pipefail
KIND="${1:?code or app}"; VERSION="${2:?version, e.g. 2.0.1}"; NOTES="${3:-}"
ROOT="${0:A:h:h}"
REPO="${JUNIPER_REPO:-MichaelBroadbrand/juniper}"
cd "$ROOT"
# Versions can't be reused: say so before spending a minute building.
if gh release view "v$VERSION" --repo "$REPO" --json isDraft --jq .isDraft 2>/dev/null | grep -q false; then
  LATEST=$(gh release list --repo "$REPO" --limit 1 --json tagName --jq '.[0].tagName' 2>/dev/null)
  echo "v$VERSION is already published (latest is ${LATEST:-unknown}). Use a higher version, e.g. the next number up."
  exit 1
fi
if [ "$KIND" = app ]; then
  # Notarized, so it opens on other Macs without Open Anyway (REQUIRE_NOTARIZED= overrides).
  REQUIRE_NOTARIZED="${REQUIRE_NOTARIZED-1}" VERSION="$VERSION" packaging/package.sh
fi
.venv/bin/python packaging/updates.py build "$KIND" "$VERSION" "$NOTES"
OUT="packaging/release/$VERSION"
ASSETS=("$OUT"/juniper-code-"$VERSION".zip "$OUT"/feed.json "$OUT"/feed.sig)
[ "$KIND" = app ] && ASSETS+=("$OUT"/Juniper-"$VERSION".dmg)
# An earlier attempt at this version that didn't finish (a leftover draft) is cleared first.
if gh release view "v$VERSION" --repo "$REPO" --json isDraft --jq .isDraft 2>/dev/null | grep -q true; then
  gh release delete "v$VERSION" --repo "$REPO" --yes --cleanup-tag
  echo "› removed an unfinished draft of v$VERSION"
fi
gh release create "v$VERSION" "${ASSETS[@]}" --repo "$REPO" --title "Juniper $VERSION" \
  --notes "${NOTES:-Juniper $VERSION}" --latest
echo "✓ published $KIND $VERSION: https://github.com/$REPO/releases/tag/v$VERSION"

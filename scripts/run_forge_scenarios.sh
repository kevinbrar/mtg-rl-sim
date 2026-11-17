#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$#" -ne 3 ]]; then
  echo "usage: $0 <scenario-spec.json> <output-dir> <fresh-forge-checkout>" >&2
  exit 2
fi

SPEC="$(realpath "$1")"
OUTDIR="$(realpath -m "$2")"
FORGE_DIR="$(realpath -m "$3")"
MAVEN_REPO="${FORGE_MAVEN_REPO:-$HOME/.cache/forge-extrapolate-m2}"
PIN_FILE="$ROOT/oracle/forge.txt"
mapfile -t pin < "$PIN_FILE"
if [[ "${#pin[@]}" -ne 2 || "${pin[0]}" != "https://github.com/Card-Forge/forge" \
  || ! "${pin[1]}" =~ ^[[:xdigit:]]{40}$ ]]; then
  echo "invalid Forge pin in oracle/forge.txt" >&2
  exit 2
fi

check_disk_floor() {
  local free_kib
  free_kib="$(df -Pk "$HOME" | awk 'NR == 2 { print $4 }')"
  if [[ -z "$free_kib" || "$free_kib" -lt 5242880 ]]; then
    echo "Forge build requires at least 5 GiB free; currently ${free_kib:-unknown} KiB" >&2
    exit 2
  fi
}

if [[ -e "$FORGE_DIR" ]]; then
  echo "Forge checkout path must be fresh: $FORGE_DIR" >&2
  exit 2
fi
check_disk_floor
mkdir -p "$(dirname "$FORGE_DIR")" "$OUTDIR" "$MAVEN_REPO"
git clone --filter=blob:none --depth=1 --no-checkout \
  --single-branch --branch master "${pin[0]}" "$FORGE_DIR"
git -C "$FORGE_DIR" fetch --depth=1 origin "${pin[1]}"
git -C "$FORGE_DIR" sparse-checkout init --no-cone
git -C "$FORGE_DIR" sparse-checkout set --no-cone \
  '/pom.xml' '/.mvn/**' '/checkstyle.xml' \
  '/forge-core/**' '/forge-game/**' '/forge-ai/**' \
  '/forge-gui/**' '/forge-gui-desktop/**' \
  '/forge-gui-android/pom.xml' '/forge-gui-ios/pom.xml' \
  '/forge-gui-mobile/pom.xml' '/forge-gui-mobile-dev/pom.xml' \
  '/forge-lda/pom.xml' '/adventure-editor/pom.xml' \
  '/forge-installer/pom.xml'
git -C "$FORGE_DIR" checkout --detach "${pin[1]}"
if [[ "$(git -C "$FORGE_DIR" rev-parse HEAD)" != "${pin[1]}" ]]; then
  echo "Forge checkout did not resolve to the pinned commit" >&2
  exit 1
fi

mapfile -t patches < <(find "$ROOT/oracle/forge-patches" -maxdepth 1 -type f -name '*.patch' -print | LC_ALL=C sort)
if [[ "${#patches[@]}" -eq 0 ]]; then
  echo "no Forge patches found" >&2
  exit 2
fi
for patch in "${patches[@]}"; do
  git -C "$FORGE_DIR" apply "$patch"
done

check_disk_floor
maven_status=0
mvn -f "$FORGE_DIR/pom.xml" \
  -B -ntp \
  -s "$ROOT/oracle/forge-maven-settings.xml" \
  -Dmaven.repo.local="$MAVEN_REPO" \
  -Denforcer.skip=true \
  -pl forge-gui-desktop -am \
  -Dtest=CardScenarioProjectionTest \
  -Dsurefire.failIfNoSpecifiedTests=false \
  -Dsurefire.useFile=false \
  -Djava.awt.headless=true \
  "-DoracleScenarioSpec=$SPEC" \
  test || maven_status=$?

REPORT="$FORGE_DIR/forge-gui-desktop/target/surefire-reports/TEST-forge.ai.CardScenarioProjectionTest.xml"
if [[ ! -s "$REPORT" ]]; then
  echo "Forge Surefire report missing: $REPORT" >&2
  if [[ "$maven_status" -eq 0 ]]; then
    maven_status=1
  fi
  exit "$maven_status"
fi
cp "$REPORT" "$OUTDIR/surefire-report.xml"
exit "$maven_status"

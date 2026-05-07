#!/usr/bin/env bash
# build-dist.sh — reproducible multi-arch dist builder for Capsule.
#
# Produces:
#   dist/Capsule/               — universal bundle (both arches, arch-detecting launcher)
#   dist/Capsule-mac-applesilicon/ — arm64-only bundle
#   dist/Capsule-mac-intel/        — amd64-only bundle
#   dist/Capsule-windows/          — amd64-only bundle (Windows .bat launcher)
#   dist/Capsule*.zip              — sibling zips for each of the above
#
# Usage:
#   bash scripts/build-dist.sh [--arch arm64,amd64] [--out dist] [--skip-pdfs]
#
# Requirements: docker (with buildx), python3, tar; ditto (macOS) or zip as fallback.
# No new dependencies beyond what CLAUDE.md §3 already mandates.

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
ARCHES="arm64,amd64"
OUT_DIR="dist"
SKIP_PDFS=0

# ---------------------------------------------------------------------------
# Argument parsing (POSIX-compatible, no associative arrays)
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --arch)   ARCHES="$2";   shift 2 ;;
    --out)    OUT_DIR="$2";  shift 2 ;;
    --skip-pdfs) SKIP_PDFS=1; shift ;;
    --) shift; break ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Locate repo root (script lives in scripts/ one level below root)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

# 1. docker buildx
if ! docker buildx version >/dev/null 2>&1; then
  echo "ERROR: docker buildx is not available." >&2
  echo "       Upgrade Docker Desktop or install the buildx plugin." >&2
  exit 1
fi

# 2. Ensure a builder is available; create one named capsule-builder if none selected.
CURRENT_BUILDER="$(docker buildx inspect 2>/dev/null | grep '^Name:' | awk '{print $2}' || true)"
if [ -z "$CURRENT_BUILDER" ] || [ "$CURRENT_BUILDER" = "default" ]; then
  echo "No non-default buildx builder active; creating 'capsule-builder'..."
  docker buildx create --name capsule-builder --use --bootstrap >/dev/null 2>&1 || \
    docker buildx use capsule-builder
fi

# 3. Template files exist
for tmpl in dist-templates/Capsule.command.in dist-templates/Capsule.bat.in; do
  if [ ! -f "$REPO_ROOT/$tmpl" ]; then
    echo "ERROR: missing template: $tmpl" >&2
    exit 1
  fi
done

# ---------------------------------------------------------------------------
# Scratch space (cleaned up on exit)
# ---------------------------------------------------------------------------
TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Build + save images for each requested arch
# ---------------------------------------------------------------------------

# Split comma-separated ARCHES into individual values without associative arrays
arch_list=""
for arch in $(echo "$ARCHES" | tr ',' ' '); do
  arch_list="$arch_list $arch"
done
arch_list="${arch_list# }"   # trim leading space

for arch in $arch_list; do
  echo "==> Building linux/$arch ..."
  docker buildx build \
    --platform "linux/$arch" \
    -t "capsule:$arch" \
    --load \
    .

  echo "==> Saving + gzipping linux/$arch image ..."
  mkdir -p "$TMP/images-$arch"
  # gzip -9 cuts the bundled tar by ~50% with negligible decompression cost
  # at first launch. Gzip is universally available on macOS (gunzip) and on
  # Windows 10+ (tar.exe via libarchive auto-detects gzip), so the launcher
  # decompression path needs no extra binaries shipped.
  docker save "capsule:$arch" | gzip -9 > "$TMP/images-$arch/capsule-image-$arch.tar.gz"

  echo "==> Capturing digest for linux/$arch ..."
  docker image inspect "capsule:$arch" --format '{{.Id}}' \
    > "$TMP/images-$arch/capsule-image-$arch.digest"

  echo "    digest: $(cat "$TMP/images-$arch/capsule-image-$arch.digest")"
done

# ---------------------------------------------------------------------------
# Render docs PDFs
# ---------------------------------------------------------------------------
if [ "$SKIP_PDFS" = 0 ]; then
  RENDER_DOCS="$REPO_ROOT/tools/render_docs.py"
  if [ -f "$RENDER_DOCS" ]; then
    echo "==> Rendering docs PDFs ..."
    python3 "$RENDER_DOCS" || {
      echo "WARNING: render_docs.py failed (missing WeasyPrint or markdown-it-py?)." >&2
      echo "         PDFs will be missing from the bundle. Use --skip-pdfs to suppress." >&2
    }
  else
    echo "WARNING: tools/render_docs.py not found; skipping PDF rendering." >&2
  fi
else
  echo "==> Skipping PDF rendering (--skip-pdfs)."
fi

# ---------------------------------------------------------------------------
# Helper: render a launcher template via sed substitution.
# render_launcher <template> <arch> <image_tag> <tar_name> <digest> <out_file>
# Pass empty strings for arch/image_tag/tar_name/digest to produce the
# universal launcher (placeholders stay empty so the runtime branch fires).
# ---------------------------------------------------------------------------
render_launcher() {
  local tmpl="$1"
  local arch="$2"
  local image_tag="$3"
  local tar_name="$4"
  local digest="$5"
  local out_file="$6"

  sed \
    -e "s|@ARCH@|${arch}|g" \
    -e "s|@IMAGE_TAG@|${image_tag}|g" \
    -e "s|@TAR_NAME@|${tar_name}|g" \
    -e "s|@EXPECTED_DIGEST@|${digest}|g" \
    "$REPO_ROOT/dist-templates/$(basename "$tmpl")" \
    > "$out_file"
}

# ---------------------------------------------------------------------------
# Helper: collect PDFs rendered from docs/
# collect_pdfs <dest_dir> [prefix_filter]
# Copies all docs/**/*.{en,ar,ja,es}.pdf + docs/dist-only/*.pdf into dest_dir,
# renaming them to their ALL-CAPS stem names as the current dist bundles use.
# ---------------------------------------------------------------------------
collect_pdfs() {
  local dest="$1"
  local docs_dir="$REPO_ROOT/docs"

  # Map of source filename prefix → bundle filename prefix (ALL-CAPS)
  # We emit every PDF that exists at the relevant paths.
  for pdf in \
    "$docs_dir/quickstart.en.pdf" \
    "$docs_dir/quickstart.ar.pdf" \
    "$docs_dir/quickstart.ja.pdf" \
    "$docs_dir/quickstart.es.pdf" \
    "$docs_dir/user-guide.en.pdf" \
    "$docs_dir/user-guide.ar.pdf" \
    "$docs_dir/user-guide.ja.pdf" \
    "$docs_dir/user-guide.es.pdf"; do
    [ -f "$pdf" ] || continue
    # e.g. quickstart.en.pdf → USER-GUIDE.en.pdf / README.en.pdf
    # Use the same naming the existing dist bundles already have.
    local basename
    basename="$(basename "$pdf")"
    local stem="${basename%.pdf}"          # e.g. quickstart.en
    local locale="${stem##*.}"            # e.g. en
    local doc="${stem%.*}"                # e.g. quickstart
    case "$doc" in
      quickstart)  cp "$pdf" "$dest/README.${locale}.pdf" ;;
      user-guide)  cp "$pdf" "$dest/USER-GUIDE.${locale}.pdf" ;;
    esac
  done

  # dist-only PDFs (install-mac, install-windows, launchers, verifying-evidence)
  local dist_only="$docs_dir/dist-only"
  if [ -d "$dist_only" ]; then
    for pdf in "$dist_only"/*.pdf; do
      [ -f "$pdf" ] || continue
      local bn
      bn="$(basename "$pdf")"
      # e.g. install-mac.en.pdf → INSTALL-MAC.en.pdf
      local upper_stem
      upper_stem="$(echo "${bn%.pdf}" | tr '[:lower:]' '[:upper:]' | tr '-' '-')"
      # tr '[:lower:]' '[:upper:]' handles the whole stem including the locale suffix
      # but we want INSTALL-MAC.en not INSTALL-MAC.EN; fix the locale part:
      local locale_part="${bn##*.}"       # pdf
      # Actually: bn = install-mac.en.pdf, so:
      #   locale = "en", upper_doc = "INSTALL-MAC"
      local stem2="${bn%.pdf}"            # install-mac.en
      local locale2="${stem2##*.}"        # en
      local doc2="${stem2%.*}"            # install-mac
      local upper_doc
      upper_doc="$(echo "$doc2" | tr '[:lower:]' '[:upper:]')"
      cp "$pdf" "$dest/${upper_doc}.${locale2}.pdf"
    done
  fi
}

# ---------------------------------------------------------------------------
# Helper: zip a dist folder.
# zip_folder <folder_path>   (produces <folder_path>.zip sibling)
# ---------------------------------------------------------------------------
zip_folder() {
  local folder="$1"
  local zip_out="${folder}.zip"

  # Remove stale zip so timestamps don't bleed in.
  rm -f "$zip_out"

  if command -v ditto >/dev/null 2>&1; then
    # macOS ditto: Finder-friendly, preserves resource forks, reproducible flags.
    ditto -c -k --keepParent "$folder" "$zip_out"
  else
    # POSIX fallback.
    local folder_name
    folder_name="$(basename "$folder")"
    local parent
    parent="$(dirname "$folder")"
    (cd "$parent" && zip -qr "$zip_out" "$folder_name")
    # zip -qr puts the zip next to the folder by default relative path — move it.
    mv "$parent/$folder_name.zip" "$zip_out" 2>/dev/null || true
  fi
}

# ---------------------------------------------------------------------------
# Assemble dist folders
# ---------------------------------------------------------------------------

# We need to know which arches were built.
# Parse $arch_list (space-separated).

has_arm64=0
has_amd64=0
for arch in $arch_list; do
  case "$arch" in
    arm64) has_arm64=1 ;;
    amd64) has_amd64=1 ;;
  esac
done

# Convenience: read digest for an arch (returns empty if arch was not built).
read_digest() {
  local arch="$1"
  local f="$TMP/images-$arch/capsule-image-$arch.digest"
  if [ -f "$f" ]; then cat "$f"; else echo ""; fi
}

ARM64_DIGEST="$(read_digest arm64)"
AMD64_DIGEST="$(read_digest amd64)"

mkdir -p "$OUT_DIR"

# ---- 1. Capsule-mac-applesilicon (arm64 only) ----
if [ "$has_arm64" = 1 ]; then
  echo "==> Assembling Capsule-mac-applesilicon ..."
  FOLDER="$OUT_DIR/Capsule-mac-applesilicon"
  rm -rf "$FOLDER"
  mkdir -p "$FOLDER/images"

  cp "$TMP/images-arm64/capsule-image-arm64.tar.gz"    "$FOLDER/images/"
  cp "$TMP/images-arm64/capsule-image-arm64.digest" "$FOLDER/images/"

  render_launcher \
    "dist-templates/Capsule.command.in" \
    "arm64" \
    "capsule:arm64" \
    "capsule-image-arm64.tar.gz" \
    "$ARM64_DIGEST" \
    "$FOLDER/Capsule.command"
  chmod +x "$FOLDER/Capsule.command"

  collect_pdfs "$FOLDER"
  zip_folder "$FOLDER"
fi

# ---- 2. Capsule-mac-intel (amd64 only) ----
if [ "$has_amd64" = 1 ]; then
  echo "==> Assembling Capsule-mac-intel ..."
  FOLDER="$OUT_DIR/Capsule-mac-intel"
  rm -rf "$FOLDER"
  mkdir -p "$FOLDER/images"

  cp "$TMP/images-amd64/capsule-image-amd64.tar.gz"    "$FOLDER/images/"
  cp "$TMP/images-amd64/capsule-image-amd64.digest" "$FOLDER/images/"

  render_launcher \
    "dist-templates/Capsule.command.in" \
    "amd64" \
    "capsule:amd64" \
    "capsule-image-amd64.tar.gz" \
    "$AMD64_DIGEST" \
    "$FOLDER/Capsule.command"
  chmod +x "$FOLDER/Capsule.command"

  collect_pdfs "$FOLDER"
  zip_folder "$FOLDER"
fi

# ---- 3. Capsule-windows (amd64 only) ----
if [ "$has_amd64" = 1 ]; then
  echo "==> Assembling Capsule-windows ..."
  FOLDER="$OUT_DIR/Capsule-windows"
  rm -rf "$FOLDER"
  mkdir -p "$FOLDER/images"

  cp "$TMP/images-amd64/capsule-image-amd64.tar.gz"    "$FOLDER/images/"
  cp "$TMP/images-amd64/capsule-image-amd64.digest" "$FOLDER/images/"

  render_launcher \
    "dist-templates/Capsule.bat.in" \
    "amd64" \
    "capsule:amd64" \
    "capsule-image-amd64.tar.gz" \
    "$AMD64_DIGEST" \
    "$FOLDER/Capsule.bat"

  collect_pdfs "$FOLDER"
  zip_folder "$FOLDER"
fi

# ---- 4. Capsule (universal) ----
# Universal macOS launcher: placeholders left empty so runtime resolves them.
# Windows launcher: amd64 stamped (Windows is amd64-only for now).
echo "==> Assembling Capsule (universal) ..."
FOLDER="$OUT_DIR/Capsule"
rm -rf "$FOLDER"
mkdir -p "$FOLDER/images"

if [ "$has_arm64" = 1 ]; then
  cp "$TMP/images-arm64/capsule-image-arm64.tar.gz"    "$FOLDER/images/"
  cp "$TMP/images-arm64/capsule-image-arm64.digest" "$FOLDER/images/"
fi
if [ "$has_amd64" = 1 ]; then
  cp "$TMP/images-amd64/capsule-image-amd64.tar.gz"    "$FOLDER/images/"
  cp "$TMP/images-amd64/capsule-image-amd64.digest" "$FOLDER/images/"
fi

# Universal macOS launcher: pass empty strings for the arch-specific placeholders.
render_launcher \
  "dist-templates/Capsule.command.in" \
  "" \
  "" \
  "" \
  "" \
  "$FOLDER/Capsule.command"
chmod +x "$FOLDER/Capsule.command"

# Windows launcher in universal bundle: amd64-stamped (no universal .bat needed).
if [ "$has_amd64" = 1 ]; then
  render_launcher \
    "dist-templates/Capsule.bat.in" \
    "amd64" \
    "capsule:amd64" \
    "capsule-image-amd64.tar.gz" \
    "$AMD64_DIGEST" \
    "$FOLDER/Capsule.bat"
fi

collect_pdfs "$FOLDER"
zip_folder "$FOLDER"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "============================================================"
echo "  Capsule dist build complete"
echo "============================================================"
for name in Capsule Capsule-mac-applesilicon Capsule-mac-intel Capsule-windows; do
  zip_path="$OUT_DIR/${name}.zip"
  if [ -f "$zip_path" ]; then
    size="$(du -sh "$zip_path" | cut -f1)"
    if command -v shasum >/dev/null 2>&1; then
      sha256="$(shasum -a 256 "$zip_path" | cut -d' ' -f1)"
    elif command -v sha256sum >/dev/null 2>&1; then
      sha256="$(sha256sum "$zip_path" | cut -d' ' -f1)"
    else
      sha256="(sha256sum not available)"
    fi
    echo "  $zip_path"
    echo "    size:   $size"
    echo "    sha256: $sha256"
    echo
  fi
done

if [ -n "$ARM64_DIGEST" ]; then
  echo "  arm64 image digest: $ARM64_DIGEST"
fi
if [ -n "$AMD64_DIGEST" ]; then
  echo "  amd64 image digest: $AMD64_DIGEST"
fi
echo "============================================================"

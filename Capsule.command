#!/usr/bin/env bash
# Capsule launcher (macOS).
# Double-click to install on first run, double-click again to start.
set -e

IMAGE="ghcr.io/capsule/capsule:latest"
CONTAINER="capsule"
PORT=8080
DOWNLOADS="$HOME/Documents/Capsule"
CONFIG="$HOME/Documents/Capsule/.config"

cd "$(dirname "$0")"

echo "============================================="
echo "  Capsule -- Capture the web, with proof"
echo "============================================="
echo

if ! command -v docker >/dev/null 2>&1; then
  cat <<'MSG'
Docker is not installed.

Capsule needs Docker Desktop. It is free.
Download:  https://www.docker.com/products/docker-desktop

After installing Docker Desktop, double-click this launcher again.
MSG
  read -n 1 -s -r -p "Press any key to close..."
  echo
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Starting Docker Desktop... (first launch can take 30 seconds)"
  open -a Docker || true
  for _ in $(seq 1 60); do
    sleep 2
    if docker info >/dev/null 2>&1; then
      break
    fi
    printf "."
  done
  echo
  if ! docker info >/dev/null 2>&1; then
    echo "Docker Desktop did not start. Open it from Applications, then re-run this launcher."
    read -n 1 -s -r -p "Press any key to close..."
    echo
    exit 1
  fi
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "Setting up Capsule (first launch only -- about 2GB download)..."
  if docker pull "$IMAGE" 2>/dev/null; then
    :
  elif [ -f "Dockerfile" ]; then
    echo "Registry image not available; building from local source..."
    docker build -t "$IMAGE" .
  else
    echo "Could not download or build the Capsule image."
    echo "Check your internet connection and try again."
    read -n 1 -s -r -p "Press any key to close..."
    echo
    exit 1
  fi
fi

mkdir -p "$DOWNLOADS" "$CONFIG"

if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "Capsule is already running."
elif docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  docker start "$CONTAINER" >/dev/null
  echo "Capsule restarted."
else
  docker run -d \
    --name "$CONTAINER" \
    --restart no \
    -p "${PORT}:8080" \
    -v "$DOWNLOADS:/downloads" \
    -v "$CONFIG:/config" \
    -e "CAPSULE_HOST_DOWNLOADS_DIR=$DOWNLOADS" \
    "$IMAGE" >/dev/null
  echo "Capsule started."
fi

URL="http://localhost:${PORT}"
echo "Waiting for the app to come online..."
for _ in $(seq 1 30); do
  if curl -sf "$URL/healthz" >/dev/null 2>&1; then
    open "$URL"
    echo
    echo "Capsule is open in your browser at  $URL"
    echo "To stop the app:           docker stop capsule"
    echo "To start it next time:     double-click this launcher."
    echo "Capsule does NOT auto-start with your computer."
    sleep 2
    exit 0
  fi
  sleep 1
done

echo "App did not respond at $URL. Open Docker Desktop to see container logs."
read -n 1 -s -r -p "Press any key to close..."
echo
exit 1

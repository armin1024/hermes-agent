#!/usr/bin/env bash
# Hermes Agent offline installer with AOPS overlay support.

set -euo pipefail

BUNDLE_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$HOME/hermes-agent"
LINK_BIN=false
INIT_CONFIG=false
UPGRADE_MODE=false
AUTO_LINK_EXISTING=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --link)
      LINK_BIN=true
      shift
      ;;
    --init-config)
      INIT_CONFIG=true
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage: bash install.sh [INSTALL_DIR] [--link] [--init-config]

Arguments:
  INSTALL_DIR    Optional install directory. Default: ~/hermes-agent

Flags:
  --link         Create symlinks in ~/.local/bin
  --init-config  Copy AOPS example config/env into ~/.hermes if missing
EOF
      exit 0
      ;;
    *)
      if [[ "$1" == /* || "$1" == ~* || "$1" != --* ]]; then
        INSTALL_DIR="$1"
        shift
      else
        echo "Unknown option: $1" >&2
        exit 1
      fi
      ;;
  esac
done

PYTHON_TAR="$(find "$BUNDLE_DIR/python" -maxdepth 1 -name 'cpython-*-x86_64-linux-install_only.tar.gz' | head -n 1)"
PYTHON_DIR="$INSTALL_DIR/python311"
VENV_DIR="$INSTALL_DIR/venv"
WHEELS_DIR="$BUNDLE_DIR/wheels"
REQUIREMENTS="$BUNDLE_DIR/requirements.txt"
OVERLAY_DIR="$BUNDLE_DIR/overlay"
OVERLAY_MANIFEST="$BUNDLE_DIR/overlay.manifest"
EXAMPLES_DIR="$BUNDLE_DIR/examples"
PYTHON_BIN=""
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR=""

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail()  { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

capture_upgrade_backup() {
  local src="$1"
  local dst="$2"
  if [[ -e "$src" ]]; then
    mkdir -p "$(dirname "$dst")"
    cp -R "$src" "$dst"
  fi
}

[[ -f "$PYTHON_TAR" ]] || fail "Bundled Python runtime not found"
[[ -f "$REQUIREMENTS" ]] || fail "requirements.txt not found"
[[ -f "$OVERLAY_MANIFEST" ]] || fail "overlay manifest not found"

mkdir -p "$INSTALL_DIR"

if [[ -d "$INSTALL_DIR/venv" || -x "$INSTALL_DIR/hermes" || -d "$INSTALL_DIR/source-overlay" ]]; then
  UPGRADE_MODE=true
  BACKUP_DIR="$INSTALL_DIR/upgrade-backups/$TIMESTAMP"
  mkdir -p "$BACKUP_DIR"
  capture_upgrade_backup "$INSTALL_DIR/source-overlay" "$BACKUP_DIR/source-overlay"
  capture_upgrade_backup "$INSTALL_DIR/hermes" "$BACKUP_DIR/hermes"
  capture_upgrade_backup "$INSTALL_DIR/hermes-gateway" "$BACKUP_DIR/hermes-gateway"
  capture_upgrade_backup "$INSTALL_DIR/hermes-dashboard" "$BACKUP_DIR/hermes-dashboard"
  capture_upgrade_backup "$INSTALL_DIR/hermes-shell" "$BACKUP_DIR/hermes-shell"
fi

if [[ "$LINK_BIN" == false ]]; then
  if [[ -L "$HOME/.local/bin/hermes" && "$(readlink "$HOME/.local/bin/hermes")" == "$INSTALL_DIR/hermes" ]]; then
    AUTO_LINK_EXISTING=true
    LINK_BIN=true
  fi
fi

if [[ "$UPGRADE_MODE" == true ]]; then
  info "Detected existing Hermes install, running in upgrade mode"
  info "Upgrade backup dir: $BACKUP_DIR"
fi

info "Step 1/6: extracting bundled Python runtime"
if [[ -x "$PYTHON_DIR/python/bin/python3.11" ]]; then
  warn "Bundled Python already exists, skipping extraction"
else
  mkdir -p "$PYTHON_DIR"
  tar -xzf "$PYTHON_TAR" -C "$PYTHON_DIR"
fi

PYTHON_BIN="$PYTHON_DIR/python/bin/python3.11"
[[ -x "$PYTHON_BIN" ]] || fail "Bundled Python verification failed"

info "Step 2/6: creating virtual environment"
if [[ -d "$VENV_DIR" ]]; then
  warn "Virtual environment already exists, reusing it"
else
  "$PYTHON_BIN" -m venv "$VENV_DIR" || {
    warn "venv creation failed once, retrying after ensurepip"
    "$PYTHON_BIN" -m ensurepip --upgrade
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  }
fi

info "Step 3/6: installing base offline dependencies"
"$VENV_DIR/bin/python" -m pip install \
  --no-index \
  --find-links "$WHEELS_DIR" \
  -r "$REQUIREMENTS"

info "Step 4/6: applying AOPS overlay files"
SITE_PACKAGES="$("$VENV_DIR/bin/python" - <<'PY'
import sysconfig
print(sysconfig.get_paths()["purelib"])
PY
)"

while IFS= read -r rel; do
  [[ -n "$rel" ]] || continue
  src="$OVERLAY_DIR/$rel"
  dst="$SITE_PACKAGES/$rel"
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
done < "$OVERLAY_MANIFEST"

mkdir -p "$INSTALL_DIR/source-overlay"
cp "$OVERLAY_MANIFEST" "$INSTALL_DIR/source-overlay/"
while IFS= read -r rel; do
  [[ -n "$rel" ]] || continue
  mkdir -p "$(dirname "$INSTALL_DIR/source-overlay/$rel")"
  cp "$OVERLAY_DIR/$rel" "$INSTALL_DIR/source-overlay/$rel"
done < "$OVERLAY_MANIFEST"

info "Step 5/6: generating launchers"
cat > "$INSTALL_DIR/hermes" <<EOF
#!/usr/bin/env bash
exec "$VENV_DIR/bin/hermes" "\$@"
EOF
chmod +x "$INSTALL_DIR/hermes"

cat > "$INSTALL_DIR/hermes-dashboard" <<EOF
#!/usr/bin/env bash
HOST="\${HERMES_DASHBOARD_HOST:-0.0.0.0}"
PORT="\${HERMES_DASHBOARD_PORT:-9119}"
exec "$VENV_DIR/bin/hermes" dashboard --host "\$HOST" --port "\$PORT" --no-open "\$@"
EOF
chmod +x "$INSTALL_DIR/hermes-dashboard"

cat > "$INSTALL_DIR/hermes-gateway" <<EOF
#!/usr/bin/env bash
exec "$VENV_DIR/bin/hermes" gateway "\$@"
EOF
chmod +x "$INSTALL_DIR/hermes-gateway"

cat > "$INSTALL_DIR/hermes-shell" <<EOF
#!/usr/bin/env bash
source "$VENV_DIR/bin/activate"
exec "\$SHELL"
EOF
chmod +x "$INSTALL_DIR/hermes-shell"

if [[ "$LINK_BIN" == true ]]; then
  info "Step 6/6: creating symlinks in ~/.local/bin"
  mkdir -p "$HOME/.local/bin"
  ln -sf "$INSTALL_DIR/hermes" "$HOME/.local/bin/hermes"
  ln -sf "$INSTALL_DIR/hermes-dashboard" "$HOME/.local/bin/hermes-dashboard"
  ln -sf "$INSTALL_DIR/hermes-gateway" "$HOME/.local/bin/hermes-gateway"
else
  info "Step 6/6: skipping ~/.local/bin symlinks"
fi

if [[ "$INIT_CONFIG" == true ]]; then
  HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
  mkdir -p "$HERMES_HOME"
  if [[ ! -f "$HERMES_HOME/config.yaml" ]]; then
    cp "$EXAMPLES_DIR/config.aops.example.yaml" "$HERMES_HOME/config.yaml"
    info "Initialized $HERMES_HOME/config.yaml from example"
  else
    info "Keeping existing $HERMES_HOME/config.yaml"
  fi
  if [[ ! -f "$HERMES_HOME/.env" ]]; then
    cp "$EXAMPLES_DIR/aops.env.example" "$HERMES_HOME/.env"
    info "Initialized $HERMES_HOME/.env from example"
  else
    info "Keeping existing $HERMES_HOME/.env"
  fi
fi

echo
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Hermes Agent offline bundle installed${NC}"
echo -e "${GREEN}========================================${NC}"
echo "Install dir: $INSTALL_DIR"
echo "CLI launcher: $INSTALL_DIR/hermes"
echo "Gateway launcher: $INSTALL_DIR/hermes-gateway"
echo "Dashboard launcher: $INSTALL_DIR/hermes-dashboard"
if [[ "$UPGRADE_MODE" == true ]]; then
  echo "Upgrade mode: preserved existing runtime config and launch path"
  echo "Upgrade backup: $BACKUP_DIR"
fi
if [[ "$AUTO_LINK_EXISTING" == true ]]; then
  echo "Symlinks: preserved existing ~/.local/bin links"
fi
echo
echo "Examples:"
echo "  $INSTALL_DIR/hermes"
echo "  $INSTALL_DIR/hermes-gateway"
echo "  $INSTALL_DIR/hermes-gateway run"
echo "  $INSTALL_DIR/hermes-dashboard"
echo
echo "AOPS examples:"
echo "  $BUNDLE_DIR/examples/config.aops.example.yaml"
echo "  $BUNDLE_DIR/examples/aops.env.example"

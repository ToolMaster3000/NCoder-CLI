#!/data/data/com.termux/files/usr/bin/bash
#
# setup.sh — Idempotent installer + launcher for NCoder, a local
# Gemini-CLI/Claude-Code-style agentic chat interface running
# heretic-org/Nanbeige4.1-3B-heretic via llama.cpp, tuned for 4GB-RAM
# Android devices (Xiaomi/Redmi/Samsung/Huawei).
#
# Version: see NCODER_VERSION below. Full history: CHANGELOG.md.
#
# Safe to re-run: every step checks current state before acting.
# Usage:
#   bash setup.sh              # check/install everything, then launch CLI
#   bash setup.sh --setup-only # install/build only, don't launch
#   bash setup.sh --run-only   # skip checks, just start server+CLI
#   bash setup.sh --force-rebuild  # rebuild llama.cpp even if present
#   bash setup.sh --selftest   # end-to-end health check, then exit
#   bash setup.sh --stop       # stop the background server
#   bash setup.sh --disable-autostart  # remove the Termux launch hook

set -uo pipefail  # not -e: we want to control error handling per-step

NCODER_VERSION="1.8.0"

# ── Config ───────────────────────────────────────────────────────────────
PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
HOME_DIR="${HOME:-/data/data/com.termux/files/home}"
BASE_DIR="$HOME_DIR/ncoder-cli"
LLAMACPP_DIR="$BASE_DIR/llama.cpp"
# Pinned to a specific commit (verified reachable at the time this was
# written) rather than tracking master, since this project depends on
# llama-server's exact "grammar" and OpenAI-compatible chat-completions
# behavior — an upstream CLI/API change could silently break the tool
# orchestration pipeline otherwise. Bump this deliberately (and re-test)
# when you want a newer llama.cpp, rather than always building HEAD.
LLAMACPP_PIN="${LLAMACPP_PIN:-049326a00025d00b08cc188ed716b681e984a3f8}"
MODELS_DIR="$BASE_DIR/models"
VENV_DIR="$BASE_DIR/venv"
LOG_DIR="$BASE_DIR/logs"
BIN_SERVER="$LLAMACPP_DIR/build/bin/llama-server"

MODEL_REPO="heretic-org/Nanbeige4.1-3B-heretic"
# NOTE: fill in the exact GGUF filename you want after checking the repo's
# "Files" tab — quantization filenames vary by uploader convention.
MODEL_FILE="${MODEL_FILE:-Nanbeige4.1-3B-heretic-Q4_K_M.gguf}"
MODEL_URL="${MODEL_URL:-https://huggingface.co/${MODEL_REPO}/resolve/main/${MODEL_FILE}}"
MODEL_PATH="$MODELS_DIR/$MODEL_FILE"

SERVER_HOST="127.0.0.1"
SERVER_PORT="8080"
SERVER_LOG="$LOG_DIR/llama-server.log"
PROMPT_CACHE="$BASE_DIR/prompt.cache"

FORCE_REBUILD=0
SETUP_ONLY=0
RUN_ONLY=0
NO_AUTOSTART=0
DISABLE_AUTOSTART=0
STOP_SERVER=0
SELFTEST=0
SHOW_VERSION=0
for arg in "$@"; do
  case "$arg" in
    --force-rebuild) FORCE_REBUILD=1 ;;
    --setup-only) SETUP_ONLY=1 ;;
    --run-only) RUN_ONLY=1 ;;
    --no-autostart) NO_AUTOSTART=1 ;;
    --disable-autostart) DISABLE_AUTOSTART=1 ;;
    --stop) STOP_SERVER=1 ;;
    --selftest) SELFTEST=1 ;;
    --version) SHOW_VERSION=1 ;;
  esac
done

# ── Helpers ──────────────────────────────────────────────────────────────
C_RESET="\033[0m"; C_G="\033[1;32m"; C_Y="\033[1;33m"; C_R="\033[1;31m"; C_B="\033[1;34m"
log()  { echo -e "${C_B}[*]${C_RESET} $*"; }
ok()   { echo -e "${C_G}[ok]${C_RESET} $*"; }
warn() { echo -e "${C_Y}[!]${C_RESET} $*"; }
err()  { echo -e "${C_R}[error]${C_RESET} $*" >&2; }
die()  { err "$*"; exit 1; }

need_cmd() { command -v "$1" >/dev/null 2>&1; }

# ── 0. Sanity: must be Termux (skip for --version, which needs nothing) ──
if [ "$SHOW_VERSION" -eq 1 ]; then
  echo "NCoder v${NCODER_VERSION}"
  exit 0
fi
if [ ! -d "$PREFIX" ]; then
  die "This doesn't look like Termux (PREFIX not found). Run this inside Termux."
fi
mkdir -p "$BASE_DIR" "$MODELS_DIR" "$LOG_DIR"

# ── 1. Detect hardware (RAM + CPU cores) for tuning later ────────────────
detect_hw() {
  TOTAL_RAM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
  TOTAL_RAM_MB=$(( TOTAL_RAM_KB / 1024 ))
  TOTAL_CORES=$(nproc 2>/dev/null || echo 4)

  # Try to detect "big" cores via cpufreq max frequency clustering.
  # Cores with the highest max freq are treated as performance cores.
  BIG_CORES=0
  if [ -d /sys/devices/system/cpu ]; then
    max_freqs=""
    for cpu in /sys/devices/system/cpu/cpu[0-9]*; do
      f="$cpu/cpufreq/cpuinfo_max_freq"
      [ -r "$f" ] && max_freqs="$max_freqs $(cat "$f")"
    done
    if [ -n "$max_freqs" ]; then
      top_freq=$(echo $max_freqs | tr ' ' '\n' | sort -rn | head -1)
      BIG_CORES=$(echo $max_freqs | tr ' ' '\n' | awk -v t="$top_freq" '$1==t' | wc -l)
    fi
  fi
  [ "$BIG_CORES" -lt 1 ] && BIG_CORES=$(( TOTAL_CORES / 2 ))
  [ "$BIG_CORES" -lt 1 ] && BIG_CORES=2

  log "Detected: ${TOTAL_RAM_MB}MB RAM, ${TOTAL_CORES} CPU cores (${BIG_CORES} performance cores estimated)"

  if [ "$TOTAL_RAM_MB" -lt 3500 ]; then
    warn "RAM is below ~3.5GB. This setup targets 4GB+ devices; expect it to be tight."
  fi
}

# ── 2. Package checks ────────────────────────────────────────────────────
install_pkgs() {
  log "Checking Termux packages..."
  pkg update -y >/dev/null 2>&1 || warn "pkg update had issues, continuing"

  local pkgs=(git cmake clang make python build-essential libopenblas openmp)
  local missing=()
  for p in "${pkgs[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
  done

  if [ "${#missing[@]}" -gt 0 ]; then
    log "Installing missing packages: ${missing[*]}"
    pkg install -y "${missing[@]}" || die "Package install failed"
  else
    ok "All required Termux packages already installed"
  fi

  # storage permission (needed if you ever want to import a model from Downloads)
  if [ ! -d "$HOME_DIR/storage" ]; then
    warn "Termux storage not set up. Run 'termux-setup-storage' manually if you plan to copy models from shared storage."
  fi
}

# ── 3. Python venv + deps for the CLI ────────────────────────────────────
setup_python() {
  if [ ! -d "$VENV_DIR" ]; then
    log "Creating Python venv..."
    python -m venv "$VENV_DIR" || die "venv creation failed"
  fi
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"

  local need_install=0
  for mod in requests rich prompt_toolkit bs4; do
    python -c "import $mod" >/dev/null 2>&1 || need_install=1
  done

  if [ "$need_install" -eq 1 ]; then
    log "Installing Python CLI dependencies..."
    pip install --upgrade pip >/dev/null
    pip install requests rich prompt_toolkit beautifulsoup4 || die "pip install failed"
  else
    ok "Python CLI dependencies already installed"
  fi
  deactivate
}

# ── 4. Build llama.cpp (native, no proot) ────────────────────────────────
build_llamacpp() {
  if [ -x "$BIN_SERVER" ] && [ "$FORCE_REBUILD" -eq 0 ]; then
    ok "llama-server binary already built at $BIN_SERVER"
    return
  fi

  if [ ! -d "$LLAMACPP_DIR" ]; then
    log "Cloning llama.cpp and pinning to a known-working commit..."
    git clone https://github.com/ggml-org/llama.cpp "$LLAMACPP_DIR" \
      || die "git clone failed — check network settings if this repeatedly fails"
    (cd "$LLAMACPP_DIR" && git checkout "$LLAMACPP_PIN") \
      || die "Failed to check out pinned commit $LLAMACPP_PIN — it may have been removed from history (force-push/rebase upstream). Update LLAMACPP_PIN in setup.sh to a current commit."
  else
    local current
    current=$(cd "$LLAMACPP_DIR" && git rev-parse HEAD)
    if [ "$current" != "$LLAMACPP_PIN" ]; then
      log "Checkout is at $current, pinned version is $LLAMACPP_PIN — updating..."
      (cd "$LLAMACPP_DIR" && git fetch origin && git checkout "$LLAMACPP_PIN") \
        || warn "Could not check out pinned commit, building existing checkout instead"
    else
      ok "llama.cpp already at pinned commit $LLAMACPP_PIN"
    fi
  fi

  log "Configuring build (CMake, native ARM optimizations, no GPU backend)..."
  # GGML_NATIVE picks up NEON/dotprod/i8mm automatically where the toolchain
  # supports it; LTO + OpenBLAS give a meaningful prompt-processing speedup
  # on mid-range Arm cores without touching the model itself.
  cmake -S "$LLAMACPP_DIR" -B "$LLAMACPP_DIR/build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DGGML_NATIVE=ON \
    -DGGML_LTO=ON \
    -DGGML_OPENMP=ON \
    -DGGML_BLAS=ON \
    -DGGML_BLAS_VENDOR=OpenBLAS \
    -DLLAMA_CURL=OFF \
    || die "cmake configure failed"

  log "Building llama.cpp (this can take several minutes on-device)..."
  local build_jobs
  build_jobs="${BIG_CORES:-2}"
  cmake --build "$LLAMACPP_DIR/build" --config Release -j"$build_jobs" \
    || die "build failed"

  [ -x "$BIN_SERVER" ] || die "Build finished but llama-server binary not found at $BIN_SERVER"
  ok "llama.cpp built successfully"
}

# ── 5. Model download ────────────────────────────────────────────────────
fetch_expected_sha256() {
  # Queries HF's API for the LFS object's sha256, which is what actually
  # identifies file integrity (file size alone can't catch bit-level
  # corruption or a truncated-but-plausible-length download).
  #
  # Parsed with Python's json module rather than grep/sed against raw
  # JSON — HF's tree API nests the hash as lfs.oid on the matching file
  # entry, which is fragile to hand-parse reliably with shell text tools.
  local api_url="https://huggingface.co/api/models/${MODEL_REPO}/tree/main"
  curl -sL --fail "$api_url" 2>/dev/null | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
target = '${MODEL_FILE}'
for entry in data:
    if entry.get('path') == target:
        lfs = entry.get('lfs') or {}
        oid = lfs.get('oid') or entry.get('oid', '')
        print(oid)
        break
" 2>/dev/null
}

verify_model_checksum() {
  local expected
  expected=$(fetch_expected_sha256)
  if [ -z "$expected" ]; then
    warn "Could not fetch an expected checksum from Hugging Face (API shape may have changed, or offline). Skipping integrity check — file size check already passed."
    return 0
  fi
  log "Verifying model checksum (sha256)..."
  local actual
  actual=$(sha256sum "$MODEL_PATH" | awk '{print $1}')
  if [ "$actual" != "$expected" ]; then
    err "Checksum mismatch! expected $expected, got $actual"
    return 1
  fi
  ok "Checksum verified"
  return 0
}

fetch_model() {
  if [ -f "$MODEL_PATH" ]; then
    local size
    size=$(stat -c%s "$MODEL_PATH" 2>/dev/null || echo 0)
    if [ "$size" -gt 500000000 ]; then   # sanity: >500MB means likely complete
      if verify_model_checksum; then
        ok "Model already present and verified: $MODEL_PATH ($(( size / 1024 / 1024 ))MB)"
        return
      else
        warn "Existing model failed checksum verification, re-downloading"
        rm -f "$MODEL_PATH"
      fi
    else
      warn "Existing model file looks incomplete, re-downloading"
      rm -f "$MODEL_PATH"
    fi
  fi

  # Rough disk space check before a multi-GB download — a download that
  # fails halfway through on a full phone is a bad first-run experience.
  local avail_kb
  avail_kb=$(df -Pk "$MODELS_DIR" 2>/dev/null | awk 'NR==2 {print $4}')
  if [ -n "$avail_kb" ] && [ "$avail_kb" -lt 3000000 ]; then  # ~3GB floor
    warn "Less than ~3GB free on this partition. The model download may fail partway through — consider freeing up space first."
  fi

  log "Downloading model (this is several GB depending on quant, be patient)..."
  log "Source: $MODEL_URL"
  # -C - resumes partial downloads if interrupted
  curl -L -C - --fail --retry 5 --retry-delay 5 -o "$MODEL_PATH" "$MODEL_URL" \
    || die "Model download failed. Verify MODEL_FILE matches an actual file in the HF repo's Files tab, and check your network settings."
  ok "Model downloaded: $MODEL_PATH"

  if ! verify_model_checksum; then
    rm -f "$MODEL_PATH"
    die "Downloaded model failed checksum verification and was deleted. Re-run setup.sh to retry the download."
  fi
}

# ── 6. Vendor-specific battery/OEM guidance (non-interactive, just prints) ─
oem_hints() {
  local manufacturer
  manufacturer=$(getprop ro.product.manufacturer 2>/dev/null | tr '[:upper:]' '[:lower:]')
  case "$manufacturer" in
    xiaomi|redmi)
      warn "MIUI/HyperOS detected: whitelist Termux in Security app > Battery > No restrictions, and disable 'MIUI Optimization' in Developer Options, or long generations may get killed."
      ;;
    samsung)
      warn "One UI detected: add Termux to Device Care > Battery > 'Never sleeping apps', and disable 'Put unused apps to sleep' for it."
      ;;
    huawei)
      warn "EMUI/HarmonyOS detected: add Termux to Protected Apps in Battery settings to prevent background kill."
      ;;
    *)
      log "Manufacturer: ${manufacturer:-unknown}. If generation gets killed mid-run, check your device's battery optimization settings for Termux."
      ;;
  esac
}

# ── 7. Auto-start hook: launch this CLI whenever Termux is opened ────────
AUTOSTART_MARKER="$BASE_DIR/.autostart_installed"
install_autostart() {
  [ -f "$AUTOSTART_MARKER" ] && return   # already installed

  local rc_file="$HOME_DIR/.bashrc"
  local hook='
# --- ncoder-cli autostart (added by setup.sh) ---
if [ -z "$NANBEIGE_CLI_ACTIVE" ] && [ -t 0 ]; then
    export NANBEIGE_CLI_ACTIVE=1
    bash "$HOME/ncoder-cli/setup.sh" --run-only
fi
# --- end ncoder-cli autostart ---
'
  touch "$rc_file"
  if ! grep -q "ncoder-cli autostart" "$rc_file" 2>/dev/null; then
    echo "$hook" >> "$rc_file"
    log "Autostart hook added to ~/.bashrc — the CLI will launch automatically next time Termux opens."
  fi
  # Copy this script + the CLI into the persistent location referenced by
  # the hook, so autostart works even if the user launched setup.sh from
  # a different (e.g. Downloads) directory the first time.
  cp -f "$0" "$BASE_DIR/setup.sh" 2>/dev/null
  cp -f "$(dirname "$0")/nanbeige_cli.py" "$BASE_DIR/nanbeige_cli.py" 2>/dev/null
  touch "$AUTOSTART_MARKER"
}

disable_autostart() {
  local rc_file="$HOME_DIR/.bashrc"
  [ -f "$rc_file" ] || return
  sed -i '/# --- ncoder-cli autostart/,/# --- end ncoder-cli autostart ---/d' "$rc_file"
  rm -f "$AUTOSTART_MARKER"
  ok "Autostart disabled."
}

# ── 8. Launch server + CLI ───────────────────────────────────────────────
server_running() {
  curl -s -o /dev/null -m 2 "http://$SERVER_HOST:$SERVER_PORT/health"
}

reap_stale_server() {
  # Distinguishes three cases: (a) a previous run is alive and healthy —
  # nothing to do; (b) a previous run's pid is alive but not answering
  # /health (hung or mid-crash) — kill it so a restart doesn't collide on
  # the port; (c) a stale pidfile pointing at a dead/reused pid — just
  # clean it up. Without this, a second launch after an unclean exit can
  # silently fail to bind the port or leave two servers fighting over it.
  local pid_file="$BASE_DIR/server.pid"
  [ -f "$pid_file" ] || return
  local old_pid
  old_pid=$(cat "$pid_file" 2>/dev/null)
  [ -n "$old_pid" ] || { rm -f "$pid_file"; return; }

  if kill -0 "$old_pid" 2>/dev/null; then
    if server_running; then
      return  # healthy — start_server's own check will reuse it
    fi
    warn "Found an unresponsive llama-server process (pid $old_pid) — stopping it before restart."
    kill "$old_pid" 2>/dev/null
    sleep 1
    kill -9 "$old_pid" 2>/dev/null  # in case it ignored SIGTERM
  fi
  rm -f "$pid_file"
}

rotate_log_if_large() {
  # Prevents llama-server.log from growing unbounded across many sessions
  # on a phone with limited storage — keeps one rotated backup, not a
  # full logrotate-style chain, since that's all this scale needs.
  local max_bytes=$((5 * 1024 * 1024))  # 5MB
  if [ -f "$SERVER_LOG" ]; then
    local size
    size=$(stat -c%s "$SERVER_LOG" 2>/dev/null || echo 0)
    if [ "$size" -gt "$max_bytes" ]; then
      mv -f "$SERVER_LOG" "${SERVER_LOG}.1"
    fi
  fi
}

pick_context_size() {
  # New improvement 5: RAM-tiered, not a blanket increase. The 4GB tier
  # keeps EXACTLY the context size already proven safe on the minimum
  # target device — no risk introduced there. Higher-RAM devices get a
  # proportional increase (context/KV-cache memory scales roughly
  # linearly with token count), reasoned from the same q8_0 KV cache
  # quantization and headroom margin already validated at the 4GB
  # baseline, not an arbitrary bump. This directly serves Nanbeige4.1-3B
  # being trained for long, coherent single-pass reasoning (documented
  # recommended max_new_tokens: 131072) — capable devices can actually
  # make use of more context toward that, without the minimum-spec
  # device's configuration changing at all.
  if [ "$TOTAL_RAM_MB" -ge 7000 ]; then
    echo 16384
  elif [ "$TOTAL_RAM_MB" -ge 5500 ]; then
    echo 10240
  else
    echo 6144
  fi
}

start_server() {
  reap_stale_server

  if server_running; then
    ok "llama-server already running on $SERVER_HOST:$SERVER_PORT"
    return
  fi

  rotate_log_if_large

  local threads="${BIG_CORES:-4}"
  # Batch threads can safely exceed generation threads since prompt
  # processing parallelizes better across all cores (including small ones).
  local batch_threads="${TOTAL_CORES:-$threads}"
  local ctx_size
  ctx_size=$(pick_context_size)
  log "Starting llama-server (gen-threads=$threads, batch-threads=$batch_threads, ctx=$ctx_size, RAM tier: ${TOTAL_RAM_MB}MB)..."

  nohup "$BIN_SERVER" \
    -m "$MODEL_PATH" \
    -t "$threads" -tb "$batch_threads" \
    -c "$ctx_size" \
    -b 512 -ub 128 \
    -fa on \
    --cache-type-k q8_0 --cache-type-v q8_0 \
    --mlock \
    --no-mmap \
    --prompt-cache "$PROMPT_CACHE" \
    --host "$SERVER_HOST" --port "$SERVER_PORT" \
    > "$SERVER_LOG" 2>&1 &

  local server_pid=$!
  echo "$server_pid" > "$BASE_DIR/server.pid"

  log "Waiting for server to become ready (pid $server_pid)..."
  for i in $(seq 1 60); do
    if server_running; then
      ok "Server ready on http://$SERVER_HOST:$SERVER_PORT"
      return
    fi
    if ! kill -0 "$server_pid" 2>/dev/null; then
      err "Server process died. Last 30 log lines:"
      tail -n 30 "$SERVER_LOG"
      die "Server failed to start"
    fi
    sleep 1
  done
  die "Server did not become ready in time. Check $SERVER_LOG"
}

stop_server() {
  # Server lifecycle note (see README): llama-server intentionally
  # outlives the CLI session for faster subsequent launches. This is the
  # explicit, documented way to stop it when you actually want it down
  # (e.g. to free RAM, or before rebuilding/upgrading).
  local pid_file="$BASE_DIR/server.pid"
  if [ -f "$pid_file" ]; then
    local pid
    pid=$(cat "$pid_file")
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid"
      ok "Stopped llama-server (pid $pid)"
    else
      warn "pidfile present but process not running"
    fi
    rm -f "$pid_file"
  else
    warn "No server.pid found — nothing to stop (or it was started outside this script)"
  fi
}

launch_cli() {
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  python "$BASE_DIR/nanbeige_cli.py" --host "$SERVER_HOST" --port "$SERVER_PORT"
  echo
  log "CLI exited. llama-server is still running in the background for faster next launch."
  log "Run 'bash setup.sh --stop' if you want to shut it down and free RAM."
}

# ── Self-test: verifies the whole stack end-to-end ────────────────────────
run_selftest() {
  log "Running self-test..."
  start_server
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  python "$BASE_DIR/nanbeige_cli.py" --host "$SERVER_HOST" --port "$SERVER_PORT" --selftest
  local rc=$?
  if [ "$rc" -eq 0 ]; then
    ok "Self-test passed"
  else
    err "Self-test failed (see output above)"
  fi
  return "$rc"
}

# ── Main ─────────────────────────────────────────────────────────────────
main() {
  log "NCoder v${NCODER_VERSION}"
  detect_hw

  if [ "$STOP_SERVER" -eq 1 ]; then
    stop_server
    exit 0
  fi

  if [ "$DISABLE_AUTOSTART" -eq 1 ]; then
    disable_autostart
    exit 0
  fi

  if [ "$RUN_ONLY" -eq 0 ]; then
    install_pkgs
    setup_python
    build_llamacpp
    fetch_model
    oem_hints
    [ "$NO_AUTOSTART" -eq 0 ] && install_autostart
  fi

  if [ "$SETUP_ONLY" -eq 1 ]; then
    ok "Setup complete. Run without --setup-only to launch."
    exit 0
  fi

  if [ "$SELFTEST" -eq 1 ]; then
    run_selftest
    exit $?
  fi

  start_server
  launch_cli
}

main "$@"

#!/usr/bin/env bash
# entrypoint.sh — env-only bootstrap for oh-my-pi over IRC via MCP.
#
# The USER only sets environment variables (see .env.example). Everything
# omp normally needs on disk is generated here at container start:
#   $OMP_WORKDIR/.omp/mcp.json   — MCP server pointing at mcp_irc_server.py
#   $LLM_PROVIDER key env        — LLM_API_KEY is re-exported under the
#                                  provider-specific name omp expects.
#
# Architecture: ONE long-running omp process (a persistent session). The IRC
# daemon (mcp_irc_server.py --daemon) owns the IRC socket and spools traffic;
# loop.py is the supervisor: it forwards new trusted mentions and durable
# queued tasks into the omp session's stdin and sends each stdout reply back
# to IRC with irc_say (over the daemon's unix socket). All state lives under
# /state and survives restarts.
set -euo pipefail

# ------------------------------------------------------------------ config
export OMP_WORKDIR="${OMP_WORKDIR:-/state/omp-work}"
export TASKS_DIR="${TASKS_DIR:-/state/tasks}"
export IRC_SPOOL_DIR="${IRC_SPOOL_DIR:-/state/irc}"
export TRUST_FILE="${TRUST_FILE:-/state/trust.txt}"
LOOP_POLL_SECONDS="${LOOP_POLL_SECONDS:-15}"
LOOP_REPLY_IDLE_SECONDS="${LOOP_REPLY_IDLE_SECONDS:-8}"   # stdout quiescence = reply done
LLM_PROVIDER="${LLM_PROVIDER:-}"
LLM_MODEL="${LLM_MODEL:-}"
LLM_API_KEY="${LLM_API_KEY:-}"
LLM_BASE_URL="${LLM_BASE_URL:-}"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$OMP_WORKDIR/.omp" "$TASKS_DIR/archive" "$IRC_SPOOL_DIR"

# ------------------------------------------- env -> provider key (omp reads
# provider-specific names, e.g. OPENROUTER_API_KEY; map LLM_API_KEY across).
declare -A PROVIDER_KEY=(
  [openrouter]=OPENROUTER_API_KEY   [openai]=OPENAI_API_KEY
  [anthropic]=ANTHROPIC_API_KEY     [gemini]=GEMINI_API_KEY
  [groq]=GROQ_API_KEY               [deepseek]=DEEPSEEK_API_KEY
  [mistral]=MISTRAL_API_KEY         [xai]=XAI_API_KEY
  [zai]=ZAI_API_KEY                 [together]=TOGETHER_API_KEY
  [fireworks]=FIREWORKS_API_KEY     [cerebras]=CEREBRAS_API_KEY
  [ollama]=OLLAMA_API_KEY           [vllm]=VLLM_API_KEY
  [lm-studio]=LM_STUDIO_API_KEY     [openai-compatible]=OPENAI_API_KEY
)
if [[ -n "$LLM_PROVIDER" && -n "$LLM_API_KEY" ]]; then
  key_name="${PROVIDER_KEY[$LLM_PROVIDER]:-}"
  if [[ -n "$key_name" ]]; then
    export "$key_name=$LLM_API_KEY"
  else
    echo "[entrypoint] unknown LLM_PROVIDER '$LLM_PROVIDER'; set the provider key env yourself" >&2
  fi
fi
if [[ -n "$LLM_BASE_URL" ]]; then
  case "$LLM_PROVIDER" in
    openai|openai-compatible|ollama|vllm|lm-studio) export OPENAI_BASE_URL="$LLM_BASE_URL" ;;
  esac
fi

# ------------------------------------------- generate MCP config (exception:
# omp reads MCP servers from .omp/mcp.json — there is no env var for it, so
# the entrypoint renders it from env at start).
cat > "$OMP_WORKDIR/.omp/mcp.json" <<JSON
{
  "mcpServers": {
    "irc": {
      "type": "stdio",
      "command": "python3",
      "args": ["${APP_DIR}/mcp_irc_server.py"],
      "env": {
        "IRC_SERVER": "${IRC_SERVER:-}",
        "IRC_PORT": "${IRC_PORT:-6697}",
        "IRC_TLS": "${IRC_TLS:-}",
        "IRC_NICK": "${IRC_NICK:-omp-bot}",
        "IRC_NICKSERV_PASS": "${IRC_NICKSERV_PASS:-}",
        "IRC_SERVER_PASS": "${IRC_SERVER_PASS:-}",
        "IRC_CHANNELS": "${IRC_CHANNELS:-}",
        "IRC_IGNORE_NICKS": "${IRC_IGNORE_NICKS:-}",
        "IRC_SPOOL_DIR": "${IRC_SPOOL_DIR}",
        "TRUST_FILE": "${TRUST_FILE}"
      }
    }
  }
}
JSON
chmod 600 "$OMP_WORKDIR/.omp/mcp.json"   # it may hold IRC_NICKSERV_PASS

# ------------------------------------------- seed durable state
task_db="$TASKS_DIR/queue.jsonl"
[[ -f "$task_db" ]] || echo '{"id":"example","status":"pending","prompt":"Say hello in the channel and introduce yourself briefly.","checkpoint":""}' > "$task_db"
if [[ ! -f "$TRUST_FILE" && -n "${IRC_TRUST_HOSTS:-}" ]]; then
  # Host globs from env (comma/space separated), fleet policy.json style.
  tr ',[:space:]' '\n\n' <<<"$IRC_TRUST_HOSTS" | sed '/^$/d' > "$TRUST_FILE"
  chmod 600 "$TRUST_FILE"
fi

# ------------------------------------------------------------------ daemon
# The daemon owns the IRC socket so the nick never flaps and no channel
# traffic is missed. It spools messages and serves MCP (irc_say, irc_recent)
# over a unix socket; omp's MCP instance and loop.py both talk to it.
python3 "$APP_DIR/mcp_irc_server.py" --daemon &
DAEMON_PID=$!
trap 'kill "$DAEMON_PID" 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do [[ -S "$IRC_SPOOL_DIR/mcp.sock" ]] && break; sleep 0.1; done

echo "[entrypoint] omp-irc-mcp up: nick=${IRC_NICK:-omp-bot} server=${IRC_SERVER:-?} workdir=$OMP_WORKDIR"

# ------------------------------------------------------------------ supervise
# ONE persistent omp session; loop.py feeds it mentions + tasks and relays
# its stdout to IRC. exec so loop.py is PID of this container process.
exec python3 "$APP_DIR/loop.py" \
  --workdir "$OMP_WORKDIR" \
  --prompt "$APP_DIR/PROMPT.md" \
  --tasks "$task_db" \
  --poll "$LOOP_POLL_SECONDS" \
  --reply-idle "$LOOP_REPLY_IDLE_SECONDS" \
  ${LLM_MODEL:+--model "$LLM_MODEL"}

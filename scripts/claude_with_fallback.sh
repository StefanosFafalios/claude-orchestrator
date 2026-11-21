#!/usr/bin/env bash
# Run a task or handoff file through the local Claude Code CLI.
#
# Usage: claude_with_fallback.sh <task-or-handoff-file>
# Exit codes: 0 success, 42 usage limit (prints CLAUDE_USAGE_LIMIT), 1 other failure, 2 bad usage.
# Env: CLAUDE_BIN (default: claude), CLAUDE_MODEL, CLAUDE_EFFORT (optional),
#      CLAUDE_PERMISSION_MODE (default acceptEdits: file edits, no unapproved shell commands).
set -uo pipefail

if [[ $# -ne 1 || ! -f "$1" ]]; then
  echo "usage: $0 <task-or-handoff-file>" >&2
  exit 2
fi

limit_re='hit your (session|weekly) limit|usage limit|limit has been reached'
weak_re='resets'
# A successful reply is only a limit notice when it says so plainly; "resets" or "usage limit" alone is ordinary prose.
success_limit_re="you('ve| have) hit your ([a-z0-9-]+ )?limit|(session|weekly|5-hour|usage) limit reached|limit has been reached"
ordinary_re='unauthori[sz]ed|authenticat|not logged in|log ?in required|invalid api key|permission denied|forbidden|ENOTFOUND|ECONNREFUSED|ECONNRESET|ETIMEDOUT|network|could not resolve|timed out'

out_file=$(mktemp)
err_file=$(mktemp)
trap 'rm -f "$out_file" "$err_file"' EXIT

args=(-p "$(cat "$1")" --output-format text --permission-mode "${CLAUDE_PERMISSION_MODE:-acceptEdits}")
[[ -n "${CLAUDE_MODEL:-}" ]] && args+=(--model "$CLAUDE_MODEL")
[[ -n "${CLAUDE_EFFORT:-}" ]] && args+=(--effort "$CLAUDE_EFFORT")

"${CLAUDE_BIN:-claude}" "${args[@]}" >"$out_file" 2>"$err_file"
status=$?

is_limit() {
  local text="$1"
  grep -qiE "$limit_re" <<<"$text" && return 0
  grep -qiE "$weak_re" <<<"$text" && ! grep -qiE "$ordinary_re" <<<"$text"
}

if [[ $status -eq 0 ]]; then
  # A successful answer may legitimately discuss limits; only a short reply is a limit notice.
  if [[ $(wc -c <"$out_file") -lt 400 ]] && grep -qiE "$success_limit_re" <<<"$(cat "$out_file" "$err_file")"; then
    echo "CLAUDE_USAGE_LIMIT"
    cat "$out_file" "$err_file" >&2
    exit 42
  fi
  cat "$out_file"
  exit 0
fi

if is_limit "$(cat "$out_file" "$err_file")"; then
  echo "CLAUDE_USAGE_LIMIT"
  cat "$out_file" "$err_file" >&2
  exit 42
fi

cat "$out_file"
cat "$err_file" >&2
exit 1

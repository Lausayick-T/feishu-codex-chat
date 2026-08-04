#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
ROOT_DIR="${CHAT_AGENT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PYTHON="${CHAT_AGENT_PYTHON:-$ROOT_DIR/.venv/bin/python}"
STATE_DIR="${CHAT_AGENT_STATE_DIR:-$ROOT_DIR/state/services}"
TMUX_SESSION="${CHAT_AGENT_TMUX_SESSION:-feishu-codex-chat}"
RUNTIME_PATH="${CHAT_AGENT_RUNTIME_PATH:-${PATH:-/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin}}"

SERVER_SCRIPT="${CHAT_AGENT_SERVER_SCRIPT:-$ROOT_DIR/server.py}"
SCHEDULER_SCRIPT="${CHAT_AGENT_SCHEDULER_SCRIPT:-$ROOT_DIR/scheduler_service.py}"

SERVICE_NAMES=(server scheduler)
SERVICE_SCRIPTS=("$SERVER_SCRIPT" "$SCHEDULER_SCRIPT")

MACOS_LABEL="io.github.feishu-codex-chat"
LINUX_UNIT="feishu-codex-chat.service"
# Bash 3.2（macOS 系统自带版本）在 set -u 下展开空数组会报错。
# 保留一个空哨兵，并在所有使用点跳过它，兼容新旧 Bash。
LEGACY_MACOS_LABELS=("")
LEGACY_LINUX_UNITS=("")
LEGACY_TMUX_SESSIONS=(
  feishu-server
  chat-agent-server
  feishu-scheduler
)

DRY_RUN=0

usage() {
  cat <<'EOF'
用法：
  scripts/servicectl.sh                         # 默认：当前终端前台运行两个服务
  scripts/servicectl.sh doctor [--offline]      # 启动前诊断
  scripts/servicectl.sh foreground
  scripts/servicectl.sh start                   # foreground 的简写
  scripts/servicectl.sh tmux       start|stop|restart|status|logs
  scripts/servicectl.sh nohup      start|stop|restart|status|logs
  scripts/servicectl.sh autostart   install|uninstall|restart|status|logs
  scripts/servicectl.sh autostart   render [macos|linux] [输出目录]

运行方式：
  foreground  默认方式；在当前终端托管两个服务，Ctrl-C 一起停止
  tmux        一个 tmux 会话，每个服务一个窗口
  nohup       纯后台进程，PID 和日志保存在 state/services
  autostart   用 LaunchAgent/systemd 在登录或开机后启动上述 tmux 会话

兼容别名：
  background  等同于 nohup

全局选项：
  --dry-run   只显示将执行的操作

环境覆盖（主要用于迁移和测试）：
  CHAT_AGENT_ROOT
  CHAT_AGENT_PYTHON
  CHAT_AGENT_STATE_DIR
  CHAT_AGENT_TMUX_SESSION
  CHAT_AGENT_RUNTIME_PATH
  CHAT_AGENT_SERVER_SCRIPT
  CHAT_AGENT_SCHEDULER_SCRIPT
  CHAT_AGENT_AUTOSTART_DIR
  CHAT_AGENT_PLATFORM=macos|linux
EOF
}

log() {
  printf '%s\n' "$*"
}

die() {
  printf '错误：%s\n' "$*" >&2
  exit 1
}

print_command() {
  local rendered=""
  local arg
  for arg in "$@"; do
    printf -v arg '%q' "$arg"
    rendered="${rendered}${rendered:+ }${arg}"
  done
  log "[dry-run] $rendered"
}

run_command() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    print_command "$@"
    return 0
  fi
  "$@"
}

ensure_runtime() {
  [[ -x "$PYTHON" ]] || die "Python 不可执行：$PYTHON"

  local script
  for script in "${SERVICE_SCRIPTS[@]}"; do
    [[ -f "$script" ]] || die "服务入口不存在：$script"
  done

  run_command mkdir -p "$STATE_DIR"
}

run_doctor() {
  local doctor_python="$PYTHON"
  if [[ ! -x "$doctor_python" ]]; then
    doctor_python="$(command -v python3 || true)"
  fi
  [[ -n "$doctor_python" ]] || die "未找到可用的 Python 3"
  "$doctor_python" "$SCRIPT_DIR/doctor.py" --root "$ROOT_DIR" "$@"
}

pid_file() {
  printf '%s/%s.pid' "$STATE_DIR" "$1"
}

stdout_file() {
  printf '%s/%s.out.log' "$STATE_DIR" "$1"
}

stderr_file() {
  printf '%s/%s.err.log' "$STATE_DIR" "$1"
}

read_pid() {
  local file
  file="$(pid_file "$1")"
  [[ -f "$file" ]] || return 1

  local pid
  pid="$(tr -d '[:space:]' < "$file")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  printf '%s' "$pid"
}

background_service_running() {
  local pid
  pid="$(read_pid "$1")" || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  pid_matches_service "$1" "$pid"
}

service_script_for_name() {
  local target_name="$1"
  local index
  for index in "${!SERVICE_NAMES[@]}"; do
    if [[ "${SERVICE_NAMES[$index]}" == "$target_name" ]]; then
      printf '%s' "${SERVICE_SCRIPTS[$index]}"
      return 0
    fi
  done
  return 1
}

pid_matches_service() {
  local name="$1"
  local pid="$2"
  local script
  script="$(service_script_for_name "$name")" || return 1

  local command
  command="$(ps -p "$pid" -o command= 2>/dev/null)" || return 1
  [[ "$command" == *"$script"* ]]
}

any_background_running() {
  local name
  for name in "${SERVICE_NAMES[@]}"; do
    if background_service_running "$name"; then
      return 0
    fi
  done
  return 1
}

tmux_session_exists() {
  command -v tmux >/dev/null 2>&1 || return 1
  tmux has-session -t "=$1" 2>/dev/null
}

legacy_tmux_sessions() {
  if [[ "${CHAT_AGENT_IGNORE_LEGACY_TMUX:-0}" == "1" ]]; then
    return 1
  fi
  local found=0
  local session
  for session in "${LEGACY_TMUX_SESSIONS[@]}"; do
    if tmux_session_exists "$session"; then
      log "  - $session"
      found=1
    fi
  done
  return "$((1 - found))"
}

detect_platform() {
  if [[ -n "${CHAT_AGENT_PLATFORM:-}" ]]; then
    case "$CHAT_AGENT_PLATFORM" in
      macos|linux)
        printf '%s' "$CHAT_AGENT_PLATFORM"
        return 0
        ;;
      *)
        die "CHAT_AGENT_PLATFORM 只能是 macos 或 linux"
        ;;
    esac
  fi

  case "$(uname -s)" in
    Darwin) printf 'macos' ;;
    Linux) printf 'linux' ;;
    *) die "暂不支持的平台：$(uname -s)" ;;
  esac
}

autostart_service_active() {
  local platform
  platform="$(detect_platform)"

  if [[ "$platform" == "macos" ]]; then
    local label
    for label in "$MACOS_LABEL" "${LEGACY_MACOS_LABELS[@]}"; do
      [[ -n "$label" ]] || continue
      if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
        return 0
      fi
    done
  else
    command -v systemctl >/dev/null 2>&1 || return 1
    local unit
    for unit in "$LINUX_UNIT" "${LEGACY_LINUX_UNITS[@]}"; do
      [[ -n "$unit" ]] || continue
      if systemctl --user is-enabled --quiet "$unit"; then
        return 0
      fi
    done
  fi
  return 1
}

check_start_conflicts() {
  local target_mode="$1"
  local has_conflict=0

  if [[ "$target_mode" != "tmux" && "$target_mode" != "autostart" ]] \
    && tmux_session_exists "$TMUX_SESSION"; then
    log "检测到统一 tmux 会话正在运行：$TMUX_SESSION"
    has_conflict=1
  fi

  if [[ "$target_mode" != "background" ]] && any_background_running; then
    log "检测到 servicectl 管理的后台进程正在运行。"
    has_conflict=1
  fi

  if [[ "$target_mode" != "autostart" && "${CHAT_AGENT_AUTOSTART_BOOT:-0}" != "1" ]] \
    && autostart_service_active; then
    log "检测到开机自启动服务正在运行。"
    has_conflict=1
  fi

  local legacy_output
  legacy_output="$(legacy_tmux_sessions 2>/dev/null || true)"
  if [[ -n "$legacy_output" ]]; then
    log "检测到旧 tmux 会话："
    printf '%s\n' "$legacy_output"
    has_conflict=1
  fi

  if [[ "$has_conflict" -eq 1 ]]; then
    die "为避免同一服务重复运行，请先停止现有实例。"
  fi
}

service_command() {
  local script="$1"
  printf '%q -u %q' "$PYTHON" "$script"
}

FOREGROUND_PIDS=()
FOREGROUND_STOPPING=0

foreground_cleanup() {
  if [[ "$FOREGROUND_STOPPING" -eq 1 ]]; then
    return 0
  fi
  FOREGROUND_STOPPING=1
  trap - EXIT INT TERM HUP

  local index
  local pid
  for ((index=${#FOREGROUND_PIDS[@]} - 1; index >= 0; index--)); do
    pid="${FOREGROUND_PIDS[$index]}"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done

  local attempt
  local any_alive
  for attempt in $(seq 1 50); do
    any_alive=0
    for pid in "${FOREGROUND_PIDS[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then
        any_alive=1
        break
      fi
    done
    [[ "$any_alive" -eq 0 ]] && break
    sleep 0.1
  done

  for pid in "${FOREGROUND_PIDS[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null || true
  done
}

foreground_signal() {
  local exit_code="$1"
  log "收到停止信号，正在停止两个服务……"
  foreground_cleanup
  exit "$exit_code"
}

foreground_start() {
  ensure_runtime
  check_start_conflicts foreground

  local index
  local name
  local script
  local pid
  cd "$ROOT_DIR"
  for index in "${!SERVICE_NAMES[@]}"; do
    name="${SERVICE_NAMES[$index]}"
    script="${SERVICE_SCRIPTS[$index]}"
    "$PYTHON" -u "$script" &
    pid=$!
    FOREGROUND_PIDS+=("$pid")
    log "$name 已在前台托管，pid=$pid"
  done

  trap 'foreground_signal 130' INT
  trap 'foreground_signal 143' TERM
  trap 'foreground_signal 129' HUP
  trap foreground_cleanup EXIT
  log "两个服务均已启动；按 Ctrl-C 一起停止。"

  local status
  while true; do
    for index in "${!FOREGROUND_PIDS[@]}"; do
      pid="${FOREGROUND_PIDS[$index]}"
      if ! kill -0 "$pid" 2>/dev/null; then
        status=0
        wait "$pid" || status=$?
        log "${SERVICE_NAMES[$index]} 已退出（code=${status}），正在停止另一个服务。"
        foreground_cleanup
        [[ "$status" -ne 0 ]] && return "$status"
        return 1
      fi
    done
    sleep 0.2
  done
}

tmux_start() {
  ensure_runtime
  command -v tmux >/dev/null 2>&1 || die "未找到 tmux"

  if tmux_session_exists "$TMUX_SESSION"; then
    log "tmux 已在运行：$TMUX_SESSION"
    return 0
  fi
  check_start_conflicts tmux

  local index
  local name
  local command
  for index in "${!SERVICE_NAMES[@]}"; do
    name="${SERVICE_NAMES[$index]}"
    command="$(service_command "${SERVICE_SCRIPTS[$index]}")"

    if [[ "$index" -eq 0 ]]; then
      run_command tmux new-session -d -s "$TMUX_SESSION" -n "$name" -c "$ROOT_DIR" "$command"
    else
      run_command tmux new-window -d -t "=$TMUX_SESSION" -n "$name" -c "$ROOT_DIR" "$command"
    fi
  done

  if [[ "$DRY_RUN" -eq 0 ]]; then
    sleep 1
    tmux_session_exists "$TMUX_SESSION" || die "tmux 会话启动后立即退出"
  fi
  log "tmux 已启动：${TMUX_SESSION}（${#SERVICE_NAMES[@]} 个窗口）"
}

tmux_stop() {
  command -v tmux >/dev/null 2>&1 || die "未找到 tmux"
  if ! tmux_session_exists "$TMUX_SESSION"; then
    log "tmux 未运行：$TMUX_SESSION"
    return 0
  fi
  run_command tmux kill-session -t "=$TMUX_SESSION"
  log "tmux 已停止：$TMUX_SESSION"
}

tmux_status() {
  if ! tmux_session_exists "$TMUX_SESSION"; then
    log "tmux：未运行（${TMUX_SESSION}）"
    return 1
  fi

  log "tmux：运行中（${TMUX_SESSION}）"
  tmux list-windows -t "=$TMUX_SESSION" -F '  #{window_index}: #{window_name} pid=#{pane_pid} command=#{pane_current_command}'
}

tmux_logs() {
  tmux_session_exists "$TMUX_SESSION" || die "tmux 未运行：$TMUX_SESSION"
  local name
  for name in "${SERVICE_NAMES[@]}"; do
    log "[$name]"
    tmux capture-pane -p -t "=${TMUX_SESSION}:$name" -S -120
  done
}

background_start() {
  ensure_runtime
  check_start_conflicts background

  local index
  local name
  local script
  local pid
  local pid_path
  local out_path
  local err_path

  for index in "${!SERVICE_NAMES[@]}"; do
    name="${SERVICE_NAMES[$index]}"
    script="${SERVICE_SCRIPTS[$index]}"
    pid_path="$(pid_file "$name")"
    out_path="$(stdout_file "$name")"
    err_path="$(stderr_file "$name")"

    if background_service_running "$name"; then
      log "$name 已在运行，pid=$(read_pid "$name")"
      continue
    fi

    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "[dry-run] nohup $PYTHON -u $script >> $out_path 2>> $err_path &"
      continue
    fi

    rm -f "$pid_path"
    nohup "$PYTHON" -u "$script" >>"$out_path" 2>>"$err_path" &
    pid=$!
    printf '%s\n' "$pid" > "$pid_path"

    sleep 1
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$pid_path"
      die "$name 启动后立即退出，请查看 $err_path"
    fi
    log "$name 已启动，pid=$pid"
  done
}

stop_background_service() {
  local name="$1"
  local pid
  pid="$(read_pid "$name")" || {
    rm -f "$(pid_file "$name")"
    log "$name 未运行"
    return 0
  }

  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$(pid_file "$name")"
    log "$name 未运行（已清理过期 PID）"
    return 0
  fi

  if ! pid_matches_service "$name" "$pid"; then
    rm -f "$(pid_file "$name")"
    log "$name 的 PID 已被其他进程复用，未发送停止信号并已清理过期 PID。"
    return 0
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    print_command kill "$pid"
    return 0
  fi

  kill "$pid"
  local attempt
  for attempt in $(seq 1 50); do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$(pid_file "$name")"
      log "$name 已停止"
      return 0
    fi
    sleep 0.1
  done

  kill -KILL "$pid"
  rm -f "$(pid_file "$name")"
  log "$name 超时，已强制停止"
}

background_stop() {
  local index
  for ((index=${#SERVICE_NAMES[@]} - 1; index >= 0; index--)); do
    stop_background_service "${SERVICE_NAMES[$index]}"
  done
}

background_status() {
  local all_running=1
  local name
  local pid
  for name in "${SERVICE_NAMES[@]}"; do
    if background_service_running "$name"; then
      pid="$(read_pid "$name")"
      log "${name}：运行中，pid=$pid"
    else
      log "${name}：未运行"
      all_running=0
    fi
  done
  [[ "$all_running" -eq 1 ]]
}

background_logs() {
  local name
  local file
  for name in "${SERVICE_NAMES[@]}"; do
    for file in "$(stdout_file "$name")" "$(stderr_file "$name")"; do
      log "[$file]"
      if [[ -f "$file" ]]; then
        tail -n 80 "$file"
      else
        log "暂无日志"
      fi
    done
  done
}

xml_escape() {
  local value="$1"
  value="${value//&/&amp;}"
  value="${value//</&lt;}"
  value="${value//>/&gt;}"
  printf '%s' "$value"
}

write_launchd_plist() {
  local output="$1"

  local escaped_label
  local escaped_controller
  local escaped_root
  local escaped_python
  local escaped_state
  local escaped_session
  local escaped_path
  local escaped_out
  local escaped_err
  escaped_label="$(xml_escape "$MACOS_LABEL")"
  escaped_controller="$(xml_escape "$SCRIPT_PATH")"
  escaped_root="$(xml_escape "$ROOT_DIR")"
  escaped_python="$(xml_escape "$PYTHON")"
  escaped_state="$(xml_escape "$STATE_DIR")"
  escaped_session="$(xml_escape "$TMUX_SESSION")"
  escaped_path="$(xml_escape "$RUNTIME_PATH")"
  escaped_out="$(xml_escape "$STATE_DIR/autostart.out.log")"
  escaped_err="$(xml_escape "$STATE_DIR/autostart.err.log")"

  {
    printf '%s\n' '<?xml version="1.0" encoding="UTF-8"?>'
    printf '%s\n' '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    printf '%s\n' '<plist version="1.0">'
    printf '%s\n' '<dict>'
    printf '  <key>Label</key><string>%s</string>\n' "$escaped_label"
    printf '%s\n' '  <key>ProgramArguments</key>'
    printf '%s\n' '  <array>'
    printf '    <string>%s</string>\n' "$escaped_controller"
    printf '%s\n' '    <string>tmux</string>'
    printf '%s\n' '    <string>start</string>'
    printf '%s\n' '  </array>'
    printf '  <key>WorkingDirectory</key><string>%s</string>\n' "$escaped_root"
    printf '%s\n' '  <key>EnvironmentVariables</key>'
    printf '%s\n' '  <dict>'
    printf '    <key>PATH</key><string>%s</string>\n' "$escaped_path"
    printf '    <key>CHAT_AGENT_ROOT</key><string>%s</string>\n' "$escaped_root"
    printf '    <key>CHAT_AGENT_PYTHON</key><string>%s</string>\n' "$escaped_python"
    printf '    <key>CHAT_AGENT_STATE_DIR</key><string>%s</string>\n' "$escaped_state"
    printf '    <key>CHAT_AGENT_TMUX_SESSION</key><string>%s</string>\n' "$escaped_session"
    printf '%s\n' '    <key>CHAT_AGENT_AUTOSTART_BOOT</key><string>1</string>'
    printf '%s\n' '  </dict>'
    printf '%s\n' '  <key>RunAtLoad</key><true/>'
    printf '%s\n' '  <key>ProcessType</key><string>Background</string>'
    printf '  <key>StandardOutPath</key><string>%s</string>\n' "$escaped_out"
    printf '  <key>StandardErrorPath</key><string>%s</string>\n' "$escaped_err"
    printf '%s\n' '</dict>'
    printf '%s\n' '</plist>'
  } > "$output"
}

systemd_escape_value() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '%s' "$value"
}

write_systemd_unit() {
  local output="$1"

  local escaped_controller
  local escaped_root
  local escaped_python
  local escaped_state
  local escaped_session
  local escaped_path
  local escaped_out
  local escaped_err
  escaped_controller="$(systemd_escape_value "$SCRIPT_PATH")"
  escaped_python="$(systemd_escape_value "$PYTHON")"
  escaped_root="$(systemd_escape_value "$ROOT_DIR")"
  escaped_state="$(systemd_escape_value "$STATE_DIR")"
  escaped_session="$(systemd_escape_value "$TMUX_SESSION")"
  escaped_path="$(systemd_escape_value "$RUNTIME_PATH")"
  escaped_out="$(systemd_escape_value "$STATE_DIR/autostart.out.log")"
  escaped_err="$(systemd_escape_value "$STATE_DIR/autostart.err.log")"

  {
    printf '%s\n' '[Unit]'
    printf '%s\n' 'Description=Feishu Codex Chat tmux session'
    printf '%s\n' 'After=network-online.target'
    printf '%s\n' 'Wants=network-online.target'
    printf '\n%s\n' '[Service]'
    printf '%s\n' 'Type=oneshot'
    printf '%s\n' 'RemainAfterExit=yes'
    printf 'WorkingDirectory="%s"\n' "$escaped_root"
    printf 'Environment="PATH=%s"\n' "$escaped_path"
    printf 'Environment="CHAT_AGENT_ROOT=%s"\n' "$escaped_root"
    printf 'Environment="CHAT_AGENT_PYTHON=%s"\n' "$escaped_python"
    printf 'Environment="CHAT_AGENT_STATE_DIR=%s"\n' "$escaped_state"
    printf 'Environment="CHAT_AGENT_TMUX_SESSION=%s"\n' "$escaped_session"
    printf '%s\n' 'Environment="CHAT_AGENT_AUTOSTART_BOOT=1"'
    printf 'ExecStart="%s" tmux start\n' "$escaped_controller"
    printf 'ExecStop="%s" tmux stop\n' "$escaped_controller"
    printf 'StandardOutput=append:%s\n' "$escaped_out"
    printf 'StandardError=append:%s\n' "$escaped_err"
    printf '\n%s\n' '[Install]'
    printf '%s\n' 'WantedBy=default.target'
  } > "$output"
}

render_autostart() {
  local platform="$1"
  local output_dir="$2"
  mkdir -p "$output_dir" "$STATE_DIR"

  local output
  if [[ "$platform" == "macos" ]]; then
    output="$output_dir/$MACOS_LABEL.plist"
    write_launchd_plist "$output"
  else
    output="$output_dir/$LINUX_UNIT"
    write_systemd_unit "$output"
  fi
  log "已生成：$output"
}

autostart_install_macos() {
  command -v launchctl >/dev/null 2>&1 || die "未找到 launchctl"
  local launch_agents="${CHAT_AGENT_AUTOSTART_DIR:-$HOME/Library/LaunchAgents}"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    local preview="$STATE_DIR/autostart-preview/macos"
    render_autostart macos "$preview"
    log "[dry-run] 将注册到：$launch_agents"
    return 0
  fi

  mkdir -p "$launch_agents"
  render_autostart macos "$launch_agents"

  local plist="$launch_agents/$MACOS_LABEL.plist"
  local domain="gui/$(id -u)"
  launchctl bootout "$domain" "$plist" >/dev/null 2>&1 || true
  launchctl bootstrap "$domain" "$plist"
  launchctl enable "$domain/$MACOS_LABEL"
  log "macOS LaunchAgent 已安装，并通过 tmux 启动两个服务。"
}

autostart_uninstall_macos() {
  local launch_agents="${CHAT_AGENT_AUTOSTART_DIR:-$HOME/Library/LaunchAgents}"
  local domain="gui/$(id -u)"
  local plist="$launch_agents/$MACOS_LABEL.plist"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    print_command launchctl bootout "$domain" "$plist"
    log "[dry-run] 删除 $plist"
    print_command tmux kill-session -t "=$TMUX_SESSION"
    return 0
  fi
  launchctl bootout "$domain" "$plist" >/dev/null 2>&1 || true
  rm -f "$plist"
  tmux_stop
  log "macOS LaunchAgent 已卸载。"
}

autostart_install_linux() {
  command -v systemctl >/dev/null 2>&1 || die "未找到 systemctl"
  local unit_dir="${CHAT_AGENT_AUTOSTART_DIR:-$HOME/.config/systemd/user}"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    local preview="$STATE_DIR/autostart-preview/linux"
    render_autostart linux "$preview"
    log "[dry-run] 将注册到：$unit_dir"
    return 0
  fi

  mkdir -p "$unit_dir"
  render_autostart linux "$unit_dir"
  systemctl --user daemon-reload
  systemctl --user enable --now "$LINUX_UNIT"
  log "Linux systemd user service 已安装，并通过 tmux 启动两个服务。"
  log "若需未登录也启动，请另行执行：loginctl enable-linger $USER"
}

autostart_uninstall_linux() {
  command -v systemctl >/dev/null 2>&1 || die "未找到 systemctl"
  local unit_dir="${CHAT_AGENT_AUTOSTART_DIR:-$HOME/.config/systemd/user}"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    print_command systemctl --user disable --now "$LINUX_UNIT"
    log "[dry-run] 删除 $unit_dir/$LINUX_UNIT"
    return 0
  fi

  systemctl --user disable --now "$LINUX_UNIT" >/dev/null 2>&1 || true
  rm -f "$unit_dir/$LINUX_UNIT"
  systemctl --user daemon-reload
  log "Linux systemd user service 已卸载。"
}

cleanup_legacy_autostart() {
  local platform
  platform="$(detect_platform)"
  local item
  if [[ "$platform" == "macos" ]]; then
    local launch_agents="${CHAT_AGENT_AUTOSTART_DIR:-$HOME/Library/LaunchAgents}"
    local domain="gui/$(id -u)"
    for item in "${LEGACY_MACOS_LABELS[@]}"; do
      [[ -n "$item" ]] || continue
      if [[ "$DRY_RUN" -eq 1 ]]; then
        log "[dry-run] 清理旧 LaunchAgent：$item"
      else
        launchctl bootout "$domain" "$launch_agents/$item.plist" >/dev/null 2>&1 || true
        rm -f "$launch_agents/$item.plist"
      fi
    done
  else
    local unit_dir="${CHAT_AGENT_AUTOSTART_DIR:-$HOME/.config/systemd/user}"
    if [[ "$DRY_RUN" -eq 1 ]]; then
      for item in "${LEGACY_LINUX_UNITS[@]}"; do
        [[ -n "$item" ]] || continue
        log "[dry-run] 清理旧 systemd unit：$item"
      done
      return 0
    fi
    for item in "${LEGACY_LINUX_UNITS[@]}"; do
      [[ -n "$item" ]] || continue
      systemctl --user disable --now "$item" >/dev/null 2>&1 || true
      rm -f "$unit_dir/$item"
    done
    systemctl --user daemon-reload
  fi
}

autostart_install() {
  ensure_runtime
  cleanup_legacy_autostart
  check_start_conflicts autostart
  local platform
  platform="$(detect_platform)"
  if [[ "$platform" == "macos" ]]; then
    autostart_install_macos
  else
    autostart_install_linux
  fi
}

autostart_uninstall() {
  local platform
  platform="$(detect_platform)"
  if [[ "$platform" == "macos" ]]; then
    autostart_uninstall_macos
  else
    autostart_uninstall_linux
  fi
  cleanup_legacy_autostart
}

autostart_restart() {
  local platform
  platform="$(detect_platform)"

  if [[ "$platform" == "macos" ]]; then
    local domain="gui/$(id -u)"
    tmux_stop
    run_command launchctl kickstart -k "$domain/$MACOS_LABEL"
  else
    run_command systemctl --user restart "$LINUX_UNIT"
  fi
  log "开机自启动的 tmux 会话已重启。"
}

autostart_status() {
  local platform
  platform="$(detect_platform)"
  if [[ "$platform" == "macos" ]]; then
    local domain="gui/$(id -u)"
    launchctl print "$domain/$MACOS_LABEL" >/dev/null 2>&1 \
      && log "LaunchAgent：已注册" \
      || { log "LaunchAgent：未注册"; return 1; }
  else
    systemctl --user is-enabled --quiet "$LINUX_UNIT" \
      && log "systemd user service：已启用" \
      || { log "systemd user service：未启用"; return 1; }
  fi
  tmux_status
}

autostart_logs() {
  if tmux_session_exists "$TMUX_SESSION"; then
    tmux_logs
    return
  fi
  local file
  for file in "$STATE_DIR/autostart.out.log" "$STATE_DIR/autostart.err.log"; do
    log "[$file]"
    [[ -f "$file" ]] && tail -n 80 "$file" || log "暂无日志"
  done
}

handle_tmux() {
  case "$1" in
    start) tmux_start ;;
    stop) tmux_stop ;;
    restart) tmux_stop; tmux_start ;;
    status) tmux_status ;;
    logs) tmux_logs ;;
    *) die "未知 tmux 操作：$1" ;;
  esac
}

handle_background() {
  case "$1" in
    start) background_start ;;
    stop) background_stop ;;
    restart) background_stop; background_start ;;
    status) background_status ;;
    logs) background_logs ;;
    *) die "未知 background 操作：$1" ;;
  esac
}

handle_autostart() {
  local action="$1"
  case "$action" in
    install|start) autostart_install ;;
    uninstall|stop) autostart_uninstall ;;
    restart) autostart_restart ;;
    status) autostart_status ;;
    logs) autostart_logs ;;
    render)
      local platform="${2:-$(detect_platform)}"
      [[ "$platform" == "macos" || "$platform" == "linux" ]] || die "render 平台只能是 macos 或 linux"
      local output_dir="${3:-$STATE_DIR/autostart-preview/$platform}"
      ensure_runtime
      render_autostart "$platform" "$output_dir"
      ;;
    *) die "未知 autostart 操作：$action" ;;
  esac
}

main() {
  if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
    shift
  fi

  case "${1:-}" in
    -h|--help|help)
      usage
      return 0
      ;;
  esac

  if [[ "${1:-}" == "doctor" ]]; then
    shift
    run_doctor "$@"
    return
  fi

  local mode="${1:-foreground}"
  local action="${2:-start}"
  if [[ "$#" -ge 2 ]]; then
    shift 2
  elif [[ "$#" -eq 1 ]]; then
    shift
  fi

  case "$mode" in
    start|foreground)
      [[ "$action" == "start" ]] || die "foreground 只支持 start；停止请在当前终端按 Ctrl-C"
      foreground_start
      ;;
    tmux) handle_tmux "$action" "$@" ;;
    nohup|background) handle_background "$action" "$@" ;;
    autostart) handle_autostart "$action" "$@" ;;
    *) die "未知启动类型：$mode" ;;
  esac
}

main "$@"

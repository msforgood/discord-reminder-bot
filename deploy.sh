#!/usr/bin/env bash
#
# deploy.sh — Discord 리마인더 봇 재배포 스크립트
#
# 하는 일:
#   1) 최신 코드 pull (--no-pull 로 건너뜀)
#   2) requirements.txt 변경 시 의존성 설치
#   3) systemd 서비스 파일이 바뀌었으면 /etc/systemd/system 에 반영 + daemon-reload
#   4) 봇 서비스 재시작 (reload)
#   5) 상태 확인 + 최근 로그 출력
#
# 사용법:
#   ./deploy.sh              # git pull 후 재배포
#   ./deploy.sh --no-pull    # 현재 코드 그대로 재시작만
#
set -euo pipefail

cd "$(dirname "$0")"

SERVICE="discord-reminder-bot.service"
SERVICE_SRC="discord-reminder-bot.service"
SERVICE_DST="/etc/systemd/system/discord-reminder-bot.service"
VENV_PY=".venv/bin/python"
VENV_PIP=".venv/bin/pip"

PULL=1
for arg in "$@"; do
  case "$arg" in
    --no-pull) PULL=0 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "알 수 없는 옵션: $arg" >&2; exit 1 ;;
  esac
done

log() { printf '\n\033[1;36m▶ %s\033[0m\n' "$1"; }

# 1) 최신 코드 pull
if [[ "$PULL" -eq 1 ]]; then
  log "git pull"
  git pull --ff-only
else
  log "git pull 건너뜀 (--no-pull)"
fi

# 2) requirements.txt 변경 시 의존성 설치
#    마지막 설치 시점을 .venv/.deploy-reqs-installed 마커로 기록해, 실제로 바뀐 경우에만 설치한다.
REQS_MARKER=".venv/.deploy-reqs-installed"
if [[ ! -f "$REQS_MARKER" || requirements.txt -nt "$REQS_MARKER" ]]; then
  log "requirements.txt 변경 감지 → 의존성 설치"
  "$VENV_PIP" install -r requirements.txt
  touch "$REQS_MARKER"
else
  log "의존성 변경 없음 — 설치 건너뜀"
fi

# 3) systemd 서비스 파일 동기화
if ! sudo cmp -s "$SERVICE_SRC" "$SERVICE_DST" 2>/dev/null; then
  log "서비스 파일 변경 감지 → $SERVICE_DST 반영"
  sudo cp "$SERVICE_SRC" "$SERVICE_DST"
  sudo systemctl daemon-reload
else
  log "서비스 파일 변경 없음"
fi

# 4) 재시작
log "$SERVICE 재시작"
sudo systemctl restart "$SERVICE"

# 5) 상태 확인
sleep 2
log "상태 확인"
if systemctl is-active --quiet "$SERVICE"; then
  printf '\033[1;32m✅ 봇이 정상적으로 실행 중입니다.\033[0m\n'
else
  printf '\033[1;31m❌ 봇 시작 실패 — 아래 로그를 확인하세요.\033[0m\n'
fi

sudo systemctl status "$SERVICE" --no-pager -l | head -n 12

log "최근 로그 (journalctl 최근 20줄)"
sudo journalctl -u "$SERVICE" -n 20 --no-pager

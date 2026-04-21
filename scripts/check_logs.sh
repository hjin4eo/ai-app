#!/bin/bash
# 텔레그램 봇 로그 확인 스크립트
# 로그는 journald에 저장됨 (systemd 서비스)
# journalctl -u telegram-bot.service 로 조회

LOG_FILE="/home/home/ai-worker/bot.log"
JOURNAL_UNIT="telegram-bot.service"

# 색상 설정
RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
RESET='\033[0m'

usage() {
    echo "Usage: $0 [옵션]"
    echo ""
    echo "옵션:"
    echo "  (없음)       최근 50줄 출력"
    echo "  -n <N>       최근 N줄 출력"
    echo "  -f           실시간 tail -f"
    echo "  -e           에러/예외만 필터"
    echo "  -w           경고(WARNING)만 필터"
    echo "  -i           INFO만 필터"
    echo "  -s           로그 통계 (ERROR/WARN/INFO 카운트)"
    echo "  -d <날짜>    특정 날짜 필터 (예: 2026-04-16)"
    echo "  -g <패턴>    grep 패턴 검색"
    echo "  -c           로그 파일 초기화 (clear)"
    echo "  -h           도움말"
}

if [ ! -f "$LOG_FILE" ]; then
    echo -e "${RED}로그 파일 없음: $LOG_FILE${RESET}"
    exit 1
fi

# 기본값
LINES=50
ACTION="tail"

case "$1" in
    -f)
        echo -e "${CYAN}[실시간 로그 감시 중... Ctrl+C로 종료]${RESET}"
        tail -f "$LOG_FILE" | awk '
            /ERROR|CRITICAL/ { print "\033[0;31m" $0 "\033[0m"; next }
            /WARNING/         { print "\033[1;33m" $0 "\033[0m"; next }
            /INFO/            { print "\033[0;32m" $0 "\033[0m"; next }
                              { print $0 }
        '
        ;;
    -e)
        echo -e "${RED}[ERROR / CRITICAL / Traceback]${RESET}"
        grep -E "ERROR|CRITICAL|Traceback|Exception|raise " "$LOG_FILE" | tail -100
        ;;
    -w)
        echo -e "${YELLOW}[WARNING]${RESET}"
        grep "WARNING" "$LOG_FILE" | tail -100
        ;;
    -i)
        echo -e "${GREEN}[INFO]${RESET}"
        grep "INFO" "$LOG_FILE" | tail -100
        ;;
    -s)
        echo -e "${CYAN}=== 로그 통계 ===${RESET}"
        echo -e "${RED}ERROR  : $(grep -c 'ERROR\|CRITICAL' "$LOG_FILE")${RESET}"
        echo -e "${YELLOW}WARNING: $(grep -c 'WARNING' "$LOG_FILE")${RESET}"
        echo -e "${GREEN}INFO   : $(grep -c ' INFO ' "$LOG_FILE")${RESET}"
        echo ""
        echo "전체 줄 수  : $(wc -l < "$LOG_FILE")"
        echo "파일 크기   : $(du -h "$LOG_FILE" | cut -f1)"
        echo "첫 번째 기록: $(head -1 "$LOG_FILE" | cut -d' ' -f1,2)"
        echo "마지막 기록 : $(tail -1 "$LOG_FILE" | cut -d' ' -f1,2)"
        ;;
    -d)
        if [ -z "$2" ]; then
            echo "날짜를 입력하세요. 예: $0 -d 2026-04-16"
            exit 1
        fi
        echo -e "${CYAN}[날짜: $2]${RESET}"
        grep "^$2" "$LOG_FILE" | awk '
            /ERROR|CRITICAL/ { print "\033[0;31m" $0 "\033[0m"; next }
            /WARNING/         { print "\033[1;33m" $0 "\033[0m"; next }
            /INFO/            { print "\033[0;32m" $0 "\033[0m"; next }
                              { print $0 }
        '
        ;;
    -g)
        if [ -z "$2" ]; then
            echo "검색 패턴을 입력하세요. 예: $0 -g '409 Conflict'"
            exit 1
        fi
        echo -e "${CYAN}[검색: $2]${RESET}"
        grep -n "$2" "$LOG_FILE"
        ;;
    -n)
        if [ -z "$2" ]; then
            echo "줄 수를 입력하세요. 예: $0 -n 100"
            exit 1
        fi
        echo -e "${CYAN}[최근 $2줄]${RESET}"
        tail -"$2" "$LOG_FILE" | awk '
            /ERROR|CRITICAL/ { print "\033[0;31m" $0 "\033[0m"; next }
            /WARNING/         { print "\033[1;33m" $0 "\033[0m"; next }
            /INFO/            { print "\033[0;32m" $0 "\033[0m"; next }
                              { print $0 }
        '
        ;;
    -c)
        read -p "로그 파일을 초기화하시겠습니까? (y/N): " confirm
        if [[ "$confirm" =~ ^[Yy]$ ]]; then
            > "$LOG_FILE"
            echo -e "${GREEN}로그 파일 초기화 완료${RESET}"
        else
            echo "취소됨"
        fi
        ;;
    -h)
        usage
        ;;
    "")
        echo -e "${CYAN}[최근 ${LINES}줄]${RESET}"
        tail -"$LINES" "$LOG_FILE" | awk '
            /ERROR|CRITICAL/ { print "\033[0;31m" $0 "\033[0m"; next }
            /WARNING/         { print "\033[1;33m" $0 "\033[0m"; next }
            /INFO/            { print "\033[0;32m" $0 "\033[0m"; next }
                              { print $0 }
        '
        ;;
    *)
        echo "알 수 없는 옵션: $1"
        usage
        exit 1
        ;;
esac

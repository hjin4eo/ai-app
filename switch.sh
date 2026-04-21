#!/bin/bash
# 모델 백엔드 전환 스크립트
# 사용법: ./switch.sh lm | ./switch.sh ollama

ENV_FILE="$(dirname "$0")/.env"

set_env() {
    local key=$1
    local val=$2
    if grep -q "^${key}=" "$ENV_FILE"; then
        sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
    else
        echo "${key}=${val}" >> "$ENV_FILE"
    fi
}

case "$1" in
    lm|lm-studio)
        set_env "MODEL_BACKEND" "llama-cpp"
        set_env "EMBEDDING_BACKEND" "lm-studio"
        echo "✅ LM Studio 모드로 전환됨 (1234)"
        ;;
    ollama)
        set_env "MODEL_BACKEND" "ollama"
        set_env "EMBEDDING_BACKEND" "ollama"
        echo "✅ Ollama 모드로 전환됨 (11434)"
        ;;
    *)
        echo "사용법: $0 [lm|ollama]"
        echo ""
        current_model=$(grep "^MODEL_BACKEND=" "$ENV_FILE" 2>/dev/null | cut -d= -f2)
        current_embed=$(grep "^EMBEDDING_BACKEND=" "$ENV_FILE" 2>/dev/null | cut -d= -f2)
        echo "현재: MODEL_BACKEND=${current_model:-config.yaml 기본값}"
        echo "      EMBEDDING_BACKEND=${current_embed:-config.yaml 기본값}"
        exit 1
        ;;
esac

echo "봇을 재시작해야 적용됩니다."

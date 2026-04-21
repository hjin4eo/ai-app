import sys
import asyncio
import json
import re
from pathlib import Path

# 에이전트 경로 추가
_agent_dir = Path(__file__).parent
sys.path.append(str(_agent_dir))

from weather_service import get_cached_weather
from shared import ask_ollama, CHAT_SYSTEM_PROMPT

async def simulate_telegram_weather_request():
    text = "안성 날씨 알려줘"
    print(f"--- [시뮬레이션 시작] 입력: '{text}' ---")
    
    # 텔레그램 봇 로직 복제
    weather_context = ""
    skip_planner = False
    _weather_keywords = ["날씨", "기온", "기상", "weather", "비 오", "눈 오", "안성날씨", "지금날씨", "미세먼지", "초미세먼지", "공기", "먼지"]
    
    if any(kw in text.lower().replace(" ", "") for kw in _weather_keywords):
        cached = get_cached_weather("안성 날씨")
        if cached:
            weather_context = (
                f"\n\n[실시간 안성 기상 관측 정보]\n{cached}\n"
                "위 데이터는 시스템이 기상청 API에서 직접 가져온 현재 시황입니다. "
                "너의 내부 지식(훈련 데이터)보다 이 정보를 최우선으로 신뢰하여 답변하세요. "
                "데이터가 이미 존재하므로 별도의 웹 검색 없이 이 정보로 즉시 답변하십시오."
            )
            skip_planner = True
            print("✅ skip_planner 활성화 (웹 검색 우회)")

    if skip_planner:
        action = "direct"
        curr_system_prompt = CHAT_SYSTEM_PROMPT + weather_context
        
        print("💡 AI 답변 생성 중 (Agent 3)...")
        # 실제 AI 호출 (메시지 히스토리는 비움)
        response = await asyncio.get_event_loop().run_in_executor(
            None, lambda: ask_ollama(text, system_prompt=curr_system_prompt, messages=[])
        )
        
        print("\n--- [AI 최종 답변] ---")
        print(response)
        print("----------------------")
    else:
        print("❌ 플래너 호출이 필요합니다. (테스트 실패)")

if __name__ == "__main__":
    asyncio.run(simulate_telegram_weather_request())

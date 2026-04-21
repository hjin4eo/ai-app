import sys
from pathlib import Path

# 에이전트 경로 추가
_agent_dir = Path(__file__).parent
sys.path.append(str(_agent_dir))

from weather_service import fetch_weather, fetch_air_quality, parse_weather, parse_air_quality

def verify_integration():
    print("=== 날씨 + 미세먼지 통합 연동 테스트 ===")
    
    # 1. 안성(보개면) 지점 테스트
    print("\n[지점: 안성시 보개면]")
    weather_items = fetch_weather(69, 107)
    if weather_items:
        weather_text = parse_weather(weather_items)
        air_data = fetch_air_quality("공도읍")
        air_text = parse_air_quality(air_data)
        
        # 통합 텍스트 시뮬레이션
        final_text = weather_text.replace("📍 안성", "📍 안성시 보개면")
        final_text = final_text.replace("- 환경:", f"- 대기: {air_text}\n- 환경:")
        
        print(final_text)
    else:
        print("❌ 날씨 데이터를 가져오는데 실패했습니다.")

    # 2. 죽산면 지점 테스트
    print("\n[지점: 안성시 죽산면]")
    weather_items_js = fetch_weather(67, 112)
    if weather_items_js:
        weather_text_js = parse_weather(weather_items_js)
        air_data_js = fetch_air_quality("죽산면")
        air_text_js = parse_air_quality(air_data_js)
        
        final_text_js = weather_text_js.replace("📍 안성", "📍 안성시 죽산면")
        final_text_js = final_text_js.replace("- 환경:", f"- 대기: {air_text_js}\n- 환경:")
        
        print(final_text_js)
    else:
        print("❌ 죽산면 날씨 데이터를 가져오는데 실패했습니다.")

if __name__ == "__main__":
    verify_integration()

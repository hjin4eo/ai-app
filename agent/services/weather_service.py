#!/usr/bin/env python3
"""
기상청 API 연동 날씨 서비스
- 안성시(보개면, 죽산면) 날씨 정보 동기화 및 캐싱
"""
import hashlib
import json
import logging
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
import sys
_agent_dir = Path(__file__).parent.parent.resolve()
if str(_agent_dir) not in sys.path:
    sys.path.append(str(_agent_dir))

from core.bot_config import CFG, DATA_DIR

log = logging.getLogger(__name__)

KMA_SERVICE_KEY = os.environ.get("KMA_SERVICE_KEY", "")
AIRKOREA_SERVICE_KEY = os.environ.get(
    "AIRKOREA_SERVICE_KEY",
    "68722a7c49e20231cec32814234047ad82f782045575f3466faa450fdba7b191",
)
DB_PATH = DATA_DIR / "search_cache.db"

# ── DB 초기화 ─────────────────────────────────────────────────────────────────
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
_conn = sqlite3.connect(str(DB_PATH))
_conn.execute("""
    CREATE TABLE IF NOT EXISTS cache (
        key   TEXT PRIMARY KEY,
        query TEXT,
        result TEXT,
        ts    REAL,
        hit_count INTEGER DEFAULT 0
    )
""")
_conn.commit()
_conn.close()

# 안성 지점 정보 (보개면 근처, 죽산면)
LOCATIONS = [
    {"name": "보개면", "nx": 69, "ny": 107, "query": "안성시 보개면 날씨"},
    {"name": "죽산면", "nx": 67, "ny": 112, "query": "안성시 죽산면 날씨"},
]

# ── 전국 85개 도시 격자 좌표 ──────────────────────────────────────────────────
CITY_GRID = {
    # 안성 세부
    "안성": (69, 107), "보개면": (69, 107), "죽산면": (67, 112),
    # 주요 도시
    "서울": (60, 127), "부산": (98, 76), "대구": (89, 90), "인천": (55, 124),
    "광주": (58, 74), "대전": (67, 100), "울산": (102, 84), "세종": (66, 103),
    # 경기
    "수원": (60, 121), "성남": (63, 124), "의정부": (61, 130), "안양": (59, 123),
    "부천": (56, 125), "광명": (58, 125), "평택": (62, 114), "동두천": (61, 134),
    "안산": (57, 121), "고양": (57, 128), "과천": (60, 124), "구리": (64, 128),
    "남양주": (64, 131), "오산": (62, 118), "시흥": (57, 123), "군포": (59, 122),
    "의왕": (60, 122), "하남": (64, 126), "용인": (64, 119), "파주": (56, 131),
    "이천": (68, 118), "김포": (55, 128), "화성": (57, 119), "양주": (61, 131),
    "포천": (64, 134), "여주": (71, 116),
    # 강원
    "춘천": (73, 134), "원주": (76, 122), "강릉": (92, 131), "동해": (97, 127),
    "태백": (95, 119), "속초": (87, 141), "삼척": (98, 125),
    # 충북
    "청주": (69, 106), "충주": (76, 114), "제천": (81, 118),
    # 충남
    "천안": (63, 110), "공주": (63, 102), "보령": (54, 100), "아산": (60, 110),
    "서산": (51, 110), "논산": (62, 97), "계룡": (65, 99), "당진": (54, 112),
    # 전북
    "전주": (63, 89), "군산": (56, 92), "익산": (60, 91), "정읍": (58, 83),
    "남원": (68, 80), "김제": (59, 88),
    # 전남
    "목포": (50, 67), "여수": (73, 66), "순천": (70, 70), "나주": (56, 71),
    "광양": (73, 68),
    # 경북
    "포항": (102, 94), "경주": (100, 91), "김천": (80, 96), "안동": (91, 106),
    "구미": (84, 96), "영주": (89, 114), "영천": (95, 93), "상주": (81, 102),
    "문경": (81, 106), "경산": (91, 90),
    # 경남
    "창원": (89, 77), "진주": (81, 75), "통영": (87, 68), "사천": (80, 71),
    "김해": (95, 77), "밀양": (92, 83), "거제": (90, 69), "양산": (97, 79),
    # 제주
    "제주": (52, 38), "서귀포": (52, 33),
}

# ── 도시→에어코리아 측정소 매핑 ───────────────────────────────────────────────
CITY_STATION = {
    "안성": "공도읍", "보개면": "공도읍", "죽산면": "공도읍",
    "서울": "중구", "부산": "연산동", "대구": "상인동", "인천": "부평구",
    "광주": "농성동", "대전": "구성동", "울산": "신정동", "세종": "세종",
    "수원": "장안구", "성남": "수정구", "의정부": "의정부",
    "안양": "만안구", "부천": "원미구", "평택": "평택",
    "고양": "덕양구", "용인": "수지구", "파주": "파주",
    "전주": "덕진구", "군산": "비응도", "익산": "남중동",
    "춘천": "석사동", "원주": "명륜동", "강릉": "옥천동",
    "제주": "이도이동", "서귀포": "강정동",
    "포항": "장량동", "경주": "성건동", "안동": "명륜동", "구미": "공단동",
    "창원": "성산동", "김해": "삼방동", "진주": "상대동", "거제": "아주동", "양산": "북부동",
    "천안": "성황동", "공주": "교동", "아산": "배방읍",
    "수원": "광교동", "성남": "상대원동", "고양": "식사동", "용인": "수지",
    "파주": "금촌동", "의정부": "의정부동", "평택": "비전동",
}

def _get_base_time():
    """기상청 단기예보 업데이트 주기에 따른 최신 base_time 반환 (02, 05, 08, 11, 14, 17, 20, 23)"""
    now = datetime.now()
    hour = now.hour
    
    # 예보 발표 시점 (10분 여유)
    if hour < 2:
        base_date = (now - timedelta(days=1)).strftime("%Y%m%d")
        base_time = "2300"
    elif hour < 5:
        base_time = "0200"
    elif hour < 8:
        base_time = "0500"
    elif hour < 11:
        base_time = "0800"
    elif hour < 14:
        base_time = "1100"
    elif hour < 17:
        base_time = "1400"
    elif hour < 20:
        base_time = "1700"
    elif hour < 23:
        base_time = "2000"
    else:
        base_time = "2300"
        
    return now.strftime("%Y%m%d"), base_time

def fetch_weather(nx: int, ny: int):
    """기상청 API로부터 단기예보 데이터 수집"""
    if not KMA_SERVICE_KEY:
        log.error("KMA_SERVICE_KEY가 설정되지 않았습니다.")
        return None

    # 기상청 API 키는 인코딩/디코딩 이슈가 잦으므로 원본 그대로 사용 시도
    # (data.go.kr에서 발급받은 키가 이미 인코딩된 상태일 수 있음)
    service_key = KMA_SERVICE_KEY # urllib.parse.unquote(KMA_SERVICE_KEY)
    
    base_date, base_time = _get_base_time()
    url = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
    params = {
        "serviceKey": service_key,
        "pageNo": "1",
        "numOfRows": "1000",
        "dataType": "JSON",
        "base_date": base_date,
        "base_time": base_time,
        "nx": nx,
        "ny": ny,
    }
    
    query_str = urllib.parse.urlencode(params, safe="%")
    full_url = f"{url}?{query_str}"
    
    try:
        req = urllib.request.Request(full_url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw_res = resp.read()
            # 401 오류가 아니더라도 결과 코드가 오류일 수 있음
            try:
                data = json.loads(raw_res.decode("utf-8"))
            except:
                log.error("API 응답이 JSON 형식이 아닙니다: %s", raw_res[:200])
                return None
                
            if "response" not in data:
                log.error("잘못된 API 응답 구조: %s", data)
                return None
            if data["response"]["header"]["resultCode"] != "00":
                log.error("KMA API 오류: %s (%s)", data["response"]["header"]["resultMsg"], data["response"]["header"]["resultCode"])
                return None
            return data["response"]["body"]["items"]["item"]
    except urllib.error.HTTPError as e:
        log.error("KMA API HTTP 오류: %s (Key 확인 필요)", e)
        return None
    except Exception as e:
        log.error("KMA API 호출 중 예외 발생: %s", e)
        return None

def parse_weather(items, air_quality: str = ""):
    """API 응답 파싱하여 보기 좋은 텍스트로 변환"""
    if not items: return "날씨 정보를 가져올 수 없습니다."

    now_dt = datetime.now()
    today = now_dt.strftime("%Y%m%d")
    current_hour = now_dt.strftime("%H00")

    # 주요 코드 매핑
    sky_map = {"1": "맑음 ☀️", "3": "구름많음 ⛅", "4": "흐림 ☁️"}
    pty_map = {"0": "없음", "1": "비 ☔", "2": "비/눈 🌨️", "3": "눈 ❄️", "4": "소나기 🌦️"}

    tmp_list = []
    tmx, tmn, cur_tmp = None, None, None
    sky, pty = "알 수 없음", "0"

    for item in items:
        if item["fcstDate"] == today:
            cat = item["category"]
            val = item["fcstValue"]

            if cat == "TMP":
                tmp_list.append(float(val))
                if item["fcstTime"] == current_hour:
                    cur_tmp = val
            if cat == "TMX": tmx = val
            if cat == "TMN": tmn = val
            if cat == "SKY" and item["fcstTime"] == current_hour:
                sky = sky_map.get(val, "알 수 없음")
            if cat == "PTY" and item["fcstTime"] == current_hour:
                pty = val

    # 현재 기온이 없을 경우 최근 TMP 사용
    if not cur_tmp and tmp_list: cur_tmp = tmp_list[-1]
    if not tmx and tmp_list: tmx = max(tmp_list)
    if not tmn and tmp_list: tmn = min(tmp_list)

    pty_str = pty_map.get(pty, "없음")
    weather_desc = sky
    if pty != "0": weather_desc += f" · {pty_str}"

    lines = [
        f"🌤️ 날씨  ·  {now_dt.strftime('%m/%d %H:%M')} (기상청)",
        f"{'─' * 26}",
        f"🌡️ 현재 기온    {cur_tmp}°C",
        f"🌥️ 하늘 상태    {weather_desc}",
        f"📊 최고 / 최저  {tmx}°C / {tmn}°C",
    ]
    if air_quality:
        lines += ["", air_quality]
    return "\n".join(lines)

def save_to_cache(query: str, result_text: str):
    """SQLite 캐시 DB에 저장"""
    key = hashlib.sha256(query.strip().lower().encode()).hexdigest()[:16]
    
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            "INSERT OR REPLACE INTO cache (key, query, result, ts, hit_count) VALUES (?, ?, ?, ?, ?)",
            (key, query, result_text, time.time(), 100), # 100으로 설정하여 장기 기억 유도
        )
        conn.commit()
        conn.close()
        log.info("캐시 저장 완료: %s", query)
    except Exception as e:
        log.error("캐시 저장 중 오류: %s", e)

def fetch_air_quality(station_name: str):
    """에어코리아 API로 실시간 대기오염 정보 조회"""
    if not AIRKOREA_SERVICE_KEY:
        log.warning("AIRKOREA_SERVICE_KEY 미설정")
        return None

    url = "https://apis.data.go.kr/B552584/ArpltnInforInqireSvc/getMsrstnAcctoRltmMesureDnsty"
    params = {
        "serviceKey": AIRKOREA_SERVICE_KEY,
        "returnType": "json",
        "numOfRows": "1",
        "pageNo": "1",
        "stationName": station_name,
        "dataTerm": "DAILY",
        "ver": "1.0",
    }
    query_str = urllib.parse.urlencode(params, safe="%")
    full_url = f"{url}?{query_str}"

    try:
        req = urllib.request.Request(full_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        items = data.get("response", {}).get("body", {}).get("items", [])
        if not items:
            return None
        item = items[0]

        grade_map = {"1": "좋음 😊", "2": "보통 😐", "3": "나쁨 😷", "4": "매우나쁨 🚨"}
        pm10 = item.get("pm10Value", "-")
        pm25 = item.get("pm25Value", "-")
        pm10_grade = grade_map.get(item.get("pm10Grade"), "알 수 없음")
        pm25_grade = grade_map.get(item.get("pm25Grade"), "알 수 없음")
        o3 = item.get("o3Value", "-")
        data_time = item.get("dataTime", "")

        return (
            f"🌫️ 대기질  ·  {station_name} · {data_time[11:16] if len(data_time) > 10 else data_time}\n"
            f"  PM10   {str(pm10):>4}㎍/㎥  {pm10_grade}\n"
            f"  PM2.5  {str(pm25):>4}㎍/㎥  {pm25_grade}\n"
            f"  O₃     {o3} ppm"
        )
    except Exception as e:
        log.error("에어코리아 API 오류: %s", e)
        return None


def get_cached_weather(query: str) -> str | None:
    """캐시에서 날씨 정보 조회 (30분 이내 데이터만 유효)"""
    key = hashlib.sha256(query.strip().lower().encode()).hexdigest()[:16]
    try:
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute(
            "SELECT result, ts FROM cache WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        if row and (time.time() - row[1]) < 1800:  # 30분
            return row[0]
    except Exception as e:
        log.error("캐시 조회 오류: %s", e)
    return None


def _extract_cached_air(cache_key: str) -> str:
    """기존 캐시에서 대기질 블록만 추출. 없으면 빈 문자열."""
    cached = get_cached_weather(cache_key)
    if not cached:
        # 만료된 캐시도 시도 (시간 제한 없이)
        key = __import__('hashlib').sha256(cache_key.strip().lower().encode()).hexdigest()[:16]
        try:
            conn = sqlite3.connect(str(DB_PATH))
            row = conn.execute("SELECT result FROM cache WHERE key = ?", (key,)).fetchone()
            conn.close()
            cached = row[0] if row else ""
        except Exception:
            return ""
    idx = cached.find("🌫️")
    return cached[idx:] if idx != -1 else ""


def sync_city(city: str) -> bool:
    """특정 도시의 날씨 + 미세먼지를 수집하여 캐시에 저장"""
    grid = CITY_GRID.get(city)
    if not grid:
        log.warning("미등록 도시: %s", city)
        return False

    nx, ny = grid
    items = fetch_weather(nx, ny)
    if not items:
        return False

    station = CITY_STATION.get(city)
    air = fetch_air_quality(station) if station else None
    if air is None and station:
        log.warning("에어코리아 실패 (%s) — 기존 캐시 대기질 재사용", station)
        air = _extract_cached_air(f"{city} 날씨") or None
    text = parse_weather(items, air_quality=air or "")
    text = text.replace("📍 안성", f"📍 {city}")

    save_to_cache(f"{city} 날씨", text)
    log.info("sync_city 완료: %s", city)
    return True


def sync_all():
    """모든 지점 날씨 동기화 (안성 세부 + 주요 도시)"""
    log.info("기상청 날씨 동기화 시작...")

    # 안성 세부 지점
    for loc in LOCATIONS:
        items = fetch_weather(loc["nx"], loc["ny"])
        if items:
            station = CITY_STATION.get(loc["name"])
            air = fetch_air_quality(station) if station else None
            if air is None and station:
                log.warning("에어코리아 실패 (%s) — 기존 캐시 대기질 재사용", station)
                air = _extract_cached_air(loc["query"]) or None
            text = parse_weather(items, air_quality=air or "")
            text = text.replace("📍 안성", f"📍 안성시 {loc['name']}")

            save_to_cache(loc["query"], text)
            if loc["name"] == "보개면":
                save_to_cache("안성 날씨", text.replace(f"안성시 {loc['name']}", "안성시"))

    # 주요 도시 (3시간 갱신용)
    major_cities = [
        "서울", "부산", "대구", "인천", "광주", "대전", "울산",
        "수원", "청주", "파주", "안동", "강릉", "제주",
    ]
    for city in major_cities:
        sync_city(city)

    log.info("기상청 날씨 동기화 완료.")


if __name__ == "__main__":
    sync_all()

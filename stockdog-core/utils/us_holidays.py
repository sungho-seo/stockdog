"""
NYSE 2026년 휴장일 set.

출처: NYSE 공식 holiday calendar
(https://www.nyse.com/markets/hours-calendars).
형식: "YYYY-MM-DD" ISO date 문자열 (KR pattern은 "YYYYMMDD" 사용하지만
M7 트래커는 SEC/FINRA URL이 ISO 형식 또는 "YYYYMMDD" 둘 다 쓰므로
호출부에서 strftime로 변환). 휴장일 비교는 date.isoformat()으로.

2026년 NYSE 공식 풀휴장일:
  - 1/1 New Year's Day (목)
  - 1/19 MLK Day (월)
  - 2/16 Presidents' Day (월)
  - 4/3 Good Friday (금)
  - 5/25 Memorial Day (월)
  - 6/19 Juneteenth (금)
  - 7/3 Independence Day observed (금, 7/4가 토요일이라 이전 영업일)
  - 9/7 Labor Day (월)
  - 11/26 Thanksgiving Day (목)
  - 12/25 Christmas Day (금)

조기 폐장(early close 1pm ET)은 풀휴장 아니므로 제외. T-1 데이터 일반적으로 정상 산출됨.
2027 이후는 매년 갱신 필요.
"""

NYSE_HOLIDAYS_2026: set = {
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # MLK Day
    "2026-02-16",  # Presidents' Day
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day observed (7/4 Sat)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving Day
    "2026-12-25",  # Christmas Day
}

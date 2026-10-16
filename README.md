# daily-ai-news
학생들에게 도움이 되는 ai 뉴스에 대해서 업로드합니다.

## 구조

```
index.html            디자인(틀)만 담당 - 매일 바뀌지 않음
data/issues.json      발행 목록 (드롭다운, 최신순)
data/YYYY-MM-DD.json  하루치 신문 한 호
generate_news.py      오늘 호 JSON을 만들어 data/ 에 "추가"
```

- 평일(월~금) 새벽 5시 13분(한국 시간)에 GitHub Actions가 `generate_news.py`를 실행해 새 호를 추가합니다. 6시 43분에 한 번 더 실행되며, 이미 발행됐으면 아무것도 하지 않습니다. 지난 호는 지워지지 않습니다.
- 한국 공휴일(대체공휴일·선거일 포함)에는 발행하지 않습니다. `korean_holidays.json` 목록(2026~2027년)과 `holidays` 라이브러리를 함께 확인합니다. 임시공휴일·재량휴업일·방학 등 쉬고 싶은 날은 이 파일에 `"YYYY-MM-DD": "이유"`로 추가하세요. 매년 6월쯤 다음 해 공휴일(우주항공청 월력요항)이 발표되면 목록을 추가해 주세요.
- GPT 자동 발행은 2026-10-20부터 시작합니다. 시작일은 저장소 변수 `GPT_START_DATE`로 바꿀 수 있습니다.
- 필요한 설정: Settings > Secrets and variables > Actions 에 `OPENAI_API_KEY` 시크릿 등록.
- 저장 전 자동 검사: 웹 검색 여부, 출처가 검색 결과에 있는지, 링크 접속(404), 유튜브 영상 존재, 지난 호와 중복, 글자 수. 실패하면 한 번 다시 생성하고, 그래도 실패하면 그날은 발행하지 않습니다.
- 퀴즈 정답 위치는 매일 무작위로 정해집니다.
- 지난 호 바로가기: `https://yuumyuum.github.io/daily-ai-news/?date=2026-10-15`
- 디자인을 고치면 지난 호 전체에 한꺼번에 반영됩니다.

## 손으로 고치거나 추가하기

- 기사 내용 수정: `data/해당날짜.json`만 고치면 됩니다.
- 직접 만든 JSON을 오늘 호로 등록: `python generate_news.py --from-file my.json`
- 오늘 호 다시 생성: Actions 탭 > Daily AI News Generator > Run workflow > `force` 체크

## 로컬에서 보기

JSON을 불러오는 방식이라 `index.html`을 더블클릭하면 내용이 안 보입니다. 폴더에서 아래처럼 실행한 뒤 http://localhost:8000 으로 여세요.

```
python -m http.server 8000
```

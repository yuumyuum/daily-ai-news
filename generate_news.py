"""
1분 AI 모닝 리포트 - 하루치 신문(JSON) 생성 스크립트

동작 방식
  1. 한국 시간 기준 오늘 날짜의 호가 이미 있으면 아무것도 하지 않음
  2. OpenAI(웹 검색 사용)로 오늘의 기사 데이터를 JSON 형식으로 받음
  3. 형식·링크를 검사한 뒤 data/YYYY-MM-DD.json 으로 저장
  4. data/issues.json 발행 목록 맨 앞에 추가 (호수는 자동으로 +1)

index.html(디자인)은 절대 수정하지 않습니다. 그래서 지난 호가 모두 남습니다.

사용법
  python generate_news.py                     # 오늘 호 생성 (OPENAI_API_KEY 필요)
  python generate_news.py --force             # 오늘 호가 있어도 다시 생성
  python generate_news.py --from-file my.json # 직접 쓴/다른 도구로 만든 JSON을 오늘 호로 등록
  python generate_news.py --date 2026-10-17   # 날짜 지정
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
ISSUES_FILE = DATA_DIR / "issues.json"
KST = ZoneInfo("Asia/Seoul")
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]

DEFAULT_MODEL = "gpt-4o"
DEFAULT_SEARCH_TOOL = "web_search"
DEFAULT_START_DATE = "2026-10-20"  # GPT 자동 발행 시작일 (저장소 변수 GPT_START_DATE 로 변경 가능)
PUBLISH_WEEKDAYS = {0, 1, 2, 3, 4}  # 월~금 (한국 시간 기준)
MAX_ATTEMPTS = 2  # 검사에 실패하면 오류 내용을 알려주고 한 번 더 생성

# ─────────────────────────────────────────────
# JSON 스키마 (OpenAI Structured Outputs, strict 모드)
# ─────────────────────────────────────────────
STR = {"type": "string"}
NULLABLE_STR = {"type": ["string", "null"]}
STR_LIST = {"type": "array", "items": STR}


def obj(props):
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


NEWS_BASE = {
    "badge": STR,
    "cardTitle": STR,
    "cardSummary": STR,
    "title": STR,
    "date": STR,
    "sourceName": STR,
    "sourceUrl": STR,
    "deepDive": STR,
}

BIGTECH_ITEM = obj({**NEWS_BASE, "tag": STR, "summary": STR_LIST})
TECH_ITEM = obj({**NEWS_BASE, "category": STR, "embedYoutubeId": NULLABLE_STR, "summary": STR_LIST})
SOCIAL_ITEM = obj({
    **NEWS_BASE,
    "cardBadge": STR,
    "question": STR,
    "sections": {"type": "array", "items": obj({"title": STR, "items": STR_LIST})},
})

ISSUE_SCHEMA = obj({
    "news": obj({
        "openai": BIGTECH_ITEM,
        "gemini": BIGTECH_ITEM,
        "claude": BIGTECH_ITEM,
        "tech": TECH_ITEM,
        "social": SOCIAL_ITEM,
    }),
    "dictionary": obj({"term": STR, "body": STR}),
    "tip": obj({"title": STR, "prompt": STR, "body": STR}),
    "quiz": obj({
        "question": STR,
        "options": STR_LIST,
        "correct": {"type": "integer", "enum": [1, 2, 3]},
        "explanation": STR,
    }),
    "sources": STR,
})


# ─────────────────────────────────────────────
# 발행 목록
# ─────────────────────────────────────────────
def load_issues():
    if ISSUES_FILE.exists():
        return json.loads(ISSUES_FILE.read_text(encoding="utf-8"))
    return []


def load_issue(date_key):
    path = DATA_DIR / f"{date_key}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def next_issue_no(issues, date_key):
    for entry in issues:
        if entry["date"] == date_key:  # 같은 날 재생성이면 호수 유지
            return entry["issueNo"]
    return max((e["issueNo"] for e in issues), default=0) + 1


# ─────────────────────────────────────────────
# 프롬프트
# ─────────────────────────────────────────────
SYSTEM_PROMPT = """너는 초·중·고등학생들을 위한 '1분 AI 아침 신문 리포터'야.
웹 검색으로 실제 기사와 공식 발표를 직접 확인한 뒤, 주어진 JSON 형식으로만 답해.
학생에게 말하듯 친근한 반말 요약(cardSummary)과, 정확한 존댓말 상세 설명(summary, sections, deepDive)을 섞어 써."""


def build_user_prompt(date_key, weekday, example, recent_titles):
    example_json = json.dumps(example, ensure_ascii=False, indent=2) if example else "(없음)"
    recent = "\n".join(f"- {t}" for t in recent_titles) or "(없음)"
    return f"""[오늘 날짜] {date_key} ({weekday}) / 한국 시간 기준

[목표]
오늘 기준 24~48시간 이내의 최신 AI 소식을 웹 검색으로 수집·검증해서 오늘 호 데이터를 만들어 줘.

[섹션별 요구사항]
1. news.openai / news.gemini / news.claude
   - 각각 OpenAI, Google(Gemini·DeepMind), Anthropic(Claude)의 가장 최근 공식 발표 1건.
   - 학생·교육과 관련 있으면 우선 선택. 기업 실적·주가·소송만 다루는 기사는 피할 것.
   - tag: 카드 오른쪽 작은 꼬리표(예: '학습 기능', '안전성 검증', '신규 모델'), 6자 안팎.
   - badge는 각각 정확히 'OpenAI (오픈AI)', 'Google Gemini (제미나이)', 'Anthropic Claude (클로드)'.
2. news.tech (오늘의 AI 신기술)
   - 피지컬 AI, 온디바이스 AI, 휴머노이드, 로보틱스 중 눈으로 봐야 이해되는 흥미로운 주제 1건.
   - embedYoutubeId: 최근 1~2주 안에 공식 채널이나 언론사가 올린 실제 YouTube 영상 ID(11자). 확실하지 않으면 null.
   - sourceUrl은 그 영상 주소(https://www.youtube.com/watch?v=ID) 또는 원문 기사.
   - category 예: '🤖 휴머노이드 & 로보틱스 (최신 영상)'. badge는 '신기술 탐구 (최신 영상)'.
3. news.social (AI 시사 & 사회)
   - AI 윤리·교육·저작권·정서적 영향 등에 관한 학술 논문(JAMA, Stanford, MIT 등) 또는 대규모 실태조사 1건.
   - sections는 최소 3개: '1. 이용 현황(또는 연구 방법)', '2. 주요 장점', '3. 위험성', 필요하면 '4. 연구의 한계'.
   - question: 학생들이 토론할 수 있는 핵심 탐구 질문 1개 (따옴표 없이).
   - cardBadge 예: '📊 학술 연구 (UC Berkeley · COLM 2026)'.
4. dictionary: 오늘 기사에 나온 AI 용어 1개를 학생 눈높이로 설명.
5. tip: 오늘 기사와 연결된 프롬프트 팁. body에는 '{{prompt}}' 자리표시자를 정확히 한 번 넣을 것.
6. quiz: 오늘 뉴스 기반 3지선다. options는 '1) ...', '2) ...', '3) ...' 형식. 정답 위치는 1~3 중 골고루.
   explanation 첫 줄은 '🎉 정답: N번 (정답)', 줄바꿈(\\n) 후 해설.
7. sources: 푸터에 쓸 출처 요약 한 줄.

[★ 반드시 지킬 것]
- sourceUrl은 실제로 열리는 기사·보도자료·공식 블로그 원문 주소. 검색 결과 페이지나 홈페이지 주소(예: https://openai.com) 금지.
- 날짜(date)는 원문에 적힌 발표일을 'YYYY년 M월 D일' 형식으로.
- 원문에 없는 숫자나 사실을 지어내지 말 것. 검증되지 않은 내용은 '보도됐어요/아직 검증 전'처럼 표시.
- cardTitle은 카드용 짧은 제목(느낌표 가능), title은 상세창용 정확한 제목.

[최근 7일 동안 다룬 제목 - 같은 소식 반복 금지]
{recent}

[지난 호 예시 - 형식과 말투 참고용, 내용은 복사 금지]
{example_json}
"""


# ─────────────────────────────────────────────
# OpenAI 호출
# ─────────────────────────────────────────────
def generate_with_openai(date_key, weekday, issues, feedback=None):
    from openai import OpenAI, BadRequestError

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    model = os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
    tool = os.environ.get("OPENAI_SEARCH_TOOL") or DEFAULT_SEARCH_TOOL

    example = None
    recent_titles = []
    for entry in issues[:7]:
        doc = load_issue(entry["date"])
        if not doc:
            continue
        if example is None:
            example = {k: doc[k] for k in ("news", "dictionary", "tip", "quiz", "sources") if k in doc}
        recent_titles += [item.get("title", "") for item in doc.get("news", {}).values()]

    request = dict(
        model=model,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(date_key, weekday, example, recent_titles)
                + (f"\n\n[이전 시도에서 검사에 실패한 부분 - 반드시 고칠 것]\n{feedback}" if feedback else "")},
        ],
        text={"format": {"type": "json_schema", "name": "daily_issue", "schema": ISSUE_SCHEMA, "strict": True}},
    )

    print(f"모델: {model} / 검색 도구: {tool}")
    try:
        response = client.responses.create(tools=[{"type": tool}], **request)
    except BadRequestError as e:
        if tool == "web_search":  # 구버전 계정·모델 호환
            print(f"web_search 사용 불가({e}). web_search_preview 로 다시 시도합니다.")
            response = client.responses.create(tools=[{"type": "web_search_preview"}], **request)
        else:
            raise

    return json.loads(response.output_text)


# ─────────────────────────────────────────────
# 검사
# ─────────────────────────────────────────────
BAD_URL_PATTERNS = [
    r"google\.[a-z.]+/search",
    r"bing\.com/search",
    r"search\.naver\.com",
    r"youtube\.com/results",
]


def validate(content):
    errors = []
    news = content.get("news", {})
    for key in ("openai", "gemini", "claude", "tech", "social"):
        item = news.get(key)
        if not item:
            errors.append(f"news.{key} 없음")
            continue
        url = item.get("sourceUrl", "")
        if not url.startswith("https://"):
            errors.append(f"news.{key}.sourceUrl 이 https 주소가 아님: {url}")
        elif any(re.search(p, url) for p in BAD_URL_PATTERNS):
            errors.append(f"news.{key}.sourceUrl 이 검색 결과 페이지임: {url}")
        elif re.fullmatch(r"https://[^/]+/?", url):
            errors.append(f"news.{key}.sourceUrl 이 원문이 아닌 홈페이지 주소임: {url}")

    yt = news.get("tech", {}).get("embedYoutubeId")
    if yt and not re.fullmatch(r"[\w-]{11}", yt):
        errors.append(f"news.tech.embedYoutubeId 형식 오류: {yt}")

    quiz = content.get("quiz", {})
    if len(quiz.get("options", [])) != 3:
        errors.append("quiz.options 는 3개여야 함")
    if quiz.get("correct") not in (1, 2, 3):
        errors.append("quiz.correct 는 1~3")

    tip = content.get("tip")
    if tip and "{prompt}" not in tip.get("body", ""):
        tip["body"] = "{prompt} " + tip.get("body", "")  # 자리표시자가 빠졌으면 앞에 붙여 줌

    return errors


# ─────────────────────────────────────────────
# 저장
# ─────────────────────────────────────────────
def save_issue(date_key, content, issues):
    weekday = WEEKDAYS[dt.date.fromisoformat(date_key).weekday()]
    issue_no = next_issue_no(issues, date_key)
    doc = {"date": date_key, "issueNo": issue_no, "weekday": weekday, **content}

    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / f"{date_key}.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    issues = [e for e in issues if e["date"] != date_key]
    issues.append({"date": date_key, "issueNo": issue_no, "weekday": weekday})
    issues.sort(key=lambda e: e["date"], reverse=True)
    ISSUES_FILE.write_text(json.dumps(issues, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"저장 완료: data/{date_key}.json (제{issue_no}호)")


def main():
    parser = argparse.ArgumentParser(description="1분 AI 모닝 리포트 하루치 생성")
    parser.add_argument("--date", help="YYYY-MM-DD (기본: 한국 시간 오늘)")
    parser.add_argument("--force", action="store_true", help="이미 있는 호도 다시 생성 (시작일·주말 조건도 무시)")
    parser.add_argument("--from-file", help="직접 준비한 JSON 파일을 이 날짜 호로 등록")
    parser.add_argument("--scheduled", action="store_true",
                        help="자동 실행용: GPT 시작일 이전이거나 주말이면 아무것도 하지 않음")
    args = parser.parse_args()

    date_key = args.date or dt.datetime.now(KST).date().isoformat()
    day = dt.date.fromisoformat(date_key)  # 형식 검사
    weekday = WEEKDAYS[day.weekday()]
    issues = load_issues()

    if args.scheduled and not args.force:
        start = dt.date.fromisoformat(os.environ.get("GPT_START_DATE") or DEFAULT_START_DATE)
        if day < start:
            print(f"GPT 자동 발행은 {start} 부터 시작해요. 오늘({date_key})은 건너뜁니다.")
            return 0
        if day.weekday() not in PUBLISH_WEEKDAYS:
            print(f"오늘({date_key}, {weekday}요일)은 발행하지 않는 날이에요.")
            return 0

    if (DATA_DIR / f"{date_key}.json").exists() and not args.force:
        print(f"{date_key} 호가 이미 있어요. 다시 만들려면 --force 를 붙이세요.")
        return 0

    if args.from_file:
        content = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
        for k in ("date", "issueNo", "weekday"):
            content.pop(k, None)
        errors = validate(content)
    else:
        if not os.environ.get("OPENAI_API_KEY"):
            print("OPENAI_API_KEY 가 없어요. 저장소 Settings > Secrets and variables > Actions 에 등록해 주세요.")
            return 1
        feedback = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(f"{date_key} ({weekday}) 신문 생성 중... (시도 {attempt}/{MAX_ATTEMPTS})")
            content = generate_with_openai(date_key, weekday, issues, feedback)
            errors = validate(content)
            if not errors:
                break
            feedback = "\n".join(f"- {e}" for e in errors)
            print("검사 실패:\n" + feedback)

    if errors:
        print("검사 실패 - 저장하지 않았어요:")
        for e in errors:
            print("  -", e)
        return 1

    save_issue(date_key, content, issues)
    return 0


if __name__ == "__main__":
    sys.exit(main())

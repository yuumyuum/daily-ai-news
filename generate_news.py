"""
1분 AI 모닝 리포트 - 하루치 신문(JSON) 생성 스크립트

동작 방식
  1. 한국 시간 기준 오늘 날짜의 호가 이미 있으면 아무것도 하지 않음
  2. OpenAI(웹 검색 사용)로 오늘의 기사 데이터를 JSON 형식으로 받음
  3. 검사(웹 검색 여부, 인용 대조, 링크 접속, 유튜브 영상, 중복, 글자 수) 후
     data/YYYY-MM-DD.json 으로 저장
  4. data/issues.json 발행 목록 맨 앞에 추가 (호수는 자동으로 +1)

index.html(디자인)은 절대 수정하지 않습니다. 그래서 지난 호가 모두 남습니다.

사용법
  python generate_news.py                     # 오늘 호 생성 (OPENAI_API_KEY 필요)
  python generate_news.py --scheduled         # 자동 실행용 (시작일 이전·주말이면 건너뜀)
  python generate_news.py --force             # 오늘 호가 있어도 다시 생성
  python generate_news.py --from-file my.json # 직접 쓴/다른 도구로 만든 JSON을 오늘 호로 등록
  python generate_news.py --date 2026-10-17   # 날짜 지정
  python generate_news.py --skip-link-check   # 인터넷이 막힌 환경에서 링크 접속 확인 생략
"""

import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
ISSUES_FILE = DATA_DIR / "issues.json"
HOLIDAYS_FILE = ROOT / "korean_holidays.json"
KST = ZoneInfo("Asia/Seoul")
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
NEWS_KEYS = ("openai", "gemini", "claude", "tech", "social")

DEFAULT_MODEL = "gpt-4o"
DEFAULT_SEARCH_TOOL = "web_search"
DEFAULT_START_DATE = "2026-10-20"  # GPT 자동 발행 시작일 (저장소 변수 GPT_START_DATE 로 변경 가능)
PUBLISH_WEEKDAYS = {0, 1, 2, 3, 4}  # 월~금 (한국 시간 기준)
MAX_ATTEMPTS = 2  # 검사에 실패하면 오류 내용을 알려주고 한 번 더 생성
RECENT_ISSUES_FOR_DUPLICATES = 20  # 최근 몇 개 호의 원문 주소와 겹치면 안 되는지
SOCIAL_LOOKBACK_DAYS = 14  # 시사·논문 섹션은 최근 2주 안의 연구에서 고름

# 글자 수 제한 (공백 포함). 넘으면 1차 시도에서는 다시 쓰게 하고, 마지막 시도에서는 경고만 함.
LIMITS = {
    "tag": 8,
    "cardTitle": 40,
    "cardSummary": 110,
    "title": 70,
    "category": 30,
    "cardBadge": 40,
    "question": 80,
    "summary_item": 120,
    "section_item": 120,
    "deepDive": 300,
    "dictionary.term": 30,
    "dictionary.body": 150,
    "tip.title": 30,
    "tip.prompt": 50,
    "tip.body": 130,
    "quiz.question": 100,
    "quiz.option": 30,
    "quiz.explanation": 150,
    "debate.question": 60,
    "debate.label": 12,
    "debate.reason": 80,
}

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)

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
DEBATE_SIDE = obj({"label": STR, "reasons": STR_LIST})
SOCIAL_ITEM = obj({
    **NEWS_BASE,
    "cardBadge": STR,
    "question": STR,
    "sections": {"type": "array", "items": obj({"title": STR, "items": STR_LIST})},
    "debate": obj({"question": STR, "pro": DEBATE_SIDE, "con": DEBATE_SIDE}),
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
# 발행 목록 · 지난 호 정보
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


def holiday_name(day):
    """한국 공휴일·쉬는 날이면 이름을, 아니면 None.
    1) korean_holidays.json (직접 관리: 공휴일·대체공휴일·임시공휴일·재량휴업일 등)
    2) holidays 라이브러리의 한국 공휴일 (목록에 없는 해를 위한 보조)"""
    key = day.isoformat()
    listed = {}
    if HOLIDAYS_FILE.exists():
        listed = json.loads(HOLIDAYS_FILE.read_text(encoding="utf-8")).get("dates", {})
    if key in listed:
        return listed[key]
    if not any(d.startswith(f"{day.year}-") for d in listed):
        print(f"참고: korean_holidays.json 에 {day.year}년 목록이 없어 holidays 라이브러리만 확인해요.")
    try:
        import holidays
        kr = holidays.country_holidays("KR", years=day.year, language="ko")
        return kr.get(day)
    except Exception as e:  # 라이브러리가 없거나 언어 옵션 미지원 등
        print(f"참고: holidays 라이브러리 확인 실패({type(e).__name__}) - 목록 파일만 사용해요.")
        return None


def normalize_url(url):
    """비교용 주소: http/https·www·끝 슬래시·추적용 파라미터 차이를 무시."""
    try:
        p = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return ""
    host = p.netloc.lower().removeprefix("www.").removeprefix("m.")
    path = p.path.rstrip("/")
    query = ""
    if host in ("youtube.com", "youtu.be"):
        vid = urllib.parse.parse_qs(p.query).get("v", [path.lstrip("/") if host == "youtu.be" else ""])[0]
        return f"youtube:{vid}"
    return f"{host}{path}{('?' + query) if query else ''}"


def gather_context(date_key, issues):
    """오늘 호를 만들 때 필요한 지난 호 정보."""
    past = sorted((e for e in issues if e["date"] < date_key), key=lambda e: e["date"], reverse=True)
    ctx = {
        "prev_date": past[0]["date"] if past else None,
        "example": None,
        "recent_titles": [],
        "used_urls": {},       # 정규화 주소 -> 날짜
        "past_research": [],   # 시사·논문 섹션에서 다룬 연구
    }
    for i, entry in enumerate(past):
        doc = load_issue(entry["date"])
        if not doc:
            continue
        news = doc.get("news", {})
        if ctx["example"] is None:
            ctx["example"] = {k: doc[k] for k in ("news", "dictionary", "tip", "quiz", "sources") if k in doc}
        if i < 7:
            ctx["recent_titles"] += [item.get("title", "") for item in news.values()]
        if i < RECENT_ISSUES_FOR_DUPLICATES:
            for item in news.values():
                if item.get("sourceUrl"):
                    ctx["used_urls"].setdefault(normalize_url(item["sourceUrl"]), entry["date"])
        social = news.get("social")
        if social:  # 시사·논문은 전체 기간에서 중복 금지
            ctx["past_research"].append(f"{social.get('title', '')} ({social.get('sourceUrl', '')})")
            ctx["used_urls"].setdefault(normalize_url(social.get("sourceUrl", "")), entry["date"])
    return ctx


# ─────────────────────────────────────────────
# 프롬프트
# ─────────────────────────────────────────────
SYSTEM_PROMPT = """너는 초·중·고등학생들을 위한 '1분 AI 아침 신문 리포터'야.
반드시 웹 검색으로 실제 기사와 공식 발표를 직접 찾아 읽은 뒤, 주어진 JSON 형식으로만 답해.
검색으로 확인하지 못한 기사·숫자·주소는 절대 지어내지 마. 지어낸 기사는 신문 전체를 망쳐.
학생에게 말하듯 친근한 반말 요약(cardSummary)과, 정확한 존댓말 상세 설명(summary, sections, deepDive)을 섞어 써."""


def build_user_prompt(date_key, weekday, ctx, quiz_answer):
    example_json = json.dumps(ctx["example"], ensure_ascii=False, indent=2) if ctx["example"] else "(없음)"
    recent = "\n".join(f"- {t}" for t in ctx["recent_titles"]) or "(없음)"
    used = "\n".join(f"- {u}" for u in list(ctx["used_urls"])[:60]) or "(없음)"
    research = "\n".join(f"- {r}" for r in ctx["past_research"]) or "(없음)"
    prev = ctx["prev_date"]
    since = f"지난 호 발행일({prev}) 이후부터 오늘({date_key}) 아침까지" if prev else "최근 2~3일 사이"
    limits = LIMITS
    return f"""[오늘 날짜] {date_key} ({weekday}요일) / 한국 시간 기준

[목표]
{since} 나온 AI 소식을 웹 검색으로 찾아 읽고 검증해서 오늘 호 데이터를 만들어 줘.

[섹션별 요구사항]
1. news.openai / news.gemini / news.claude
   - 각각 OpenAI, Google(Gemini·DeepMind·YouTube 등), Anthropic(Claude)의 공식 발표 1건.
   - 학생·교육과 관련 있으면 우선 선택. 기업 실적·주가·소송·사건 사고만 다루는 기사는 피할 것.
   - tag: 카드 오른쪽 작은 꼬리표(예: '학습 기능', '안전성 검증', '신규 모델').
   - badge는 각각 정확히 'OpenAI (오픈AI)', 'Google Gemini (제미나이)', 'Anthropic Claude (클로드)'.
2. news.tech (오늘의 AI 신기술)
   - 피지컬 AI, 온디바이스 AI, 휴머노이드, 로보틱스 중 눈으로 봐야 이해되는 흥미로운 주제 1건.
   - embedYoutubeId: 최근 1~2주 안에 공식 채널이나 언론사가 올린 실제 YouTube 영상 ID(11자).
     검색 결과에서 직접 확인한 영상만. 확실하지 않으면 null.
   - sourceUrl은 그 영상 주소(https://www.youtube.com/watch?v=ID) 또는 원문 기사.
   - category 예: '🤖 휴머노이드 & 로보틱스 (최신 영상)'. badge는 '신기술 탐구 (최신 영상)'.
3. news.social (AI 시사 & 사회)
   - AI 윤리·교육·저작권·정서적 영향 등에 관한 학술 논문(JAMA, Stanford, MIT 등) 또는 대규모 실태조사 1건.
   - 이 섹션만 기간을 넓혀 최근 {SOCIAL_LOOKBACK_DAYS}일 안에 공개되거나 보도된 연구에서 고를 것.
   - 아래 [이미 다룬 연구] 목록에 있는 연구는 다른 기사로 보도됐더라도 다시 고르지 말 것.
   - sections는 최소 3개: '1. 이용 현황(또는 연구 방법)', '2. 주요 장점', '3. 위험성', 필요하면 '4. 연구의 한계'.
   - question: 학생들이 토론할 수 있는 핵심 탐구 질문 1개 (따옴표 없이).
   - cardBadge 예: '📊 학술 연구 (UC Berkeley · COLM 2026)'.
   - debate: 학생 찬반 투표용 질문 1개 (탐구 질문과 별개).
     · question: '예/아니오'로 답할 수 있는 한 문장. 예: '숙제할 때 AI에게 정답을 물어봐도 될까?'
     · pro.label / con.label: 버튼에 들어갈 짧은 입장. 예: '괜찮아', '안 돼'
     · pro.reasons / con.reasons: 각 2~3개. 오늘 기사 내용에 근거하고, 양쪽 무게가 비슷하게.
     · 정답이 뻔한 질문(예: 괴롭힘을 해도 될까?), 특정 정당·정치인·종교에 관한 질문, 학생 개인 경험을 털어놓게 하는 질문은 금지.
4. dictionary: 오늘 기사에 나온 AI 용어 1개를 학생 눈높이로 설명.
5. tip: 오늘 기사와 연결된 프롬프트 팁. body에는 '{{prompt}}' 자리표시자를 정확히 한 번 넣을 것.
6. quiz: 오늘 뉴스 기반 3지선다.
   - ★ 정답은 반드시 {quiz_answer}번 보기에 둘 것 (correct = {quiz_answer}).
   - options는 '1) ...', '2) ...', '3) ...' 형식.
   - explanation 첫 줄은 '🎉 정답: {quiz_answer}번 (정답 내용)', 줄바꿈(\\n) 후 해설.
7. sources: 푸터에 쓸 출처 요약 한 줄.

[소식이 없을 때 - 매우 중요]
- 위 기간 안에 해당 회사의 새 공식 발표가 없으면, 기간 밖이라도 가장 최근의 "실제" 공식 발표를 고르고
  date에 실제 발표일을 적을 것. 단, 아래 [이미 사용한 원문 주소]와 겹치면 안 됨.
- 어떤 경우에도 존재하지 않는 기사·기능·숫자·주소를 만들지 말 것.

[원문 링크 규칙]
- sourceUrl은 이번 웹 검색에서 직접 찾아 열어 본 기사·보도자료·공식 블로그 원문 주소 그대로.
  검색 결과 페이지, 홈페이지 주소(예: https://openai.com), 추측해서 만든 주소는 금지.
- date는 원문에 적힌 발표일을 'YYYY년 M월 D일' 형식으로.
- 원문에 없는 숫자나 사실을 덧붙이지 말 것. 검증되지 않은 내용은 '보도됐어요/아직 검증 전'처럼 표시.

[저작권 · 인용]
- 원문 문장을 그대로 옮기지 말고 학생 눈높이의 자기 말로 바꿔 쓸 것.
- 원문 표현을 꼭 그대로 써야 할 때만 짧게(한 문장 이내) 큰따옴표로 묶고, 바로 뒤에
  (출처: 매체·기관명)을 붙일 것. 예: OpenAI는 이 기능을 "학생이 스스로 생각하도록 돕는 도구"라고 설명했어요(출처: OpenAI 공식 블로그).
- 인용은 기사 하나당 최대 1번.

[학생 안전 기준 - 독자는 초·중·고등학생]
- 범죄·폭력·자해·자살의 구체적인 방법이나 장면은 쓰지 말 것. 이런 사건이 핵심인 기사는 고르지 말 것.
- 정신건강·위기 상황을 다루는 연구라면 deepDive 끝에 도움 요청을 권하는 문장을 넣을 것
  (예: 힘들 때는 가까운 어른이나 청소년상담 1388에 이야기하기).
- 특정 정당·정치인을 지지하거나 비난하지 말고, 논쟁적인 주제는 여러 입장을 함께 소개할 것.
- 혐오 표현, 성적인 내용, 특정 집단에 대한 고정관념은 쓰지 말 것.

[글자 수 제한 - 공백 포함, 넘으면 다시 쓰게 됨]
- tag {limits['tag']}자, cardTitle {limits['cardTitle']}자, cardSummary {limits['cardSummary']}자(1~2문장), title {limits['title']}자
- summary·sections 항목 하나당 {limits['summary_item']}자, deepDive {limits['deepDive']}자
- category {limits['category']}자, cardBadge {limits['cardBadge']}자, social.question {limits['question']}자
- dictionary.term {limits['dictionary.term']}자, dictionary.body {limits['dictionary.body']}자
- tip.title {limits['tip.title']}자, tip.prompt {limits['tip.prompt']}자, tip.body {limits['tip.body']}자
- quiz.question {limits['quiz.question']}자, 보기 하나당 {limits['quiz.option']}자, quiz.explanation {limits['quiz.explanation']}자
- cardTitle은 카드용 짧은 제목(느낌표 가능), title은 상세창용 정확한 제목.

[최근 7개 호에서 다룬 제목 - 같은 소식 반복 금지]
{recent}

[이미 사용한 원문 주소 - 다시 쓰지 말 것]
{used}

[이미 다룬 연구 - 시사·논문 섹션에서 다시 쓰지 말 것]
{research}

[지난 호 예시 - 형식과 말투 참고용, 내용은 복사 금지]
{example_json}
"""


# ─────────────────────────────────────────────
# OpenAI 호출
# ─────────────────────────────────────────────
def collect_search_meta(response):
    """응답에서 웹 검색 횟수와 검색으로 인용된 주소 목록을 꺼냄."""
    search_calls = 0
    cited = set()
    for item in getattr(response, "output", None) or []:
        kind = getattr(item, "type", "")
        if kind.startswith("web_search"):
            search_calls += 1
            action = getattr(item, "action", None)
            for src in getattr(action, "sources", None) or []:
                url = getattr(src, "url", None)
                if url:
                    cited.add(normalize_url(url))
        for part in getattr(item, "content", None) or []:
            for ann in getattr(part, "annotations", None) or []:
                if getattr(ann, "type", "") == "url_citation" and getattr(ann, "url", None):
                    cited.add(normalize_url(ann.url))
    return {"search_calls": search_calls, "cited_urls": cited}


def generate_with_openai(date_key, weekday, ctx, quiz_answer, feedback=None):
    from openai import OpenAI, BadRequestError

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    model = os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
    tool = os.environ.get("OPENAI_SEARCH_TOOL") or DEFAULT_SEARCH_TOOL

    prompt = build_user_prompt(date_key, weekday, ctx, quiz_answer)
    if feedback:
        prompt += f"\n\n[이전 시도에서 검사에 실패한 부분 - 반드시 고칠 것]\n{feedback}"

    request = dict(
        model=model,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        include=["web_search_call.action.sources"],
        text={"format": {"type": "json_schema", "name": "daily_issue", "schema": ISSUE_SCHEMA, "strict": True}},
    )

    print(f"모델: {model} / 검색 도구: {tool}")
    try:
        response = client.responses.create(tools=[{"type": tool}], **request)
    except BadRequestError as e:
        # 구버전 계정·모델 호환: 검색 도구 이름이나 include 옵션을 지원하지 않는 경우
        print(f"요청 거절({e}). web_search_preview 로 다시 시도합니다.")
        request.pop("include", None)
        response = client.responses.create(tools=[{"type": "web_search_preview"}], **request)

    meta = collect_search_meta(response)
    print(f"웹 검색 {meta['search_calls']}회, 검색으로 확인된 주소 {len(meta['cited_urls'])}개")
    try:
        content = json.loads(response.output_text)
    except json.JSONDecodeError as e:
        content = {"_parse_error": str(e)}
    return content, meta


# ─────────────────────────────────────────────
# 퀴즈 정답 위치 맞추기
# ─────────────────────────────────────────────
def fix_quiz(quiz, target):
    """보기 번호를 다시 매기고, 정답이 target 번에 오도록 자리를 바꿈."""
    options = [re.sub(r"^\s*[1-3]\s*[).]\s*", "", o) for o in quiz.get("options", [])]
    correct = quiz.get("correct")
    if len(options) == 3 and correct in (1, 2, 3) and correct != target:
        options[correct - 1], options[target - 1] = options[target - 1], options[correct - 1]
        quiz["correct"] = target
    quiz["options"] = [f"{i}) {o}" for i, o in enumerate(options, 1)]
    if quiz.get("correct") in (1, 2, 3):
        quiz["explanation"] = re.sub(r"정답\s*:\s*[1-3]\s*번", f"정답: {quiz['correct']}번",
                                     quiz.get("explanation", ""), count=1)


# ─────────────────────────────────────────────
# 검사
# ─────────────────────────────────────────────
BAD_URL_PATTERNS = [
    r"google\.[a-z.]+/search",
    r"bing\.com/search",
    r"search\.naver\.com",
    r"youtube\.com/results",
]


def check_url_alive(url):
    """(상태, 설명) - 'ok' | 'dead'(확실히 없음) | 'unknown'(봇 차단·네트워크 문제로 확인 불가)."""
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "ko,en;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return "ok", str(res.status)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return "dead", f"HTTP {e.code}"
        return "unknown", f"HTTP {e.code}"
    except Exception as e:  # DNS 실패, 시간 초과, 프록시 차단 등
        return "unknown", type(e).__name__


def check_youtube(video_id):
    url = ("https://www.youtube.com/oembed?format=json&url="
           + urllib.parse.quote(f"https://www.youtube.com/watch?v={video_id}", safe=""))
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": BROWSER_UA}), timeout=20) as res:
            data = json.loads(res.read().decode("utf-8"))
            return "ok", data.get("author_name", "")
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403, 404):
            return "dead", f"HTTP {e.code}"
        return "unknown", f"HTTP {e.code}"
    except Exception as e:
        return "unknown", type(e).__name__


def check_lengths(content):
    issues = []

    def over(path, text, limit):
        if text and len(text) > limit:
            issues.append(f"{path} 글자 수 초과 ({len(text)}자 > {limit}자): {text[:30]}…")

    news = content.get("news", {})
    for key in NEWS_KEYS:
        item = news.get(key) or {}
        for field in ("tag", "cardTitle", "cardSummary", "title", "category", "cardBadge", "deepDive"):
            if field in item:
                over(f"news.{key}.{field}", item[field], LIMITS[field])
        if key == "social":
            over("news.social.question", item.get("question"), LIMITS["question"])
            debate = item.get("debate") or {}
            over("news.social.debate.question", debate.get("question"), LIMITS["debate.question"])
            for side in ("pro", "con"):
                s = debate.get(side) or {}
                over(f"news.social.debate.{side}.label", s.get("label"), LIMITS["debate.label"])
                for r in s.get("reasons", []):
                    over(f"news.social.debate.{side} 근거", r, LIMITS["debate.reason"])
            for s in item.get("sections", []):
                for it in s.get("items", []):
                    over("news.social.sections 항목", it, LIMITS["section_item"])
        for s in item.get("summary", []) or []:
            over(f"news.{key}.summary 항목", s, LIMITS["summary_item"])
    for group in ("dictionary", "tip"):
        for field, text in (content.get(group) or {}).items():
            if f"{group}.{field}" in LIMITS:
                over(f"{group}.{field}", text, LIMITS[f"{group}.{field}"])
    quiz = content.get("quiz") or {}
    over("quiz.question", quiz.get("question"), LIMITS["quiz.question"])
    over("quiz.explanation", quiz.get("explanation"), LIMITS["quiz.explanation"])
    for o in quiz.get("options", []):
        over("quiz 보기", o, LIMITS["quiz.option"])
    return issues


def validate(content, ctx, meta=None, check_links=True):
    """(errors, soft_errors, warnings) 반환.
    errors      : 반드시 고쳐야 저장 가능
    soft_errors : 다시 쓰게 하지만 마지막 시도에서는 경고로만 남김 (글자 수)
    warnings    : 참고용 로그
    """
    errors, warnings = [], []
    if "_parse_error" in content:
        return [f"JSON 해석 실패: {content['_parse_error']}"], [], []

    if meta is not None and meta["search_calls"] == 0:
        errors.append("웹 검색을 한 번도 하지 않고 작성함 - 반드시 웹 검색으로 실제 기사를 찾을 것")
    cited = meta["cited_urls"] if meta else set()
    if meta is not None and meta["search_calls"] and not cited:
        warnings.append("검색 인용 주소 목록을 받지 못해 인용 대조는 생략")

    news = content.get("news", {})
    yt_ok = False
    tech = news.get("tech") or {}
    yt = tech.get("embedYoutubeId")
    if yt:
        if not re.fullmatch(r"[\w-]{11}", yt):
            errors.append(f"news.tech.embedYoutubeId 형식 오류: {yt}")
        elif check_links:
            status, info = check_youtube(yt)
            if status == "dead":
                errors.append(f"news.tech.embedYoutubeId 영상이 없거나 퍼갈 수 없음({info}): {yt} - 확실한 영상이 없으면 null")
            elif status == "ok":
                yt_ok = True
                print(f"  유튜브 영상 확인: {yt} ({info})")
            else:
                warnings.append(f"유튜브 영상 확인 불가({info}): {yt}")

    for key in NEWS_KEYS:
        item = news.get(key)
        if not item:
            errors.append(f"news.{key} 없음")
            continue
        url = item.get("sourceUrl", "")
        norm = normalize_url(url)
        if not url.startswith("https://"):
            errors.append(f"news.{key}.sourceUrl 이 https 주소가 아님: {url}")
            continue
        if any(re.search(p, url) for p in BAD_URL_PATTERNS):
            errors.append(f"news.{key}.sourceUrl 이 검색 결과 페이지임: {url}")
            continue
        if re.fullmatch(r"https://[^/]+/?", url):
            errors.append(f"news.{key}.sourceUrl 이 원문이 아닌 홈페이지 주소임: {url}")
            continue
        if norm in ctx["used_urls"]:
            errors.append(f"news.{key}.sourceUrl 은 {ctx['used_urls'][norm]} 호에서 이미 다룬 기사임: {url}")
        is_checked_video = key == "tech" and yt_ok and norm == f"youtube:{yt}"
        if cited and norm not in cited and not is_checked_video:
            errors.append(f"news.{key}.sourceUrl 이 이번 웹 검색 결과에서 확인되지 않은 주소임: {url}")
        if check_links and not is_checked_video:
            status, info = check_url_alive(url)
            if status == "dead":
                errors.append(f"news.{key}.sourceUrl 이 열리지 않음({info}): {url}")
            elif status == "unknown":
                warnings.append(f"news.{key}.sourceUrl 접속 확인 불가({info}, 봇 차단일 수 있음): {url}")

    debate = (news.get("social") or {}).get("debate")
    if not debate or not (debate.get("question") or "").strip():
        errors.append("news.social.debate (찬반 투표 질문) 없음")
    else:
        for side in ("pro", "con"):
            s = debate.get(side) or {}
            if not (s.get("label") or "").strip():
                errors.append(f"news.social.debate.{side}.label 없음")
            if not 2 <= len(s.get("reasons") or []) <= 3:
                errors.append(f"news.social.debate.{side}.reasons 는 2~3개여야 함")

    quiz = content.get("quiz", {})
    if len(quiz.get("options", [])) != 3:
        errors.append("quiz.options 는 3개여야 함")
    if quiz.get("correct") not in (1, 2, 3):
        errors.append("quiz.correct 는 1~3")

    tip = content.get("tip")
    if tip and "{prompt}" not in tip.get("body", ""):
        tip["body"] = "{prompt} " + tip.get("body", "")  # 자리표시자가 빠졌으면 앞에 붙여 줌

    return errors, check_lengths(content), warnings


def report(title, lines):
    if lines:
        print(title)
        for line in lines:
            print("  -", line)


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
    parser.add_argument("--skip-link-check", action="store_true",
                        help="링크·유튜브 접속 확인 생략 (인터넷이 막힌 환경용)")
    args = parser.parse_args()
    check_links = not (args.skip_link_check or os.environ.get("SKIP_LINK_CHECK"))

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
        holiday = holiday_name(day)
        if holiday:
            print(f"오늘({date_key})은 공휴일({holiday})이라 발행하지 않아요.")
            return 0

    if (DATA_DIR / f"{date_key}.json").exists() and not args.force:
        print(f"{date_key} 호가 이미 있어요. 다시 만들려면 --force 를 붙이세요.")
        return 0

    ctx = gather_context(date_key, issues)

    if args.from_file:
        content = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
        for k in ("date", "issueNo", "weekday"):
            content.pop(k, None)
        errors, soft, warnings = validate(content, ctx, meta=None, check_links=check_links)
        report("경고:", warnings)
        errors += soft  # 직접 만든 파일은 글자 수도 지켜야 저장
    else:
        if not os.environ.get("OPENAI_API_KEY"):
            print("OPENAI_API_KEY 가 없어요. 저장소 Settings > Secrets and variables > Actions 에 등록해 주세요.")
            return 1
        quiz_answer = random.randint(1, 3)
        print(f"지난 호: {ctx['prev_date']} / 오늘 퀴즈 정답 위치: {quiz_answer}번")
        feedback = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            final = attempt == MAX_ATTEMPTS
            print(f"{date_key} ({weekday}) 신문 생성 중... (시도 {attempt}/{MAX_ATTEMPTS})")
            content, meta = generate_with_openai(date_key, weekday, ctx, quiz_answer, feedback)
            if "quiz" in content:
                fix_quiz(content["quiz"], quiz_answer)
            errors, soft, warnings = validate(content, ctx, meta=meta, check_links=check_links)
            report("경고:", warnings)
            if final:
                report("글자 수 경고 (저장은 진행):", soft)
            else:
                errors = errors + soft
            if not errors:
                break
            feedback = "\n".join(f"- {e}" for e in errors)
            report("검사 실패:", errors)

    if errors:
        report("검사 실패 - 저장하지 않았어요:", errors)
        return 1

    save_issue(date_key, content, issues)
    return 0


if __name__ == "__main__":
    sys.exit(main())

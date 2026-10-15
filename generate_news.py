import os
import datetime
import re
from openai import OpenAI

# 1. GitHub Secrets에서 OpenAI API Key 가져오기
api_key = os.environ.get("OPENAI_API_KEY")
client = OpenAI(api_key=api_key)

def generate_ai_news():
    today = datetime.datetime.now().strftime("%Y년 %m월 %d일")
    
    # [사용자 지정 프롬프트 입력]
    system_prompt = """
너는 초·중·고등학생들을 위한 '1분 AI 아침 신문 리포터'이자 웹 개발자야.
다른 부연 설명이나 마크다운 텍스트 없이, 오직 브라우저에서 실행 가능한 완벽한 단일 HTML(index.html) 코드만 출력해줘.
    """

    user_prompt = f"""
[오늘 날짜]
{today}

[목표]
오늘 날짜 기준 24시간 이내의 최신 AI 소식을 수집 및 검증하여, 기존 작성된 '1분 AI 모닝 리포트' HTML 코드의 데이터 내용을 최신 상태로 교체해 줘.

[★ 필수 준수 사항 및 주의사항 (CRITICAL)]
1. 출처 원문 링크 연결 (검색 페이지 금지):
  - 뉴스 카드의 '자세히 보기' 및 모달 하단 버튼을 클릭했을 때 단순 검색 결과 페이지(google.com/search 등)로 이동해서는 절대 안 됨.
  - 반드시 해당 소식을 다룬 '공식 보도자료', '공식 블로그', '실제 뉴스 기사 원문 페이지' URL로 바로 연결되도록 작성할 것.

2. 오늘의 AI 신기술 (시각 자료 및 영상 최신성 준수):
  - 텍스트로만 읽어서는 이해하기 어려운 피지컬 AI, 온디바이스 AI, 휴머노이드, 로보틱스 중 흥미로운 주제 1건 선별.
  - 관련 YouTube 영상은 수개월 전 오래된 영상이 아닌, 반드시 '가장 최근에 발표되거나 보도된 최신 영상'의 ID와 embed 링크를 연결할 것.
  - 모달 상세창 내에 해당 유튜브 영상 플레이어(iframe)가 바로 뜨도록 구성할 것.

3. AI 시사 & 사회 (학술 논문 및 실증 데이터 중심):
  - AI 윤리, 교육 현장, 저작권, 정서적 영향 등과 관련된 주요 학술 연구 논문(JAMA Pediatrics, Stanford, MIT 등) 또는 대규모 실태조사 1건 선별.
  - 단순히 요약만 하지 말고 [이용 현황 / 주요 장점 / 과순응 및 위험성] 등 다각도 분석 및 학생들이 생각할 수 있는 '핵심 탐구 질문' 포함할 것.

4. 1분 상식 퀴즈 & 인터랙션 리셋 기능:
  - 오늘 뉴스 내용 기반 3지선다형 퀴즈 1개 제작.
  - 사용자가 보기를 클릭하면 정답/오답 표시(배지 및 테두리 색)가 즉시 나타나야 함.
  - '전체 정답 및 해설 닫기' 버튼을 누르면 이전에 선택했던 정답/오답 표시 및 스타일이 '초기화(Reset)'되어 다음 사람도 처음부터 다시 풀 수 있도록 스크립트가 유지되어야 함.

[출력 형식]
- TailWind CSS, 모달 팝업, 퀴즈 리셋 JavaScript 스크립트 등이 모두 포함된 완성된 단일 HTML(index.html) 코드를 제공해줘.
    """

    response = client.chat.completions.create(
        model="gpt-4o",  # 최신 뉴스 정확도 및 HTML 구조 보장을 위해 gpt-4o 권장
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.3  # 사실성 및 링크 정확도를 위해 낮은 온도 설정
    )
    
    content = response.choices[0].message.content.strip()
    
    # AI 응답 중 마크다운 코드블록(` ```html ... ``` `) 부분 제거
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\n", "", content)
        content = re.sub(r"\n```$", "", content)
        
    return content.strip()

def save_html(html_content):
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    print("index.html 저장 완료!")

if __name__ == "__main__":
    print("GPT API를 이용해 AI 뉴스 HTML 생성 중...")
    news_html = generate_ai_news()
    save_html(news_html)

-- ─────────────────────────────────────────────────────────────
-- 1분 AI 모닝 리포트 · 찬반 투표 (Supabase SQL Editor 에 통째로 붙여넣고 Run)
--
-- 보안 구조
--   · 학생(웹페이지)은 아래 두 함수만 호출할 수 있어요.
--       cast_debate_vote  : 투표 저장(같은 날 같은 학번이면 수정) → 집계 반환
--       debate_counts     : 찬성/반대 숫자만 반환
--   · debate_votes 테이블(학번·이름·이유)은 웹페이지에서 직접 읽거나 쓸 수 없어요.
--     선생님만 Supabase 대시보드(Table Editor)에서 볼 수 있어요.
-- 여러 번 실행해도 안전하게 작성되어 있어요.
-- ─────────────────────────────────────────────────────────────

create table if not exists public.debate_votes (
  id           bigint generated always as identity primary key,
  issue_date   date        not null,                 -- 신문 날짜 (예: 2026-10-16)
  student_id   text        not null,                 -- 학번
  student_name text        not null,                 -- 이름
  choice       text        not null check (choice in ('pro', 'con')),
  reason       text,                                 -- 이유 (선택)
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  unique (issue_date, student_id)
);

-- 행 단위 보안 켜기 + 정책 없음 = 웹페이지(anon)는 테이블에 직접 접근 불가
alter table public.debate_votes enable row level security;
revoke all on table public.debate_votes from anon, authenticated;

-- 찬성/반대 숫자만 알려주는 함수
create or replace function public.debate_counts(p_issue_date date)
returns table (pro bigint, con bigint)
language sql
stable
security definer
set search_path = public
as $$
  select count(*) filter (where v.choice = 'pro'),
         count(*) filter (where v.choice = 'con')
  from public.debate_votes v
  where v.issue_date = p_issue_date;
$$;

-- 투표 저장 함수 (입력값 검사 포함)
create or replace function public.cast_debate_vote(
  p_issue_date   date,
  p_student_id   text,
  p_student_name text,
  p_choice       text,
  p_reason       text default null
)
returns table (pro bigint, con bigint)
language plpgsql
security definer
set search_path = public
as $$
declare
  v_id     text := btrim(coalesce(p_student_id, ''));
  v_name   text := btrim(coalesce(p_student_name, ''));
  v_reason text := nullif(btrim(coalesce(p_reason, '')), '');
begin
  if p_choice is null or p_choice not in ('pro', 'con') then
    raise exception '찬성 또는 반대를 골라 주세요.';
  end if;
  if v_id !~ '^[0-9A-Za-z-]{1,15}$' then
    raise exception '학번을 확인해 주세요. (숫자·영문, 15자 이내)';
  end if;
  if char_length(v_name) < 1 or char_length(v_name) > 20 then
    raise exception '이름을 확인해 주세요. (20자 이내)';
  end if;
  if v_reason is not null and char_length(v_reason) > 200 then
    raise exception '이유는 200자 이내로 써 주세요.';
  end if;
  -- 너무 오래된 호나 미래 날짜에는 투표할 수 없음 (지난 호는 60일까지 투표 가능)
  if p_issue_date is null or p_issue_date < current_date - 60 or p_issue_date > current_date + 1 then
    raise exception '이 날짜의 신문에는 투표할 수 없어요.';
  end if;

  insert into public.debate_votes (issue_date, student_id, student_name, choice, reason)
  values (p_issue_date, v_id, v_name, p_choice, v_reason)
  on conflict (issue_date, student_id) do update
    set student_name = excluded.student_name,
        choice       = excluded.choice,
        reason       = excluded.reason,
        updated_at   = now();

  return query select c.pro, c.con from public.debate_counts(p_issue_date) c;
end;
$$;

-- 웹페이지(anon)는 두 함수만 실행 가능
revoke all on function public.debate_counts(date) from public;
revoke all on function public.cast_debate_vote(date, text, text, text, text) from public;
grant execute on function public.debate_counts(date) to anon, authenticated;
grant execute on function public.cast_debate_vote(date, text, text, text, text) to anon, authenticated;


-- ─────────────────────────────────────────────────────────────
-- (선생님용) 결과 보기 예시 - 필요할 때 SQL Editor 에서 따로 실행
-- ─────────────────────────────────────────────────────────────
-- 특정 날짜 투표 명단
--   select student_id, student_name,
--          case choice when 'pro' then '찬성' else '반대' end as 입장,
--          reason, updated_at
--   from public.debate_votes
--   where issue_date = '2026-10-16'
--   order by student_id;
--
-- 날짜별 집계
--   select issue_date,
--          count(*) filter (where choice = 'pro') as 찬성,
--          count(*) filter (where choice = 'con') as 반대
--   from public.debate_votes
--   group by issue_date
--   order by issue_date desc;

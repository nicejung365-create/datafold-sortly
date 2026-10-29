-- classification_results 표에 키워드·요약·분류 AI 칸을 더한다.
--   keywords : 고정 기준(대상·분야·정보유형)에 담기지 않는 내용을 키워드로 (여러 개면 쉼표로 이어서)
--   summary  : URL 크롤링으로 읽은 게시물을 AI 가 한두 줄로 요약한 글 (파일로 넣은 소재는 비어 있음)
--   ai_model : 그 소재를 분류한 AI 모델 이름 (예: claude-opus-5-5, Claude 를 쓸 수 없을 때는 gemini-3.5-flash-lite)
-- Supabase 대시보드 > SQL Editor 에 통째로 붙여 넣고 [Run].
-- 다시 실행해도 오류가 나지 않는다: 칸이 이미 있으면 그대로 둔다 (예전에 한 번 실행했어도 다시 실행하면 ai_model 만 더해짐).
-- 대상은 지금처럼 lifecycle 칸에 저장한다 (여러 개면 "청년, 구직자/근로자" 처럼 쉼표로 이어서).

alter table public.classification_results
  add column if not exists keywords text,
  add column if not exists summary text,
  add column if not exists ai_model text;

-- 확인: 아래 결과에 ai_model · keywords · summary 세 줄이 나오면 칸이 생긴 것
select column_name as 칸이름, data_type as 형식
from information_schema.columns
where table_schema = 'public'
  and table_name = 'classification_results'
  and column_name in ('keywords', 'summary', 'ai_model')
order by column_name;

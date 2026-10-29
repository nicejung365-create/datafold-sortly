-- Supabase Storage 의 docs 버킷에 화면(anon 키)에서 문서를 올리고, 올라온 목록을 볼 수 있게 허용한다.
-- Supabase 대시보드 > SQL Editor 에 통째로 붙여 넣고 [Run].
-- 다시 실행해도 오류가 나지 않는다: 버킷은 없을 때만 만들고, 허용 규칙은 지운 뒤 다시 만든다.
-- (참고) storage.objects 표는 Supabase 가 이미 행 단위 보안(RLS)을 켜 두었으므로 따로 켜지 않는다.

-- 1) docs 버킷이 없으면 만든다 (이미 있으면 그대로 둔다)
--    문서는 주소만 알면 누구나 열 수 없도록 비공개(public = false)로 만든다
insert into storage.buckets (id, name, public)
values ('docs', 'docs', false)
on conflict (id) do nothing;

-- 2) 예전에 만든 같은 이름의 허용 규칙이 있으면 지운다
drop policy if exists "docs 목록 보기" on storage.objects;
drop policy if exists "docs 올리기" on storage.objects;

-- 3) 화면(anon 키)에서 docs 버킷 안의 파일 목록 보기 허용
create policy "docs 목록 보기" on storage.objects
  for select to anon, authenticated
  using (bucket_id = 'docs');

-- 4) 화면(anon 키)에서 docs 버킷에 문서 올리기 허용
create policy "docs 올리기" on storage.objects
  for insert to anon, authenticated
  with check (bucket_id = 'docs');

-- 5) 확인: 아래 결과에 두 줄(SELECT 목록 보기, INSERT 올리기)이 나오면 허용이 들어간 것
select policyname as 규칙이름, cmd as 동작, roles as 대상
from pg_policies
where schemaname = 'storage'
  and tablename = 'objects'
  and policyname in ('docs 목록 보기', 'docs 올리기')
order by policyname;

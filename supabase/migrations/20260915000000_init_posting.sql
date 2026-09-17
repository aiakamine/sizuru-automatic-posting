-- =============================================================================
-- sizuru-automatic-posting: 投稿記録スキーマ（第1段階）
--
-- 単位:
--   submissions … 承認済みの「動画1本＋投稿文セット」（1回の承認 = 1行）
--   posts       … submission を各SNSへ出した1件（媒体ごとに1行）
--   channels    … Buffer に接続した SNS アカウント（各SNS 1アカウント想定）
--   admin_users … 管理画面にログインできる管理者
--
-- 書き込み: service_role のみ（CLI と、管理画面のサーバー処理）。
--           管理画面での日時変更・投稿文編集・予約キャンセルは、サーバー側で
--           管理者か確認 → Buffer API（editPost / deletePost）成功 → DB 更新 の順に行い、
--           Buffer と DB の内容がずれないようにする。ブラウザから直接は書き込ませない。
-- 読み取り: admin_users に登録されたログインユーザーのみ
-- 指標（再生数など）は管理画面の段階で別マイグレーションとして追加する
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 型
-- -----------------------------------------------------------------------------
create type public.sns_service as enum ('tiktok', 'instagram', 'x');

create type public.share_mode as enum ('shareNow', 'addToQueue', 'customScheduled');

-- pending   … DB 登録済み・Buffer へ未送信（送信中に落ちた場合もここに残る）
-- draft     … Buffer に下書きとして登録
-- scheduled … Buffer のキュー／予約に登録済み
-- sent      … SNS に公開済み（sync で確定）
-- failed    … 送信または公開に失敗（error_message に理由）
-- canceled  … 管理画面で予約をキャンセルした、または Buffer 上で削除された（公開されずに終わった）
create type public.post_status as enum ('pending', 'draft', 'scheduled', 'sent', 'failed', 'canceled');

-- -----------------------------------------------------------------------------
-- updated_at 自動更新
-- -----------------------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

-- -----------------------------------------------------------------------------
-- channels: Buffer に接続した SNS アカウント
-- -----------------------------------------------------------------------------
create table public.channels (
  id                uuid primary key default gen_random_uuid(),
  service           public.sns_service not null,
  buffer_channel_id text not null unique,
  name              text not null,
  display_name      text,
  is_disconnected   boolean not null default false,
  is_queue_paused   boolean not null default false,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

comment on table public.channels is 'Buffer に接続した SNS アカウント。`sizuru-post channels` で同期する';

create trigger channels_set_updated_at
  before update on public.channels
  for each row execute function public.set_updated_at();

-- -----------------------------------------------------------------------------
-- submissions: 承認済みの動画＋投稿文セット
-- -----------------------------------------------------------------------------
create table public.submissions (
  id            uuid primary key default gen_random_uuid(),
  title         text,
  s3_key        text not null unique,
  video_url     text not null,
  thumbnail_url text,
  duration_sec  numeric(7, 2),
  approved_at   timestamptz not null,
  created_at    timestamptz not null default now(),

  constraint submissions_duration_positive check (duration_sec is null or duration_sec > 0)
);

comment on table  public.submissions is '承認済みの「動画1本＋投稿文セット」。1回の承認で1行';
comment on column public.submissions.video_url   is 'Buffer に渡した S3 の公開URL。S3 ライフサイクルで60日後に消えるため、以降はリンク切れになる';
comment on column public.submissions.thumbnail_url is 'カレンダー表示用のサムネイル画像（動画から切り出したJPEG）。動画と違い自動削除しない';
comment on column public.submissions.duration_sec  is '動画の長さ（秒）。カレンダーでの表示用';
comment on column public.submissions.approved_at   is '人間が投稿を承認した日時';

create index submissions_created_at_idx on public.submissions (created_at desc);

-- -----------------------------------------------------------------------------
-- posts: 媒体ごとの投稿
-- -----------------------------------------------------------------------------
create table public.posts (
  id               uuid primary key default gen_random_uuid(),
  submission_id    uuid not null references public.submissions (id) on delete cascade,
  channel_id       uuid not null references public.channels (id) on delete restrict,
  caption          text not null,
  mode             public.share_mode not null,
  due_at           timestamptz,
  is_draft         boolean not null default false,
  platform_options jsonb not null default '{}'::jsonb,
  buffer_post_id   text unique,
  status           public.post_status not null default 'pending',
  external_link    text,
  sent_at          timestamptz,
  error_message    text,
  retry_count      integer not null default 0,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),

  -- 同じセットを同じアカウントへ二重に出さない
  constraint posts_submission_channel_key unique (submission_id, channel_id),
  -- 日時指定のときだけ due_at を持つ（キュー／即時投稿は Buffer 登録後に sync で due_at が埋まる）
  constraint posts_due_at_required_for_custom check (mode <> 'customScheduled' or due_at is not null),
  constraint posts_retry_count_nonnegative check (retry_count >= 0),
  constraint posts_platform_options_is_object check (jsonb_typeof(platform_options) = 'object'),
  constraint posts_failed_has_error check (status <> 'failed' or error_message is not null),
  constraint posts_sent_has_sent_at check (status <> 'sent' or sent_at is not null),
  constraint posts_sent_to_buffer_has_id check (status in ('pending', 'failed') or buffer_post_id is not null)
);

comment on table  public.posts is 'submission を各SNSへ出した1件。投稿時間は媒体ごとに持つ。状態は最新のみ保持（試行履歴は持たない）';
comment on column public.posts.due_at is '投稿予定日時。customScheduled は必須。addToQueue / shareNow は Buffer が決めた日時を sync で反映';
comment on column public.posts.platform_options is '媒体固有の投稿オプション（例: {"tiktok_title": "..."}, {"share_to_feed": true}）。再送時に同じ内容で送るため保持';
comment on column public.posts.retry_count      is '再送した回数（初回送信は含まない）';

create index posts_submission_id_idx on public.posts (submission_id);
create index posts_channel_id_idx    on public.posts (channel_id);
-- 週カレンダー表示用
create index posts_due_at_idx        on public.posts (due_at);
-- sync 対象（未確定の投稿）を引くための部分インデックス
create index posts_unsettled_idx     on public.posts (status) where status in ('pending', 'scheduled');

create trigger posts_set_updated_at
  before update on public.posts
  for each row execute function public.set_updated_at();

-- -----------------------------------------------------------------------------
-- admin_users: 管理画面にログインできる管理者
-- 追加は Supabase ダッシュボードまたは SQL で行う（画面からの追加はしない）
-- -----------------------------------------------------------------------------
create table public.admin_users (
  user_id      uuid primary key references auth.users (id) on delete cascade,
  email        text not null,
  display_name text,
  created_at   timestamptz not null default now()
);

comment on table public.admin_users is '管理画面にログインできる管理者。編集・キャンセルはサーバー側で is_admin() を確認してから行う';

-- RLS ポリシー内で admin_users 自身の RLS を再帰評価しないよう security definer にする
create or replace function public.is_admin()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.admin_users where user_id = (select auth.uid())
  );
$$;

revoke all on function public.is_admin() from public;
grant execute on function public.is_admin() to authenticated;

-- -----------------------------------------------------------------------------
-- RLS: 管理者は閲覧のみ。書き込みポリシーは作らない（service_role のみ書ける）
-- -----------------------------------------------------------------------------
alter table public.channels    enable row level security;
alter table public.submissions enable row level security;
alter table public.posts       enable row level security;
alter table public.admin_users enable row level security;

create policy "admins can read channels"
  on public.channels for select to authenticated
  using ((select public.is_admin()));

create policy "admins can read submissions"
  on public.submissions for select to authenticated
  using ((select public.is_admin()));

create policy "admins can read posts"
  on public.posts for select to authenticated
  using ((select public.is_admin()));

create policy "admins can read admin_users"
  on public.admin_users for select to authenticated
  using ((select public.is_admin()));

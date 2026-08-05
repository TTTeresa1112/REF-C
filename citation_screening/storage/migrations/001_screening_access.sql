create extension if not exists pgcrypto;

create table if not exists public.screening_users (
    id uuid primary key default gen_random_uuid(),
    display_name text not null,
    access_code_hash text not null unique,
    daily_limit integer not null default 200 check (daily_limit >= 0),
    enabled boolean not null default true,
    created_at timestamptz not null default now()
);

create table if not exists public.screening_daily_usage (
    user_id uuid not null references public.screening_users(id) on delete cascade,
    usage_date date not null default (timezone('Asia/Shanghai', now()))::date,
    used_calls integer not null default 0 check (used_calls >= 0),
    updated_at timestamptz not null default now(),
    primary key (user_id, usage_date)
);

create table if not exists public.screening_tasks (
    task_id uuid primary key,
    user_id uuid not null references public.screening_users(id) on delete cascade,
    filename_hash text not null,
    estimated_calls integer not null check (estimated_calls >= 0),
    actual_calls integer,
    status text not null default 'running' check (status in ('running', 'completed', 'failed')),
    created_at timestamptz not null default now(),
    completed_at timestamptz
);

alter table public.screening_users enable row level security;
alter table public.screening_daily_usage enable row level security;
alter table public.screening_tasks enable row level security;

create or replace function public.get_screening_quota(p_user_id uuid)
returns table(daily_limit integer, used integer, remaining integer)
language sql security definer set search_path = public
as $$
    select u.daily_limit,
           coalesce(d.used_calls, 0)::integer,
           greatest(u.daily_limit - coalesce(d.used_calls, 0), 0)::integer
    from screening_users u
    left join screening_daily_usage d
      on d.user_id = u.id and d.usage_date = (timezone('Asia/Shanghai', now()))::date
    where u.id = p_user_id and u.enabled = true;
$$;

create or replace function public.reserve_screening_quota(
    p_user_id uuid, p_task_id uuid, p_requested_calls integer, p_filename_hash text
)
returns table(allowed boolean, reserved integer, remaining integer, message text)
language plpgsql security definer set search_path = public
as $$
declare
    v_limit integer;
    v_used integer;
    v_refund integer;
begin
    if p_requested_calls < 0 then raise exception 'invalid requested_calls'; end if;
    select daily_limit into v_limit from screening_users
      where id = p_user_id and enabled = true for update;
    if v_limit is null then
        return query select false, 0, 0, '用户不存在或已停用'::text; return;
    end if;
    insert into screening_daily_usage(user_id, usage_date, used_calls)
      values(p_user_id, (timezone('Asia/Shanghai', now()))::date, 0)
      on conflict (user_id, usage_date) do nothing;
    select coalesce(sum(estimated_calls), 0)::integer into v_refund
      from screening_tasks
     where user_id=p_user_id and status='running' and created_at < now()-interval '2 hours'
       and (timezone('Asia/Shanghai', created_at))::date=(timezone('Asia/Shanghai', now()))::date;
    if v_refund > 0 then
        update screening_daily_usage set used_calls=greatest(used_calls-v_refund, 0), updated_at=now()
         where user_id=p_user_id and usage_date=(timezone('Asia/Shanghai', now()))::date;
        update screening_tasks set status='failed', actual_calls=0, completed_at=now()
         where user_id=p_user_id and status='running' and created_at < now()-interval '2 hours';
    end if;
    select used_calls into v_used from screening_daily_usage
      where user_id = p_user_id and usage_date = (timezone('Asia/Shanghai', now()))::date for update;
    if v_used + p_requested_calls > v_limit then
        return query select false, 0, greatest(v_limit-v_used, 0), '今日额度不足'::text; return;
    end if;
    insert into screening_tasks(task_id, user_id, filename_hash, estimated_calls)
      values(p_task_id, p_user_id, p_filename_hash, p_requested_calls)
      on conflict (task_id) do nothing;
    if not found then
        return query select false, 0, greatest(v_limit-v_used, 0), '任务已提交，请勿重复点击'::text; return;
    end if;
    update screening_daily_usage set used_calls=used_calls+p_requested_calls, updated_at=now()
      where user_id=p_user_id and usage_date=(timezone('Asia/Shanghai', now()))::date;
    return query select true, p_requested_calls, v_limit-v_used-p_requested_calls, '额度已预扣'::text;
end;
$$;

create or replace function public.settle_screening_quota(
    p_task_id uuid, p_actual_calls integer, p_succeeded boolean
)
returns void language plpgsql security definer set search_path = public
as $$
declare
    v_task screening_tasks%rowtype;
    v_final integer;
begin
    select * into v_task from screening_tasks where task_id=p_task_id for update;
    if not found or v_task.status <> 'running' then return; end if;
    v_final := least(greatest(p_actual_calls, 0), v_task.estimated_calls);
    update screening_daily_usage
       set used_calls=greatest(used_calls-(v_task.estimated_calls-v_final), 0), updated_at=now()
     where user_id=v_task.user_id and usage_date=(timezone('Asia/Shanghai', v_task.created_at))::date;
    update screening_tasks set actual_calls=v_final,
      status=case when p_succeeded then 'completed' else 'failed' end, completed_at=now()
      where task_id=p_task_id;
end;
$$;

revoke all on function public.get_screening_quota(uuid) from public, anon, authenticated;
revoke all on function public.reserve_screening_quota(uuid, uuid, integer, text) from public, anon, authenticated;
revoke all on function public.settle_screening_quota(uuid, integer, boolean) from public, anon, authenticated;
grant execute on function public.get_screening_quota(uuid) to service_role;
grant execute on function public.reserve_screening_quota(uuid, uuid, integer, text) to service_role;
grant execute on function public.settle_screening_quota(uuid, integer, boolean) to service_role;

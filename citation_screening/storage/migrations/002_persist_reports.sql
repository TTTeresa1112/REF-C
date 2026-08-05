alter table public.screening_tasks
    add column if not exists display_filename text,
    add column if not exists result_payload jsonb,
    add column if not exists report_html text,
    add column if not exists error_message text,
    add column if not exists expires_at timestamptz;

create index if not exists screening_tasks_user_created_idx
    on public.screening_tasks(user_id, created_at desc);

create or replace function public.complete_screening_task(
    p_task_id uuid,
    p_actual_calls integer,
    p_result_payload jsonb,
    p_report_html text
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
     where user_id=v_task.user_id
       and usage_date=(timezone('Asia/Shanghai', v_task.created_at))::date;
    update screening_tasks
       set actual_calls=v_final,
           status='completed',
           result_payload=p_result_payload,
           report_html=p_report_html,
           error_message=null,
           completed_at=now(),
           expires_at=now()+interval '24 hours'
     where task_id=p_task_id;
end;
$$;

create or replace function public.fail_screening_task(
    p_task_id uuid,
    p_actual_calls integer,
    p_error_message text
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
     where user_id=v_task.user_id
       and usage_date=(timezone('Asia/Shanghai', v_task.created_at))::date;
    update screening_tasks
       set actual_calls=v_final,
           status='failed',
           error_message=left(coalesce(p_error_message, '任务失败'), 500),
           completed_at=now(),
           expires_at=now()+interval '24 hours'
     where task_id=p_task_id;
end;
$$;

revoke all on function public.complete_screening_task(uuid, integer, jsonb, text)
    from public, anon, authenticated;
revoke all on function public.fail_screening_task(uuid, integer, text)
    from public, anon, authenticated;
grant execute on function public.complete_screening_task(uuid, integer, jsonb, text)
    to service_role;
grant execute on function public.fail_screening_task(uuid, integer, text)
    to service_role;

create or replace function public.purge_expired_screening_tasks()
returns integer language plpgsql security definer set search_path = public
as $$
declare
    v_count integer;
begin
    delete from screening_tasks where expires_at is not null and expires_at < now();
    get diagnostics v_count = row_count;
    return v_count;
end;
$$;

revoke all on function public.purge_expired_screening_tasks()
    from public, anon, authenticated;
grant execute on function public.purge_expired_screening_tasks()
    to service_role;

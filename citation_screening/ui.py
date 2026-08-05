import hashlib
import json
import os
import time
import uuid
from datetime import datetime

import pandas as pd
import streamlit as st

from generate_json import process_single_reference_new

from .auth import QuotaStore, QuotaStoreError
from .pipeline import execute_screening, prepare_screening


MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_CALLS_PER_TASK = int(os.getenv("SCREENING_MAX_CALLS_PER_TASK", "150"))


@st.cache_data(ttl=86400, show_spinner=False)
def _resolve_reference(ref_text: str):
    return process_single_reference_new(ref_text, 1, 1, {}, {})


def _secret(name: str) -> str:
    try:
        return str(st.secrets.get(name, "") or "")
    except Exception:
        return os.getenv(name, "")


@st.cache_resource(show_spinner=False)
def _make_quota_store(url: str, key: str, pepper: str) -> QuotaStore:
    return QuotaStore(url, key, pepper)


def _quota_store() -> QuotaStore:
    return _make_quota_store(
        _secret("SUPABASE_URL"),
        _secret("SUPABASE_SERVICE_ROLE_KEY"),
        _secret("ACCESS_CODE_PEPPER"),
    )


def _download_name(project_id: str, extension: str) -> str:
    prefix = "".join(c for c in (project_id or "citation_screening") if c.isalnum() or c in "-_")
    return f"{prefix or 'citation_screening'}_{datetime.now().strftime('%Y%m%d_%H%M')}.{extension}"


def _login_panel() -> bool:
    user = st.session_state.get("screening_user")
    if user:
        return True
    lock_until = st.session_state.get("screening_login_lock_until", 0)
    if lock_until > time.time():
        st.error(f"访问码错误次数过多，请在 {int(lock_until - time.time()) + 1} 秒后重试。")
        return False
    st.warning("引用内容初筛仅向持有访问码的用户开放。")
    with st.form("screening_access_form"):
        access_code = st.text_input("访问码", type="password", autocomplete="off")
        submitted = st.form_submit_button("进入引用初筛", type="primary", use_container_width=True)
    if submitted:
        try:
            authenticated = _quota_store().authenticate(access_code)
            st.session_state["screening_user"] = authenticated
            st.session_state["screening_login_attempts"] = 0
            st.session_state.pop("screening_login_lock_until", None)
            st.rerun()
        except QuotaStoreError as exc:
            attempts = st.session_state.get("screening_login_attempts", 0) + 1
            st.session_state["screening_login_attempts"] = attempts
            if attempts >= 5:
                st.session_state["screening_login_lock_until"] = time.time() + 300
                st.session_state["screening_login_attempts"] = 0
            st.error(str(exc))
    return False


def _show_results(data, project_id: str) -> None:
    stats = data["statistics"]
    st.divider()
    col1, col2, col3 = st.columns(3)
    col1.metric("匹配", stats.get("匹配", 0))
    col2.metric("存疑", stats.get("存疑", 0))
    col3.metric("领域不符", stats.get("领域不符", 0))
    st.caption(f"预计调用 {data.get('estimated_calls', 0)} 次；实际发出 {data.get('actual_calls', 0)} 次 DeepSeek 请求。")

    rows = [{
        "前文": item.get("context_before", ""),
        "目标引用句": item["sentence_text"],
        "后文": item.get("context_after", ""),
        "Ref.": item["label"],
        "文献题名": item["title"],
        "结果": item["result"],
        "简要理由": item["reason"],
    } for item in data["results"]]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    export = {key: value for key, value in data.items() if key != "html"}
    col_html, col_json = st.columns(2)
    col_html.download_button("下载 HTML 报告", data=data["html"].encode("utf-8"), file_name=_download_name(project_id, "html"), mime="text/html", use_container_width=True)
    col_json.download_button("下载 JSON 数据", data=json.dumps(export, ensure_ascii=False, indent=2).encode("utf-8"), file_name=_download_name(project_id, "json"), mime="application/json", use_container_width=True)


def _show_recent_tasks(store: QuotaStore, user) -> None:
    with st.expander("最近任务（刷新页面后可在这里找回结果）", expanded=True):
        st.button("刷新任务状态", key="refresh_screening_tasks")
        try:
            tasks = store.recent_tasks(user["id"])
        except QuotaStoreError as exc:
            st.warning(str(exc))
            return
        if not tasks:
            st.caption("最近 24 小时没有任务。")
            return
        labels = {"running": "处理中", "completed": "已完成", "failed": "失败"}
        for task in tasks:
            filename = task.get("display_filename") or "未命名稿件"
            status = task.get("status", "running")
            st.markdown(f"**{filename}** · {labels.get(status, status)} · 预计 {task.get('estimated_calls', 0)} 次")
            if status == "running":
                st.caption("后台仍在处理。稍后点击“刷新任务状态”即可查看结果。")
            elif status == "failed":
                st.error(task.get("error_message") or "任务失败。")
            elif task.get("result_payload"):
                payload = task["result_payload"]
                project = os.path.splitext(filename)[0]
                html_col, json_col = st.columns(2)
                html_col.download_button(
                    "下载 HTML 报告", data=(task.get("report_html") or "").encode("utf-8"),
                    file_name=_download_name(project, "html"), mime="text/html",
                    use_container_width=True, key=f"history_html_{task['task_id']}",
                )
                json_col.download_button(
                    "下载 JSON 数据", data=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
                    file_name=_download_name(project, "json"), mime="application/json",
                    use_container_width=True, key=f"history_json_{task['task_id']}",
                )
            st.divider()


def show_citation_screening(system_status) -> None:
    st.subheader("引用内容初筛")
    st.caption("上传 Word 或 NLM XML，根据题名和摘要，用 DeepSeek 给出匹配、存疑或领域不符三类初筛结果。")
    st.info("目标引用句会连同前后句和所在段落一起发送，用于减少脱离语境造成的误判。", icon="ℹ️")
    try:
        store = _quota_store()
    except QuotaStoreError as exc:
        st.error(f"管理员配置错误：{exc}")
        return
    if not _login_panel():
        return

    user = st.session_state["screening_user"]
    try:
        quota = store.quota_status(user["id"])
    except QuotaStoreError as exc:
        st.error(str(exc))
        return
    user_col, quota_col, logout_col = st.columns([2, 2, 1])
    user_col.metric("当前用户", user["display_name"])
    quota_col.metric("今日剩余额度", f"{quota['remaining']} / {quota['daily_limit']}")
    if logout_col.button("退出", key="screening_logout", use_container_width=True):
        st.session_state.pop("screening_user", None)
        st.session_state.pop("citation_screening_prepared", None)
        st.rerun()

    _show_recent_tasks(store, user)

    project_id = st.text_input("项目名称（选填）", key="screen_project_id", placeholder="例如：MyPaper-2026-001")
    uploaded = st.file_uploader("上传稿件", type=["docx", "xml"], key="screen_manuscript")
    ref_input = st.text_area(
        "参考文献列表", key="screen_references", height=220,
        placeholder="Word 稿件请每行粘贴一条参考文献。NLM XML 会优先读取内嵌 <ref-list>，此处可留空。",
        help="Word 必填；NLM XML 可选。参考文献顺序需要与正文编号一致。",
    )

    if st.button("分析稿件并估算调用量", type="primary", use_container_width=True, key="prepare_citation_screening"):
        if uploaded is None:
            st.warning("请先上传 Word 或 NLM XML 稿件。")
        elif uploaded.size > MAX_FILE_BYTES:
            st.warning("文件超过 10 MB 上限。")
        elif uploaded.name.lower().endswith(".docx") and not ref_input.strip():
            st.warning("Word 稿件需要粘贴参考文献列表。")
        elif not system_status["lock"].acquire(blocking=False):
            st.warning("当前处理任务较多，请稍后重试。")
        else:
            progress = st.progress(0, text="准备开始…")
            try:
                prepared = prepare_screening(
                    uploaded.getvalue(), uploaded.name, ref_input,
                    reference_resolver=_resolve_reference,
                    progress_callback=lambda pct, msg: progress.progress(pct, text=msg),
                    max_workers=3,
                )
                prepared["task_id"] = str(uuid.uuid4())
                prepared["filename_hash"] = hashlib.sha256(uploaded.getvalue()).hexdigest()
                prepared["project_id"] = project_id
                st.session_state["citation_screening_prepared"] = prepared
                st.session_state.pop("citation_screening_result", None)
                st.success("分析完成；此阶段没有调用 DeepSeek，也没有扣除额度。")
            except Exception as exc:
                st.error(f"稿件分析失败：{exc}")
            finally:
                system_status["lock"].release()

    prepared = st.session_state.get("citation_screening_prepared")
    if prepared:
        st.divider()
        st.markdown("#### 运行前确认")
        cols = st.columns(4)
        cols[0].metric("引用位置", prepared["citation_locations"])
        cols[1].metric("判断组合", prepared["total_pairs"])
        cols[2].metric("无需 AI，直接存疑", prepared["direct_doubt"])
        cols[3].metric("预计 DeepSeek 调用", prepared["estimated_calls"])
        estimated = prepared["estimated_calls"]
        if estimated > MAX_CALLS_PER_TASK:
            st.error(f"预计调用 {estimated} 次，超过单次任务上限 {MAX_CALLS_PER_TASK} 次。请拆分稿件。")
        elif estimated > quota["remaining"]:
            st.error(f"预计需要 {estimated} 次，但今日只剩 {quota['remaining']} 次额度。")
        else:
            st.warning(f"确认后将预扣 {estimated} 次额度，预计剩余 {quota['remaining'] - estimated} 次。")
            if st.button(f"确认消耗 {estimated} 次并开始初筛", type="primary", use_container_width=True, key=f"execute_{prepared['task_id']}"):
                reservation = store.reserve(
                    user["id"], prepared["task_id"], estimated,
                    prepared["filename_hash"], prepared["filename"],
                )
                if not reservation.get("allowed"):
                    st.error(reservation.get("message", "额度不足或任务已经提交。"))
                elif not system_status["lock"].acquire(blocking=False):
                    store.settle(prepared["task_id"], 0, succeeded=False)
                    st.warning("当前处理任务较多，已退回预扣额度，请稍后重新分析稿件。")
                    st.session_state.pop("citation_screening_prepared", None)
                else:
                    progress = st.progress(0, text="开始调用 DeepSeek…")
                    try:
                        data = execute_screening(prepared, lambda pct, msg: progress.progress(pct, text=msg), max_workers=3)
                        st.session_state["citation_screening_result"] = data
                        st.session_state.pop("citation_screening_prepared", None)
                        try:
                            store.complete_task(prepared["task_id"], data["actual_calls"], data)
                            st.success("初筛完成，结果已保存 24 小时，额度已按实际请求次数结算。")
                        except QuotaStoreError:
                            st.warning("初筛已完成并保留在当前页面，但云端结果保存暂时失败；请立即下载，并联系管理员核对额度。")
                    except Exception as exc:
                        refunded = True
                        try:
                            store.fail_task(prepared["task_id"], 0, str(exc))
                        except QuotaStoreError:
                            refunded = False
                        st.session_state.pop("citation_screening_prepared", None)
                        suffix = "预扣额度已退回" if refunded else "额度退款暂时失败，请联系管理员"
                        st.error(f"初筛失败，{suffix}：{exc}")
                    finally:
                        system_status["lock"].release()

    data = st.session_state.get("citation_screening_result")
    if data:
        _show_results(data, project_id or st.session_state.get("screen_project_id", ""))

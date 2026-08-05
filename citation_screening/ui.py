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
from .fulltext_review import execute_fulltext_review, prepare_fulltext_review
from .parsers.common import split_reference_lines
from .pipeline import execute_screening, prepare_screening
from .reports import build_html_report


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


def _download_result_buttons(data, project_id: str, key_prefix: str) -> None:
    export = {key: value for key, value in data.items() if key != "html"}
    version_label = "最终" if data.get("review_stage") == "fulltext" else "当前"
    col_html, col_json = st.columns(2)
    col_html.download_button(
        f"下载{version_label} HTML 报告", data=data["html"].encode("utf-8"),
        file_name=_download_name(project_id, "html"), mime="text/html",
        use_container_width=True, key=f"{key_prefix}_html",
    )
    col_json.download_button(
        f"下载{version_label} JSON 数据", data=json.dumps(export, ensure_ascii=False, indent=2).encode("utf-8"),
        file_name=_download_name(project_id, "json"), mime="application/json",
        use_container_width=True, key=f"{key_prefix}_json",
    )


def _show_results(data, project_id: str) -> None:
    stats = data["statistics"]
    st.divider()
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("匹配", stats.get("匹配", 0))
    col2.metric("存疑", stats.get("存疑", 0))
    col3.metric("领域不符", stats.get("领域不符", 0))
    col4.metric("未获取数据", stats.get("未获取数据", 0))
    col5.metric("引用无关内容", stats.get("引用无关内容", 0))
    st.caption(f"预计调用 {data.get('estimated_calls', 0)} 次；实际发出 {data.get('actual_calls', 0)} 次 DeepSeek 请求。")

    rows = [{
        "前文": item.get("context_before", ""),
        "目标引用句": item["sentence_text"],
        "后文": item.get("context_after", ""),
        "Ref.": item["label"],
        "文献题名": item["title"],
        "结果": item["result"],
        "全文复核": "已复核" if item.get("fulltext_paragraphs_checked") is not None else "",
        "简要理由": item["reason"],
    } for item in data["results"]]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    _download_result_buttons(data, project_id, "current_result")


def _show_fulltext_review(store: QuotaStore, user, system_status, data, project_id: str, quota):
    """Offer a separately metered, open-full-text pass for doubtful rows only."""
    doubtful = sum(1 for item in data.get("results", []) if item.get("result") == "存疑")
    if not doubtful:
        return data
    try:
        quota = store.quota_status(user["id"])
    except QuotaStoreError:
        pass
    st.divider()
    st.markdown("#### 下一步（可选）：核查存疑项全文")
    st.caption(
        "只查找合法开放获取的 PMC/Europe PMC XML 或开放 PDF；PDF 会在本地提取文本。"
        "系统先选出最多 8 个高相关段落，再逐段交给 DeepSeek，找到明确支持证据后立即停止。"
    )
    prepared = st.session_state.get("citation_fulltext_prepared")
    if prepared is None and st.button(
        f"继续核查 {doubtful} 个存疑项",
        use_container_width=True,
        key="prepare_fulltext_review",
    ):
        if not system_status["lock"].acquire(blocking=False):
            st.warning("当前处理任务较多，请稍后重试。")
        else:
            progress = st.progress(0, text="正在查找开放全文……")
            try:
                prepared = prepare_fulltext_review(
                    data,
                    progress_callback=lambda pct, msg: progress.progress(pct, text=msg),
                    max_paragraphs=8,
                    max_workers=3,
                )
                prepared["task_id"] = str(uuid.uuid4())
                prepared["filename_hash"] = hashlib.sha256(
                    f"{prepared['filename']}:{prepared['task_id']}:fulltext".encode("utf-8")
                ).hexdigest()
                prepared["project_id"] = project_id
                st.session_state["citation_fulltext_prepared"] = prepared
                st.success("已找到可核查的全文段落。请确认下面的最大调用量后继续。")
            except Exception as exc:
                st.error(f"开放全文准备失败：{exc}")
            finally:
                system_status["lock"].release()

    prepared = st.session_state.get("citation_fulltext_prepared")
    if not prepared:
        return data
    cols = st.columns(4)
    cols[0].metric("存疑项", prepared["doubtful_count"])
    cols[1].metric("找到开放全文", prepared["fulltexts_found"])
    cols[2].metric("无法取得/提取", prepared["unavailable_count"])
    cols[3].metric("最多调用", prepared["estimated_calls"])
    estimated = prepared["estimated_calls"]
    st.caption("这是上限；每个引用一旦找到支持证据就停止，实际调用量通常更少，并按实际次数结算。")
    if estimated == 0:
        st.info("没有找到可用于复核的开放全文，因此不会调用 DeepSeek。原结果保持不变。")
        st.session_state.pop("citation_fulltext_prepared", None)
    elif estimated > MAX_CALLS_PER_TASK:
        st.error(f"最多需要 {estimated} 次调用，超过单次任务上限 {MAX_CALLS_PER_TASK} 次。")
    elif estimated > quota["remaining"]:
        st.error(f"最多需要 {estimated} 次调用，但今日只剩 {quota['remaining']} 次额度。")
    elif st.button(
        f"开始全文核查（最多 {estimated} 次调用）",
        type="primary",
        use_container_width=True,
        key=f"execute_fulltext_{prepared['task_id']}",
    ):
        reservation = store.reserve(
            user["id"], prepared["task_id"], estimated,
            prepared["filename_hash"], f"{prepared['filename']}（全文复核）",
        )
        if not reservation.get("allowed"):
            st.error(reservation.get("message", "额度不足或任务已经提交。"))
        elif not system_status["lock"].acquire(blocking=False):
            store.settle(prepared["task_id"], 0, succeeded=False)
            st.warning("当前任务较多，预扣额度已退回，请稍后重新准备。")
            st.session_state.pop("citation_fulltext_prepared", None)
        else:
            progress = st.progress(0, text="开始逐段复核……")
            try:
                reviewed = execute_fulltext_review(
                    prepared,
                    progress_callback=lambda pct, msg: progress.progress(pct, text=msg),
                    max_workers=3,
                )
                st.session_state["citation_screening_result"] = reviewed
                st.session_state.pop("citation_fulltext_prepared", None)
                try:
                    store.complete_task(prepared["task_id"], reviewed["actual_calls"], reviewed)
                    st.success("全文核查完成。下方是已经更新的最终报告，可直接下载；结果同时保存 24 小时。")
                except QuotaStoreError:
                    st.warning("全文复核已完成并保留在当前页面，但云端保存暂时失败；请立即下载结果并联系管理员核对额度。")
                _download_result_buttons(reviewed, project_id, f"fulltext_final_{prepared['task_id']}")
                return reviewed
            except Exception as exc:
                try:
                    store.fail_task(prepared["task_id"], 0, str(exc))
                except QuotaStoreError:
                    pass
                st.session_state.pop("citation_fulltext_prepared", None)
                st.error(f"全文复核失败：{exc}")
            finally:
                system_status["lock"].release()
    return st.session_state.get("citation_screening_result", data)


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
                latest_html = build_html_report(
                    payload.get("filename") or filename,
                    payload.get("results", []),
                )
                html_col, json_col = st.columns(2)
                html_col.download_button(
                    "下载 HTML 报告", data=latest_html.encode("utf-8"),
                    file_name=_download_name(project, "html"), mime="text/html",
                    use_container_width=True, key=f"history_html_{task['task_id']}",
                )
                json_col.download_button(
                    "下载 JSON 数据", data=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
                    file_name=_download_name(project, "json"), mime="application/json",
                    use_container_width=True, key=f"history_json_{task['task_id']}",
                )
                if st.button(
                    "在当前页面打开此结果",
                    key=f"history_open_{task['task_id']}",
                    use_container_width=True,
                ):
                    payload["html"] = latest_html
                    st.session_state["citation_screening_result"] = payload
                    st.session_state.pop("citation_fulltext_prepared", None)
                    st.rerun()
            st.divider()


def show_citation_screening(system_status) -> None:
    st.subheader("引用内容初筛")
    st.caption("上传 Word 或 NLM XML，根据题名和摘要，用 DeepSeek 给出匹配、存疑或领域不符；没有取得摘要的项目单列为未获取数据。")
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
        st.session_state.pop("citation_fulltext_prepared", None)
        st.rerun()

    _show_recent_tasks(store, user)

    project_id = st.text_input("项目名称（选填）", key="screen_project_id", placeholder="例如：MyPaper-2026-001")
    uploaded = st.file_uploader("上传稿件", type=["docx", "xml"], key="screen_manuscript")
    ref_input = st.text_area(
        "参考文献列表", key="screen_references", height=220,
        placeholder="Word 稿件请每行粘贴一条参考文献。NLM XML 会优先读取内嵌 <ref-list>，此处可留空。",
        help="Word 必填；NLM XML 可选。参考文献顺序需要与正文编号一致。",
    )

    count_col, count_hint_col = st.columns([1, 4])
    if count_col.button("点击统计条数", key="count_screen_references"):
        st.session_state["screen_ref_count"] = len(split_reference_lines(ref_input))
        st.session_state["screen_ref_count_for"] = ref_input
    stored_count = st.session_state.get("screen_ref_count")
    if stored_count is not None and st.session_state.get("screen_ref_count_for") == ref_input:
        if stored_count:
            count_hint_col.caption(f"识别到 **{stored_count}** 条参考文献。")
        else:
            count_hint_col.caption("未识别到参考文献，请确认每行粘贴一条。")

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
        cols[2].metric("未获取摘要", prepared["direct_doubt"])
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
        active_project = project_id or st.session_state.get("screen_project_id", "")
        result_area = st.empty()
        data = _show_fulltext_review(
            store, user, system_status, data,
            active_project, quota,
        )
        with result_area.container():
            _show_results(data, active_project)

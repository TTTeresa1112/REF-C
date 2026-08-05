"""
参考文献核查工具 - Streamlit Web 应用
支持 24 小时缓存机制，用户粘贴参考文献后按行识别并生成 HTML 报告
"""

import streamlit as st
import hashlib
import json
import tempfile
import os
import time
import datetime
import random
import threading
import pandas as pd

# 导入核心处理函数
from generate_json import (
    process_single_reference_new, 
    find_fuzzy_duplicates, 
    calculate_statistics,
    extract_doi_from_text
)
from generate_html import generate_html_report
from citation_screening.ui import show_citation_screening

# 页面配置
st.set_page_config(
    page_title="REF-C 参考文献核验",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# 自定义样式（简洁风格）
st.markdown("""
<style>
    /* 隐藏侧边栏，保持单栏布局 */
    [data-testid="stSidebar"] { display: none; }

    /* 顶部标题区 */
    .hero {
        padding: 1.5rem 0 0.5rem 0;
    }
    .hero h1 {
        font-size: 2.2rem;
        font-weight: 700;
        letter-spacing: 0.5px;
        margin: 0 0 0.3rem 0;
        color: #1f2933;
    }
    .hero p {
        margin: 0;
        color: #64748b;
        font-size: 1.05rem;
    }

    /* 标签页样式 */
    .stTabs [data-baseweb="tab-list"] {
        gap: 0.6rem;
        border-bottom: 1px solid #e2e8f0;
    }
    .stTabs [data-baseweb="tab"] {
        height: 2.4rem;
        padding: 0 1.1rem;
        font-size: 0.95rem;
    }

    /* 输入框使用等宽字体，方便核对 */
    .stTextArea textarea {
        font-family: 'Consolas', 'Monaco', monospace;
        font-size: 13px;
    }

    /* 按钮圆角 */
    .stButton > button {
        border-radius: 8px;
    }
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def get_system_status():
    """创建全局共享状态，包含信号量、任务开始时间和取消标志"""
    return {
        "lock": threading.Semaphore(3),  # 允许最多3人同时查询
        "start_time": None,
        "active_users": 0,               # 当前活跃用户数
        "cancel_requested": False
    }


def get_text_hash(text: str) -> str:
    """计算文本的 MD5 哈希值作为缓存键"""
    return hashlib.md5(text.encode('utf-8')).hexdigest()


# 使用 24 小时缓存 (86400 秒)
@st.cache_data(ttl=86400, show_spinner=False)
def process_single_ref_cached(ref_text: str, ref_hash: str) -> dict:
    """
    缓存单条参考文献的处理结果
    如果上次处理超时（timeout_error=True），则不缓存，下次重新请求
    
    Args:
        ref_text: 参考文献原文
        ref_hash: 用于缓存键的哈希值
        
    Returns:
        处理结果字典
    """
    all_authors_count = {}
    all_doi_count = {}
    result = process_single_reference_new(ref_text, 1, 1, all_authors_count, all_doi_count)
    
    # 如果该条目超时，抛出异常使 st.cache_data 不缓存此结果
    if result.get('timeout_error', False):
        raise Exception("TIMEOUT_NO_CACHE")
    
    return result


def process_references(refs: list) -> tuple:
    """
    处理参考文献列表
    
    Args:
        refs: 参考文献列表
        
    Returns:
        (results, stats) 元组
    """
    total = len(refs)
    results = []
    all_authors_count = {}
    all_doi_count = {}
    
    # 创建进度条和状态显示
    progress_bar = st.progress(0)
    status_container = st.empty()
    
    for idx, ref in enumerate(refs, 1):
        # 检查是否被取消
        system_status = get_system_status()
        if system_status["cancel_requested"]:
            status_container.warning(f"任务已被中断，已处理 {idx-1}/{total} 条")
            break
        
        status_container.info(f"正在处理 {idx}/{total}：{ref[:50]}...")
        
        # 计算该条参考文献的哈希
        ref_hash = get_text_hash(ref)
        
        # 使用缓存处理
        try:
            result = process_single_ref_cached(ref, ref_hash)
        except Exception as e:
            if "TIMEOUT_NO_CACHE" in str(e):
                # 超时的条目：构造临时结果，标记 timeout_error
                result = {
                    "original_text": ref,
                    "extracted_doi": "",
                    "api_doi": "",
                    "match_status": "None",
                    "has_retraction": False,
                    "has_correction": False,
                    "title": "",
                    "journal": "",
                    "year": "",
                    "all_authors": [],
                    "pmid": "",
                    "pmcid": "",
                    "is_recent_5_years": False,
                    "is_recent_3_years": False,
                    "ai_diagnosis": "",
                    "ai_extracted_title": "",
                    "ai_extracted_url": "",
                    "ai_search_query": "",
                    "timeout_error": True,
                    "matched_ref": "Not Found",
                    "similarity": 0,
                }
            else:
                raise
        results.append(result)
        
        # 更新全局计数器（用于高频作者统计）
        for author_name in result.get('all_authors', []):
            if author_name:
                all_authors_count[author_name] = all_authors_count.get(author_name, 0) + 1
        
        api_doi = result.get('api_doi', '')
        if api_doi:
            all_doi_count[api_doi] = all_doi_count.get(api_doi, 0) + 1
        
        # 更新进度
        progress_bar.progress(idx / total)
        
        # 适当延迟避免 API 限速（缓存命中时不需要延迟）
        time.sleep(0.5 + random.uniform(0, 0.5))
    
    status_container.success(f"处理完成！共处理 {total} 条参考文献")
    
    # 执行模糊查重
    with st.spinner("正在进行查重分析..."):
        duplicate_info, fuzzy_pairs = find_fuzzy_duplicates(results)
        for index, info in duplicate_info.items():
            if index < len(results):
                results[index]['fuzzy_duplicates'] = info
    
    # 计算统计信息
    stats = calculate_statistics(results, total, fuzzy_pairs)
    
    return results, stats


def display_dashboard(stats: dict):
    """显示统计仪表板"""
    st.subheader("统计概览")
    
    # 第一行：核心指标
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("总参考文献", stats.get('total_references', 0))
    with col2:
        val = stats.get('matched_refs', 0)
        pct = stats.get('matched_refs_pct', 0)
        st.metric("匹配成功", f"{val} ({pct:.1f}%)")
    with col3:
        val = stats.get('with_doi', 0)
        pct = stats.get('with_doi_pct', 0)
        st.metric("有DOI", f"{val} ({pct:.1f}%)")
    with col4:
        val = stats.get('recent_5_years', 0)
        pct = stats.get('recent_5_years_pct', 0)
        st.metric("近5年", f"{val} ({pct:.1f}%)")
    with col5:
        val = stats.get('recent_3_years', 0)
        pct = stats.get('recent_3_years_pct', 0)
        st.metric("近3年", f"{val} ({pct:.1f}%)")
    
    # 第二行：风险指标
    col6, col7, col8, col9, col10 = st.columns(5)
    with col6:
        val = stats.get('inappropriate_count', 0)
        pct = stats.get('inappropriate_pct', 0)
        st.metric("不合适引用", f"{val} ({pct:.1f}%)", delta_color="inverse")
    with col7:
        val = stats.get('high_risk_count', 0)
        pct = stats.get('high_risk_pct', 0)
        st.metric("AI无法判断", f"{val} ({pct:.1f}%)", delta_color="inverse")
    with col8:
        val = stats.get('doi_mismatch_count', 0)
        pct = stats.get('doi_mismatch_pct', 0)
        st.metric("DOI不符", f"{val} ({pct:.1f}%)", delta_color="inverse")
    with col9:
        val = stats.get('duplicate_refs', 0)
        pct = stats.get('duplicate_refs_pct', 0)
        st.metric("DOI重复", f"{val} ({pct:.1f}%)", delta_color="inverse")
    with col10:
        val = stats.get('fuzzy_duplicate_pairs', 0)
        pct = stats.get('fuzzy_duplicate_pct', 0)
        st.metric("可能重复", f"{val} ({pct:.1f}%)", delta_color="inverse")


def generate_and_offer_download(results: list, stats: dict, project_id: str = ""):
    """生成 HTML 报告并提供下载"""
    st.subheader("下载报告")
    
    # 为结果添加缺失字段
    for res in results:
        res.setdefault('fuzzy_duplicates', '')
        res.pop('cleaned_original_ref', None)
    
    # 创建临时 JSON 文件
    output_data = {
        "statistics": stats,
        "results": results
    }
    
    # 使用临时目录
    temp_dir = tempfile.mkdtemp()
    json_path = os.path.join(temp_dir, "temp_cache.json")
    
    try:
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
        
        # 生成 HTML 报告
        html_path = generate_html_report(json_path)
        
        # 读取 HTML 内容
        with open(html_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        # 生成文件名：如果有项目ID则使用 ID_年月日时分，否则只用时间戳
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M')
        if project_id.strip():
            # 清理项目ID中的非法字符
            safe_id = "".join(c for c in project_id.strip() if c.isalnum() or c in '-_')
            file_prefix = f"{safe_id}_{timestamp}"
        else:
            file_prefix = f"report_{timestamp}"
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.download_button(
                label="下载 HTML 报告",
                data=html_content,
                file_name=f"{file_prefix}.html",
                mime="text/html",
                type="primary",
                use_container_width=True
            )
        
        with col2:
            # 也提供 JSON 下载
            json_content = json.dumps(output_data, ensure_ascii=False, indent=2)
            st.download_button(
                label="下载 JSON 数据",
                data=json_content,
                file_name=f"{file_prefix}_cache.json",
                mime="application/json",
                use_container_width=True
            )
        
    finally:
        # 清理临时文件
        try:
            if os.path.exists(json_path):
                os.unlink(json_path)
            if os.path.exists(html_path):
                os.unlink(html_path)
            os.rmdir(temp_dir)
        except:
            pass


def display_results_table(results: list):
    """显示结果预览表格"""
    st.subheader("结果预览")

    # 状态背景色与中英文映射
    status_colors = {
        "不合适": "#f8d7da",
        "DOI不符": "#fff3cd",
        "DOI重复": "#f8d7da",
        "可能重复": "#fff3cd",
        "AI无法判断": "#e2e3e5",
        "通过": "#d4edda",
        "未匹配": "#e2e3e5",
    }
    match_map = {
        "match": "匹配成功",
        "doi_mismatch": "DOI不符",
    }
    diag_map = {
        "BOOK": "书籍",
        "CONF": "会议论文",
        "PREPRINT": "预印本",
        "WEBSITE": "网页",
        "PATENT": "专利",
        "HIGH_RISK": "高风险",
        "UNKNOWN": "无法判断",
    }

    # 构建表格数据
    table_data = []
    for idx, item in enumerate(results, 1):
        # 计算状态（纯文字）
        if item.get('has_retraction') or item.get('is_retraction_notice') or item.get('has_correction') or item.get('is_erratum_notice'):
            status = "不合适"
        elif item.get('match_status') == 'doi_mismatch':
            status = "DOI不符"
        elif item.get('is_doi_duplicate'):
            status = "DOI重复"
        elif item.get('fuzzy_duplicates'):
            status = "可能重复"
        elif item.get('ai_diagnosis') == 'HIGH_RISK':
            status = "AI无法判断"
        elif item.get('match_status') == 'match':
            status = "通过"
        else:
            status = "未匹配"

        original = item.get('original_text', '')
        table_data.append({
            "#": idx,
            "状态": status,
            "参考文献": (original[:100] + "..." if len(original) > 100 else original),
            "DOI": item.get('api_doi', '') or item.get('extracted_doi', '') or "-",
            "匹配": match_map.get(item.get('match_status', ''), "未匹配"),
            "文献类型": diag_map.get(item.get('ai_diagnosis', ''), "-")
        })

    df = pd.DataFrame(table_data)

    # 按状态给「状态」列着色
    def _color_status(val):
        color = status_colors.get(val, "")
        return f"background-color: {color}" if color else ""

    styler = df.style
    if hasattr(styler, "map"):
        styler = styler.map(_color_status, subset=["状态"])
    else:
        styler = styler.applymap(_color_status, subset=["状态"])

    st.dataframe(
        styler,
        use_container_width=True,
        hide_index=True,
        column_config={
            "#": st.column_config.NumberColumn(width="small"),
            "状态": st.column_config.TextColumn(width="small"),
            "参考文献": st.column_config.TextColumn(width="large"),
            "DOI": st.column_config.TextColumn(width="medium"),
            "匹配": st.column_config.TextColumn(width="small"),
            "文献类型": st.column_config.TextColumn(width="small")
        }
    )


def show_guide():
    """显示使用说明"""
    st.markdown("""
**它怎么核查文献：**
REF-C 优先看参考文献有没有 DOI 号。有 DOI 的，会用它去数据库里调取对应信息，再和你提供的题目、年份等对照，判断文献是否真实；没有 DOI 的，就直接拿你提供的文献信息去数据库里匹配核验。

**怎么用：**
1. **填写项目名称（选填）：** 用于命名导出文件，方便区分不同的核查结果。
2. **录入参考文献：** 把参考文献列表粘贴到输入框里。每行一条，带不带数字编号都可以（如 1、[1]、(1)），REF-C 能自动识别。
3. **开始核验：** 点「开始核验」按钮，REF-C 就开始逐条核验，页面会显示进度。
4. **查看报告：** 核验完成后，下载 HTML 格式的报告，用浏览器打开就能看。
5. **清空缓存：** 每条文献的处理结果会暂存 24 小时。因为要调外部数据库，网络不稳时输出可能出错，遇到报错先点「清空缓存」再重新核验，就能拿到最新数据。

**输出结果怎么看：** 1-5 项是统计信息，6-10 项需要打开 HTML 报告人工复核，确实有问题就得修改。
1. **总项：** 参考文献总数
2. **匹配成功：** 数据库里能找到、且和作者提供信息完全一致的文献数量及占比
3. **有DOI：** 能检索到 DOI 号的文献数量及占比
4. **近5年：** 近 5 年内发表的文献数量及占比
5. **近3年：** 近 3 年内发表的文献数量及占比
6. **不适合引用：** 更正、被更正、撤稿、被撤稿文章的数量及占比
7. **AI无法判断：** 文献结构存疑，无法识别是网页、预印本、专利、书籍、期刊还是会议论文
8. **DOI不符：** 用 DOI 号查到的文献信息和作者提供的题目等信息对不上
9. **DOI重复：** 两篇或多篇文献对应同一个 DOI 号
10. **可能重复：** 对比两篇或多篇文献的题目、作者、期卷号、页码等，相似度超过 80% 就视为可能重复

**引用内容初筛（检查正文引用是否靠谱）：**
这是进阶功能，用来核对正文里引用的内容，是否真的能被对应的参考文献支撑。需要访问码才能使用，想要试用的话，联系我：teresaleeovo@gmail.com

怎么用：
1. **登录：** 输入管理员发给你的访问码。
2. **上传稿件：** 上传 Word 或 NLM XML 稿件。Word 稿件还需要把参考文献粘贴到输入框，每行一条；输入框下方有「点击统计条数」按钮，随时可以看看识别了多少条。
3. **分析稿件：** 点「分析稿件并估算调用量」，系统会解析正文引用、匹配文献、去公开数据库拉题名和摘要，并估算要调用几次 AI。这一步不扣额度。
4. **确认执行：** 确认后才会预扣额度并开始调用 AI。没拿到摘要的条目不调用 AI，直接标成「未获取数据」。
5. **看结果：** 每个引用位置会得到四种判断之一——
   - **匹配：** 摘要能支撑正文的说法
   - **存疑：** 信息不足或对不上，需要人工复核
   - **领域不符：** 文献主题、对象和正文明显无关
   - **引用无关内容：** 其实是作者单位、邮箱、ORCID 之类的文头编号，不是真引用，也不影响占比统计
6. **全文复核（可选）：** 对「存疑」条目可以继续查找合法开放全文，逐段让 AI 核对，找到支持证据就自动停止，只按实际调用次数扣额度。
7. **英文作者报告（可选）：** 可以把「存疑」和「领域不符」的条目生成英文反馈报告（Reference Check Report），直接发给作者。原文句子保持不变，AI 只负责润色英文说明和修改建议。
8. **额度：** 每天有一定次数。分析稿件不扣，确认执行才扣，按实际使用次数结算，没用完的会自动退回。
""")


def show_about():
    """显示项目介绍、开源许可与免责声明"""
    st.markdown("""
**REF-C 是什么：**

REF-C 是一个开源的参考文献核验工具。它通过 DOI 号与公开学术数据库自动匹配，帮助核对参考文献的真实性与规范性，并生成可下载的 HTML 核查报告。

**开源许可：**

本项目基于 **MIT** 协议开源，欢迎自由使用、修改与分发。

**免责声明：**

核查结果仅供参考。数据库信息可能不全或更新不及时，结果难免有不精准的地方，最终请以人工复核为准。
""")


def main():
    """主函数"""
    # 全局共享状态（仅用于并发保护，不提供管理界面）
    system_status = get_system_status()

    # --- 顶部标题区 ---
    st.markdown(
        '<div class="hero"><h1>REF-C 参考文献核验</h1>'
        '<p>粘贴参考文献，自动匹配数据库核验，一键生成 HTML 核查报告</p></div>',
        unsafe_allow_html=True
    )

    # --- 标签页：参考文献核验 / 引用内容初筛 / 使用说明 / 关于 ---
    tab_check, tab_screen, tab_guide, tab_about = st.tabs(
        ["开始核验", "引用内容初筛", "使用说明", "关于"]
    )

    with tab_screen:
        show_citation_screening(system_status)

    # ===== 使用说明 =====
    with tab_guide:
        show_guide()

    # ===== 关于 =====
    with tab_about:
        show_about()

    # ===== 开始核验 =====
    with tab_check:
        # 项目名称（选填，用于命名导出文件）
        project_id = st.text_input(
            "项目名称（选填，用于命名导出文件）",
            placeholder="例如：MyPaper-2026-001",
            help="输入后，导出的文件名将形如：项目名称_年月日时分.html"
        )

        # 输入区
        with st.container(border=True):
            st.markdown("**参考文献列表**（每行一条，带不带数字编号均可）")
            count_placeholder = st.empty()

            ref_input = st.text_area(
                "请粘贴参考文献（每行一条）：",
                height=280,
                placeholder="""示例：
1. Smith, J., & Johnson, A. (2023). Example article title. Journal of Examples, 15(3), 123-145. https://doi.org/10.1234/example.2023
2. Brown, M. (2022). Another research paper. Science Today, 8(2), 56-78.
3. Davis, K., et al. (2021). Important findings in research. Nature Reviews, 10(1), 1-20.""",
                label_visibility="collapsed"
            )

            # 实时统计有效条目数（去掉空行）
            current_ref_count = len([line for line in ref_input.strip().split('\n') if line.strip()])
            if current_ref_count > 0:
                count_placeholder.markdown(f"已识别到 **{current_ref_count}** 条有效参考文献")
            else:
                count_placeholder.caption("提示：输入后点击页面空白处，即可统计条目数。")

            # 操作按钮
            col1, col2 = st.columns([2, 1])
            with col1:
                process_btn = st.button("开始核验", type="primary", use_container_width=True)
            with col2:
                clear_btn = st.button("清空缓存", use_container_width=True)

            if clear_btn:
                st.cache_data.clear()
                st.success("缓存已清空！")

            # 存储项目名称到 session_state
            if project_id:
                st.session_state['project_id'] = project_id

        # --- 处理逻辑 ---
        if process_btn:
            if not ref_input.strip():
                st.warning("请先输入参考文献")
                return

            # 自动超时保护：任务超过 30 分钟未结束则强制释放名额（防止用户关闭浏览器导致占位）
            if system_status["start_time"] and system_status["active_users"] >= 3:
                elapsed = (datetime.datetime.now() - system_status["start_time"]).total_seconds()
                if elapsed > 1800:  # 30分钟超时
                    system_status["start_time"] = None
                    system_status["cancel_requested"] = False
                    system_status["active_users"] = max(0, system_status["active_users"] - 1)
                    try:
                        system_status["lock"].release()
                    except ValueError:
                        pass
                    st.info("上一个任务已超时（30分钟），已自动释放一个名额。")

            # 尝试获取并发名额（非阻塞模式）
            if not system_status["lock"].acquire(blocking=False):
                st.warning("""
### 同时处理的任务有点多

为避免数据库接口被并发请求挤爆，系统最多同时支持 **3** 个核验任务。
当前任务已满，请稍等片刻后重新点击「开始核验」。
""")
                return

            try:
                # 记录任务开始时间，重置取消标志，增加活跃任务数
                if system_status["start_time"] is None:
                    system_status["start_time"] = datetime.datetime.now()
                system_status["active_users"] = system_status.get("active_users", 0) + 1
                system_status["cancel_requested"] = False

                # 按行分割，过滤空行
                refs = [line.strip() for line in ref_input.strip().split('\n') if line.strip()]

                if len(refs) == 0:
                    st.warning("未识别到有效的参考文献，请检查输入格式")
                    return

                st.info(f"共识别到 **{len(refs)}** 条参考文献，开始处理...")

                # 处理参考文献
                results, stats = process_references(refs)

                # 检查是否有超时的条目，向用户展示提示
                timeout_refs = []
                for i, res in enumerate(results, 1):
                    if res.get('timeout_error', False):
                        timeout_refs.append(f"Ref.{i}")

                if timeout_refs:
                    st.warning(
                        f"以下条目因网络超时未能获取数据：{', '.join(timeout_refs)}。\n\n"
                        f"建议：获取全部文献后，**不要清除缓存**，重新点击「开始核验」，"
                        f"系统将仅重新请求超时的条目，已成功的条目会直接使用缓存。"
                    )

                # 存储到 session_state 以便后续显示
                st.session_state['results'] = results
                st.session_state['stats'] = stats
                st.session_state['project_id'] = project_id
            finally:
                # 任务结束，减少活跃任务数并释放名额
                system_status["active_users"] = max(0, system_status.get("active_users", 1) - 1)
                if system_status["active_users"] == 0:
                    system_status["start_time"] = None
                    system_status["cancel_requested"] = False
                system_status["lock"].release()

        # --- 显示结果（如果有） ---
        if 'results' in st.session_state and 'stats' in st.session_state:
            st.divider()

            # 统计概览
            display_dashboard(st.session_state['stats'])

            st.divider()

            # 结果预览
            display_results_table(st.session_state['results'])

            st.divider()

            # 下载报告
            generate_and_offer_download(
                st.session_state['results'],
                st.session_state['stats'],
                st.session_state.get('project_id', '')
            )


if __name__ == "__main__":
    main()

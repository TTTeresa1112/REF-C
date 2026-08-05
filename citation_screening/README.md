# 引用内容初筛配置

本模块只对持有独立访问码的受邀用户开放。默认每日额度为 200 次；一次代表一个“引用上下文 × 一篇参考文献”的 DeepSeek 判断。

## 1. 建立 Supabase 表和函数

在 Supabase SQL Editor 中执行：

```text
citation_screening/storage/migrations/001_screening_access.sql
```

迁移会建立用户、每日用量和任务表，并建立原子预扣及结算函数。表已启用 RLS；应用只通过服务端的 service role key 访问。

## 2. 配置 Streamlit Secrets

在 Streamlit Community Cloud 的 Secrets 中加入：

```toml
SUPABASE_URL = "https://YOUR_PROJECT.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "YOUR_SERVICE_ROLE_KEY"
ACCESS_CODE_PEPPER = "至少 32 字节的随机字符串"
SCREENING_MAX_CALLS_PER_TASK = "150"
```

`SUPABASE_SERVICE_ROLE_KEY` 和 `ACCESS_CODE_PEPPER` 绝不能提交到 GitHub，也不能发送给用户。

## 3. 创建邀请码

在本地设置与 Streamlit Secrets 完全相同的 `ACCESS_CODE_PEPPER`，然后运行：

```powershell
$env:ACCESS_CODE_PEPPER='你的随机字符串'
python tools/create_screening_invite.py "用户名称" 200
```

命令会显示一次访问码和一条 SQL。将 SQL 放入 Supabase SQL Editor 执行，只把访问码发给用户。

## 4. 额度行为

- “分析稿件并估算调用量”不会调用 DeepSeek，也不扣额度。
- 只有取得摘要的判断组合才计入预计调用量；未取得摘要的项目单列为“未获取数据”，不再与“存疑”混合。
- DeepSeek 会把作者姓名、单位、地址、邮箱、ORCID 等文头编号识别为“引用无关内容”；HTML 仍显示其数量，但计算各判断类别占比时完全排除这些记录。
- 用户确认后，数据库原子预扣预计额度，防止并发超额。
- 完成后按实际发出的 DeepSeek 请求结算并退回差额。
- 运行超过两小时仍未结算的任务会在该用户下次提交时自动释放额度。
- 默认单任务最多 150 次，可通过 `SCREENING_MAX_CALLS_PER_TASK` 修改。

## 5. 存疑项开放全文复核

- 初筛完成后，页面可单独查找“存疑”项的合法开放全文。
- 优先读取 PMC/Europe PMC 的 NLM/JATS XML；无 XML 时通过 Unpaywall/OpenAlex 查找开放 PDF，并仅在服务器本地提取文本。
- BM25 先选出每条引用最多 8 个高相关段落。DeepSeek 按顺序复核，一旦找到明确支持证据立即停止。
- 查找和排序阶段不扣额度；页面先显示最大调用量，确认后预扣，完成后按实际请求数结算。
- 全文证据、章节/页码和来源链接会写入 HTML 与 JSON 报告；未取得开放全文时原结果保持“存疑”。

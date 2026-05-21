# 测试计划

## 踩坑总结

### 一、格式破坏

| # | 坑 | 根因 | 修复 | 验证方式 |
|---|-----|------|------|---------|
| 1 | Bash heredoc `\n` 破坏 `<details>` 列表 | 命令文本换行写入 markdown | `\n` → 空格 | 检查输出中无多行工具调用条目 |
| 2 | 对话文本中 `<details>` 干扰 HTML 折叠 | Obsidian 将文本中 `<details>` 当 HTML 解析 | 改用 Obsidian callout `> [!note]-` | `grep -c '<details>' output.md` 与 `grep -c '</details>'` 相等 |
| 3 | ANSI 转义码污染文本 | subagent JSONL 中 hook 输出含颜色码 | `_sanitize_text()` 正则移除 `\x1b[...` | `grep '\[0;36m' output.md` 返回 0 |
| 4 | 未闭合代码围栏吞后续内容 | 对话中 ``` 开闭不平衡 + 截断切断闭合 | `_sanitize_text()` 检测不平衡时自动补 `\`\`\`` ；`_truncate_text()` 截断后重新 sanitize | 检查围栏数偶数；被截断的代码块后内容正常渲染 |
| 5 | Null bytes 破坏解析 | Subagent 输出含 `\x00` | `_sanitize_text()` 移除 | 无编码异常 |

### 二、内容提取遗漏

| # | 坑 | 根因 | 修复 | 验证方式 |
|---|-----|------|------|---------|
| 6 | AskUserQuestion 回答未提取 | user 消息 `tool_result` 类型未处理 | `_extract_tool_result_text()` 从 `toolUseResult.answers` 提取 | `grep '\[回答\]' output.md` 命中 |
| 7 | ExitPlanMode "tell claude what to do instead" 未提取 | 用户文字在 `toolUseResult` 错误字符串里 | 正则匹配 `"The user provided the following reason for the rejection: "` 提取 | `grep '倾向于' output.md` 命中 |
| 8 | Bash 输出显示为用户消息 | `tool_result` 通用分支把所有返回值当文本 | 删除通用 fallback，仅处理 AskUserQuestion/ExitPlanMode | `ls` 输出、目录列表不出现在 `**用户**` 后 |
| 9 | 审批面板输入不构成编辑周期边界 | 只认 `userType=="external"` | 所有非系统注入 user 消息都作边界 | 审批面板回复后 plan 版本正确分界 |
| 10 | AskUserQuestion `questions` 字段为 JSON 字符串 | 某些 JSONL 中 `questions` 是 `str` 非 `list` | `json.loads()` 尝试解析，失败时正则提取 | 无字符级乱码（如 `**🤖 {**` `**🤖 "**`) |
| 11 | `questions` JSON 解析后为 dict 非 list | 旧格式 `{"question": "..."}` | `isinstance(dict)` → 包裹为 `[dict]` | 问题文本正常显示 | ### 三、Obsidian 渲染

| # | 坑 | 根因 | 修复 | 验证方式 |
|---|-----|------|------|---------|
| 12 | `---` 分隔线导致 heading 不出现在大纲 | `</details>`→`---`→`# Round` 无空行 | 删除 `---`，heading 前保留空行 | Obsidian 大纲中所有 `# Round N` 可见 |
| 13 | `## 📋` 不出现在大纲 | `</details>`→`## 📋` 无空行 | heading 前加空行 | Obsidian 大纲中 `## 📋` 可见 |
| 14 | `## Round N` 与对话中 `##` 同级 | 都用 H2 | Round 改为 `#` (H1) | 对话内容中 `##` 为 Round 子层级 |
| 15 | Callout 吞后续 `**Claude**` 文本 | callout 后无空行 | `_render_tools()` 末尾加空行 | `**Claude**` 在 callout 外正常显示 |
| 16 | Round 前双空行 | 两处各自加空行 | 删除冗余 `lines.append("")` | 每个 `# Round N` 前恰 1 空行 |
| 17 | 工具调用显示在文本之后 | text→tools 顺序 | 改为 tools→text | AskUserQuestion 问题前先见工具 callout |

### 四、Plan 版本管理

| # | 坑 | 根因 | 修复 | 验证方式 |
|---|-----|------|------|---------|
| 18 | Plan 被覆写导致上下文错乱 | 同一 stem 被重写 | 编辑周期 + 版本文件 | 旧版本保留，前后引用不同文件 |
| 19 | `[[<原始名>]]` 虚假引用 | 正则扫描文本误匹配占位符 | 删除 `_PLAN_PATH_RE`，改用 `plan_timeline` | 输出中无 `[[plans/<原始名>]]` |
| 20 | Plan 引用泛滥（191 个→5 个） | `start_ts <= ts` 匹配了编辑后的所有消息 | 改为 `start_ts <= ts <= end_ts` | 只在编辑周期内的消息有引用 |

### 五、代码质量

| # | 坑 | 说明 |
|---|-----|------|
| 21 | 4 个 timestamp 函数重复解析 | 提取 `_to_beijing_dt()`，消除 ~30 行重复 |
| 22 | 正则每消息重复编译 | `_SYSTEM_XML_TAG_RE`、`_SKIP_PATTERNS` 预编译至模块级 |
| 23 | `sanitize_markdown_links` 破坏 `[[wikilink]]` | `(?<!\[)\[([^\]]+)\](?!\])` 负向环视 |
| 24 | O(n) 边界检测 | `bisect` 改为 O(log n) |
| 25 | Parse_transcript 嵌套 10 层 | 提取 `_track_plan_write()` |

---

## 测试文件

测试输入：构造一个最小 JSONL 文件，包含上述所有边界情况。验证脚本输出。

### 测试 JSONL (`test.jsonl`)

每条消息为一行 JSON，包含：

```
# 1. 普通文本
{"type":"user","userType":"external","message":{"content":[{"type":"text","text":"帮我修复登录页的bug"}]},"timestamp":"2026-05-21T10:00:00Z","sessionId":"test-session-001","cwd":"/test/project"}

# 2. 含 heredoc 的 Bash
{"type":"assistant","userType":"external","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"git commit -m \"$(cat <<'EOF'\nfeat: new feature\nEOF\n)\""}}]},"timestamp":"2026-05-21T10:00:01Z"}

# 3. AskUserQuestion
{"type":"assistant","userType":"external","message":{"content":[{"type":"tool_use","name":"AskUserQuestion","input":{"questions":[{"question":"倾向哪种方案？","options":[{"label":"方案A","description":"描述A"},{"label":"方案B","description":"描述B"}]}]}}]},"timestamp":"2026-05-21T10:00:02Z"}

# 4. 用户回答 (tool_result)
{"type":"user","userType":"external","message":{"content":[{"type":"tool_result","content":"User has answered your questions: ...","tool_use_id":"call_..."}]},"toolUseResult":{"questions":[{"question":"倾向哪种方案？"}],"answers":{"倾向哪种方案？":"方案A"}},"timestamp":"2026-05-21T10:00:03Z"}

# 5. ExitPlanMode
{"type":"assistant","userType":"external","message":{"content":[{"type":"tool_use","name":"ExitPlanMode","input":{"plan":"# 计划：测试\n## Context\n测试内容"}}]},"timestamp":"2026-05-21T10:00:04Z"}

# 6. 用户拒绝 ExitPlanMode 并输入文字
{"type":"user","userType":"external","message":{"content":[{"type":"tool_result","content":"The user doesn't want to proceed with this tool use. The tool use was rejected...","is_error":true}]},"toolUseResult":"Error: The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if it was a file edit, the new_string was NOT written to the file). The user provided the following reason for the rejection: 1. 修改方案A的细节 2. 增加容错处理","timestamp":"2026-05-21T10:00:05Z"}

# 7. Write plan 文件
{"type":"assistant","userType":"external","message":{"content":[{"type":"tool_use","name":"Write","input":{"file_path":"/Users/admin/.claude/plans/test-plan-steady-quail.md","content":"# 测试计划：修复登录页\n\n## Context\n\n登录页有bug需要修复"}}]},"timestamp":"2026-05-21T10:00:06Z"}

# 8. 用户回复（编辑周期边界）
{"type":"user","userType":"external","message":{"content":[{"type":"text","text":"好的，按计划执行"}]},"timestamp":"2026-05-21T10:01:00Z"}

# 9. 对话文本含 <details> 标签
{"type":"assistant","userType":"external","message":{"content":[{"type":"text","text":"输出格式如下：\n\n<details><summary>工具调用</summary>\n\n- `Read`: file.ts\n\n</details>\n\n注意空格"}]},"timestamp":"2026-05-21T10:01:01Z"}

# 10. 对话文本含未闭合的 ```json
{"type":"assistant","userType":"external","message":{"content":[{"type":"text","text":"映射结构：\n\n```json\n{\n  \"stem\": {\n    \"current\": \"修复登录页bug 20260521-100000.md\",\n    \"versions\": [\n      {\"name\": \"...\", \"hash\": \"abc123\"}\n    ]\n  }\n}"}]},"timestamp":"2026-05-21T10:01:02Z"}

# 11. 对话文本含 Obsidian wikilink 和 #tag
{"type":"user","userType":"external","message":{"content":[{"type":"text","text":"参考 [[某笔记]] 和 #tag1 #tag2 的做法"}]},"timestamp":"2026-05-21T10:02:00Z"}

# 12. Bash 输出（不应显示为 user 消息）
{"type":"user","userType":"external","message":{"content":[{"type":"tool_result","content":"total 168\ndrwxr-xr-x 14 admin staff 448 May 12 11:56 ."}]},"timestamp":"2026-05-21T10:02:01Z"}

# 13. 重写同一 plan（新编辑周期）
{"type":"assistant","userType":"external","message":{"content":[{"type":"tool_use","name":"Write","input":{"file_path":"/Users/admin/.claude/plans/test-plan-steady-quail.md","content":"# 新增用户权限管理\n\n## Context\n\n需要加权限系统"}}]},"timestamp":"2026-05-21T10:03:00Z"}
```

### 验证项

| # | 检查项 | 预期 |
|---|--------|------|
| 1 | 无 `<details>` 结构性标签 | 输出用 `> [!note]- 工具调用` callout |
| 2 | Bash heredoc 无换行断裂 | 命令在单行内，以 `...` 截断 |
| 3 | AskUserQuestion 问题可见 | `**🤖 倾向哪种方案？**` 后跟选项列表 |
| 4 | AskUserQuestion 回答可见 | `[回答] 倾向哪种方案？: 方案A` |
| 5 | ExitPlanMode 提案可见 | `**📋 计划提案**` 后跟计划内容 |
| 6 | 拒绝时的用户文字提取 | `1. 修改方案A的细节 2. 增加容错处理` |
| 7 | `<details>` 文本不干扰 | 对话文本中 `<details>` 原样显示，不影响 callout |
| 8 | ANSI 码被清除 | 无 `[0;36m` 等 |
| 9 | 未闭合代码围栏被补全 | ` ```json ` 有对应的闭合 ` ``` `` |
| 10 | `#tag1` 被转义 | `\#tag1` |
| 11 | `[[某笔记]]` 不被破坏 | `[[某笔记]]` 保留 |
| 12 | Bash 输出不显示为用户消息 | `total 168` 不出现在 `**用户**` 后 |
| 13 | 两次 Write 同 stem 生成两个 plan 版本 | `plans/` 下两个文件，不同时间戳 |
| 14 | `# Round N` 前恰 1 空行 | 无多余空行 |
| 15 | Plan 引用仅编辑周期内出现 | 消息 7 有引用，消息 9 无引用 |
| 16 | frontmatter 字段正确 | `created`/`modified`/`cwd`/`label`（session）；`created`/`modified`/`cwd`/`ref_plan_file`（plan） |

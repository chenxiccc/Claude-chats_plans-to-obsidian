# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

单一 Python 脚本，把 Claude Code 的 JSONL 转录文件转换为 Obsidian 可搜索的 Markdown 笔记。通过 Claude Code 的 SessionEnd/SessionStart hooks 自动触发。

## 开发和测试

- 纯 Python 标准库，无第三方依赖
- 没有测试套件。验证方式：`python3 session-to-obsidian.py --scan-all`，检查 `OBSIDIAN_DIR` 下的输出
- 配置变量在脚本顶部 `# ===== 配置 =====` 区域

## 架构要点

- **双运行模式**：默认找最新 JSONL 处理（SessionEnd hook），`--scan-all` 遍历所有子目录（SessionStart hook）
- **多项目目录映射**：`TRANSCRIPTS_DIR`（父目录）下每个子目录 → `OBSIDIAN_DIR` 下由 cwd 派生的子目录。`get_cwd_from_jsonl()` 从 JSONL 的 `cwd` 字段提取工作目录，`resolve_folder_name()` 取最后一级路径组件作为文件夹名，同名冲突时向后追加父目录直至唯一
- **标题提取**：`extract_topic()` 始终取首条用户消息，按 East Asian Width 截断（CJK/全角计 2，ASCII 计 1，累计 ≤ 40），去 Markdown 格式符和路径不安全字符
- **增量更新**：仅 mtime 对比（JSONL vs MD），无复杂检测逻辑；mapping `.session_mapping.json` 存储 `{stem: filename}` 简单字符串
- **标题清洗**：`extract_topic()` 去掉 `# * _ ` ~ > [ ] ! | \ / : ? " < >` 和首尾空格
- **# 转义**：`escape_obsidian_tags()` 防止 Obsidian 将正文中的 `#tag` 误识别为标签
- **VS Code 标签**：`find_session_name()` 读取 `~/Library/Application Support/Code/User/workspaceStorage/*/state.vscdb` 中的 `label` 字段，写入 frontmatter `label:` 元数据（不作为标题）。仅 VS Code 插件启动的会话有 label，CLI 会话无
- **Python 兼容**：`from __future__ import annotations` 保证 3.9+ 可用

## label/name 数据来源

- **VS Code 插件**：会话标签存储在 `~/Library/Application Support/Code/User/workspaceStorage/*/state.vscdb`，SQLite 数据库中 `ItemTable.value` 的 JSON 数组，字段 `label`（由 Claude Code AI 自动生成或用户手动重命名）。仅 VS Code 插件启动的会话有记录，CLI 终端会话无
- **Claude Code 自身**：运行时会话元数据存储在 `~/.claude/sessions/{pid}.json`，含 `name` 字段（Claude Code AI 自动生成）。会话结束后该文件被删除，无法作为可靠数据源
- **两者都不可靠**：AI 自动生成的标签质量参差不齐（如 "做吧"、"commit"），且无法在 state.vscdb 中区分自动生成 vs 用户手动重命名。因此本脚本将 label 降级为 frontmatter 元数据，标题始终用首条用户消息

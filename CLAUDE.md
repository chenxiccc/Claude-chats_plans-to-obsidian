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
- **多项目目录映射**：`TRANSCRIPTS_DIR`（父目录）下每个子目录 → `OBSIDIAN_DIR` 下同名子目录
- **主题优先级**：`find_session_name()` 读 VS Code workspace state.vscdb 的持久标签 → 首条用户消息前 20 字（去 Markdown 格式符）
- **增量更新**：JSONL mtime vs MD mtime 检测内容变化；mapping `.session_mapping.json` 存储 `{filename, topic}`，topic 变化检测标题更新
- **标题清洗**：`extract_topic()` 去掉 `# * _ ` ~ > [ ] ! | \ / : ? " < >` 和首尾空格
- **# 转义**：`escape_obsidian_tags()` 防止 Obsidian 将正文中的 `#tag` 误识别为标签
- **Python 兼容**：`from __future__ import annotations` 保证 3.9+ 可用

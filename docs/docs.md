# docs/ — 项目文档库导读

`docs/` 是开发者与 AI 共同维护的项目文档库：权责分明、流程单向、全程可追溯。

## 目录结构与各子文件夹说明

| 路径 | 功能作用 | 维护方 |
|---|---|---|
| `docs.md` | 本文档：docs/ 体系导读 | AI |
| `log/` | 开发日志，每天一个 `YYMMDD.md`（详见 `log/log.md`） | AI |
| `project/` | 项目文档库核心：project.md 项目总介绍、机制正本 README、开发者记录区（优化建议区 / 新功能开发区 / 问题疑惑区）、演示汇报.md（课堂汇报底稿，AI 实时同步）、各模块目录（详见 `project/project.md` 顶部「文件夹说明」） | 开发者与 AI 按权责表分工 |
| `study/` | 学习笔记：流程讲解、原理剖析、复盘总结等问答整理（详见 `study/study.md`） | AI |

## 工作流（单向）

开发者写 design.md（需求正本）→ AI 依据它生成 designs-specs.md（开发直接依据）→ AI 按 specs 开发 → AI 记开发日志（log/）→ 落地/废弃的 spec/plan 归档进所属模块 archive/。

完整权责表见根目录 `CLAUDE.md`「文档体系与维护权责」；spec/plan 存放与归档机制见 `project/README.md`。

## 铁律

- AI 永不擅改开发者的需求正本（design.md / project.md 主体）
- 归档只是移动位置，文件永不删除
- 问题疑惑区、临时草稿：AI 不主动查阅
- 每个子文件夹须有以该文件夹命名的说明文档（新建文件夹时同步创建）

---
schema_version: 1
mode: tracked
docs_root: docs/workflow
current_node: "b02-review"
next_action: "执行 B02 r3 独立复审并收口归档，同时定稿 B03/B04 开启工作树并行"
check_cmd: "uv run ruff check src tests && uv run python scripts/verify_contracts.py && uv run pytest tests/unit -q && npm --prefix frontend run typecheck"
---

# 项目研发工作流总索引

> **读取纪律**
> 1. **寻址顺序**：`<当前根>/.wf-docs/INDEX.md` →（Git Worktree 时）`<主仓根>/.wf-docs/INDEX.md` → `<当前根>/docs/workflow/INDEX.md`。表内路径均**相对 `<项目根>`**。
> 2. **局部精读**：仅读当前任务直接相关的文件；长文档（超 300 行）按 `关联锚点` 检索行号后分段读取，不整篇硬吞；某节索引增长过大时拆分为二级 `INDEX.md`。
> 3. **历史禁读**：状态为 `已完成`、`已搁置` 的大功能目录默认禁止读取（仅修改该模块或排查关联问题时点读）。
> 4. **Git 远端状态**：`Git remote: none`（用户要求全程自主本地开发并交付代码包/仓库）。

## 1. 全局基线、契约与待办池
| 类别 | 路径（相对项目根） | 内容与覆盖范围摘要 | 按需读取条件 |
| :--- | :--- | :--- | :--- |
| 产品需求文档（PRD） | `docs/workflow/product-spec.md` | 航运智能账单助手 PRD（`F-01`~`F-07` 功能流程、四态决策、校验门禁、验收标准与技术备注） | 架构设计、UI 原型、功能拆片与验收核对时读 |
| 系统架构与契约规格书 | `docs/workflow/arch-contract.md` | 技术选型与许可证、模块单向依赖边界、安全底座与测试分层规范 | 搭建 B00、拆片开发、编码与审查时读 |
| 数据库建表契约 | `docs/workflow/contracts/schema.sql` | SQLite 3 八张核心表 DDL（十进制字符串精度、局部同步幂等表与索引） | 编写 `storage` 层与迁移脚本时读 |
| REST 接口契约 | `docs/workflow/contracts/openapi.yaml` | OpenAPI 3.1.0 接口定义、错误模型与 4 类场景示例 | 编写 `api` 路由与生成前端 `schema.d.ts` 时读 |
| 标准输出 JSON Schema | `docs/workflow/contracts/billing-output.schema.json` | 题目第 5/6/7 节标准输出 JSON Draft 2020-12 校验契约 | 实现导出器、校验器与 Harness 时读 |
| 分级待办池 | `docs/workflow/backlog.md` | 延后需求、死代码清理项、待补自动化检查队列 | 代码清理演进或规划下一轮功能时读 |

## 2. 图示与界面原型
| 类型 | 路径（相对项目根） | 对应范围 / 页面 | 按需读取条件 |
| :--- | :--- | :--- | :--- |
| 业务主流程图（泳道） | `docs/workflow/diagrams/user-billing-flow.html` | `F-01`~`F-06` 端到端单据解析、校验与人工复核回流主线 | 核对跨角色交互与闭环流转时查看 |
| 决策与门禁流程图 | `docs/workflow/diagrams/operation-decision-flow.html` | `F-03`~`F-05` 校验门禁与四级匹配四态决策流程 | 实现核心判定引擎或复核规则时查看 |
| 系统架构总图 | `docs/workflow/diagrams/architecture.html` | 调用方、四层模块依赖边界、本机存储与外部服务调用方向 | 核对模块职责与单向依赖边界时查看 |
| 核心处理时序图 | `docs/workflow/diagrams/pipeline-sequence.html` | 投递拆包、OCR/LLM、匹配校验、1 轮自修复与人工修订锁定 | 核对 LangGraph 编排与失败边界时查看 |
| 前端界面规格指令（Lumno B2） | `docs/workflow/prototypes/workbench/README.md` | 财务复核工作台（桌面 1280×800 & 移动 390×844） | 开发 `B04` 前端工作台与 `browser_tester` 盲盒验收时读 |

## 3. 大功能目录索引（features/）
> 状态枚举：`待规划` | `方案中` | `开发中(...)` | `待实连` | `待验收` | `受阻` | `已完成`（禁读） | `已搁置`（禁读）

| 编号 | 目录路径（相对项目根） | 大功能名称与包含小功能 | 依赖 | 关联锚点（PRD / 接口 / 表） | 状态 |
| :--- | :--- | :--- | :---: | :--- | :---: |
| B01 | `docs/workflow/features/B01-多格式解析与OCR网关` | 多格式文档解包、Excel/Word/EML/PDF解析、SSRF防护与OCR/LLM网关适配 | B00 | PRD F-01/F-02, arch §1.4/§2, schema.sql source_documents | `已完成` |
| B02 | `docs/workflow/features/B02-智能抽取匹配与校验门禁` | 航运账单LangGraph编排、AR/AP与贷项判定、四级匹配继承、确定性校验与1轮自修复 | B01 | PRD F-02~F-05, billing-output.schema.json | `开发中(T08-T09)` |
| B03 | `docs/workflow/features/B03-持久层API与CLI复核包` | SQLite 8表仓储、批处理分阶段服务、FastAPI全路由与Origin防护、Typer CLI与复核包互通 | B02 | PRD F-06/F-07, schema.sql 全表, openapi.yaml 全路由 | `方案中` |
| B04 | `docs/workflow/features/B04-财务复核工作台前端` | Lumno B2三栏/移动双视口React工作台、全字段复核、三口径导出与评测看板 | B00, B03 | PRD F-06, prototypes/workbench/README.md, openapi.yaml | `开发中(T01-T03)` |
| B05 | `docs/workflow/features/B05-Harness评测与交付文档` | >=10组真值与对抗样本Harness评测套件、Harness与深度思考报告、README与交付ZIP打包验收 | B03 | PRD F-07, 题面 §8.8/§8.9, eval_runs | `方案中` |

## 4. 发版与交付归档（releases/）
| 版本/批次 | 路径（相对项目根） | 交付范围与回滚基线摘要 |
| :--- | :--- | :--- |
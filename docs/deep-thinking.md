# 航运账单 Agent 的六项思考

对应原题 §8.9。每项都从样本或回归用例出发，说明财务判断如何落实到程序与验收。

## 思考一 单据类型、记账主体和冲减方向各做一个决定

`图片示例.png` 的两条费用来自同一张账单。切换到 ZIM 时，两条都是 AR。切换到 ALL AMERICA 时，两条都是 AP。输入原件没变，变化的是谁在记账。方向要从当前主体与付款方、收款方的关系推出。签发方和收件人是证据之一，有的账单签发方只是转发通知的一方。

`PDF 示例账单2.pdf` 就是后一种情况。签发方为 `Pilot Aiship Tech co., Ltd.`，可核对的收款方是 `NEPTUNE SHIPPING LIMITED (LAX)`。NEPTUNE 视角取得 AR，无关主体进入待复核。括号中的分公司仍参与主体匹配。显式不同的分公司必须隔离，名称相似也不能共享旧账候选。

供应商 `Credit Note` 用 `-40.00 AP` 表示应付余额减少 `40.00`。方向仍由交易双方决定，符号仅说明冲减。回归同时用 `Credit Card Invoice` 检查名称里有 `Credit` 的普通发票继续按正额处理。 [主体判定](../src/shipping_billing_agent/domain/entity_resolver.py) · [用例 C01、C03、C06](../src/shipping_billing_agent/harness/dataset.py)

## 思考二 报价、罚则和汇款说明要与应结算金额隔离

`PDF示例账单1.pdf` 中的 `$25`、`$1100`、`$150` 是预取消罚金说明，第 2–3 页是汇款说明。若只搜索货币符号，罚则、账户编号和实际费用会进入同一列表。规则保留真实的一笔 `245.00 USD CC`，把条款排除及其出处留给复核。PRE-ALERT EML 同时写了 `SHA pay TAR $50` 和 `Carrier Free H/C`，结算候选取前者，免收费说明保留排除证据。

原件有金额也可能缺少业务字段。`图片示例.png` 的 `Wharfage` 金额为 `21.90`，旁边印了单价 `2.85`，数量字段仍保留 `null`。反除 `21.90 / 2.85` 会造出原件从未给出的计费数量。无原文付款方式或单据编号且无适用旧账可继承时，同样留 `null`。对可疑数值，复核人员应能看到原文位置和被排除的理由。 [抽取规则](../src/shipping_billing_agent/agent/rule_extractor.py) · [用例 C01、C02、C04](../src/shipping_billing_agent/harness/dataset.py)

## 思考三 把批次内的历史匹配和复核后的台账写入都做成可追溯操作

附件到达顺序不能当成证据优先级。先按 SHA-256 归并同内容来源，保留来源路径，再对跨邮件、PDF 的同一票货做冲突比较。`DC-108478` 同一单据有两条 `Handling Charge`，`35.00` 与 `65.00` 各对应一条历史行。两条旧账反顺序输入时仍按金额和币种一对一定位，有歧义的剩余候选交给人工。

`DN-202609-001` 的变更材料可能只给 `payment_term`，也可能只给 `amount`。匹配唯一旧账后，`PP → CC` 继承金额与币种，`30.00 → 40.00` 继承付款方式，缺席的双方字段继承原值。再拿同一材料对照更新后的旧账，结果应为 `NO_CHANGE`。用这两组对照可发现全量覆盖或重复调整问题。可入账输出走 `official`，给排查的标准行走 `full_standard`，所有行与差异、排除条款和人工记录可从 `audit` 核对。

CLI 的 `--bills` 提供本批 `existing_bills` 快照，让复核对应到明确的候选和旧值。工作台的编辑和基线同步检查批次、记录或旧账版本。并发发生时，过期的复核结果遇到版本冲突，能先刷新差异再提交。基线同步还记录对应批次与记录，避免重试重复写同一笔。 [匹配器](../src/shipping_billing_agent/domain/matcher.py) · [持久化](../src/shipping_billing_agent/storage/repositories.py) · [复核服务](../src/shipping_billing_agent/services/review_service.py)

## 思考四 精度要在每次跨语言、跨存储时保住

`9007199254740991.01` 进入 JavaScript `Number` 时会失去小数位。`0.0025` 即使能按原样打印，二进制浮点运算也可能使核对和再序列化偏离原十进制值。账单与税额需要从接收、校验、保存、响应到复核都使用同一数值事实。

后端从原文构造 `Decimal`，以十进制文本写入 SQLite `TEXT`，经金额序列化函数直接输出 JSON 数字。FastAPI 的 `DecimalJsonResponse` 绕过默认 JSON encoder，避免在原值上再做一次转换。前端先用 `response.text()` 取原文，`decimalJson.ts` 的状态机捕获金额数字的字符序列，显示、编辑和请求回写使用该精确信息。C11 对比两个边缘数的输出数值，前端测试核对读取和写回。 [后端响应](../src/shipping_billing_agent/api/routers/http.py) · [前端金额编解码](../frontend/src/api/decimalJson.ts) · [前端金额测试](../frontend/src/api/decimalJson.test.ts)

## 思考五 让模型提名候选，让确定性校验决定账单能否出门

自由文本里的付费句式可能超出规则覆盖：当前离线无密钥路径走纯确定性规则，联机开启 LLM 时则以确定性规则构建原文基线，并让 LangChain 模型对所有已解析文档进行双路候选提取（首轮模型或额度异常按单文档 fail-closed 生成失败卡，确保健康文档不受连累，也便于高吞吐落地时继续演进为“高置信规则直通 + 缺口按需调模型”的分层路由）。无论哪条路径，最终金额、日期、ISO 4217 币种、方向、审批状态均由原文接地、数值校验和复核服务决定。金额或编号中的 `0/O`、`1/I` 需要按字段处理。校验和唯一时，箱号可给 `WARNING` 建议。`1.310.40`、`2026-02-30`、`ZZZ` 无可靠账务值，保留 `raw_invalid_fields` 并阻断出账。第二轮模型重修也必须重新过门禁。

C08 文档企图把原文 `100.00` 改成零并声称 `APPROVED`。文档片段以证据标签包裹并转义，系统提示词由代码独立构造，汇款、邮箱、电话与签名查询参数出网前脱敏。即使模型给出零元和 `APPROVED`，原文合计冲突会挡住零元出账，审批只能由复核操作授予。联机查验还要检查实际发出的 HTTP 请求，离线已验证的是替身调用和门禁。

输入 URL 先经 SSRF 校验，内网和回环地址被拦截。`OCR_TRUST_ENV=false` 且未显式设置 `OCR_PROXY_URL` 时，网关客户端绕过本机环境代理，规避代理 SSL EOF。公网中转默认关闭，原件上传须配置受授权的发布器及基址。 [提示词边界](../src/shipping_billing_agent/agent/prompts.py) · [URL 防护](../src/shipping_billing_agent/gateways/url_guard.py) · [C08、C09 与联机断言](harness-design.md)

## 思考六 用可复现的账务断言衡量每一轮，以运行状态记录证据空缺

冻结四处契约，避免解析、复核和前端各自解释数据。`openapi.yaml` 定义 API，`schema.sql` 定义持久化约束，`billing-output.schema.json` 定义标准导出，前端 `schema.d.ts` 由 OpenAPI 生成。交付包保留这四处和验证入口，给跨模块改动同一份核对依据。

顶层数组 Schema 允许 `[]`，因为业务确有应当零出账的情况。完整回归还要判明每份样本该有几行、哪些行该写、错行是否成功拦截。六条混合状态记录的 `official`、`full_standard`、`audit` 必须精确输出非空的 `3 / 5 / 6` 行。同一组预期在 `pass@k` 衡量至少一次达到，在 `pass^k` 衡量每次都达到。只有后者能发现三轮中两次错账、一次答对的波动。

联机缺条件时，真正写一条 `eval_runs` 记录，状态为 `UNVERIFIED`、`actual_trials=0`、七项汇总指标为 SQL `NULL`，CLI 非零退出。跑到中途遇基础故障记 `FAILED`，保留已执行计数。模型答错但运行完成时照实计分。真实 OCR 和真实 LLM 的通过证据必须来自对应模式，离线 `pass^3=1.0` 仅覆盖本地与确定性替身。 [SQL 和输出契约](workflow/contracts/) · [前端类型](../frontend/src/api/schema.d.ts) · [评测设计与观测值](harness-design.md)

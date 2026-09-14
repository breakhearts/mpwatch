# 公众号监控最小设计

更新日期：2026-09-15。状态：真实登录、目标订阅、最新一篇正文获取及持久化验证通过；列表及完整覆盖未通过。已按用户要求移除模拟测试，只以真实验收记录作为可用性证据。承接任务：QuantOS「设计公众号发文监控工具」。

实现说明：当前命令以 [README](../README.md) 为准。上游协议参考固定提交 `d8feb6a42c6773d7374e03c487d3ae3426084af8` 的 [weread_mp.py](https://github.com/rachelos/we-mp-rss/blob/d8feb6a42c6773d7374e03c487d3ae3426084af8/core/wx/model/weread_mp.py) 与 [weread.py](https://github.com/rachelos/we-mp-rss/blob/d8feb6a42c6773d7374e03c487d3ae3426084af8/core/wx/model/weread.py)。本地为独立编写的适配器，不导入上游服务，也未复制其采集类。offset 暂按顶层群发组数量推进，空 reviews 暂作列表结束标志；这是待真实样本核实的协议假设，报告的 complete 限于该假设。

## 1. 目标与范围

监控少量大 V 公众号，发现新文章、取得正文并保存可追溯事实，供 agent 阅读和研究。首版的“信号”是新文事件及其正文；证券映射、观点分类和交易含义不由采集器推断。

采用独立 Python 项目、本地 SQLite 和 JSON CLI。没有自建 Web 界面、常驻服务或自动通知。2026-09-14 按用户要求增加 `auth login`：独立浏览器打开官方扫码页，手机确认后验证书架接口并保存 Cookie；过期后再次扫码。采集链路仍待真实账号验证。

## 2. 上游依据与未决问题

参考项目是 [rachelos/we-mp-rss](https://github.com/rachelos/we-mp-rss)，不是此前对话中曾混淆的 ttttmr/Wechat2RSS。

2026-09-14 重新读取了上游 [采集源码](https://github.com/rachelos/we-mp-rss/blob/main/core/wx/model/weread_mp.py)、[微信读书说明](https://github.com/rachelos/we-mp-rss/blob/main/docs/weread-mp.md) 和 [许可证](https://github.com/rachelos/we-mp-rss/blob/main/LICENSE)。这些链接指向可变分支；实施时应记录实际参考的提交 SHA，并在复用代码时保留要求的版权与许可声明。本轮未复制上游代码。

源码提供以下接口线索：

| 用途 | HTTP 路径 | 主要输入 |
| --- | --- | --- |
| 文章列表 | `/web/mp/articles` | `bookId`、`offset` |
| 最新文章降级入口 | `/api/mp/cover` | `bookId` |
| 正文 | `/web/mp/content` | `reviewId` |

请求目标为 `https://weread.qq.com`。上游以 Cookie 发起请求，并从正文中的 `#js_content` 或 `.rich_media_content` 提取内容。源码存在列表优先、cover 降级逻辑；文档与源码对列表可用性的表述存在差异。以上只能证明参考实现存在，不能证明当前账号可用或覆盖所有微信文章。

真实验证需要确定：登录态的必要字段与过期行为、账号发现入口、分页 offset 的含义和结束条件、时间字段的语义、列表实际覆盖范围，以及目标公众号是否可在微信读书访问。不能把接口字段名直接当成已验证合同。

## 3. 命令与模块

拟议命令：

| 命令 | 行为 |
| --- | --- |
| `mpwatch sources` | 联网列出可选择的微信读书公众号；发现入口需实测 |
| `mpwatch add --book-id ID --name NAME` | 本地添加或更新监控账号，不自动修改远端书架 |
| `mpwatch refresh` | 对启用账号执行一次有界采集，保存并返回 run 结果 |
| `mpwatch report --run-id ID` | 离线输出指定检查的新增文章、正文状态和错误 |

若账号发现入口暂不可用，允许直接配置已知 bookId；不能伪装 sources 成功。需要添加远端书架时，应另设显式动作。

代码按职责组织：`collector.py` 负责请求、响应校验和解析；`storage.py` 负责 SQLite、幂等和查询；`cli.py` 负责命令、采集编排和 JSON 输出；`auth.py` 负责独立浏览器扫码及验证后保存登录态。真实验收入口为 `scripts/verify_live.py`。

用户随后要求增加添加公众号 CLI，因此新增 `subscriptions.py` 和显式 `subscribe` 命令：按 bookId、名称或文章链接解析账号，添加微信读书书架，回读确认后启用本地监控。原 add 保持离线配置语义，refresh 不自动添加远端书架。`--dry-run` 不写本地或远端。文章链接中的 __biz 解码后仍须通过账号接口核实，短链接验证页不能当作有效文章。名称搜索最多读取前 20 个结果，仅唯一精确匹配可进入添加流程。

stdout 只输出一个 JSON 结果，诊断写 stderr，且不含 Cookie 或原始响应头。refresh 退出码：0 为所有启用账号列表覆盖 complete 且正文队列清空；2 为已有可用结果但覆盖、正文或部分账号未完成；1 为配置错误或全无可用结果。无启用账号是配置错误。report 成功读取已有报告时返回 0，采集结果仍以 JSON 内的 status 为准；报告不存在时返回 1。

## 4. 数据与时间

数据库放在 `MPWATCH_DATA_DIR` 下，凭据由 `MPWATCH_COOKIE_FILE` 指向仓库外私有文件；未设置时使用用户应用数据目录下 mpwatch 的固定位置，详见 README。仅联网命令验证凭据，add 和 report 不需要登录态。不扫描现有浏览器或其他项目凭据；扫码只读取本次独立浏览器中适用于微信读书的 Cookie。

| 表 | 最少字段与约束 |
| --- | --- |
| sources | book_id 主键、name、enabled、added_at、initialized_at |
| articles | article_id 主键、book_id + review_id 联合唯一、title、url、published_at 可空、publish_time_source、first_seen_at、discovery_kind、content_status、last_content_attempt_at、next_content_retry_at |
| content_versions | article_id、content_hash、正文文本、原始正文 HTML、fetched_at；同文章同哈希唯一 |
| runs | run_id、started_at、finished_at、status |
| source_checks | run_id + book_id 唯一、mode、coverage、coverage_reason、pages、stop_reason、错误类别与脱敏说明 |
| check_articles | run_id + article_id 联合唯一、event_kind（baseline / discovered / content_ready / content_failed）、当次标题/链接/时间/正文状态快照、content_hash 可空；外键关联文章和正文版本 |

所有时间存 UTC。`first_seen_at` 一经写入不得覆盖。只有确认来源语义后才填 `published_at`；cover 无可靠发布时间时必须为 null。正文每个版本记录自己的 fetched_at，不能用后来补抓的正文反填过去的可用信息。

首次采集内容标为 `baseline`。初始化期间中断后继续初始化，直至首次受限列表扫描完成；达到页数预算也可完成初始化，但 coverage 仍为 partial，cover 降级不能完成初始化。初始化完成前发现的内容均为 baseline。后续首次发现的文章标为 `observed`，这不等于作者刚刚发布，报告必须展示已知发布时间和发现时间。历史补抓默认不产生“刚发布”判断。

## 5. 采集、去重和恢复

1. 建立 run，逐账号保存检查状态；单个账号失败保留其他账号结果。
2. 有界读取列表，展开一次群发中的全部子文章，以完整 reviewId 去重。
3. 不因遇到一个已入库 ID 就停止；每次检查配置范围内的所有页面，或到达经过验证的列表结束标志。默认从最新页检查最多 3 页，允许用 `refresh --max-pages N` 显式扩大范围；达到页数上限保留 `partial/page_limit`。首版不承诺自动补齐窗口之外的文章，报告必须提示扩大扫描范围。分页参数校验为正整数，具体 offset 推进方式待实测，不直接把页号当 offset。
4. 认证失败停止后续联网，不降级伪装成功；403 或验证页保留访问受限/需要验证的错误类别，不能仅凭 403 断言 Cookie 过期，同样停止后续联网。限流遵守 Retry-After，若超出本次等待预算则结束并记录可重试错误。一般网络失败只有限重试。
5. 列表不可用且不是认证、访问受限、验证或限流失败时，可以尝试 cover。成功也只标 `partial/cover_only`，保留列表原始失败类别；没有发布时间就保持未知。
6. 新文章先入库，再处理所有启用账号中 pending 及到达重试时间的 failed 正文。按 last_content_attempt_at（未尝试优先）、first_seen_at、article_id 排序，并持久化尝试时间；失败设置有上限的退避时间，避免固定失败文章或持续新文饿死其他待办。正文失败保留文章记录；下一次运行不因文章存在就跳过正文。只重试尚未成功的正文，首版不主动探测已成功正文的后续修订；若以后再次获取，按哈希留存版本。
7. 正文必须匹配预期内容容器并包含有效文本；登录页、验证页、空响应和畸形 JSON 均属于失败，不是无新文。
8. 单篇文章与对应 check_articles 记录原子入库；正文版本和当次状态快照也在同一短事务保存，避免文章已去重但当次事件丢失。网络请求不持有数据库写事务。首版使用操作系统文件锁覆盖 refresh 全生命周期，进程退出释放；取得锁后才能将遗留 running run 标记 interrupted，不能把仍活动的进程误判为中断。add 同样取得写锁，report 只读。中断时已落盘文章可继续去重和补抓。

`coverage=complete` 仅表示本次上游列表扫描到已验证的结束条件，不保证微信平台全部文章都可获取；`partial` 表示页数限制、cover 降级或中断；`failed` 表示没有可用列表结果。正文状态独立使用 pending / ready / failed。

初始请求预算建议：顺序执行、每账号最多 3 页、每轮最多 20 篇正文、请求间隔至少 2 秒、网络失败最多重试 2 次，单请求超时 30 秒，本轮联网总预算 300 秒。Retry-After 超过剩余预算时直接结束；重试和降级也计入预算。预算耗尽须报告已知正文待办数量，未扫描文章数量为 unknown，不能猜测；这些值是可调整配置，不是上游认可的限频标准。

## 6. 报告合同

JSON 带 schema_version、run_id、status、账号级 checks 和文章列表。文章包含完整身份、标题、原文链接、发布时间、首次发现时间、discovery_kind、正文状态及所用正文版本的哈希和获取时间。checks 包含覆盖范围、降级原因及错误。

按 run_id 重读直接读取 check_articles 的当次字段快照及固定 content_hash，不查询文章的最新字段来替代历史事实。新文后来补抓正文成功，在补抓 run 中记录 content_ready；报告同时输出新增事件和正文补齐事件，使 agent 能发现可用正文。若同一 run 中先发现再补正文，保留发现事件类型并更新该 run 尚未完成的状态快照；run 终结后快照不可变。中断 run 读取已提交的快照并显示 interrupted。

agent 后续提取观点时引用 article_id 和 content_hash，分别保存作者原话、原文证据与分析推断；信号生成时间独立记录。当前阶段不自动生成观点或下单。

## 7. 实施顺序

1. 适配器通过真实接口核实响应、群发结构、错误分类和正文提取。
2. 2–3 个真实公众号的小样本探针：核实接口、分页和正文；保存脱敏证据。
3. SQLite 和 CLI：验证重复检查、失败补抓、初始化及离线报告。
4. 真实连续观察：记录发现延迟和覆盖缺口，评估是否值得增加周期运行。

已通过真实浏览器识别目标、订阅及最新一篇正文采集。列表接口 -2041 不再一律当作凭据过期：仅在独立书架认证成功后标为 list_unavailable，允许 cover 降级；真正的认证失败继续停止。当前覆盖为 partial。300 秒联网预算在请求/等待边界检查，HTTP 阶段超时不能保证进程严格在 300 秒内退出；如需硬截止应另实现进程监督。真实验收记录见 [验收方案](acceptance.md)。

## 8. 与参考仓库的差异

对照 `rachelos/we-mp-rss` 提交 `d8feb6a42c6773d7374e03c487d3ae3426084af8`：

- 上游 `apis/mps.py` 的 by_article 使用 `driver/wxarticle.py` 浏览器读取文章及 window.biz。mpwatch 最初只用普通 HTTP，现已加入正常浏览器回退；两者遇到浏览器验证码都不保证自动处理。
- 上游名称搜索经 `core/wx/base.py` 调用公众号后台 `/cgi-bin/searchbiz`，依赖公众号平台 Cookie/token。mpwatch 使用微信读书 `/api/store/search`，不是同一条链路；当前 scope=2 被拒绝。若要对齐，需要独立的公众号平台授权入口。
- 上游列表出错可回退 cover。mpwatch 原先将 -2041 全部视为认证失败，现依据真实结果改为先验证书架认证，再允许该列表错误降级。cover 只返回最新一篇，不宣称完整覆盖。
- 上游有 RSS、服务端接口及其数据库模型；mpwatch 保留本地 CLI、SQLite 和 JSON，正文失败可后续补抓，历史 run 使用固定快照。项目不是上游的等价移植。

# mpwatch

本地公众号发文监控工具：检查关注账号的新文章，保存正文和采集状态，输出可供 agent 分析的 JSON。

本项目承接 QuantOS 的「设计公众号发文监控工具」对话，独立维护。仅以真实验证判断可用性。当前已通过目标账号添加、最新一篇正文获取及报告持久化验证；历史列表不可用，采集覆盖仍为 partial，完整监控尚未通过。

## 首版方向

- Python 单次命令，顺序采集，执行后退出。
- 参考 `rachelos/we-mp-rss` 的微信读书 HTTP 采集方式。
- SQLite 保存账号、文章、正文版本和每次检查结果。
- 分开记录发布时间、首次发现时间和正文可用时间。
- 首版提供新文事件及正文，供 agent 后续提取观点。

详细内容见 [最小设计](docs/design.md) 和 [验收方案](docs/acceptance.md)。

## 安装与使用

需要 Python 3.11+ 和 uv。在项目目录执行（Windows 默认使用已安装的 Edge）：

```powershell
uv sync --extra auth
uv run --extra auth mpwatch auth login
uv run mpwatch auth status
uv run mpwatch sources
uv run mpwatch subscribe --book-id MP_WXS_实际数字ID
uv run mpwatch refresh
uv run mpwatch report --run-id 实际run_id
```

`auth login` 打开独立浏览器窗口，点击微信读书登录入口；用微信扫码并在手机确认后，程序自动验证书架接口，成功才原子保存 Cookie。无需手动复制 Cookie；不读取现有浏览器配置或会话。关闭窗口、扫码超时或验证失败会保留之前的 Cookie。默认等待 300 秒，可用 `--timeout 600` 调整。

`auth status` 用保存的 Cookie 再次联网验证，不启动浏览器。登录过期后重新执行 `auth login`。`--browser chrome` 可切换到已安装的 Chrome；使用 `--browser chromium` 前需执行 `uv run --extra auth playwright install chromium`。扫码功能是可选依赖，普通采集无需 Playwright。

`sources` 列微信读书书架上已有的公众号。使用新增的 `subscribe` 可直接添加远端书架并启用本地监控，无需先在手机 App 手动添加。

## 添加公众号

```powershell
# 先解析并预览，不修改书架或本地数据库
uv run mpwatch subscribe --book-id MP_WXS_实际数字ID --dry-run

# 添加到微信读书，并启用本地监控
uv run mpwatch subscribe --book-id MP_WXS_实际数字ID

# 从文章链接识别公众号，再添加
uv run mpwatch subscribe --article-url 'https://mp.weixin.qq.com/s?__biz=实际值&mid=实际值&idx=1&sn=实际值'

# 名称搜索接口可用时使用；同名或非精确匹配返回候选，需按 ID 选择
uv run mpwatch search '公众号名称'
uv run mpwatch subscribe --name '公众号完整名称'
```

`subscribe` 先通过微信读书核实账号名称，再检查书架；已存在则直接更新本地清单，不重复发送添加请求。新添加会调用一次 `/web/shelf/add`，回读书架确认后才保存本地。若网络中断，不盲目重发 POST，而是先回读确认；远端结果未确认时返回 partial、本地不启用。远端成功但本地写入失败会分别报告，可重试修复本地状态。

当前实测：用户目标文章已通过浏览器解析，远端添加及书架回读、本地监控启用均成功。名称搜索 scope=2 返回 HTTP 499，因此目前 `search` / `subscribe --name` 会报告 account_search_unavailable。文章长链接可从 __biz 解析 ID 后核实名称；短链接的普通 HTTP 请求被验证页拦截时，会使用独立普通浏览器重试（需要 auth 可选依赖）。浏览器若仍显示验证页就报 article_requires_verification，不自动解验证码，不能保证所有短链接可解析。

`add` 在本地添加或更新账号；加 `--disabled` 停用，再次不带该选项调用即可启用。`sources --local` 离线查看本地清单。`report` 离线读取指定 run 的固定快照。以上离线命令不要求 Cookie。

`refresh --max-pages 10 --max-content 50` 可扩大单次扫描和正文预算。默认 3 页、20 篇正文，未完成的正文下轮继续；失败正文从 30 秒开始退避，最长 1 小时。扫描窗口外的历史文章需显式扩大页数，不自动补齐。每次从最新页开始。

refresh 退出码：0 表示本次上游扫描和正文处理完成；2 表示部分完成；1 表示失败。JSON 包含 run_id、checks 和 articles。report 成功读取返回 0，原采集状态仍保留在 JSON 中。

Windows 默认数据目录为 `%LOCALAPPDATA%/mpwatch/data`，Cookie 为 `%LOCALAPPDATA%/mpwatch/cookie.txt`；Linux/macOS 使用 `$XDG_DATA_HOME/mpwatch`，未设置时使用 `~/.local/share/mpwatch`。可通过 `MPWATCH_DATA_DIR` 和 `MPWATCH_COOKIE_FILE` 覆盖默认位置，仍要求放在仓库外。数据库文件名为 mpwatch.sqlite3。

如已有登录态也可手动配置 Cookie 文件：UTF-8 单行 `key=value; key=value`，不含 `Cookie:` 前缀。Cookie 保存在本机用户目录，属于明文凭据；不进入日志或仓库。

## 已知边界

- 真实列表接口返回 -2041；独立验证书架认证有效后将其归类为 list_unavailable，再使用 cover 获取最新一篇。结果明确标记 partial/cover_only，不保证两次检查之间的多篇文章均被发现，也不能回补历史。
- 发布时间语义尚未核实，因此 published_at 保持 null，原始时间字段放在 source_times，first_seen_at 使用本机 UTC 时间。
- 首次内容标记 baseline，后续新发现标记 discovered；正文补齐单独输出 content_ready。历史 run 不随后续抓取改变。
- 正文以文本供分析，数据库另存解析后的 HTML；不自动下载图片或识别图片文字，也不把 HTML 当作可安全嵌入网页的内容。
- 请求超时为 HTTP 客户端各阶段最长 30 秒；300 秒预算在请求和等待边界检查，不是强制终止进程的硬截止时间。
- 未提供后台调度、通知、观点提取或交易功能。

## 真实验证

已按用户要求移除全部模拟测试及 pytest。下列命令使用本机真实登录态、实际添加目标公众号并抓取文章，验收记录保存在仓库外数据目录的 verification 子目录。失败后剩余环节标为 not_run，不以预览或模拟响应代替成功。

```powershell
uv run python scripts/verify_live.py --article-url '实际文章链接'
# 或使用已核实的公众号 ID
uv run python scripts/verify_live.py --book-id MP_WXS_实际数字ID
```

2026-09-15 修正后真实执行：登录与目标订阅通过，最新一篇正文入库并经独立进程重读确认；文章列表失败，collection 为 partial。因此完整验收仍为 **not_passed**，不会把最新一篇可用宣称为历史覆盖完整。静态代码检查不计为业务验收通过。

## 下一步

后续需要验证多篇群发覆盖、列表替代入口及持续发文发现。当前真实账号和文章内容只保存在仓库外，详见 [验收方案](docs/acceptance.md)。

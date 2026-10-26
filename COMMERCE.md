# 商品运营监控

本模块把周期采集、商品快照、字段变化和运营建议接入原有 PostgreSQL、MinIO、PydanticAI、OpenTelemetry 与 Next.js 应用。入口为 /commerce，API 文档为 /docs。

## 业务流程

1. 管理员登记来源：授权依据、允许的站点根地址、访问间隔、可选服务器凭证引用。研究员只能使用本租户登记且启用的来源。
2. 指定自有商品和竞品的商品 ID、SKU、来源地址与运营目标。支持立即分析或创建周期任务。
3. 保存原始响应到 MinIO；按明确商品 ID/SKU 提取结构化快照。不同变体、不同卖家报价和聚合价格不能混为一个价格。
4. 与上次成功运行的快照做确定性差异计算，覆盖标题、描述、币种、单位、原价、销售价、促销、库存状态、评分、评价量和图片地址。新出现或编辑过的评价按 ID 识别。
5. PydanticAI 根据快照和变化生成评价主题/情感、运营建议、负责角色、素材交付需求、验证指标和执行前提。每条建议必须引用快照；变化引用与评价原文引用另行校验。数值须出现在引用数据或宿主计算的价格变化比例中。
6. 报告以草稿保存，审核角色可批准。支持 JSON、Markdown 和包含前后快照及原始对象的证据 ZIP 下载。

首次采集建立基线，不宣称发生变化。字段缺失标记为 unavailable，不推断价格归零或促销结束。评价可能是样本，未再返回的评价不认定为已删除。没有成本、毛利或库存数据时，建议注明前提，不虚构优惠幅度或收益。数值与引用校验不等同于事实语义的全面证明，仍需结合业务审核。

## 数据源范围

### 授权 JSON 接口

支持读取经授权的数据接口/商品 Feed，响应为单个商品对象、对象数组或包含 products 数组的对象。通用适配器使用以下统一契约；其它平台的原生字段需要显式映射。淘宝另有下述官方只读适配器，但代码实现不代表实际店铺授权或线上验收已经完成。

    {
      "products": [
        {
          "product_id": "shoe-01",
          "sku_id": "blue-42",
          "title": "蓝色跑鞋",
          "description": "透气鞋面，提供尺码表",
          "currency": "CNY",
          "unit": "pair",
          "list_price": "299.00",
          "sale_price": "249.00",
          "promotion": "限时活动，具体资格见商品页",
          "availability": "in_stock",
          "rating": "4.2",
          "review_count": 120,
          "reviews": [
            {"review_id": "review-01", "text": "尺码比预期偏小", "rating": "2"}
          ],
          "images": ["https://shop.example.com/images/shoe-01.jpg"]
        }
      ]
    }

示例数据仅说明接口格式，不是已采集的真实商品。必要字段为商品 ID、SKU、标题；其余未知字段可缺省。有价格时必须提供币种；评分统一为五分制；每份响应最多匹配一个指定变体，评价样本最多 100 条。接口应提供范围明确的响应，当前不自动遍历全站或未知分页链接。

凭证在服务器环境变量 CI_SOURCE_<名称>_TOKEN 中配置，登记来源时只保存变量名。请求使用 Authorization Bearer header；不将凭证交给模型，也不放进 URL。来源根地址和跳转必须保持同源。

支持 ETag / Last-Modified 条件请求；收到 304 时读取并校验 MinIO 原始对象。同次运行相同来源/URL只请求一次，再匹配不同 SKU。

### 淘宝官方只读接口

来源类型 taobao_open_api 调用 taobao.item.seller.get；固定 HTTPS 网关为 https://gw.api.taobao.com/router/rest。仅面向具备应用接口权限和店铺授权的数据，不是任意竞品商品抓取接口，不包含修改商品、发布促销或订单操作。

实现包括 HMAC-SHA256 签名、GMT+8 时间戳、表单 POST、明确字段请求、商品/SKU 精确匹配、共享访问间隔、拒绝重定向、异常响应拦截、原始对象留存与报告流程。响应只含商品级价格或找不到准确 SKU 时失败，不把聚合价、其它变体价格或搜索摘要作为替代。接口 SKU 标价映射到 list_price；sale_price、促销、评价和评价总数保持未知。官方接口成功返回不代表有权限访问其它未授权店铺。

配置步骤：

1. 在开放平台申请相应接口权限并取得店铺授权。应用密钥与授权 Session 不交给模型。
2. 在服务器 .env 或环境变量中配置 CI_SOURCE_SHOP_TAOBAO_CREDENTIALS，值为 JSON，包含 tenant_id、app_key、app_secret、session。tenant_id 必须等于使用该来源的租户；跨租户引用凭证会在发请求前被拒绝。
3. 在商品监控台选择“淘宝官方接口（已授权店铺）”，根地址使用 https://gw.api.taobao.com，填写授权依据以及上述变量名，不填写实际密钥。
4. 目标地址使用 https://gw.api.taobao.com/router/rest，并填写真实商品 ID 和 SKU。确认来源登记后执行立即分析或创建监控任务。

本地先检查凭证（不发网络请求，不生成报告）：

    python scripts/taobao_api_smoke.py --tenant default --credentials-env CI_SOURCE_SHOP_TAOBAO_CREDENTIALS

取得真实授权、登记来源之后，由可信本地操作员运行完整链路：

    python scripts/taobao_api_smoke.py --tenant default --credentials-env CI_SOURCE_SHOP_TAOBAO_CREDENTIALS --run --source-id <已登记来源UUID> --product-id <真实商品ID> --sku-id <真实SKU>

如需续跑，追加同一个 --idempotency-key。凭证仅通过本地预检不等于平台授权已经通过。只有 --run 实际读取商品、完成模型分析并持久化报告后，commerce_e2e_passed 才为 true。

官方参考：

- https://open.taobao.com/doc.htm?docId=101617&docType=1 （协议、网关及签名）
- https://open.taobao.com/api.htm?docId=24625&docType=2 （商品详情与授权要求）
- https://open.taobao.com/docV3.htm?docId=118848&docType=1 （应用权限和店铺授权流程）

tests/test_taobao.py 覆盖签名、凭证隔离、精确 SKU、未知字段、重定向及限流拒绝；PostgreSQL 集成用例覆盖同商品多 SKU 去重、幂等、标价变化与错误响应不推进基线。使用合成 TOP 响应验证契约，不计作真实淘宝接口验收。

### 公开商品页面的处理

读取页面 Product JSON-LD，通过 productID 与 sku 精确匹配；未提供 productID 时以 sku 作为商品 ID。只接受单个明确 Offer，拒绝把 AggregateOffer 的范围或多个卖家报价当成一个 SKU 价格。

需要动态渲染的来源可设置 render_js=true，使用 Playwright 并保存截图与 HAR。动态浏览限定登记站点的同源请求。未提供可匹配结构化商品数据的页面会明确失败，应接入授权 JSON 数据源或新增针对该来源的字段适配器，不使用猜测价格作为后备。

来源授权依据由管理员登记；robots 检查不能代替来源授权或平台使用规则确认。尚未指定实际平台账号/来源时，不能宣称完成平台生产联调。

## 采集控制

- PostgreSQL 域名级访问预约，在多个 worker 间共享访问间隔。
- 对 401、403、429 或识别到的访问验证页面暂停域名；尊重 Retry-After。
- 连续三次上游/网络故障触发冷却；不通过更换浏览器继续访问已拒绝的目标。
- 单响应限制大小；限制重定向次数；验证公开网络地址、域名策略和同源边界。
- robots 缓存、404/410 无规则处理、策略不可读时默认停止访问。
- 网页/JSON 内容按不可信数据处理；检测到指令型文本会保存原始对象并隔离，不送往分析模型。
- 页面缓存恢复时重新检测注入内容。JSON 条件缓存按 tenant 和 source 隔离。

## 调度、恢复与权限

monitor_worker.py 同时执行研究监控和商品监控。每次领取一个任务，避免串行队列中尚未执行的任务锁提前过期。数据库使用 SKIP LOCKED；worker ID 绑定租约，每 20 秒续租，租约长度 90 秒，旧 worker 不能完成新 owner 的任务。

失败采用指数退避，连续五次失败自动禁用。管理页面可查看失败原因及下次执行时间并手动恢复。暂停阻止后续领取，已运行的一轮允许完成；恢复任务不会绕过域名冷却。

每个周期使用稳定幂等键；采集完每个商品即保存 checkpoint。中断后使用同一个 key 恢复，已采集快照不会重复请求。报告、快照和最新基线指针在同一数据库事务中提交；报告成功后才推进基线。不同请求不得复用同一个 key。

来源、报告、快照和任务按 tenant 校验。管理员管理来源，研究员发起任务，审批人批准报告。当前建议不会自动修改店铺、投放促销或发布素材；素材输出是制作需求单。

## Windows 启动

先按项目 .env.example 配置模型调用和服务访问凭证。模型调用在运行时初始化，所以尚未配置模型凭证时仍可启动页面、登记来源和读取历史数据，但执行分析会失败。

    cd D:\yan\learn\pydanticai-competitor-intelligence-agent
    python -m pip install -e .
    cd frontend
    pnpm install
    cd ..
    .\scripts\start_commerce.ps1 -StartWorker -Tenant default

启动脚本使用本机已有原生 PostgreSQL、MinIO 与 Jaeger，随后启动 API、前端及指定租户 worker。-SkipServices 可复用已运行基础服务。启动器前端使用开发模式方便本机操作；部署环境应执行 pnpm build / pnpm start 并由进程管理器守护 API 与 worker。

页面：http://127.0.0.1:3000/commerce；API：http://127.0.0.1:8787/docs。日志在 data/*.log。API key 只在当前页面内存保存，下载通过 header 鉴权，不把 key 拼入下载 URL。

## API

| 方法 | 路径 | 用途 |
|---|---|---|
| POST / GET | /v1/commerce/sources | 登记 / 查询租户数据源 |
| PATCH | /v1/commerce/sources/{id} | 启停来源 |
| POST | /v1/commerce/runs | 立即分析；Idempotency-Key header 支持恢复 |
| POST | /v1/commerce/monitor-jobs | 创建周期商品监控 |
| GET / PATCH | /v1/monitor-jobs / /v1/monitor-jobs/{id} | 查看状态 / 暂停恢复 |
| GET | /v1/commerce/reports / /v1/commerce/reports/{id} | 列出 / 查看报告 |
| POST | /v1/commerce/reports/{id}/approve | 审批 |
| GET | /v1/commerce/reports/{id}/export?format=json | JSON、markdown 或 bundle |

接口使用现有 X-API-Key、X-User-Id 鉴权。运营建议支持 listing、promotion、pricing、creative、customer_experience 五类。

## 验证位置与运行方法

- tests/test_commerce.py：SKU/币种/未知值、JSON-LD、引用和评价原文、结构契约、限流暂停、重定向、robots。
- tests/test_commerce_postgres.py：真实 PostgreSQL 隔离 schema 中验证恢复、幂等、任务锁与续租、多租户权限、API 审批、证据包导出及 worker 调度。
- CI_TEST_MINIO=1 启用真实 MinIO + PydanticAI FunctionModel 完整路径；特意先返回错误结构，验证框架把错误反馈给模型并修复。网络商品响应是录制测试样本，不是真实平台采集。
- CI_TEST_LIVE_COMMERCE=1 启用真实模型 + PostgreSQL + MinIO 联调；需要有效模型凭证。仍使用标明来源的录制商品响应，不计为真实平台业务验收或 benchmark 分数。

    $env:PYTHONPATH='src'
    $env:CI_TEST_DATABASE_URL='<专用 PostgreSQL 测试连接串>'
    $env:CI_TEST_MINIO='1'
    python -m pytest -q

数据库测试创建随机 ci_test_... schema，结束后仅清理该 schema。MinIO 测试仅清理当前 smoke 的唯一 tenant 前缀对象，不触碰原有业务数据。

## 供应商连接与淘宝实测边界

模型凭证通过本地 .env 加载，不提交到仓库。MODEL_PROXY_URL 可指定模型接口专用代理；不修改系统代理或网页采集策略。模型 HTTP 客户端在 API/worker 关闭时释放。代理需要由部署者提供并持续运行。

    $env:PYTHONPATH='src'
    python scripts/provider_probe.py --call

该命令读取本地配置，验证 /v1/models 并发起一次短推理请求。模型列表可读不等于推理成功；HTTP 401 属于鉴权失败，连接重置属于网络故障，两者分开报告。程序不会打印密钥或原始供应商错误响应。

2026-10-26 现场检查：供应商公开元数据列出 /v1/chat/completions、/v1/messages、/v1beta/models/{model}:generateContent。项目使用第一种接口；另两种仅确认列出，未宣称已完成调用验收。已授权的既有凭证可读取 273 个模型，包含当前研究与评审模型；新提供的另一凭证在模型列表和推理接口均返回 401，未作为有效配置使用。

淘宝公开页面访问检查可复现：

    python scripts/taobao_access_smoke.py --url 'https://item.taobao.com/item.htm?id=625137837209'

该商品 URL 从搜索结果发现，不代表商品/SKU 已验证。2026-10-26 实际结果为 robots.txt 拒绝访问，退出码 2；未继续获取商品详情、未启动浏览器绕过、未生成商品分析报告。测试输出中的 commerce_e2e_passed 明确为 false。访问探针通过或受控阻断，均不能替代真实商品端到端验收。

要完成淘宝真实业务验收，仍须配置官方适配器所需授权凭证，或提供符合通用契约的授权 Feed，以及准确商品/SKU 标识。第三方原生接口如不符合通用契约，仍需额外字段适配。模型和搜索凭证不等于商品平台数据授权。没有执行新的 benchmark 质量评分，也没有用录制数据结果代替真实平台验收。

本轮真实模型集成用例 test_live_model_and_minio_with_recorded_feed 已通过：基线与后续变化两轮均调用配置的真实研究模型，执行 PostgreSQL checkpoint、MinIO 原始对象校验、差异计算及建议/评价引用校验。商品响应仍为明确标注的录制样本，不是淘宝采集。

实测修正了数值契约的可用性：宿主显式提供评价样本数量与规范化评分上限等可复算派生事实；模型输出的未知数字仍拒绝，并在修复反馈中返回具体不受支持的值。没有删除证据校验、替换模型或把失败返回为成功。

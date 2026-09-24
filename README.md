# clash-sub-builder

公开合法代理订阅的 **聚合 → 解析 → 去重 → 可选测速 → 国家分类 → 清洗 → 重命名 → 单订阅输出** 系统。

最终用户只需要添加 **一个** 订阅地址：

```text
https://<your-worker>.workers.dev/sub
```

该订阅包含经过去重和字段校验的候选节点，以及美国 / 日本 / 新加坡 / 香港 / 台湾 / 韩国 / 欧洲 / 其他地区 / 自动测速 / 手动选择等分组；是否在你所在地可用，以客户端测速为准。

> **合规声明**：只处理你主动配置、且有权使用的公开合法订阅源。本项目不扫描互联网、不破解私人订阅、不绕过机场授权。

---

## 1. 项目介绍

| 能力 | 说明 |
|------|------|
| 多源聚合 | `config/sources.yaml` 配置多个公开订阅 |
| 格式识别 | Clash YAML / Clash Meta / Base64 V2Ray / vmess / vless / trojan / ss |
| 真实测速 | Actions 默认先确认 CN 候选的实际出口，再通过 **Mihomo** 链式测试；客户端仍在本地复测 |
| 国家识别 | 可选 GeoLite2 + DNS；关键词词边界匹配兜底 |
| 单文件输出 | 只生成 `output/all.yaml` |
| 分发 | Cloudflare Worker 读取 GitHub Raw（模式 A） |

---

## 2. 架构图

```text
config/sources.yaml
        │
        ▼
GitHub Actions (每 4 小时 / 手动触发)
        │
        ├─ Python 下载订阅
        ├─ 解析节点 → Node
        ├─ SHA256 指纹去重
        ├─ GeoIP / 名称识别国家
        ├─ 验证 CN 节点出口 → 作为链式入口测试候选节点
        ├─ 校验字段、均衡截取各来源和地区（失败节点保留待客户端复测）
        ├─ 统一重命名
        └─ 生成 output/all.yaml + stats.json
                │
                ▼
        git commit & push（无变化不空提交）
                │
                ▼
Cloudflare Worker  (模式 A: fetch GitHub Raw)
                │
                ▼
   https://xxx.workers.dev/sub
        │
        ▼
Clash Verge Rev / Mihomo / 其他 Clash Meta 客户端
```

默认不使用 VPS、不依赖本机常开。Action 使用临时 Mihomo 连接一个出口确认为 CN 的候选节点，再通过它连接待测节点；它**不能**模拟你本人从大陆网络直连待测节点，因此不会因链式失败就删除候选。订阅导入后仍由客户端从所在网络执行 `url-test`。

### 架构可行性说明

| 点 | 结论 |
|----|------|
| 用 aiohttp 直连测 vmess/vless 延迟 | **不可行**（不能走这些协议）→ 已用 Mihomo 真测速 |
| Worker 内测速 | **不可行**（无内核、超时限制）→ Worker 只分发；大陆可用性由本地客户端或大陆测速出口判断 |
| GeoLite2 自动下载 | 需 MaxMind 账号 → **可选**；默认关键词/DNS 兜底 |
| 模式 A Raw 延迟 | 通常可接受；需要更低延迟可改模式 B（KV/R2） |

---

## 3. 项目结构

```text
.
├── main.py
├── config/
│   ├── config.yaml
│   └── sources.yaml
├── src/
│   ├── fetcher.py
│   ├── models.py
│   ├── deduplicate.py
│   ├── geo.py
│   ├── rename.py
│   ├── filter.py
│   ├── generator.py
│   ├── utils.py
│   ├── parsers/
│   │   ├── clash.py
│   │   ├── base64_subscription.py
│   │   ├── vmess.py
│   │   ├── vless.py
│   │   ├── trojan.py
│   │   └── shadowsocks.py
│   └── checker/
│       ├── mihomo.py
│       └── delay.py
├── output/
│   ├── all.yaml
│   └── stats.json
├── worker/
│   ├── src/index.js
│   ├── wrangler.toml.example   # 复制为 wrangler.toml 后填写
│   └── package.json
├── scripts/
│   └── download_mihomo.py
├── .github/workflows/update.yml
├── tests/
├── requirements.txt
└── README.md
```

---

## 4. 快速开始（推荐路径）

```text
Fork 本仓库
  → 修改 config/sources.yaml（填入你的公开订阅）
  → 启用 GitHub Actions 并手动 Run workflow
  → 部署 Cloudflare Worker 并设置 GITHUB_RAW_URL
  → 客户端添加 https://xxx.workers.dev/sub
```

---

## 5. Fork 项目

1. 打开本仓库页面，点击 **Fork**
2. 在你的账号下得到副本，例如：`https://github.com/<you>/auto-vpn`

---

## 6. 配置 sources.yaml

编辑 `config/sources.yaml`：

```yaml
sources:
  - name: my_public_source
    url: "https://example.com/clash.yaml"
    enabled: true

  - name: another_base64
    url: "https://example.com/v2ray.txt"
    enabled: true
```

规则：

- 只填 **你有权使用** 的公开源
- `enabled: false` 会跳过
- **单个源失败不会中断** 整次任务
- 日志中带 `token` / `key` / `secret` 的 URL 会自动打码

### 运行参数（config/config.yaml）

```yaml
checker:
  enabled: true
  cn_chain:
    enabled: true         # 尝试从订阅中选 CN 入口；无合格入口时回退到客户端测速
    max_candidates: 5
    geo_url: "https://ipinfo.io/json"  # HTTPS JSON，country 字段必须为 CN
  timeout: 5000          # ms
  concurrency: 20
  retries: 1             # 主地址失败后测试一个备用地址
  max_latency: 1200      # ms，仅在关闭 cn_chain、直接检测时用于过滤
  test_url: "https://cp.cloudflare.com/generate_204"
  fallback_test_urls:
    - "https://www.apple.com/library/test/success.html"

filter:
  max_nodes_total: 400
  max_nodes_per_country: 60

generator:
  include_latency_in_name: true
  include_city_in_name: false
```

默认在 Actions 下载 Mihomo，从上次生成的订阅优先尝试一个标为 CN 的节点，再尝试本轮不同来源的 CN 节点。通过该节点访问 HTTPS 地理位置接口，只有响应中的 `country` 为 `CN` 才用作入口；后续通过 `dialer-proxy` 链接最多 400 个均衡选取的候选节点。入口节点本身不通过自己链式测试。如果出现没有合格 CN 入口、地理位置接口不可达或全部链式测试失败，订阅仍会保留通过字段校验的候选节点。客户端的「⚡ 自动选择」与地区组会从**你的网络**复测；链式延迟不会写入节点名称，避免误认作本地延迟。客户端每 900 秒重测，可调整 `filter.max_nodes_total` 和 `generator.url_test_interval` 控制开销。

`output/stats.json` 中 `pipeline.cn_chain` 为 `verified` 时表示找到经地理位置接口验证的 CN 出口，`fallback` 表示没找到、没有链式测速，此时 `tested: 0` 并不代表零个可用节点。CN 标签通常依赖节点名称（未安装 GeoIP 数据库时），地理位置 API 也可能不可达或不准确；**CN 出口只证明 CN 节点到其他节点可连接，不能证明你在中国大陆到那些节点直连可用**。想完全跳过 Actions 测速，可设置 `checker.enabled: false`，由客户端独立测速。

### 在 GitHub Actions 中使用大陆出口测速

GitHub 托管 Runner 不在中国大陆，不能直接判断大陆用户到节点的连通性。若不希望常开本机，可准备一台你有权使用的、长期在线的大陆服务器，并提供受访问控制的 SOCKS5 出口。启用后，GitHub Runner 通过该出口连接每个候选节点，测速的入口网络即为该大陆服务器。

1. 在 `config/config.yaml` 保持 `checker.enabled: true`，将 `checker.cn_chain.enabled` 改为 `false`、`checker.dialer_proxy.enabled` 改为 `true`。
2. 在仓库 `Settings -> Secrets and variables -> Actions` 中创建以下 Secrets：`CN_DIALER_SERVER`、`CN_DIALER_PORT`，有认证时再创建 `CN_DIALER_USERNAME`、`CN_DIALER_PASSWORD`。
3. `CN_DIALER_SERVER` 必须是大陆 SOCKS5 服务的地址；不要使用不可信的公共代理，也不要将认证信息写入 Git。

项目会将候选节点的 `dialer-proxy` 指向该 SOCKS5 出口，只用于临时 Mihomo 测速配置；不会写入最终的 `output/all.yaml`。该方案仍依赖大陆出口的稳定性和当地网络、服务条款及法律许可。

固定 SOCKS5 模式会像原先一样按该出口的延迟筛除节点；CN 候选链式模式则始终保留链式失败的节点。如果不希望候选节点请求经过第三方公共 CN 入口，不要启用 `cn_chain`，使用你控制的 SOCKS5 出口或仅使用客户端测速。


---

## 7. 启用 GitHub Actions

1. 仓库 **Settings → Actions → General**
2. 允许 **Allow all actions**
3. **Workflow permissions** 选择 **Read and write permissions**（需要 push `output/`）
4. 打开 **Actions** 页签，选择 **Update Proxy Subscription**
5. 点击 **Run workflow**

默认：

- `cron: 0 */4 * * *`（UTC 每 4 小时）
- 支持 `workflow_dispatch` 手动触发
- 无文件变化时 **不产生空 commit**
- commit message：`chore: update proxy subscription`

流程摘要：

```text
Checkout → Python 3.12 → pip install
  → 按需下载 mihomo → python main.py
  → validate all.yaml → commit output → push
```

> Actions 默认超时上限 6 小时；本 workflow 设为 120 分钟。节点极多时请调低 `max_nodes_total` / `concurrency`。

---

## 8. Cloudflare Worker 创建与部署

### 8.1 安装 Wrangler

```bash
cd worker
npm install
npx wrangler login
```

浏览器完成 Cloudflare 授权。

### 8.2 配置变量

```bash
# 从示例生成本地配置（wrangler.toml 已被 gitignore，不会提交）
cp wrangler.toml.example wrangler.toml
```

编辑 `worker/wrangler.toml`，把 Raw 地址改成你的仓库：

```toml
[vars]
GITHUB_RAW_URL = "https://raw.githubusercontent.com/<you>/<repo>/main/output/all.yaml"
GITHUB_STATS_URL = "https://raw.githubusercontent.com/<you>/<repo>/main/output/stats.json"
```

可选访问令牌：

```bash
npx wrangler secret put SUB_TOKEN          # 不设置则公开
```

> 私有仓库 Raw 默认不可匿名读取。模式 A 要求仓库 **Public**，或改用模式 B。

### 8.3 部署

```bash
cd worker
npm run deploy
```

部署成功后会得到：

```text
https://clash-sub-builder.<your-subdomain>.workers.dev
```

订阅地址：

```text
https://clash-sub-builder.<your-subdomain>.workers.dev/sub
```

若设置了 `SUB_TOKEN`：

```text
https://....workers.dev/sub?token=MY_SECRET
```

### 8.4 自定义域名

1. Cloudflare Dashboard → Workers → 你的 Worker → **Triggers / Custom Domains**
2. 添加域名，例如 `sub.example.com`
3. 按提示完成 DNS

在大陆网络上 `workers.dev` 或 GitHub Raw 可能无法直接访问；请用本机网络验证订阅地址。需要时给 Worker 配置可访问的自定义域名，或通过本地生成的 `output/all.yaml` 导入。自定义域名也不能保证连通性。

### 8.5 Worker 路由

| 路径 | 说明 |
|------|------|
| `GET /` | 状态页 |
| `GET /sub` | 返回 `all.yaml`（`Cache-Control: public, max-age=300` + ETag） |
| `GET /health` | `{"status","updated_at","nodes"}` |
| `GET /stats` | `{"total","countries"}` |

Worker **不做** 协议测速，只分发已生成文件。

---

## 9. 存储方案

### 模式 A（默认，已实现）

GitHub Actions 把 `output/all.yaml` 提交回仓库 → Worker `fetch(GITHUB_RAW_URL)`。

优点：零额外费用、实现简单。  
缺点：依赖 GitHub Raw 可用性；私有库需 token（本 Worker 默认不带 GH token，请保持 public 或改模式 B）。

### 模式 B（可选，README 方案）

1. 创建 KV namespace 或 R2 bucket  
2. Actions 中用 API 上传 `all.yaml`  
3. Worker 从 `env.SUB_KV.get("all.yaml")` 或 R2 读取  

示例（KV，需自行扩展 Actions）：

```js
// Worker 侧伪代码
const body = await env.SUB_KV.get("all.yaml");
return new Response(body, { headers: { "content-type": "text/yaml" } });
```

Actions 侧可用 `cloudflare/wrangler-action` 或 REST API `PUT` 写入 KV。

---

## 10. 本地运行

```bash
# Python 3.11+
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux/macOS
# source .venv/bin/activate

pip install -r requirements.txt
python scripts/download_mihomo.py -o bin  # 默认启用 CN 链式测试

# 编辑 config/sources.yaml 后：
python main.py

# 跳过测速（离线调试）
python main.py --skip-check
```

可选 GeoIP：将 `GeoLite2-Country.mmdb` 放到 `data/GeoLite2-Country.mmdb`（需自行从 MaxMind 获取，遵守许可）。

---

## 11. 客户端添加订阅

### Clash Verge Rev

1. 打开 Clash Verge Rev  
2. **订阅 → 新建**  
3. 远程订阅 URL 填：`https://xxx.workers.dev/sub`（或带 `?token=`）  
4. 更新订阅 → 选择配置 → 启用系统代理  

### Mihomo / Clash Meta

将订阅 URL 写入你的配置管理器，或：

```yaml
# 仅示意：多数 GUI 直接填订阅链接即可
proxy-providers:
  sub:
    type: http
    url: "https://xxx.workers.dev/sub"
    interval: 21600
    path: ./providers/sub.yaml
    health-check:
      enable: true
       url: https://cp.cloudflare.com/generate_204
       interval: 900
```

更简单：直接把 `/sub` 当作完整配置导入（本项目生成的是 **完整 Clash Meta 配置**，含 `proxies` / `proxy-groups` / `rules`）。客户端的自动测速才反映当前设备所在网络的连通性；如果测速地址本身不可达，可调整 `generator.url_test_url` 后重新生成。

---

## 12. 生成配置内容说明

`output/all.yaml` 至少包含：

- `mixed-port` / `allow-lan` / `mode` / `log-level` / `ipv6` / `external-controller`
- `proxies`：全部可用节点  
- `proxy-groups`：
  - 🚀 节点选择（select）
  - ⚡ 自动选择（url-test，全部节点）
  - 🇺🇸 美国 / 🇯🇵 日本 / 🇸🇬 新加坡 / 🇭🇰 香港 / 🇹🇼 台湾 / 🇰🇷 韩国
  - 🇪🇺 欧洲（DE/FR/GB/NL/... 真实国码节点仍显示 🇩🇪 DE-xx）
  - 🌎 其他地区
- `rules`：`GEOIP,LAN,DIRECT` + `MATCH,🚀 节点选择`

节点命名示例（默认 CN 链式模式不显示延迟；直接测速模式可附加 `-85ms`）：

```text
🇺🇸 US-01
🇯🇵 JP-01
🇩🇪 DE-01
```

只改 `name`，不改 uuid/password 等认证字段。

---

## 13. 测速实现（重要）

**不是** aiohttp 直连伪测速。

流程：

1. 启用 `checker.enabled` 时，`scripts/download_mihomo.py` 下载官方 mihomo
2. 按批次生成临时 Clash 配置（节点临时名 `t0000`…）  
3. 启动 mihomo，监听 External Controller  
4. 并发请求：

```http
GET /proxies/{name}/delay?url=https://cp.cloudflare.com/generate_204&timeout=5000
```

5. 记录链式测试延迟；仅在直接测速/固定 SOCKS5 模式中按延迟过滤

默认 `checker.enabled: true` 且 `checker.cn_chain.enabled: true`；CN 候选入口经过 HTTPS 出口国别验证后，以 `dialer-proxy` 连接待测节点。使用 `timeout=5000`、`concurrency=20`、`retries=1`。主测速地址失败时会轮换到 `fallback_test_urls`，最大请求次数仍为 `retries + 1`。通过 CN 入口的链式测试失败不意味着从客户端所在网络也失败；禁用链式模式后的直接测试才按延迟筛选。

---

## 14. 测试

```bash
pip install -r requirements.txt
pytest -q
```

覆盖：Clash 解析、Base64/URI、去重、国家匹配、CN 入口验证与链式回退、重命名、YAML 生成与语法校验；若本机有 Mihomo，也会测试链式临时配置能否被其接受。

---

## 15. 常见错误排查

| 现象 | 可能原因 | 处理 |
|------|----------|------|
| Actions 无法 push | 权限只读 | Settings → Actions → Read and write |
| `Mihomo binary not found` | 下载失败 | 查看 `download_mihomo.py` 日志 / GitHub API 限流 |
| Alive=0 | 检查 `pipeline.cn_chain` 是否为 fallback；链式结果也受入口节点稳定性影响 | 看 Actions 日志 / 客户端测速；确认 CN 候选能通过 HTTPS 出口国别校验 |
| Worker 502 | Raw URL 错误或私有库 | 检查 `GITHUB_RAW_URL`；仓库需 public |
| Worker 401 | 开了 SUB_TOKEN | URL 加 `?token=` |
| YAML 导入失败 | 客户端非 Meta | 使用 Clash Verge Rev / Mihomo |
| 空组报错 | 旧版本 | 本项目空组会填 `DIRECT` 兜底 |
| 测速极慢 / 超时 | 节点过多 | 降低 `batch_size` / `max_nodes_total` / `concurrency` |

---

## 16. 免费额度相关

| 服务 | 说明 |
|------|------|
| GitHub Actions | 私有库有分钟配额；公开库通常更宽松，仍请勿过高频率 |
| Cloudflare Workers | 免费套餐有日请求上限；`max-age=300` 减轻重复拉取 |
| GitHub Raw | 有速率限制；Worker 侧做了缓存与 ETag |
| MaxMind GeoLite2 | 需注册；本项目不强制 |

---

## 17. 安全

- 不要把 `SUB_TOKEN`、私人订阅 token 提交进 Git  
- Worker 密钥用 `wrangler secret put`  
- 日志自动打码 query 中的 token/key/secret  
- 不要在 Issues 中粘贴完整带密钥的 URL  

---

## 18. 本地一键验证清单

```bash
pip install -r requirements.txt
pytest -q
python main.py --skip-check   # 无源时生成骨架配置
python -c "import yaml; yaml.safe_load(open('output/all.yaml',encoding='utf-8'))"
```

---

## License

仅供学习与合法用途。使用公开订阅时请遵守源站条款与当地法律。

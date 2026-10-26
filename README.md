# qwenpaw-plugin-manager

给 **QwenPaw Hub 用 Docker 拉起的每个用户运行时**做插件统一管理的命令行工具：
批量安装、批量升级、批量卸载、批量启停，外加"声明式收敛"（`freeze` → `apply`）。

```
                 ┌──────────────────────────── Hub 宿主机 ────────────────────────────┐
 docker ps ─────▶│ 发现容器（io.qwenpaw.hub.* 标签）                                   │
                 │   └─ 读 Config.Env 里的 QWENPAW_RUNTIME_INTERNAL_TOKEN             │
                 │                                                                     │
                 │  qpm ──HTTP(127.0.0.1:随机端口)──▶ runtime A  /api/plugins/upload   │
                 │      ──HTTP────────────────────▶ runtime B  /api/plugins/...       │
                 │      ──HTTP────────────────────▶ runtime C  ...                     │
                 └─────────────────────────────────────────────────────────────────────┘
```

三个关键事实（都已对 QwenPaw 2.2.x 源码核对，见 [`docs/CONTRACT.md`](docs/CONTRACT.md)）：

1. Hub 的 Docker provisioner 给每个运行时容器打了
   `io.qwenpaw.hub.managed=true` / `io.qwenpaw.hub.instance` /
   `io.qwenpaw.hub.runtime-id` / `io.qwenpaw.hub.owner-id` 标签，并把 API 端口
   发布到 `127.0.0.1:<随机端口>`。
2. 容器环境里有 `QWENPAW_RUNTIME_INTERNAL_TOKEN`；运行时鉴权中间件只认这个
   `X-QwenPaw-Runtime-Token`，拿到即拥有完整 API 权限（Hub 自己的就绪探测和反向
   代理用的同一把钥匙）。
3. 安装/卸载/启停都走运行时**原生**接口：`POST /api/plugins/upload`、
   `DELETE /api/plugins/{id}`、`POST /api/plugins/{id}/enabled`。本工具不实现第二
   条安装管线，不直接往运行时的 `plugins/` 里塞文件（只有回滚会把备份拷回去），
   也不做 bundle 校验的第二套实现。

所以它不需要 Hub 的数据库、不需要 Fernet 密钥、不需要安装 QwenPaw，**零第三方
依赖**（纯标准库，Python ≥ 3.11）。

## 安装

零安装（内网/离线首选）：把仓库拷到 Hub 宿主机，直接跑 `bin/qpm`。它自带
`PYTHONPATH`、支持软链，不依赖 pip 和任何第三方包：

```bash
ln -s "$PWD/bin/qpm" /usr/local/bin/qpm    # 之后任意目录都能用
qpm --version

# 等价写法
python3 -m qpm --version                   # 在仓库根目录
python3 bin/qpm --version
```

愿意装包的话（需要 setuptools 和可用的索引/镜像）：

```bash
python3 -m pip install .        # 或 pipx install .
qpm --version                   # 同时注册了 qwenpaw-plugin-manager
```

要求：Python ≥ 3.11，能执行 `docker` 命令（或提供 `--inventory`）。

## 快速开始

```bash
qpm doctor                                   # 先体检：发现/令牌/插件 API/备份目录
qpm runtimes --probe                         # 列出运行时、QwenPaw 版本、已装插件数
qpm plugins                                  # 插件清单矩阵
qpm plugins --plugin 'offline-*' --json      # 按 glob 过滤，机器可读

# 先看计划，再动手（--dry-run 打印的就是真正要执行的动作）
qpm install ./dist/offline-market-0.1.3.zip --dry-run
qpm install ./dist/offline-market-0.1.3.zip --yes

# 只升级"已安装且更旧"的插件；没装的默认跳过
qpm upgrade ./dist/*.zip --yes
qpm upgrade ./dist/*.zip --install-missing --yes   # 缺失的也补装

# 卸载（支持 glob；--keep-files 只卸载不删文件/配置）
qpm uninstall offline-market --yes
qpm uninstall 'offline-*' --runtime 'rt-a*' --yes
qpm uninstall old-plugin --keep-files --yes

# 启停
qpm disable offline-market --yes
qpm enable  offline-market --yes
```

| 命令 | 用途 |
| --- | --- |
| `runtimes` | 列出运行时；`--probe` 探活，`--save-inventory` 导出清单 |
| `plugins` | 每个运行时的插件清单（版本/启停/状态） |
| `install` | 安装或收敛（`--mode sync\|install\|upgrade`） |
| `upgrade` | 只升级"已安装且更旧"的插件 |
| `uninstall` | 卸载（`--keep-files` 只卸载不删文件） |
| `enable` / `disable` | 批量启停 |
| `freeze` / `apply` | 导出状态文件 / 收敛到状态文件 |
| `doctor` | 体检：发现、令牌、插件 API、备份能力 |

`SOURCE` 可以是：

- 单个插件目录（含 `plugin.json`）→ 本地打成可复现的 ZIP 再上传；
- 单个 `.zip`；
- `http(s)://` URL（下载后原样上传，适合内网制品库）；
- 一个"发布目录"：里面同时放多个插件目录和多个 zip，一次性批量处理。

## 网页控制台

CLI 之外还有一个只监听回环地址的本地控制台。浏览器拿不到 Docker socket、也连不上
运行时的随机端口，所以必须由宿主机上的进程代理；这个服务端和 CLI **共用同一套**
发现、计划、执行代码，不存在第二套逻辑。

```bash
qpm serve                                  # http://127.0.0.1:8787
qpm serve --open                           # 顺便打开浏览器
qpm serve --port 9000 --inventory /secure/inventory.json
qpm serve --no-token                       # 明确信任本机时才用，会告警
```

启动时打印随机令牌和带 `#token=` 的地址；页面把令牌存进 `sessionStorage`，之后所有
`/api` 请求都带 `X-QPM-Token`。`qpm serve --dry-run` 会把控制台变成只读沙盘：计划
照常计算，执行只返回 `planned`，不会动任何运行时。同一时间只允许一个执行任务。

| 页面 | 能做什么 |
| --- | --- |
| Inventory | 每个运行时一条"暗盒"：端口、QwenPaw 版本、租户、每个插件槽的版本/启停/健康，行内 Enable / Disable / Uninstall |
| Install & upgrade | 拖入 `.zip`、填宿主机路径或 URL，选 mode 后 **Preview plan** → **Run changes**；计划逐条给出 install/upgrade/skip 与原因 |
| Uninstall | 从当前选中目标的已装插件里点选（chips），可只卸载保留文件，先预览再执行 |
| Apply state | 粘贴/上传 state JSON，或从某个运行时 Freeze 后下载；无 source 的条目变成漂移检查 |
| Runs | 运行历史 + 每 700ms 轮询的实时结果，结束后自动刷新清单 |

安全姿态：默认只绑 `127.0.0.1`；令牌必填；写操作必须是 `application/json` 且拒绝
跨站 `Origin`（防 CSRF），严格 CSP、`no-store`、`nosniff`；静态资源白名单；上传仅
接受 `.zip` 且做 ZIP 合法性检查。`--no-token` 或绑到非回环地址都会在启动时告警。

### 本地 HTTP API

脚本化时直接调同一套接口即可（都需要 `X-QPM-Token`；`--no-token` 时省略）：

```bash
curl -s -H "X-QPM-Token: $TOKEN" http://127.0.0.1:8787/api/inventory
curl -s -X POST -H "X-QPM-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"operation":"install","filters":{"runtime":["rt-a"]},"params":{"sources":["/srv/plugins/offline-market-0.1.3.zip"]}}' \
  http://127.0.0.1:8787/api/plan
curl -s -X POST -H "X-QPM-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"operation":"install","filters":{"runtime":["rt-a"]},"params":{"sources":["/srv/plugins/offline-market-0.1.3.zip"]}}' \
  http://127.0.0.1:8787/api/run
curl -s -H "X-QPM-Token: $TOKEN" http://127.0.0.1:8787/api/jobs/<job_id>
```

| 方法与路径 | 说明 |
| --- | --- |
| `GET /api/meta` | 版本、发现来源、备份/并发等执行配置 |
| `GET /api/runtimes` | 发现结果（不访问运行时 API） |
| `GET /api/inventory` | 发现结果 + 每个运行时的 QwenPaw 版本与插件清单 |
| `POST /api/plan` | 纯计划，无副作用；入参见下 |
| `POST /api/run` | 重新计划后后台执行，返回 `job_id`；同一时间只允许一个任务（否则 409） |
| `GET /api/jobs` / `GET /api/jobs/{id}` | 任务列表 / 含计划与逐条结果的详情 |
| `POST /api/upload` | multipart 字段 `file`（`.zip`），返回 `upload_id`；在 sources 里写 `@upload_id` |
| `GET /api/freeze?runtime=<id>` | 下载该运行时的 state 文档 |

计划/执行请求体：

```json
{
  "operation": "install | uninstall | enable | disable | apply",
  "filters": {"runtime": ["rt-a", "rt-b*"], "tenant": [], "owner": [], "include_stopped": false},
  "params": {
    "sources": ["/srv/plugins/x.zip", "@up_1a2b3c4d5e", "https://mirror/x.zip"],
    "mode": "sync",
    "reinstall": false,
    "allow_downgrade": false,
    "install_missing": true,
    "enable": null,
    "plugins": ["offline-*"],
    "protect": ["qwenpaw-hub"],
    "keep_files": false,
    "state": {"schema_version": 1, "plugins": [], "removed": []}
  }
}
```

只给 `apply` 用 `params.state`；其余操作忽略它。

## 运行时怎么被发现

默认走 Docker：

```bash
docker ps -a --filter label=io.qwenpaw.hub.managed=true
docker inspect <ids>
```

从容器读取：运行时 ID、租户、属主、状态、镜像、发布端口、`QWENPAW_WORKING_DIR`
（据此推出宿主机上的 `plugins/` 目录，备份/回滚要用）、以及边界令牌。

只处理 `running` 的容器；`--include-stopped` 会把停止的也带进计划（安装计划会显示
为跳过）。多个 Hub 实例共用一个 Docker 时用 `--docker-instance <fingerprint>` 过滤。

当 qpm 不在 Hub 宿主机上（比如跳板机上跑），先用 `runtimes --save-inventory`
导出清单：

```bash
qpm runtimes --save-inventory /secure/inventory.json     # 含令牌，自动 chmod 600
qpm runtimes --inventory /secure/inventory.json --probe  # 之后都用它
```

清单格式见 [`examples/inventory.example.json`](examples/inventory.example.json)；
`token_env` 可以引用环境变量，避免把密钥写进文件。

## 选择范围

所有命令都支持同一组过滤器（可重复，glob 匹配）：

| 选项 | 作用 |
| --- | --- |
| `--runtime GLOB` | 运行时 ID 或容器名 |
| `--tenant GLOB` | 租户 ID（`QWENPAW_TENANT_ID`） |
| `--owner GLOB` | Hub 用户 ID |
| `--plugin GLOB` | 插件 ID（`install/apply` 里筛 bundle，`plugins` 里筛展示） |
| `--protect GLOB` | 永不触碰的插件（默认空，建议把底座插件写进去） |
| `--include-stopped` | 带上未运行的运行时 |
| `--concurrency N` | 并行处理的运行时数（默认 4，单个运行时内串行） |

## 版本语义

| 已装版本 vs 目标 | `--mode sync`（默认） | `install --mode install` | `upgrade` |
| --- | --- | --- | --- |
| 未安装 | 安装 | 安装 | 跳过（`--install-missing` 则安装） |
| 相同 | 跳过（`--reinstall` 重装） | 跳过（`--reinstall` 重装） | 跳过 |
| 更旧 | 升级 | 跳过 | 升级 |
| 更新 | 跳过（`--allow-downgrade` 才降级） | 跳过 | 跳过 |

版本比较是内置的 PEP 440 子集：`2.2.2rc1 < 2.2.2 < 2.2.2.post1`，`2.2 == 2.2.0`，
`+local` 忽略。解析不了的版本按字符串比较，绝不猜。

计划阶段还会做两道兼容性闸门，跟宿主一致：

- `plugin.json` 的 `sdk.version` major 必须与运行时 SDK（默认 `1.0.0`）一致且不更高；
- `qwenpaw_version.min` 高于运行时版本 → 跳过（宿主本来也会拒绝）；
  上限只给提示，因为 QwenPaw 当前只强制下界。

## 安全设计

- **计划可见**：任何会改运行时的动作都必须先过 `_confirm`。终端交互确认；非 TTY
  必须显式 `--yes`，否则退出码 2、一个字节都不改。`--dry-run` 永远只打印。
- **改完必验**：每个动作后回读 `GET /api/plugins/{id}`，核对版本、`status`；
  卸载后核对确实消失；启停后核对 `enabled`。
- **先备份再替换**：升级/降级/重装/卸载前，从宿主机目录把 `plugins/<id>` 拷到
  `~/.qpm/backups/<时间戳>/<runtime>/<id>`（`--backup-dir` 可改，`--no-backup` 关闭）。
- **失败自动回滚**：验证失败时把备份拷回，再调运行时 `POST /api/plugins/install`
  用**容器内路径**重新装载旧版本（Hub 把宿主机 working 目录挂进容器，所以这条路可
  行）。回滚结果在输出里单列 `rolled_back`，退出码仍为 1。
- **幂等**：重复执行同一计划不会重复安装；并发下另一个工具抢先安装时，回读版本
  相同即视为成功。
- **串行化**：同一运行时内的动作严格串行（运行时本身按插件加锁）；只有运行时之间
  并行。
- **令牌不外泄**：默认只从容器环境读，不打印；`--save-inventory` 会显式警告。

## 声明式收敛：freeze → apply

```bash
# 1. 以某个运行时为准，导出它的插件集合（version/enabled 已填好）
qpm freeze --from-runtime rt-a -o plugins.lock.json

# 2. 给每个条目补上 source（目录 / zip / URL），需要钉版本的写 version
#    {
#      "schema_version": 1,
#      "plugins": [
#        {"id": "offline-market", "version": "0.1.3",
#         "source": "./dist/offline-market-0.1.3.zip", "enabled": true},
#        {"id": "report-pack", "version": "1.2.0",
#         "source": "http://artifacts.internal/plugins/report-pack-1.2.0.zip",
#         "runtimes": ["rt-a*"]}
#      ],
#      "removed": ["legacy-plugin"]
#    }

# 3. 全量收敛（有 source 的装/升，没 source 的只做漂移检查，removed 的卸载）
qpm apply plugins.lock.json --dry-run
qpm apply plugins.lock.json --yes
```

`source` 留空、只写 `version` 时，`apply` 变成**漂移检测**：版本不一致或缺失都会
判失败，适合放进 cron / systemd timer 做巡检：

```bash
qpm apply plugins.lock.json --json --yes > /var/log/qpm-drift.json
```

定时任务里记得带 `--yes`（非 TTY），并用 `--concurrency` 控制对宿主的压力。

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 全部成功或被跳过（含 `--dry-run`） |
| 1 | 至少一个动作失败（含已回滚、运行时不可达） |
| 2 | 用法/发现/来源文件错误（没执行任何动作） |
| 130 | 交互确认时被拒绝或 Ctrl-C |

## JSON 输出

`--json` 时 stdout 只有一份 JSON（进度和警告仍然走 stderr）：

```json
{
  "command": "install",
  "dry_run": false,
  "elapsed_seconds": 5.21,
  "runtimes": [{"runtime_id": "rt-a", "tenant_id": "tenant-a", "state": "running",
                "base_url": "http://127.0.0.1:32768", "qwenpaw_version": "2.2.2",
                "inventory_error": ""}],
  "bundles": [{"id": "offline-market", "version": "0.1.3", "origin": "...",
               "sha256": "…", "size": 12345}],
  "results": [{"runtime_id": "rt-a", "plugin": "offline-market",
               "action": "upgrade", "state": "ok",
               "current_version": "0.1.2", "target_version": "0.1.3",
               "message": "upgrade 0.1.2 -> 0.1.3", "duration_ms": 812,
               "http_status": null, "rolled_back": false, "rollback": "",
               "backup_path": "/home/ops/.qpm/backups/20261026-101500/rt-a/offline-market"}],
  "summary": {"actions": 1, "runtimes": 1, "ok": 1, "skipped": 0,
              "planned": 0, "failed": 0, "rolled_back": 0}
}
```

## 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| `docker CLI not found` / `permission denied` | qpm 要能执行 `docker ps/inspect`。把用户加入 `docker` 组，或换有权限的账号；或改用 `--inventory`。 |
| `runtime rejected the boundary token (401)` | 容器被重建、令牌轮换，重新发现一次即可（别缓存旧 inventory）。 |
| `HTTP 503: Plugin loader is not ready yet` | 运行时刚启动；qpm 会自动重试（`--retries`），仍失败就稍后再跑或先 `doctor`。 |
| `HTTP 409 … plugin_busy` | 有任务正持有插件；加 `--interrupt-tasks` 让运行时中断后继续。 |
| `bundle is X MiB but the runtime upload limit is Y MiB` | 运行时设了 `QWENPAW_UPLOAD_MAX_SIZE_MB`；调大它或精简插件。 |
| `no host plugins directory known` / `plugin directory not visible` | 备份/回滚不可用：容器没把 working 目录 bind 到本机，或 inventory 没写 `plugins_dir`。安装/卸载仍可执行，只是失败后不能自动回滚。 |
| 安装成功但 `status: failed` | qpm 会判失败并回滚；去读该运行时的日志（`<working>/logs` 或 `docker logs`）。 |
| 备份路径/回滚不可用 | 把 qpm 跑在 **Hub 宿主机**上（而不是 Hub 容器里）：`docker inspect` 给的 `Mounts.Source` 是宿主机路径，容器内看不到。安装/卸载仍可用，只是失败后不能自动回滚。 |
| `qpm: error: no runtimes selected` | 过滤条件没命中，或全部未运行；检查 `--runtime/--tenant/--owner`、加 `--include-stopped`、先 `qpm runtimes`。 |

## 边界（刻意不做）

- **不导出/下载**运行时上已有的插件，所以没有"从运行时 A 复制到 B"。要跨环境分发，
  请用制品库里的 zip/目录作为 `SOURCE`。
- **不管插件配置**（`config.plugins.<id>`）：只做安装/升级/卸载/启停。配置属于租户数据。
- **不读 Hub 数据库**：令牌只从容器环境拿。如果只想读运行时而拿不到 Docker，
  用 `--inventory`。
- **不做定时调度**：交给 cron / systemd timer 调 `qpm apply`。
- **单宿主机**：一次运行只面向一个 Docker 端点；多宿主机就多跑几个 qpm。
- **不猜 SDK 版本**：插件库跟着 QwenPaw 升 SDK major 时，用 `--host-sdk` 或环境变量
  `QPM_HOST_SDK_VERSION` 显式告诉 qpm，而不是让它"试一下"。

## 开发

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest        # 126 个测试：假运行时真开 HTTP，覆盖安装/升级/卸载/回滚/控制台
```

目录：

| 路径 | 内容 |
| --- | --- |
| `qpm/sources.py` | Docker/清单发现、过滤 |
| `qpm/runtime_client.py` | 运行时插件 API 客户端（含重试、多部分上传） |
| `qpm/bundles.py` | 目录/zip/URL → 可复现 ZIP，恶意包拒绝 |
| `qpm/planner.py` | 纯函数计划（版本、兼容性、保护名单） |
| `qpm/executor.py` | 并行执行、验证、备份/回滚 |
| `qpm/commands.py` | 命令编排、确认、报告 |
| `qpm/webapi.py` | 本地控制台服务端（令牌、任务、上传、计划） |
| `qpm/web/` | 控制台前端（原生 HTML/CSS/JS，无构建、无 CDN） |
| `tests/fake_runtime.py` | 假运行时（真 HTTP + 真边界令牌校验） |

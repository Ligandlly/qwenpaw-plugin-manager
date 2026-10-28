# 与 QwenPaw 的契约（已对 2.2.x 源码核对）

本文记录 qpm 依赖的每一条宿主行为、出处，以及升级 QwenPaw 时该复查什么。
路径均相对 QwenPaw 仓库根目录。

## 1. 运行时怎么被找到

Hub 的 Docker provisioner 创建容器时写了这些标签与环境变量
（`src/qwenpaw/hub/docker_provisioner.py`，`start()` 与 `_labels()`）：

| 载体 | 名字 | 用途 |
| --- | --- | --- |
| label | `io.qwenpaw.hub.managed=true` | 发现入口 |
| label | `io.qwenpaw.hub.instance=<12 hex>` | 同一宿主机多 Hub 实例隔离 |
| label | `io.qwenpaw.hub.runtime-id` | 运行时 ID |
| label | `io.qwenpaw.hub.owner-id` | Hub 用户 ID（`--owner` 过滤） |
| env | `QWENPAW_RUNTIME_ID` | 同上 |
| env | `QWENPAW_TENANT_ID` | 租户（`--tenant` 过滤） |
| env | `QWENPAW_WORKING_DIR` | 容器内 workspace（默认 `/workspace`） |
| env | `QWENPAW_RUNTIME_INTERNAL_TOKEN` | 边界令牌 |

端口发布固定为 `ports={"8088/tcp": ("127.0.0.1", None)}`，即宿主机随机端口、只绑
回环。`_published_port()` 会拒绝非回环绑定，qpm 同样只接受 `127.0.0.1`/`::1`。

workspace 挂载：`volumes={宿主机 record.working_dir: workspace}`，因此宿主机上
`<working_dir>/plugins/<id>` 就是容器内 `<workspace>/plugins/<id>`
（`src/qwenpaw/constant.py`: `PLUGINS_DIR = WORKING_DIR / "plugins"`）。qpm 用它做
备份和"用容器内路径重装"的回滚。

## 2. 鉴权

容器模式下 `QWENPAW_AUTH_ENABLED=true`，同时启用了边界中间件
（`src/qwenpaw/app/auth.py`, `_RuntimeBoundaryMiddleware`）：

- 环境里有 `QWENPAW_RUNTIME_INTERNAL_TOKEN` 时，**所有**请求都必须带
  `X-QwenPaw-Runtime-Token`，比较用 `hmac.compare_digest`，空值/非 ASCII 直接不匹配；
- 匹配即放行，不再走用户登录态；
- 不匹配返回 401 `{"detail": "Invalid runtime boundary token"}`。

令牌由 Hub 的加密凭据库生成并在容器启动时注入
（`src/qwenpaw/hub/credentials.py`: `get_or_create_runtime_secret()`，稳定存在
`runtime-control:<tenant>:<runtime>` 作用域）。重建容器不会换令牌，但删掉凭据后
会换——所以 qpm 收到 401 时不重试，直接提示重新发现。

Hub 自己也是这么用的：`docker_provisioner._wait_until_ready()` 先匿名探
`/api/version`，401 后带令牌重探；`control_app.personal_runtime_proxy` 也是加这个
头转发。

## 3. 插件 API（`src/qwenpaw/app/routers/plugins.py`）

| 方法与路径 | 入参 | 返回 / 语义 |
| --- | --- | --- |
| `GET /api/plugins` | — | 数组；每项含 `id/name/version/enabled/loaded/status/plugin_type/...`，未加载的也列（磁盘扫描） |
| `GET /api/plugins/{id}` | — | 单个插件；不存在 404 |
| `POST /api/plugins/upload` | multipart 字段 `file`（必须 `.zip`）+ query `force`、`interrupt_tasks` | 解压 → `_find_plugin_dir`（根目录或唯一顶层目录里的 `plugin.json`）→ 依赖闸门 → 装载并热生效 |
| `POST /api/plugins/install` | JSON `{source, force, interrupt_tasks}` | `source` 是**运行时进程可见**的路径或 http(s) ZIP URL |
| `DELETE /api/plugins/{id}` | query `interrupt_tasks` | 卸载并删文件；busy 时 409，清理未完成时 409 带结构化 detail |
| `POST /api/plugins/{id}/unload` | query `interrupt_tasks` | 只从进程卸载，保文件与配置 |
| `POST /api/plugins/{id}/enabled` | JSON `{enabled, interrupt_tasks}` | 写 `config.plugins.<id>.enabled`；禁用=卸载，启用=从磁盘装载 |

qpm 只用 `/upload` 安装（跨容器边界唯一可靠的方式），`/install` 只用于**回滚**：
把备份拷回宿主机目录后，让运行时按容器内路径重新装载，无需重启。

### 升级语义

`upload?force=true` 在插件已加载时走
`PluginLoader.load_plugin_from_path(..., force=True)`
（`src/qwenpaw/plugins/loader.py`）：

1. 先 `reload_plugin_unlocked()` 卸载旧实例；
2. 用新目录替换 `plugins/<id>`（`replace_files=True`）；
3. 重新读 `config.plugins.<id>` 的 `enabled`（所以升级不会偷偷把禁用插件启用）；
4. 重新调用插件注册、提交资源。

宿主**没有**降级保护，也没有"版本必须递增"的检查——版本方向完全由 qpm 的
planner 把关。同 `id` 不同 `version` 的包就是一次替换，不需要先卸载。

装载失败时会抛 `SDKRestartRequiredError`（409/500）或普通 `RuntimeError`；qpm 把这
类失败当失败处理并回滚文件。

### 失败状态

- 503：`plugin_loader` 尚未就绪（运行时刚启动）→ qpm 重试。
- 409 `plugin_busy`：有任务持有插件 → 提示 `--interrupt-tasks`。
- 409 结构化 detail（`loaded/clean/quiescent/needs_restart/errors`）：卸载未静默 →
  视为失败并回滚。
- 安装后 `status` 可能是 `failed`：qpm 回读时判失败（并回滚）。

## 4. 版本兼容闸门

- **SDK**（`src/qwenpaw/plugins/sdk_version.py`）：`sdk.version` 必须与宿主
  `SDK_VERSION`（2.2.x 为 `1.0.0`）**同 major 且不更高**；`sdk.capabilities` 必须是
  宿主支持集合的子集。qpm 在计划阶段按同一规则预判（`--host-sdk` 可覆盖）。
- **QwenPaw 版本**（`src/qwenpaw/_version_compat.py`）：`qwenpaw_version` 语义是
  `>=min, <max`，但当前实现**只强制下界**，上界检查被注释掉；无 `qwenpaw_version`
  时退回 `min_version`/`max_version`。qpm 复刻"只强制下界 + 上界仅提示"。
- **SKILL 扫描**与插件无关，此处不涉及。

## 5. 控制台里的用户名（仅显示）

Hub 的 `control.db`（`get_hub_root()`，默认 `~/.qwenpaw/hub/`）里 `hub_users`
表提供 `user_id -> username`。控制台只**只读**查这张表，把运行时的 `owner-id`
换成用户名显示；发现、计划、执行、过滤器和 `--json` 输出仍然只认 runtime id。

- 优先级：inventory 的 `username` 字段 > `control.db` 查询 > runtime id；
- 路径：`--hub-db` / `$QPM_HUB_DB` / `$QWENPAW_HUB_DIR/control.db` /
  `$QWENPAW_WORKING_DIR/hub/control.db` / `~/.qwenpaw/hub/control.db` /
  `~/.copaw/hub/control.db` / `/app/working/hub/control.db`；
- 数据库不存在、无权限或缺表时静默退回 runtime id，`qpm -v` 会打印原因。

## 6. 升级 QwenPaw 后要复查的点

1. `docker_provisioner._labels()` / `start()` 的标签、环境变量、端口是否变了；
2. `auth.py` 的边界令牌头名与"全放行"语义是否变了；
3. `routers/plugins.py` 的六条路由与参数名（`force`/`interrupt_tasks`）；
4. `loader.load_plugin_from_path(force=True)` 是否仍然先卸载再替换；
5. `sdk_version.SDK_VERSION` 是否升了 major（qpm 的 `--host-sdk` 默认值随之调整）；
6. `PLUGINS_DIR` 是否仍在 `WORKING_DIR/plugins`（备份路径与回滚容器路径依赖它）。

对应的回归方式：`qpm doctor` + 一对真实运行时的 `qpm install --dry-run` +
`qpm plugins`。仓库内的 `tests/fake_runtime.py` 是上述契约的可执行版本。

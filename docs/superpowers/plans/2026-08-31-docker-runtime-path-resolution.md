# Docker 运行目录显式配置实现计划

> **面向 AI 代理的工作者：** 在当前会话内按 TDD 执行；步骤使用复选框跟踪进度。

**目标：** 消除后端镜像从 `site-packages` 反推项目目录造成的调度器 `missing`，让 Docker 服务显式共享 `/app/runtime`。

**架构：** `DataSyncControlService` 优先读取 `FINANCE_AGENT_PROJECT_ROOT` 和 `FINANCE_AGENT_RUNTIME_DIR`，未配置时保留源码运行的原有回退逻辑。镜像布局固定在 `/app`，Compose 不提供无效的项目根目录覆盖；运行目录默认 `/app/runtime`，并通过同一个 `FINANCE_AGENT_DOCKER_RUNTIME_DIR` 表达式同步控制 API/scheduler 环境变量、卷目标以及 scheduler 状态和事件文件，确保代码安装位置与运行数据位置解耦。

**技术栈：** Python 3.11、pytest、Docker Compose、FastAPI、React/Vite、Playwright CLI。

---

### 任务 1：锁定路径解析回归

**文件：**
- 修改：`tests/test_data_sync_control_service.py`
- 修改：`tests/test_docker_deploy_templates.py`

- [x] 编写子进程导入测试，设置 `FINANCE_AGENT_PROJECT_ROOT` 和 `FINANCE_AGENT_RUNTIME_DIR` 后断言模块常量及默认状态文件均指向临时目录。
- [x] 扩展 Docker 模板测试，断言后端镜像以及 API、scheduler Compose 服务声明 `/app` 和 `/app/runtime`。
- [x] 运行路径解析和非默认 Compose 目录红灯测试，确认因环境变量或挂载目标尚未接入而失败。

### 任务 2：实现显式路径配置

**文件：**
- 修改：`src/finance_agent/application/data_sync_control_service.py`
- 修改：`deploy/docker/Dockerfile.backend`
- 修改：`deploy/docker/Dockerfile.scheduler`
- 修改：`docker-compose.yml`

- [x] 新增 `_resolve_configured_path()`：非空环境变量经 `expanduser().resolve()` 解析，否则返回既有默认路径。
- [x] 将 `ROOT_DIR` 改为优先读取 `FINANCE_AGENT_PROJECT_ROOT`，将 `RUNTIME_DIR` 改为优先读取 `FINANCE_AGENT_RUNTIME_DIR`。
- [x] 在镜像和 Compose 中固定 `FINANCE_AGENT_PROJECT_ROOT=/app`，避免暴露与镜像布局不一致的覆盖入口。
- [x] 让 `FINANCE_AGENT_DOCKER_RUNTIME_DIR` 同步控制两个服务的环境变量、卷目标和 scheduler 输出参数。
- [x] 运行任务 1 的测试，确认全部通过。

### 任务 3：全面回归与真实部署验证

**文件：**
- 不新增生产文件。

- [x] 运行 `tests/test_data_sync_control_service.py`、`tests/test_docker_deploy_templates.py` 和相关 API/前端数据同步测试。
- [x] 运行完整 Python 测试集与前端 `npm run build`、数据同步视图测试。
- [x] 运行默认与非默认 runtime 路径的 `docker compose config`，重建并重启 `finance-agent-api`。
- [x] 验证直连 API 与 Nginx 代理接口均返回 `status=ok`、`health.status=healthy`、状态文件为 `/app/runtime/base_data_scheduler/status.json`。
- [x] 使用 Playwright 打开 `http://127.0.0.1:5173`，进入数据同步页面，截图并确认页面不再显示调度器 `missing`。
- [x] 运行 GitNexus `detect-changes`；工作区存在大量既有混合改动时，结合目标符号 `impact` 结果单独判断本修复范围。

# 架构与模块说明

## 启动链路

```text
启动投资仪表盘.vbs → 启动投资仪表盘.bat → launcher.py
  → 本项目 .venv 内的 Python → 根目录 app.py
  → dash_core 共享层与业务模块 → 浏览器访问 127.0.0.1
```

前台入口 `scripts/windows/run.bat` 直接运行根目录 `app.py`，方便手动查看日志。
安装和停止脚本从自身目录向上两级定位项目，不要求用户先切换工作目录。
旧 `investment-dashboard/app.py` 使用 `runpy` 委托给根目录入口，旧 `run.bat` 委托给集中脚本。

## 代码如何组织

`dash_core/__init__.py` 创建 Flask app，并提供 JSON 读写、全局状态、锁、账户路径、行情工具、缓存和模型调用。
`app.py` 导入功能模块，模块里的 `@app.route` 装饰器将函数注册到同一个 app；然后在直接执行入口时启动服务。
因此“只导入模块”和“直接运行入口”的作用不同，直接运行还会启动预热与研报后台工作。

| 模块组 | 主要文件 |
| --- | --- |
| 干净副本初始化 | `bootstrap.py`：只补齐缺失的公共市场规则，不覆盖用户数据。 |
| 行情、账户与交易 | `quotes.py`、`trades.py`、`daystreak.py`、`nav.py` |
| 风险、建议与治理 | `risk.py`、`advice.py`、`gov.py`、`rules.py`、`gamble.py` |
| 宏观与周期 | `macro.py`、`macro_alarm.py`、`vv_macro.py`、`gold_stock.py`、`hdlb.py` |
| 信息与机会 | `xueqiu.py`、`xq_stock.py`、`arb.py`、`bias.py`、`hk_ipo.py`、`hk_ipo_perf.py`、`jin_gold.py`、`ipo_alert.py` |
| 框架与个股详情 | `frame.py`、`diary.py`、`stock_detail.py`、`vv_spread.py`、`forecast.py`、`report.py`、`events.py` |
| 量化与复盘 | `quant.py`、`quant_rebuild.py`、`attribution.py`、`close_prep.py` |
| 浏览器子进程 | `_xq_stock_worker.py`、`_bili_space_worker.py` |

正式子进程文件以 `_` 开头，但必须保留在源码中。忽略根目录临时脚本的规则有前导 `/`，不会屏蔽包内 worker 和 `__init__.py`。

## 目录定位与账户隔离

`BASE_DIR` 仍由 `dash_core/__init__.py` 的位置向上一级计算；提升包目录后自然指向仓库根目录，无需改动共享层代码。

- 首页来自 `static/index.html`，个股页来自 `static/detail/`，静态文件仍通过 `/static` 提供。
- `DATA_DIR = BASE_DIR/data`，原有数据整体迁移，内部文件名和层次不变。缺失的 A/H 与 ETF 公共规则由 `bootstrap.py` 从 `resources/defaults/` 初始化；已有规则不覆盖。
- 默认账户沿用 `data/` 根内文件，其他账户使用 `data/accounts/<id>/`。账户元数据、切换和权限口径保持原样。
- 研报查找顺序是 `DASH_REPORT_DIR` → 根目录 `研报/` → 上一级旧目录 `研报/` → `data/研报/`，只选择存在且含 Markdown 的目录。
- 雪球专用浏览器目录位于 `BASE_DIR` 下，独立于日常 Edge 个人配置。
- 个股抓取 worker 根据自身文件位置找到包目录，父进程仍通过同一个 Python 环境启动子进程。

持仓、交易、行情和回测均继续使用既有 JSON 格式，未改数据库或重新计算已有结果。

## 前端与依赖

前端使用原生 JavaScript、CSS 和 HTML，第三方资源保存在 `static/vendor/`，无需 Node.js 或前端构建步骤。
后端使用 Flask；Requests 负责 HTTP；Playwright 复用 Edge；NumPy 用于风险与数值计算；xlrd 读取 `.xls`；pdfplumber 解析研报 PDF。

本次结构调整保留共享层及跨模块引用方式。以后若拆分应用工厂或 Blueprints，需要另行迁移共享状态、导入顺序和后台线程，不能只移动文件。

## 学习资料

- [Flask 项目布局](https://flask.palletsprojects.com/en/stable/tutorial/layout/)：理解包、测试、静态资源与环境目录。
- [Python runpy](https://docs.python.org/3/library/runpy.html)：了解旧入口如何委托执行文件及 `run_name` 的作用。
- [Python pathlib](https://docs.python.org/3/library/pathlib.html)：了解基于文件位置计算路径。
- [uv CLI](https://docs.astral.sh/uv/reference/cli/)：安装工具的隔离与二进制依赖参数。

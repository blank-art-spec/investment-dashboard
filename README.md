# 投资仪表盘（分享版）

基于 **Python、Flask 和原生 JavaScript** 的本地投资管理工具。通过浏览器查看持仓、风险、宏观指标、市场机会和策略回溯，数据保存在本机 JSON 文件中。

服务监听 `127.0.0.1`，适合个人在 Windows 电脑使用。分享版启动入口默认地址为 **http://127.0.0.1:5001/**。

## 功能

| 模块 | 主要功能 |
| --- | --- |
| 持仓与交易 | A 股、港股、美股行情，多账户持仓和现金，交易流水、候选池、汇率换算、当日盈亏与连续盈利天数。 |
| 投资框架 | 投资地图、组合分层、投资日记、宏观判断报警、红利低波评分及投资行为体检。 |
| 宏观观察 | 估值、利率、汇率、商品与航运指标，大 V 宏观观点，黄金与黄金股周期观察。 |
| 寻找机会 | 雪球关注和观点整理、观点校验、LOF 套利、超跌观察、港股打新评分与表现跟踪、月度金股及打新截止提醒。 |
| 个股详情 | K 线、基本面、财务预测、舆情、近期事件、本地研报阅读和 AI 更新。 |
| 量化与复盘 | 收盘准备、评分快照、策略回测、收益归因、真实净值记录与失败条件。 |

AI 复核、观点整理、研报生成等功能需要自行配置兼容的模型接口；行情与抓取需要相应外部数据源可访问。项目记录与分析投资信息，没有券商下单接口。

## 快速开始

推荐使用 **64 位 Windows、Python 3.12 和已安装的 Microsoft Edge**。

1. 新电脑或尚未安装环境时，双击根目录 **`安装依赖.bat`**。脚本下载本项目专用的 Python，在 `.venv/` 安装依赖；不修改系统 PATH 或注册表。
2. 已有本地环境时，直接双击 **`启动投资仪表盘.vbs`**，后台启动并打开浏览器。也可运行 `启动投资仪表盘.bat`。
3. 需要看控制台日志时，手动双击 **`scripts/windows/run.bat`**。
4. 在网页右上角“设置”填写模型接口基础地址、模型名称和 API Key；首次雪球抓取时在项目专用的 Edge 窗口完成登录。
5. 结束使用时双击 **`停止服务.bat`**。脚本核对本项目解释器、启动命令和端口后停止服务。

本地安装脚本只使用现成的二进制依赖包，不预编译业务代码，也不自动启动服务。
已有 `.venv/` 和 `.runtime/` 的本地副本可以继续使用；未来从源码仓库下载的新副本需要先安装环境。

### 手动在控制台运行

在项目根目录打开 PowerShell，按顺序执行：

```powershell
$env:DASH_PORT = '5001'
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONUTF8 = '1'
& .\.venv\Scripts\python.exe -B .\app.py
```

直接运行 `app.py` 且没有设置 `DASH_PORT` 时，沿用原后端默认端口 `5000`；双击入口默认 `5001`。
源码修改后请手动停止并重新启动服务，自动重载保持关闭。
本次结构整理没有自动编译、启动服务或运行业务验证脚本，完整验证步骤见 [手动验证清单](docs/manual-validation.md)。

## 目录结构

```text
.
├── README.md                       # 项目介绍和使用入口
├── CONTRIBUTING.md                 # 代码维护与验证约定
├── requirements.txt                # 运行依赖与版本下限
├── requirements.lock.txt           # 新电脑复现当前环境的版本约束
├── pyproject.toml                  # 可选静态检查配置，不执行构建
├── .gitignore / .gitattributes      # 仓库忽略规则和文本换行约定
├── .github/                        # 问题反馈和变更说明模板
├── app.py                          # 注册全部功能路由并启动服务
├── launcher.py                     # 后台启动、等待端口及打开浏览器
├── dash_core/                      # 共享层与业务模块
├── static/                         # 首页、详情页与本地前端依赖
├── tests/                          # 原有 verify_*.py 验证脚本
├── scripts/windows/                # 安装、停止及前台运行实现
├── docs/                           # 架构、结构优化和手动验证说明
├── examples/                       # 不含真实凭据的配置示例
├── resources/defaults/             # 公共 A/H 配对与 ETF 分类默认规则
├── investment-dashboard/           # 旧 app.py、run.bat、依赖路径兼容入口
├── data/                           # 本机账户和运行数据，忽略入库
├── 研报/                           # 本机报告，忽略入库
├── .venv/ / .runtime/               # 本机依赖环境，忽略入库
└── 安装依赖.bat / 启动投资仪表盘.* / 停止服务.bat
```

当前采用可以直接从源码运行的平铺结构，保留原有 `dash_core` 包名、路由注册方式和前后端相对位置。
详细解释见 [架构与模块说明](docs/architecture.md) 和 [结构调整记录](docs/repository-structure.md)。

## 配置与数据

| 配置 | 说明 |
| --- | --- |
| `DASH_PORT` | 服务端口；分享版入口默认 `5001`，直接运行后端默认 `5000`。 |
| `DASH_NOWARM=1` | 跳过 `app.py` 中的首页接口预热；不关闭全部业务后台线程。 |
| `DASH_REPORT_DIR` | 自定义本地研报目录，优先于默认 `研报/`。 |
| `DASH_LLM_API_KEY` | 文件内未配置密钥时的兜底值，已有文件值优先。 |
| `DASH_XUEQIU_COOKIE` | 文件内未配置 Cookie 时的兜底值，已有文件值优先。 |

普通偏好在 `data/settings.json`，敏感配置在 `data/secrets.json`。
可阅读 [空配置示例](examples/README.md) 了解字段；示例不会自动加载，不要用空示例覆盖已有设置。

本次整理完整迁移了原本的账户、缓存、观点和研报文件，没有用示例替换本机数据。
仓库包含完整源码、空配置和公共市场规则。首次运行会仅补齐缺失的 `data/ah_pairs.json` 与 `data/etf_family.json`，保留已有本地规则。`data/`、浏览器会话及研报不会随源码下载；历史观点和已有研报需要单独在本机恢复或重新获取。

备份时保存 `data/` 和 `研报/`；需要保留浏览器登录态时另行保存抓取专用目录。
移动整个项目到其他位置后，虚拟环境中的绝对路径可能失效，请在停止服务后重建该副本的 `.venv/`，保留数据目录。

## 依赖与故障排查

运行依赖包括 Flask、Requests、Playwright、NumPy、xlrd 和 pdfplumber，版本下限在 `requirements.txt` 中。
`requirements.lock.txt` 纳入仓库，记录当前 Python 3.12 环境版本，供新电脑首次安装复现。`requirements.local.lock.txt` 由本地安装脚本生成，记录当前电脑的实际安装版本；已有本地约束优先，其次使用仓库约束。该本地文件不入库。
当前抓取代码复用系统 Edge，不需要另行下载 Chromium。

| 情况 | 查看位置或处理方式 |
| --- | --- |
| 双击没有打开页面 | 先确认环境已安装，检查根目录 `log_launch.txt` 和 `server.log`，或手动运行前台入口。 |
| 端口已占用 | 使用前台入口查看情况；如使用自定义端口，安装之外的启动和停止操作都设置相同的 `DASH_PORT`。 |
| AI 功能报未配置 | 在“设置”检查基础地址、模型名和密钥。基础地址后由程序追加 `/chat/completions`。 |
| 抓取失败 | 检查 Edge、专用窗口登录状态及数据源访问情况，查看服务日志。 |
| 新源码副本缺少历史数据 | 仓库不包含本机数据；从个人备份恢复或通过界面录入、获取。 |

## 在其他工作场所开发

源码仓库：[blank-art-spec/investment-dashboard](https://github.com/blank-art-spec/investment-dashboard)。仓库为公开，可直接克隆；推送修改需要仓库写入权限。

```powershell
git clone https://github.com/blank-art-spec/investment-dashboard.git
cd investment-dashboard
```

随后双击 `安装依赖.bat`，按上面的快速开始手动启动。请使用新的本地虚拟环境，不从其他电脑复制 `.venv/`。
公共分类规则会自动准备；持仓、交易、日记、历史观点、研报和密钥按需要从你自己的备份恢复。

继续开发时先拉取最新代码，修改后手动验证，再提交与推送。详细步骤与提交范围见 [跨电脑开发说明](docs/development-workflow.md)。

## 开发与参考

开发约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。`pyproject.toml` 仅提供可选的静态检查配置；依赖安装继续使用 `requirements.txt`，无需 `pip install .`。

结构参考了 [Flask 官方示例](https://github.com/pallets/flask/tree/main/examples/tutorial)、[Finalynx 投资管理项目](https://github.com/MadeInPierre/finalynx)
以及 [Alfred 投资仪表盘](https://github.com/Fournierp/alfred)，仅借鉴目录与说明组织，没有复制其业务代码。
布局取舍可阅读 [PyPA：src 与平铺布局](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)。

当前尚未指定开源许可证。仓库使用公开可见性，不配置自动编译或自动启动业务服务的流程。

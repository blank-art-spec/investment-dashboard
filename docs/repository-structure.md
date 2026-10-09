# 项目结构调整记录

本文记录首次本地结构整理阶段。当时尚未建立 Git 仓库；随后按用户授权准备 GitHub 仓库并补充公共规则与版本约束，当前开发流程见 [跨电脑开发说明](development-workflow.md)。

整理日期：2026-10-09。

## 选择的布局

采用平铺结构：根目录直接包含 `app.py`、`dash_core/`、`static/`、`tests/`、依赖清单和文档。
这是可以直接运行的 Python 应用布局，适合当前双击启动和本地 JSON 存储方式。

参考仓库中，Flask 官方示例将应用包、测试、说明与项目配置分开；Finalynx 将投资应用、文档、示例和测试分开；Alfred 提供根目录说明、依赖清单和独立功能目录。
本次采用这些分层习惯，同时保留既有 Flask 全局实例与模块导入注册方式，没有复制外部项目代码或切换框架。

`src/` 布局常与可安装包结合，需要额外处理包安装、静态资源和运行数据路径。
当前应用通过文件位置寻找数据和 worker，因此先保留可从源码直接运行的布局。
`pyproject.toml` 只用于可选静态检查设置；没有声明构建后端，也未配置自动构建工作流。

## 原位置与新位置

| 原位置 | 新位置 | 处理方式 |
| --- | --- | --- |
| `investment-dashboard/app.py` | `app.py` | 原入口逻辑保留，补充中文说明；旧路径增加转发入口。 |
| `investment-dashboard/dash_core/` | `dash_core/` | 整体迁移；仅调整研报目录优先级及其说明。 |
| `investment-dashboard/static/` | `static/` | 原样迁移。 |
| `investment-dashboard/tests/` | `tests/` | 原样迁移，脚本仍根据父目录找到项目。 |
| `investment-dashboard/data/` | `data/` | 全部原数据迁移，内部文件内容不改。 |
| `investment-dashboard/研报/` | `研报/` | 全部原报告迁移，不改正文。 |
| `investment-dashboard/requirements.txt` | `requirements.txt` | 依赖内容原样迁移；旧清单转发根清单。 |
| `investment-dashboard/server.log` | `server.log` | 原日志迁移，启动器继续写同名文件。 |
| `investment-dashboard/gold_stock_ai.json` | `gold_stock_ai.json` | 保留原文件，作为本地结果忽略入库。 |
| `安装本地环境.ps1` | `scripts/windows/install.ps1` | 集中实现；原文件保留转发入口。 |
| `停止本地服务.ps1` | `scripts/windows/stop.ps1` | 集中实现；保留原进程识别规则和旧入口。 |
| `investment-dashboard/run.bat` | `scripts/windows/run.bat` | 集中前台运行；旧路径转发。 |

`launcher.py` 的业务目录从旧子目录改为本文件所在的根目录。
`.venv/`、`.runtime/` 及根目录中文启动批处理和 VBS 不移动，因此已有解释器路径仍可使用。

## 仓库文件

新增根目录 README、维护约定、docs 文档、examples 空配置和 GitHub 反馈模板。
根目录 `.gitignore` 统一排除本机环境、真实数据、研报、会话、日志、临时文件及备份。
`.gitattributes` 约定源码文本换行，并保留 Windows GBK 入口字节。
未添加许可证文件：开源授权方式应由项目所有者确定。

本次没有创建远端仓库或本地 Git 仓库，没有提交、推送或运行自动编译。

## 迁移核对与恢复依据

迁移前的入口文件备份与全部原应用文件的 SHA-256 清单保存在本机：
`.runtime/structure-backup-20261009/`。该目录不纳入未来仓库。

`baseline.json` 记录原路径、长度与文件指纹，配合本页迁移表，可以核对新位置的内容。
其中 `app.py` 仅补充说明，`dash_core/report.py` 调整研报候选顺序，旧依赖、忽略规则和运行入口被兼容文件替代；其他原有文件应保持原指纹。
静态检查结果见 [本次核对记录](structure-validation.md)，程序实际运行仍按 [手动验证清单](manual-validation.md) 验证。

需要回退时先手动停止服务，再按迁移表恢复路径，并从备份还原入口和被编辑的文件。
若整理后已产生新交易、日记或报告，先备份当前 `data/` 和 `研报/`，保留最新数据，不能用旧副本覆盖。

## 参考出处

- [Flask 官方教程示例](https://github.com/pallets/flask/tree/main/examples/tutorial)
- [Finalynx 投资管理项目](https://github.com/MadeInPierre/finalynx)
- [Alfred 投资仪表盘](https://github.com/Fournierp/alfred)
- [PyPA 对 src 与平铺布局的说明](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)

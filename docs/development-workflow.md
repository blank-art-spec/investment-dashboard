# 跨电脑开发说明

仓库：[blank-art-spec/investment-dashboard](https://github.com/blank-art-spec/investment-dashboard)，默认分支 `main`，公开可见性。

## 第一次克隆

安装 Git 后可直接执行以下命令克隆公开源码：

```powershell
git clone https://github.com/blank-art-spec/investment-dashboard.git
cd investment-dashboard
```

在新目录双击 `安装依赖.bat`，安装独立 Python 3.12 及依赖，再手动运行 `启动投资仪表盘.vbs`。
安装优先采用本机 `requirements.local.lock.txt`，不存在时采用仓库 `requirements.lock.txt`，安装后生成本机版本记录。
完整文件包括后端、页面与 vendor 资源、两个抓取 worker、Windows 入口、原验证脚本、公共分类规则和文档。

首次启动只会补齐缺失的两份公共分类规则，不会从仓库带入真实账户或登录信息。
股票观点、账户、交易、投资日记、净值和研报需要用户恢复备份或重新录入、获取，模型和雪球登录按新电脑的设置配置。

## 每次继续开发

在目录内执行以下步骤，拉取前先确保自己的修改已妥善保存：

```powershell
git status
git pull --ff-only
```

有未提交修改时，先检查它们，再自行提交或暂存；不要直接覆盖本地修改。
`--ff-only` 不会自行合并分叉历史，遇到分叉时先检查两边提交。

修改源码后，按 [手动验证清单](manual-validation.md) 手动重启与检查。新增或修改的方法保留中文注释，说明参数和返回值。
准备同步时显式检查提交范围，例如：

```powershell
git status
git diff
# 按本次修改选择具体文件，不强行添加被忽略的数据目录。
git add app.py dash_core static tests scripts resources requirements.txt requirements.lock.txt README.md docs
git diff --cached --stat
git diff --cached
git commit -m "说明本次修改的具体内容"
git push origin main
```

首次基线包含所有适合入库的项目文件；后续按实际修改选择文件。
分支开发时可自行创建功能分支并推送该分支，不必把未验证工作直接同步至 `main`。

## 入库与本机文件

| 纳入仓库 | 留在本机 |
| --- | --- |
| 后端、前端、静态 vendor 与验证脚本 | `.venv/`、`.runtime/`、CLI 工具与迁移备份 |
| 安装、启动和停止入口 | 真实 `data/`、密钥、Cookie、浏览器 profile |
| 依赖下限及共享版本约束 | 本机依赖约束、运行日志、下载与临时输出 |
| 公共市场分类默认值、空配置示例和文档 | 持仓、交易、历史观点、账户快照及本地研报 |

只克隆源码可建立完整运行环境并使用全部功能入口，已有个人历史数据通过单独备份恢复。
不要跨电脑复制 `.venv/`，它含有绝对路径；不需要重新下载前端 vendor，它已纳入源码。

## 验证范围

仓库准备阶段进行文件完整性、语法、默认规则、忽略规则和干净克隆静态检查，不自动编译、启动服务或执行抓取与模型调用。
其他电脑的 Edge、网络和模型接口仍需使用者按手动清单验证。

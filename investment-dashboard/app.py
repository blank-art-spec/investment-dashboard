# -*- coding: utf-8 -*-
"""旧目录的兼容入口。实际业务入口已整理到仓库根目录 app.py。

直接运行本文件仍按原方式启动；导入本文件时仅加载路由，不执行主启动分支。
推荐新代码直接使用根目录入口和 dash_core 包。
"""
import os
from pathlib import Path
import runpy
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_application(run_name):
    """在正确的源码目录执行根目录应用，兼容旧的启动路径。

    参数 run_name：str，"__main__" 执行服务启动分支；其他名称只加载模块与路由。
    返回：dict，根目录 app.py 执行后产生的命名空间，包含 Flask app 等变量。
    异常：原应用加载或启动的异常继续抛出，便于使用原有日志定位问题。
    学习说明：runpy 运行文件而不复制业务逻辑；工作目录与导入路径均指向仓库根目录，
        保证静态资源、数据文件及子进程仍采用同一套定位规则。本方法不编译代码。
    """
    project_path = str(PROJECT_ROOT)
    if project_path not in sys.path:
        sys.path.insert(0, project_path)
    os.chdir(project_path)
    return runpy.run_path(str(PROJECT_ROOT / "app.py"), run_name=run_name)


def main():
    """供旧命令 python investment-dashboard/app.py 启动同一套应用。

    参数：无；源码目录通过本文件位置计算，不依赖调用者所在目录。
    返回：无；服务持续运行直到用户关闭，业务行为由根目录 app.py 决定。
    学习说明：只有直接执行兼容入口才启动服务，导入时不会执行此方法。
    """
    _run_application("__main__")


if __name__ == "__main__":
    main()
else:
    # 保留 from app import app 等旧用法；私有预热函数也继续委托给同一份实现。
    _application = _run_application("dashboard_compat")
    app = _application["app"]
    BASE_DIR = _application["BASE_DIR"]
    _warm_http = _application["_warm_http"]

# -*- coding: utf-8 -*-
"""投资仪表盘 一键启动器(共享版)。

双击 启动投资仪表盘.vbs -> 本脚本(隐藏运行) ->
  检测端口 -> 未运行则后台启动 Flask -> 等就绪 -> 打开浏览器。

与原版区别: 项目位置 / Python 解释器都按本文件夹相对定位, 换台电脑也能用。
"""
import os
import socket
import subprocess
import sys
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
# 源码位于仓库根目录；后台仍以 app.py 为入口，保持停止脚本的识别规则。
PROJ = HERE
PORT = int(os.environ.get('DASH_PORT') or '5001')
URL = 'http://127.0.0.1:%d/' % PORT
LOG = os.path.join(HERE, 'log_launch.txt')


def log(msg):
    """向本目录日志追加一条带时间的中文启动信息。

    参数 msg：str，可阅读的运行情况或异常描述。
    返回：无。日志写入失败时静默返回，避免日志问题阻止启动。
    """
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write('[%s] %s\n' % (ts, msg))
    except Exception:
        pass


def find_python():
    """返回本项目虚拟环境中的 Python 解释器，优先使用隐藏窗口的版本。

    参数：
        无。HERE 是启动器所在目录，所有解释器路径都由它计算。
    返回：
        str：存在的 .venv/Scripts/pythonw.exe 或 python.exe 的绝对路径。
    异常：
        FileNotFoundError：本地环境尚未安装，请先运行“安装依赖.bat”。
    学习说明：
        虚拟环境只保存本项目的依赖，固定使用它可避免串用其他项目的版本。
        不采用 PATH 或 DASH_PYTHON 中的解释器，保证双击启动也保持目录隔离。
    """
    for name in ('pythonw.exe', 'python.exe'):
        candidate = os.path.join(HERE, '.venv', 'Scripts', name)
        if os.path.isfile(candidate):
            return candidate
    raise FileNotFoundError('本项目 Python 环境不存在，请先运行“安装依赖.bat”。')


def port_open(port):
    """检查本机 TCP 端口是否有程序监听，不停止或修改监听程序。

    参数 port：int，待检查端口，例如共享版默认 5001。
    返回：bool，能连接为 True，否则为 False。
    学习说明：只连接 127.0.0.1，超时为 0.4 秒，最后始终关闭探测套接字。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        s.connect(('127.0.0.1', port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def start_server(py):
    """使用指定解释器在后台启动当前目录下的 Flask 服务。

    参数 py：str，find_python 返回的本项目 Python 绝对路径。
    返回：bool，子进程成功创建为 True；创建失败时记录异常并返回 False。
    学习说明：cwd 指定业务目录，日志写入 server.log，环境变量仅传给子进程。
    """
    svc_log = os.path.join(PROJ, 'server.log')
    flags = subprocess.DETACHED_PROCESS | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    env = dict(os.environ)
    env['DASH_PORT'] = str(PORT)
    env['PYTHONIOENCODING'] = 'utf-8'
    # 仅在项目子进程中屏蔽用户级依赖，并禁止自动写入 Python 字节码。
    env['PYTHONNOUSERSITE'] = '1'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['PYTHONUTF8'] = '1'
    try:
        with open(svc_log, 'w', encoding='utf-8') as fo:
            p = subprocess.Popen([py, 'app.py'], cwd=PROJ,
                                 stdout=fo, stderr=subprocess.STDOUT,
                                 creationflags=flags, close_fds=True, env=env)
        log('Flask 服务已启动 (PID=%s, py=%s)' % (p.pid, py))
        return True
    except Exception as e:
        log('启动 Flask 失败: %r' % e)
        return False


def main():
    """执行双击启动流程：检查目录、启动服务、等待就绪并打开网页。

    参数：无；目录和端口取自文件顶部的 HERE、PROJ、PORT 常量。
    返回：无；缺少本地环境或启动失败时记录中文日志并结束。
    学习说明：已有端口监听时不会重复启动，也不会关闭任何已有进程。
    """
    log('== 启动器运行 ==')
    if not os.path.isdir(PROJ):
        log('! 项目目录不存在: %s' % PROJ)
        return
    if port_open(PORT):
        log('端口 %d 已被占用, 服务可能已在运行, 直接打开网页.' % PORT)
    else:
        try:
            py = find_python()
        except FileNotFoundError as exc:
            log('! ' + str(exc))
            return
        if not start_server(py):
            log('! 启动失败, 请看 ' + os.path.join(PROJ, 'server.log'))
            return
        for _ in range(60):
            time.sleep(0.5)
            if port_open(PORT):
                break
        if not port_open(PORT):
            log('! 30 秒内端口未就绪, 可能启动失败. 请看 ' + os.path.join(PROJ, 'server.log'))
            return
        log('端口 %d 就绪.' % PORT)
    try:
        webbrowser.open(URL)
        log('已打开浏览器 -> %s' % URL)
    except Exception as e:
        log('打开浏览器失败: %r' % e)


if __name__ == '__main__':
    main()

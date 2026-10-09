# -*- coding: utf-8 -*-
"""为干净的源码副本准备公共市场规则，真实账户、密钥和历史数据仍保留在本机。"""
import json
from pathlib import Path

DEFAULT_FILES = ("ah_pairs.json", "etf_family.json")


def ensure_default_data(base_dir, data_dir):
    """仅在本地规则缺失时，从仓库默认资源建立规则文件。

    参数：
        base_dir：str 或 Path，仓库根目录，用于定位 resources/defaults 中的公共规则。
        data_dir：str 或 Path，可写的本地数据目录，通常为共享层 DATA_DIR。
    返回：list[str]，本次实际创建的文件名；已有规则完整保留，列表可能为空。
    异常：资源缺失、JSON 无效或目录不可写时抛出带中文提示的异常，便于定位安装问题。
    学习说明：仓库只保存公共分类表，运行目录保存用户修改后的副本。使用独占创建模式
        x 防止两个进程同时首次启动时互相覆盖，不能覆盖已有账户、设置或规则。
        本方法不联网、不编译代码，只处理固定清单中的两份 JSON 规则。
    """
    source_dir = Path(base_dir) / "resources" / "defaults"
    target_dir = Path(data_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    created = []
    for name in DEFAULT_FILES:
        target = target_dir / name
        if target.exists():
            continue
        source = source_dir / name
        if not source.is_file():
            raise FileNotFoundError(f"默认市场规则缺失，请确认仓库文件完整：{source}")
        content = source.read_text(encoding="utf-8")
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"默认市场规则不是有效 JSON：{source}") from exc
        try:
            with target.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
        except FileExistsError:
            continue  # 另一个进程已经创建文件，沿用它的结果，不覆盖。
        except OSError as exc:
            raise OSError(f"无法建立本地市场规则，请检查数据目录权限：{target}") from exc
        created.append(name)
    return created

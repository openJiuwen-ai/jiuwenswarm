# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
import os
import pathlib
import shutil
import json
import re
import logging

from .project_config import project_config

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = project_config.WORKSPACE_ROOT


def make_empty_dir(path: str):
    p = pathlib.Path(path)
    if not p.exists():
        p.mkdir()
        return
    clear_dir(path)


def get_extension_split(filename):
    """使用split()提取文件后缀"""
    parts = filename.split('.')
    if len(parts) > 1:
        return parts[-1].lower()
    return ""


def clear_dir(path: str):
    p = pathlib.Path(path)
    if not p.exists():
        return
    if p.is_file():
        logger.error(f"Error: {path} is a file, not a directory.")
        return
    try:
        for file_name in p.iterdir():
            clear_dir(file_name)
        if p.exists():
            p.rmdir()
    except PermissionError:
        logger.error(f"Error: Permission denied for {path}.")
    except OSError as e:
        logger.error(f"Error: {e.strerror} for {path}.")


def contain_file(file_path: str) -> bool:
    return True if os.path.exists(file_path) else False


def create_dir(path: str):
    p = pathlib.Path(path)
    if not p.exists():
        p.mkdir()


def delete_file(file_path: str):
    p = pathlib.Path(file_path)
    if p.is_file() and p.exists():
        p.unlink()


def save_txt_file(path, content: str):
    work_dir, _ = os.path.split(path)
    if not os.path.exists(work_dir):
        os.makedirs(work_dir, exist_ok=True)

    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


def write_to_file(file_path, content):
    with open(file_path, 'a', encoding='utf-8') as f:
        f.write(content)


def create_empty_file(path: str):
    if os.path.exists(path):
        return
    f = open(path, 'w', encoding='utf-8')
    f.close()


def move_file(src_path: str, dst_path: str):
    shutil.move(src_path, dst_path)


def copy_file(src_path: str, dst_path: str):
    shutil.copy(src_path, dst_path)


def read_txt_file(path) -> str:
    if not contain_file(path):
        raise RuntimeError("path not found")

    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def read_json_file(path):
    """
    读取json文件
    """
    try:
        with open(path, 'r', encoding='utf-8') as file:
            data = json.load(file)
            return data
    except FileNotFoundError:
        logger.error(f"错误：文件 不存在")
        return None
    except json.JSONDecodeError:
        logger.error(f"错误：文件不是有效的JSON格式")
        return None
    except Exception as e:
        logger.error(f"读取文件时发生错误：{type(e.__class__)}")
        return None


def get_task_dir(base_path=WORKSPACE_ROOT):
    task_id = "11111111"
    if task_id:
        # taskID带有变化的序列号
        pos = task_id.rfind('_')
        task_id = task_id[:pos] if pos != -1 else task_id
        # 文件路径太长，传不上去OSMS
        task_id = task_id[-6:]
    else:
        task_id = None
    if task_id:
        dir_path = os.path.join(base_path, f"task_{task_id}")
    else:
        dir_path = os.path.join(base_path)
    os.makedirs(dir_path, exist_ok=True)
    return dir_path


def save_each_frame_data(context, follow_label=""):
    """
        保存每一帧外发数据，且每一帧数据文件以taskid命名
    """
    if not context:
        logger.info("File content is empty!")

    task_id = "11111111"
    if not task_id:
        logger.error("task_id is None, cannot auto increment")
        return ""
    dir_path = get_task_dir()
    if not dir_path:
        logger.error("dir_path is None, cannot save frame data")
        return ""
    if not follow_label:
        path = os.path.join(str(dir_path), f"{task_id[-8:]}.txt")
    else:
        path = os.path.join(str(dir_path), f"{task_id[-8:]}_{follow_label}.txt")
    save_txt_file(path, context)

    return path


def task_id_seq_auto_increment(task_id):
    if not task_id:
        logger.error("task_id is None, cannot auto increment")

    last_underscore_index = task_id.rfind('_')
    if last_underscore_index == -1 or last_underscore_index == len(task_id) - 1:
        logger.error(f"Invalid task_id format: '{task_id}' - no trailing underline with number")

    num_part = task_id[last_underscore_index + 1:]
    try:
        num = int(num_part)
    except ValueError:
        logger.error(f"Invalid number format in task_id: '{task_id}' - trailing part '{num_part}' is not a number")


def sanitize_filename(filename: str, replace_char='_'):
    """
    简单的文件名清理函数
    """
    # 定义非法字符
    illegal_chars = r'[<>:"/\\|?*]'

    # 替换非法字符
    safe_name = re.sub(illegal_chars, replace_char, str(filename))

    # 移除首尾空白和点
    safe_name = safe_name.strip().strip('.')

    # 确保文件名非空
    if not safe_name:
        raise RuntimeError("sanitized filename is empty")

    return safe_name


def decode_text_with_bom_fallback(data: bytes) -> str:
    # 有序的编码检测列表（BOM检测优先）
    encoding_candidates = [
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xff\xfe\x00\x00", "utf-32-le"),
        (b"\x00\x00\xfe\xff", "utf-32-be"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
    ]

    # 1) BOM检测阶段
    for bom, encoding in encoding_candidates:
        if data.startswith(bom):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                logger.erro(f"BOM matched {encoding} but decode failed")
                break  # BOM匹配但解码失败，继续无BOM检测

    # 2) 无BOM：按常见编码依次尝试
    for enc in ("utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            logger.error(f"Decode with {enc} failed, trying next")

    # 3) 最后兜底
    try:
        return data.decode("gb18030", errors="replace")
    except Exception as e:
        logger.error(f"All decode attempts failed: {e}")
        # 终极兜底：忽略所有错误
        return data.decode("utf-8", errors="ignore")

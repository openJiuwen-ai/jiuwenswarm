# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""file.write 错误码常量.

三段式数字码 999001XXX，前两段固定 999001，第三段 3 位区分具体错误。
"""

from __future__ import annotations

# -- file.write 基础校验 (250-255) ------------------------------------------

ERR_FW_MISSING_TASK_OR_PATH = "999001250"  # 请求参数不完整：task_id 或 path 字段缺失或为空
ERR_FW_MISSING_CONTENT = "999001251"  # 请求参数不完整：content 字段未提供（值为 None）
ERR_FW_CONTENT_TOO_LARGE = "999001252"  # 写入内容超过单文件大小上限（_MAX_FILE_CONTENT_SIZE = 1MB）
ERR_FW_PATH_ESCAPE = "999001253"  # 路径越界：resolve 后的绝对路径不在 workspace/skill/ 目录内
ERR_FW_FILE_NOT_FOUND = "999001254"  # 目标路径在 workspace/skill/ 下不存在或不是普通文件
ERR_FW_BINARY_FILE = "999001255"  # 目标文件无法以 UTF-8 解码，判定为二进制文件，不允许写入

# -- SKILL.md frontmatter 格式校验 (260-263) ---------------------------------

ERR_FW_SKILLMD_NO_FRONTMATTER_START = "999001260"  # SKILL.md 缺少起始 --- 标记（文件不以 --- 开头）
ERR_FW_SKILLMD_NO_FRONTMATTER_END = "999001261"  # SKILL.md 缺少闭合 --- 标记（frontmatter 未正确关闭）
ERR_FW_SKILLMD_YAML_PARSE_ERROR = "999001262"  # SKILL.md frontmatter YAML 语法解析失败（yaml.safe_load 抛出 YAMLError）
ERR_FW_SKILLMD_YAML_NOT_DICT = "999001263"  # SKILL.md frontmatter YAML 解析结果不是字典（如纯字符串、列表等）

# -- SKILL.md frontmatter key 校验 (264-265) ---------------------------------

ERR_FW_SKILLMD_DUPLICATE_KEY = "999001264"  # SKILL.md frontmatter 存在重复 key
ERR_FW_SKILLMD_UNKNOWN_KEY = "999001265"  # SKILL.md frontmatter 包含不在白名单内的未知 key

# -- SKILL.md name 字段校验 (266-269) ----------------------------------------

ERR_FW_SKILLMD_NAME_MISSING = "999001266"  # SKILL.md name 字段缺失
ERR_FW_SKILLMD_NAME_EMPTY = "999001267"  # SKILL.md name 为空字符串
ERR_FW_SKILLMD_NAME_FORMAT = "999001268"  # SKILL.md name 格式不合规（非 kebab-case、含连续连字符、或首尾连字符）
ERR_FW_SKILLMD_NAME_TOO_LONG = "999001269"  # SKILL.md name 超过 64 字符长度限制

# -- SKILL.md description 字段校验 (270-273) ---------------------------------

ERR_FW_SKILLMD_DESC_MISSING = "999001270"  # SKILL.md description 字段缺失
ERR_FW_SKILLMD_DESC_EMPTY = "999001271"  # SKILL.md description 为空字符串
ERR_FW_SKILLMD_DESC_TOO_LONG = "999001272"  # SKILL.md description 超限（加权字符≤1024 或 token≤300）
ERR_FW_SKILLMD_DESC_INVALID_TYPE = "999001273"  # SKILL.md description 字段类型非字符串（如整数、列表等）

# -- SKILL.md body 校验 (274-275) --------------------------------------------

ERR_FW_SKILLMD_BODY_EMPTY = "999001274"  # SKILL.md 正文（frontmatter 之后的内容）为空
ERR_FW_SKILLMD_BODY_TOO_LONG = "999001275"  # SKILL.md 正文超限（行数>500 或 token>5000）

# -- SKILL.md 凭据泄露 (276) -------------------------------------------------

ERR_FW_SKILLMD_CREDENTIAL = "999001276"  # SKILL.md 中检测到疑似硬编码凭据（如 AWS Key、API Key、私钥、数据库连接串等）

# -- 脚本安全 — 危险命令 (280-283) -------------------------------------------

ERR_FW_SCRIPT_DESTRUCTIVE = "999001280"  # scripts/ 脚本命中破坏性删除命令（如 rm -rf）
ERR_FW_SCRIPT_RISKY_PERMISSION = "999001281"  # scripts/ 脚本命中危险权限修改（如 chmod 777、chmod u+s）
ERR_FW_SCRIPT_REMOTE_EXEC = "999001282"  # scripts/ 脚本命中远程下载并执行（如 curl|bash、iwr|iex、base64 decode|bash、certutil -decode、powershell 编码命令）
ERR_FW_SCRIPT_DYNAMIC_EXEC = "999001283"  # scripts/ 脚本命中动态代码执行（如 eval()、exec()、os.system()、subprocess shell=True）

# -- 脚本安全 — 凭据泄露 (284) -----------------------------------------------

ERR_FW_SCRIPT_CREDENTIAL = "999001284"  # scripts/ 脚本中检测到疑似硬编码凭据（如 AWS Key、API Key、私钥、数据库连接串等）

# -- 压缩包安全 (285-286) ----------------------------------------------------

ERR_FW_ARCHIVE_RATIO_EXCEEDED = "999001285"  # 压缩包解压前压缩比校验失败，疑似压缩炸弹
ERR_FW_ARCHIVE_COMMAND_BLOCKED = "999001286"  # shell 解压命令无法通过受控压缩包校验，已在执行前拦截

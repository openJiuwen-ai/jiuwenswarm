#!/bin/bash

# 文件替换脚本
# 读取包含文件路径对的CSV文件，并将源文件内容替换到目标文件中

# 检查参数
if [ $# -ne 1 ]; then
    echo "使用方法: $0 <CSV替换文件路径>"
    echo "CSV文件格式: 每行三个字段，用逗号分隔"
    echo "格式: 类型(dir/file),源文件/目录路径,目标文件/目录路径"
    echo "类型: file(文件) 或 dir(目录)"
    exit 1
fi

# 获取替换文件路径
REPLACE_FILE="$1"

# 检查替换文件是否存在
if [ ! -f "$REPLACE_FILE" ]; then
    echo "错误: 替换文件 '$REPLACE_FILE' 不存在"
    exit 1
fi

# 检查替换文件是否为空
if [ ! -s "$REPLACE_FILE" ]; then
    echo "警告: 替换文件为空"
    exit 0
fi

echo "开始处理文件替换任务..."
echo "替换文件: $REPLACE_FILE"

# 复制文件的通用函数
copy_file() {
    local src="$1"
    local dst="$2"
    local action="$3"
    echo "正在$action文件: $dst <- $src"
    cp "$src" "$dst"
    if [ $? -eq 0 ]; then
        echo "  ✓ 成功$action: $dst"
    else
        echo "  ✗ $action失败: $dst (第 $line_num 行)"
    fi
}

# 复制目录的函数
copy_directory() {
    local src="$1"
    local dst="$2"
    local action="$3"
    echo "正在$action目录: $dst <- $src"
    # 如果目标目录不存在，则创建
    if [ ! -d "$dst" ]; then
        mkdir -p "$dst"
    fi
    # 复制源目录内容到目标目录
    cp -r "$src"/* "$dst"/
    if [ $? -eq 0 ]; then
        echo "  ✓ 成功$action: $dst"
    else
        echo "  ✗ $action失败: $dst (第 $line_num 行)"
    fi
}

# 逐行处理CSV文件
line_num=0
while IFS=',' read -r TYPE SOURCE_PATH TARGET_PATH || [ -n "$TYPE" ]; do
    # 跳过空行
    if [ -z "$TYPE" ]; then
        continue
    fi
    
    # 去除行尾的换行符和空格
    TYPE=$(echo "$TYPE" | xargs)
    SOURCE_PATH=$(echo "$SOURCE_PATH" | xargs)
    TARGET_PATH=$(echo "$TARGET_PATH" | xargs)
    
    ((line_num++))
    
    # 根据类型处理
    if [ "$TYPE" = "file" ]; then
        # 文件类型处理
        # 检查源文件是否存在
        if [ ! -f "$SOURCE_PATH" ]; then
            echo "错误: 源文件 '$SOURCE_PATH' 不存在 (第 $line_num 行)"
            continue
        fi
        
        # 检查源文件是否可读
        if [ ! -r "$SOURCE_PATH" ]; then
            echo "错误: 源文件 '$SOURCE_PATH' 不可读 (第 $line_num 行)"
            continue
        fi
        
        # 检查目标文件是否存在
        if [ ! -f "$TARGET_PATH" ]; then
            # 目标文件不存在，创建目标文件目录（如果不存在）并复制源文件
            echo "目标文件 '$TARGET_PATH' 不存在，将创建并复制源文件"
            # 创建目标文件的目录
            TARGET_DIR=$(dirname "$TARGET_PATH")
            if [ ! -d "$TARGET_DIR" ]; then
                echo "创建目标目录: $TARGET_DIR"
                mkdir -p "$TARGET_DIR"
            fi
            # 复制源文件到目标文件
            copy_file "$SOURCE_PATH" "$TARGET_PATH" "创建并复制"
        else
            # 目标文件存在，检查是否可写
            if [ ! -w "$TARGET_PATH" ]; then
                echo "错误: 目标文件 '$TARGET_PATH' 不可写 (第 $line_num 行)"
                continue
            fi
            
            # 执行替换操作
            copy_file "$SOURCE_PATH" "$TARGET_PATH" "替换"
        fi
    elif [ "$TYPE" = "dir" ]; then
        # 目录类型处理
        # 检查源目录是否存在
        if [ ! -d "$SOURCE_PATH" ]; then
            echo "错误: 源目录 '$SOURCE_PATH' 不存在 (第 $line_num 行)"
            continue
        fi
        
        # 检查源目录是否可读
        if [ ! -r "$SOURCE_PATH" ]; then
            echo "错误: 源目录 '$SOURCE_PATH' 不可读 (第 $line_num 行)"
            continue
        fi
        
        # 检查目标目录是否存在
        if [ ! -d "$TARGET_PATH" ]; then
            # 目标目录不存在，创建目标目录
            echo "目标目录 '$TARGET_PATH' 不存在，将创建目录"
            mkdir -p "$TARGET_PATH"
            # 复制源目录内容到目标目录
            copy_directory "$SOURCE_PATH" "$TARGET_PATH" "创建并复制"
        else
            # 目标目录存在，检查是否可写
            if [ ! -w "$TARGET_PATH" ]; then
                echo "错误: 目标目录 '$TARGET_PATH' 不可写 (第 $line_num 行)"
                continue
            fi
            
            # 执行替换操作
            copy_directory "$SOURCE_PATH" "$TARGET_PATH" "替换"
        fi
    else
        echo "错误: 未知类型 '$TYPE' (第 $line_num 行)，类型必须是 'file' 或 'dir'"
        continue
    fi
done < "$REPLACE_FILE"

echo "文件替换任务完成"
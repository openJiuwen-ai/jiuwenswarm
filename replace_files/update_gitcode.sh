#!/bin/bash

# 全局变量
current_dir=$(pwd)
# 检查是否传入了时间戳参数
if [ -n "$1" ] && [[ "$1" =~ ^[0-9]{12}$ ]]; then
    TIMESTAMP=$1
    echo "Using provided TIMESTAMP: $TIMESTAMP"
    shift
else
    TIMESTAMP=$(date +"%Y%m%d%H%M")
    echo "Using generated TIMESTAMP: $TIMESTAMP"
fi
TARGET_DIR="$current_dir/蓝区代码/$TIMESTAMP"
FILES=(
    "jiuwenclaw/agentserver/__init__.py"
    "jiuwenclaw/resources/config.yaml"
)
cherry_pick_commitId="408d1df637412975a2ac5ad62821d84d0e795356"

# 显示帮助信息
show_help() {
    echo "Usage: $0 [TIMESTAMP] [OPTIONS]"
    echo "Options:"
    echo "  --create-dir     创建目标目录"
    echo "  --clone-repo     克隆代码仓库"
    echo "  --copy-files     复制文件"
    echo "  --git-commit     执行git commit"
    echo "  --cherry-pick    执行cherry-pick"
    echo "  --cleanup        清理旧文件夹"
    echo "  --all            执行所有步骤（默认）"
    echo "  -h, --help       显示此帮助信息"
    echo ""
    echo "Examples:"
    echo "  $0 --all                    # 使用自动生成的时间戳"
    echo "  $0 202605080942 --all       # 使用指定的时间戳"
}

# 1. 创建目标目录
create_target_directory() {
    echo "Creating target directory: $TARGET_DIR"
    mkdir -p "$TARGET_DIR"
    if [ $? -ne 0 ]; then
        echo "Error: Failed to create target directory"
        exit 1
    fi
}

# 2. 克隆代码仓库
clone_repository() {
    echo "Cloning code repository..."
    git clone -b enterprise_vibeskill_dev https://gitcode.com/openJiuwen/jiuwenclaw.git "$TARGET_DIR"
    if [ $? -ne 0 ]; then
        echo "Error: Failed to clone repository"
        exit 1
    fi
}

# 3. 复制文件
copy_files() {
    echo "Setting up source path..."
    TARGET_PATH="$TARGET_DIR"
    echo "Using source path: $TARGET_PATH"

    # 检查源路径是否存在
    if [ ! -d "$TARGET_PATH" ]; then
        echo "Error: Source path does not exist: $TARGET_PATH"
        exit 1
    fi

    # 复制文件到当前目录下的 jiuwenclaw 目录
    for file in "${FILES[@]}"; do
        source_file="$TARGET_PATH/$file"
        current_file="$current_dir/$file"
        # 创建目标目录
        mkdir -p "$(dirname "$current_file")"
        # 复制文件
        cp "$source_file" "$current_file"
        if [ $? -ne 0 ]; then
            echo "Error: Failed to copy file: $source_file -> $current_file"
            exit 1
        fi
        echo "Copied: $source_file -> $current_file"
    done
}

# 4. Git commit
git_commit() {
    cd "$current_dir"
    echo "文件路径：" "${FILES[@]}"
    git add "${FILES[@]}"
    if [ $? -ne 0 ]; then
        echo "Error: Failed to git add files"
        exit 1
    fi
    git commit --author="s30032424 <shilishan@huawei.com>" -m "[TicketNo:]DTS2026051248774
[Description:] 刷新蓝区2个文件代码
[Binary Source:]NA
[PrivateCode(Yes/No):]No"
    if [ $? -ne 0 ]; then
        echo "Error: Failed to git commit"
        exit 1
    fi
}

# 5. Cherry-pick
cherry_pick() {
    git cherry-pick --no-commit $cherry_pick_commitId
    if [ $? -ne 0 ]; then
        echo "Error: Failed to cherry-pick"
        exit 1
    fi
    git commit --author="s30032424 <shilishan@huawei.com>" -m "[TicketNo:]DTS2026051248774
[Description:] cherry-pick 2个文件
[Binary Source:]NA
[PrivateCode(Yes/No):]No"
    }

# 主函数
main() {
    local run_all=true
    local run_create_dir=false
    local run_clone=false
    local run_copy=false
    local run_git_commit=false
    local run_cherry_pick=false

    # 解析命令行参数
    while [[ $# -gt 0 ]]; do
        case $1 in
            --create-dir)
                run_create_dir=true
                run_all=false
                shift
                ;;
            --clone-repo)
                run_clone=true
                run_all=false
                shift
                ;;
            --copy-files)
                run_copy=true
                run_all=false
                shift
                ;;
            --git-commit)
                run_git_commit=true
                run_all=false
                shift
                ;;
            --cherry-pick)
                run_cherry_pick=true
                run_all=false
                shift
                ;;
            --all)
                run_all=true
                shift
                ;;
            -h|--help)
                show_help
                exit 0
                ;;
            *)
                echo "Unknown option: $1"
                show_help
                exit 1
                ;;
        esac
    done

    echo "Starting process with current_dir: $current_dir"
    echo "Starting process with timestamp: $TIMESTAMP"

    # 执行指定的步骤
    if [ "$run_all" = true ] || [ "$run_create_dir" = true ]; then
        create_target_directory
    fi

    if [ "$run_all" = true ] || [ "$run_clone" = true ]; then
        clone_repository
    fi

    if [ "$run_all" = true ] || [ "$run_copy" = true ]; then
        copy_files
    fi

    if [ "$run_all" = true ] || [ "$run_git_commit" = true ]; then
        git_commit
    fi

    if [ "$run_all" = true ] || [ "$run_cherry_pick" = true ]; then
        cherry_pick
    fi

    echo "All operations completed successfully."
}

# 如果直接运行脚本，则执行main函数
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
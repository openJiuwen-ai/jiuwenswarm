import logging
import os
import shutil
import time

from .indexing.workflows.artifacts import BuildConfig, BuildMethod
from .indexing.workflows.index_builder import IndexBuilder

logger = logging.getLogger(__name__)


def main():
    # 记录开始时间
    start_time = time.time()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger.info("=" * 60)
    logger.info("开始构建索引...")
    logger.info("开始时间: %s", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time)))
    logger.info("=" * 60)

    # 配置初始化
    config_start = time.time()
    config = BuildConfig(
        method=BuildMethod.TREE,
        llm_model=os.getenv("LLM_MODEL_NAME"),
        llm_api_key=os.getenv("OPENAI_API_KEY"),
        llm_base_url=os.getenv("OPENAI_BASE_URL"),
    )
    config_cost = time.time() - config_start
    logger.info("配置初始化完成，耗时: %.2f 秒", config_cost)

    # 索引构建
    build_start = time.time()
    logger.info("\n开始执行索引构建...")
    IndexBuilder.build(
        # 自动获取 skills_dir 下所有文件夹
        item_paths=[f.path for f in os.scandir(os.getenv("skills_dir")) if f.is_dir()][:15],
        output_dir=os.getenv("OUTPUT_DIR"),
        config=config,
    )
    build_cost = time.time() - build_start
    logger.info("索引构建完成，耗时: %.2f 秒", build_cost)

    # ===================== 新增：目录拷贝逻辑 =====================
    src_dir = os.getenv("OUTPUT_DIR")
    dst_dir = os.getenv("output_tree")

    if dst_dir and os.path.exists(dst_dir):
        logger.info("\n目标目录存在，开始拷贝: %s -> %s", src_dir, dst_dir)
        shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
        logger.info("目录拷贝完成")
    else:
        logger.info("\n目标目录不存在，跳过拷贝")
    # ==============================================================

    # 总耗时统计
    total_cost = time.time() - start_time
    logger.info("\n%s", "=" * 60)
    logger.info("索引构建任务全部完成！")
    logger.info("配置初始化耗时: %.2f 秒", config_cost)
    logger.info("索引构建耗时: %.2f 秒", build_cost)
    logger.info("总耗时: %.2f 秒 (%.2f 分钟)", total_cost, total_cost / 60)
    logger.info("结束时间: %s", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))
    logger.info("=" * 60)

    return


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.

from urllib.parse import urljoin

from .base_config import BaseConfig
from .config_helper import ConfigHelper
import ast


def safe_literal_eval(value):
    """安全解析配置项，处理各种格式问题"""
    if not value:
        return []
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError):
        # 如果 ast.literal_eval 失败，尝试手动解析
        if isinstance(value, str) and value.startswith("[") and value.endswith("]"):
            # 移除方括号并分割字符串
            import re
            # 使用正则表达式提取单引号中的内容
            agents_list = re.findall(r"'([^']*)'", value)
            return agents_list
        return []


class DevelopmentConfig(BaseConfig):
    """配置项"""
    if BaseConfig.LOCAL_FLAG:
        CONFIG = ConfigHelper("local")
    else:
        CONFIG = ConfigHelper("cloud")

    hag_osms_slb_url: str = CONFIG.get("hag_osms_slb_url")
    osms_prepare_url: str = urljoin(hag_osms_slb_url, CONFIG.get("osms_prepare_url"))
    osms_complete_url: str = urljoin(hag_osms_slb_url, CONFIG.get("osms_complete_url"))
    osms_query_url: str = urljoin(hag_osms_slb_url, CONFIG.get("osms_query_url"))
    osms_delete_url: str = urljoin(hag_osms_slb_url, CONFIG.get("osms_delete_url"))
    hag_osms_ak = CONFIG.get("hag_osms_ak")
    task_center_url: str = urljoin(hag_osms_slb_url, "/cloud-ability-gallery/v1/agent/user/operate")
    task_center_query_url: str = urljoin(hag_osms_slb_url, "/cloud-ability-gallery/v1/agent/user/query")
    agent_task_query_url: str = urljoin(hag_osms_slb_url, "/super-agent-service/v1/agent-task/query-by-id")
    agent_task_insert_url: str = urljoin(hag_osms_slb_url, "/super-agent-service/v1/agent-task/insert")
    agent_task_update_url: str = urljoin(hag_osms_slb_url, "/super-agent-service/v1/agent-task/update")
    add_table_icon_url: str = CONFIG.get("add.table.icon.url")

    upload_file_url: str = CONFIG.get("upload_file_url")
    use_k25_model: str = CONFIG.get("use.k25.model") == "true"
    use_k25_model_html_builder: str = CONFIG.get("use.k25.model.html.builder") == "true"
    use_k25_model_others: str = CONFIG.get("use.k25.model.others") == "true"
    writer_model: str = CONFIG.get("writer.model")
    instruct_follow_model: str = CONFIG.get("instruct.follow.model")
    fast_model: str = CONFIG.get("fast.model")
    code_agent_url = CONFIG.get("code_agent_url")
    jupyter_sandbox = CONFIG.get("jupyter_sandbox")
    enable_common_sandbox = CONFIG.get("enable_common_sandbox") == "true"  # bool类型需要str转bool
    enable_code_generate_safety_check = CONFIG.get("enable_code_generate_safety_check") == "true"

    service_account_clientId = CONFIG.get("service_account_clientId")
    service_account_keyId = CONFIG.get("service_account_keyId")
    service_account_tag = CONFIG.get("service_account_tag")
    sandbox_templateId = CONFIG.get("sandbox_templateId")
    sandbox_server = CONFIG.get("sandbox_server")
    jwt_aud = CONFIG.get("jwt_aud")

    planner_model_url = CONFIG.get("planner.url") + CONFIG.get("planner.bid.flowId")
    appid = CONFIG.get("planner.appid")
    planner_enabled_flag = CONFIG.get("planner.enabled.flag") == "true"  # bool类型需要str转bool
    code_agent_v2_enabled_flag = CONFIG.get("code.agent.v2.enabled.flag") == "true"  # bool类型需要str转bool
    multi_round_flag = CONFIG.get("multi.round.flag") == "true"  # bool类型需要str转bool

    deepseek_huawei_ak = CONFIG.get("deepseek_huawei_ak")
    deepseek_huawei_url = CONFIG.get("deepseek_huawei_url")
    agent_model_name = CONFIG.get("agent.model.name")
    general_model_name = CONFIG.get("general.model.name")

    deepsearch_url_toolcall = CONFIG.get("deepsearch_url_toolcall")
    deepsearch_url_deepsearch = CONFIG.get("deepsearch_url_deepsearch")
    deepsearch_app_id = CONFIG.get("deepsearch_app_id")
    deepsearch_flowId = CONFIG.get("deepsearch_flowId")
    deepsearch_uu_id = CONFIG.get("deepsearch_uu_id")

    qwen_vl_url = CONFIG.get("qwen_vl_url")

    enable_image_generate = CONFIG.get("enable_image_generate") == "true"  # bool类型需要str转bool
    plugin_service_url = CONFIG.get("plugin_service_url")
    plugin_id_gen_image = CONFIG.get("plugin_id_gen_image")
    silicon_url: str = CONFIG.get("silicon_url")
    platform_ak = CONFIG.get("platform_ak")
    platform_url = CONFIG.get("platform_url")
    call_ws_url = CONFIG.get("call_ws_url")
    call_ws_ak = CONFIG.get("call_ws_ak")
    teamId = CONFIG.get("teamId")
    sts_server = CONFIG.get("sts_serverDomain")
    sandbox_url = CONFIG.get("sandbox_url")
    sandbox_token = CONFIG.get("sandbox_token")

    file_converter = CONFIG.get("prompt.agent.file.converter")
    code_agent = CONFIG.get("prompt.agent.code.agent")
    general_tool = CONFIG.get("prompt.agent.general.tool")
    phone_call = CONFIG.get("prompt.agent.phone.call")
    gui_navigator = CONFIG.get("prompt.agent.gui.navigator")
    user_clarification = CONFIG.get("prompt.agent.user.clarification")
    deepsearch = CONFIG.get("prompt.agent.deepsearch")
    video_generation = CONFIG.get("prompt.agent.video.generation")
    html_format_agent = CONFIG.get("prompt.agent.html.format.agent")
    code_execute_agent = CONFIG.get("prompt.agent.code.execute.agent")
    editor_agent = CONFIG.get("prompt.agent.editor.agent")
    html_report_agent = CONFIG.get("prompt.agent.html.report.agent")
    html_builder_agent = CONFIG.get("prompt.agent.html.builder.agent")
    ppt_generator_agent = CONFIG.get("prompt.agent.ppt.generator.agent")
    doc_convert_agent = CONFIG.get("prompt.agent.doc.convert.agent")

    PLANNING_MUTIL_ROUND_SYSTEM_PROMPT_ZH = CONFIG.get("prompt.planning.planning.mutil_round.system.prompt.zh")
    PLANNING_SYSTEM_PROMPT_ZH = CONFIG.get("prompt.planning.planning.system.prompt.zh")
    PLANNING_FILTER_PROMPT = CONFIG.get("prompt.planning.filter.tool.user")
    NEXT_STEP_PROMPT_ZH = CONFIG.get("prompt.planning.next.step.prompt.zh")
    REVIEW_STEP_PROMPT_ZH = CONFIG.get("prompt.planning.review.step.prompt.zh")
    REPLAN_STEP_PROMPT_ZH = CONFIG.get("prompt.planning.replan.step.prompt.zh")
    PLANNING_USER_PROMPT = CONFIG.get("prompt.planning.planning.user.prompt")
    PLANNING_MUTIL_ROUND_USER_PROMPT = CONFIG.get("prompt.planning.planning.mutil_round.user.prompt")

    PLANNING_USER_EXCLUDE_PROMPT = CONFIG.get("prompt.planning.planning.user.exclude.prompt")
    PLANNING_MUTIL_ROUND_USER_EXCLUDE_PROMPT = CONFIG.get("prompt.planning.planning.mutil_round.user.exclude.prompt")
    STEP_PROMPT = CONFIG.get("prompt.planning.step.prompt")
    QUERY_CLARIFY_PROMPT_ZH = CONFIG.get("prompt.router.mode.zh")
    QUERY_CLARIFY_MULTI_PROMPT_ZH = CONFIG.get("prompt.router.multi.mode.zh")
    SUGGESTION_PROMPT_ZH = CONFIG.get("prompt.planning.suggestion.prompt.zh")
    GENERAL_TOOL_SYSTEM_PROMPT_ZH = CONFIG.get("prompt.agent.general.tool.system")
    GENERAL_TOOL_FILTER_PROMPT = CONFIG.get("prompt.agent.general.tool.filter")
    GENERAL_TOOL_EXTRACT_PROMPT = CONFIG.get("prompt.planning.general.extract.prompt")
    GENERAL_TOOL_NEXT_STEP_PROMPT = CONFIG.get("prompt.agent.general.tool.next")
    CHECK_QUERY_SECURITY_PROMPT = CONFIG.get("prompt.planning.check.query.security.prompt")
    # 修复 enabled_agents 解析问题
    enabled_agents_config = CONFIG.get("enabled_agents")
    enabled_agents = safe_literal_eval(enabled_agents_config)
    enabled_fewshot = CONFIG.get("planning.fewshot.flag") == "true"  # bool类型需要str转bool
    determine_final_outputs = CONFIG.get("prompt.planning.determine.final.outputs")
    prompt_html_abstract = CONFIG.get("prompt.html.abstract")
    prompt_html_chapters = CONFIG.get("prompt.html.chapters")
    prompt_html_chapters_image = CONFIG.get("prompt.html.chapters.image")
    prompt_html_reference = CONFIG.get("prompt.html.reference")
    html_reference_template = CONFIG.get("html.reference.template")

    reference_extract_prompt = CONFIG.get("prompt.reference.extract")

    COLOR_SCHEMES_STR = CONFIG.get("color.schemes")
    html_abstract_template = CONFIG.get("html.abstract.template")
    html_section_template = CONFIG.get("html.section.template")
    merge_file_prompt_template = CONFIG.get("merge.file.prompt.template")
    deepsearch_write_report_prompt = CONFIG.get("deepsearch.write.report.prompt")
    deepsearch_arguments_generation_prompt = CONFIG.get("deepsearch.arguments.generation.prompt")
    deepsearch_arguments_generation_simple_prompt = CONFIG.get("deepsearch.arguments.generation.simple_prompt")
    deepsearch_image_recognition_prompt = CONFIG.get("deepsearch.image.recognition.prompt")
    deepsearch_image_scenes_config = CONFIG.get("deepsearch.image.scenes")

    OCR_SYSTEM_PROMPT_ZH = CONFIG.get("prompt.ocr.system")
    OCR_USER_PROMPT_ZH = CONFIG.get("prompt.ocr.user")

    rendered_html_template = CONFIG.get("rendered.html.template")

    super_agent_id = CONFIG.get("task.space.agent.id")
    agent_metadata_url = hag_osms_slb_url + "/agent-repo-service/v1/agents/agent-repo/metadata/query-by-ids"
    agent_store_tool_prompt_url = hag_osms_slb_url + "/agent-plan/v1/tool-prompt/query-by-id"
    general_mode_task_timeout = CONFIG.get("general.mode.task.timeout")
    obs_expire_time = CONFIG.get("obs.expire.time")
    obs_need_preview = CONFIG.get("obs.need.preview") == "true"  # bool类型需要str转bool
    obs_use_edge = CONFIG.get("obs.use.edge") == "true"  # bool类型需要str转bool
    obs_preview_supported_file_format = safe_literal_eval(CONFIG.get("obs_preview_supported_file_format"))
    select_plugins_threshold = int(CONFIG.get("general_tool.select_plugins_threshold"))  # 数字需要str转int
    select_tools_threshold = int(CONFIG.get("general_tool.select_tools_threshold"))

    easy_max_rounds = int(CONFIG.get("easy.max.rounds"))  # 数字需要str转int
    easy_search_max_rounds = int(CONFIG.get("easy.search.max.rounds"))  # 数字需要str转int
    normal_max_rounds = int(CONFIG.get("normal.max.rounds"))  # 数字需要str转int
    normal_search_max_rounds = int(CONFIG.get("normal.search.max.rounds"))  # 数字需要str转int
    hard_max_rounds = int(CONFIG.get("hard.max.rounds"))  # 数字需要str转int
    hard_search_max_rounds = int(CONFIG.get("hard.search.max.rounds"))  # 数字需要str转int
    doc_convert_description = CONFIG.get('doc.convert.description')
    deepsearch_path = CONFIG.get('deepsearch.path')

    doc_convert_url_toolcall = CONFIG.get("doc_convert_url_toolcall")

    enable_code_generate = CONFIG.get("enable_code_generate")
    if enable_code_generate == "":
        enable_code_generate = True
    else:
        enable_code_generate = enable_code_generate.strip().lower() == "true"

    # 风控配置项
    riskcontrol_baseurl = CONFIG.get("riskcontrol.baseurl")
    riskcontrol_moderation_text_url = CONFIG.get("riskcontrol.moderation_text_url")
    riskcontrol_moderation_image_url = CONFIG.get("riskcontrol.moderation_image_url")
    riskcontrol_aigc_metadata_url = CONFIG.get("riskcontrol.aigc_metadata_url")
    riskcontrol_business_id = CONFIG.get("riskcontrol.business_id")
    riskcontrol_ak = CONFIG.get("riskcontrol.ak")
    riskcontrol_sk = CONFIG.get("riskcontrol.sk")
    riskcontrol_text = CONFIG.get("enable_risk_control_text") == "true"  # bool类型需要str转bool
    riskcontrol_image = CONFIG.get("enable_risk_control_image") == "true"
    riskcontrol_watermark = CONFIG.get("enable_risk_control_watermark") == "true"

    # url白名单
    if CONFIG.get("security_check.white_inputs_urls") == "":
        white_input_urls = []
    else:
        white_input_urls = safe_literal_eval(CONFIG.get("security_check.white_inputs_urls"))
    enable_ssrf_check = CONFIG.get("security_check.enable_ssrf_check") == "true"

    obp_nov_compatible_app_version = CONFIG.get("obp.nov.compatible.app.version")
    loading_nov_compatible_app_version = CONFIG.get("loading.nov.compatible.app.version")

    # 上下文配置
    context_window_keep_size = int(CONFIG.get("memory.context_window_keep_size") or 2)
    tool_message_compress_threshold = int(CONFIG.get("memory.tool_message_compress_threshold") or 10000)
    tool_message_max_length = int(CONFIG.get("memory.tool_message_max_length") or 60000)
    tool_message_max_observe_threshold = int(CONFIG.get("tool_message_max_observe_threshold") or 50000)
    model_name = CONFIG.get("memory.compress.model")
    query_regex = CONFIG.get("memory.query.regex")
    current_step_regex = CONFIG.get("memory.current_step.regex")
    compress_prompt_with_offload = CONFIG.get("memory.compress.prompt_with_offload")
    too_large_tool_msg = CONFIG.get("memory.compress.too_large_tool_msg")
    inject_prompt_suffix = CONFIG.get("memory.compress.inject_prompt_suffix")

    # codeagent V2相关
    code_agent_url_slb = CONFIG.get("remote.code_agent.url.slb")

    # websocket 配置
    websocket_server_maxConnections = int(CONFIG.get("websocket.server.maxConnections"))

    # agent-icon-url-map
    agent_icon_url_map = safe_literal_eval(CONFIG.get("agent.icon.url.map"))


project_config = DevelopmentConfig

# HarmonyOS Atomic Skills Catalog

This catalog is generated from the HarmonyOS Skills repository and summarizes optional atomic Skills that can complement `harmonyos-dev-suite`.

Use this file when a task needs a more specific HarmonyOS capability than the suite entry can provide. Install optional atomic Skills only after the user asks for that deeper specialization.

Total atomic Skills indexed: 83.

## ArkUI

- `component_basic_ui`: HarmonyOS ArkTS 基础 UI 组件使用规范。包含 Text、Button、Image、Toggle、Slider、Progress、Checkbox、Radio、Rating、LoadingProgress、Marquee、Qrcode、Badge 等基础显示和交互组件。Use when: (1) 实现文本显示，(2) 实现按钮交互，(3)…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/component_basic_ui`
- `component_container`: HarmonyOS ArkTS 容器组件使用规范。包含 Column、Row、Stack、Flex、List、Grid、Scroll、Swiper、Tabs、Refresh、RelativeContainer 等布局容器组件。Use when: (1) 实现页面布局，(2) 实现列表滚动，(3) 实现层叠布局，(4) 实现网格布局，(5) 实现轮播切换，(…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/component_container`
- `hmos-arkui-longtake-transition`: 为鸿蒙(HarmonyOS)应用添加一镜到底转场效果。当用户提到一镜到底、转场动画、页面跳转动画、Navigation转场、卡片展开动画、图片查看大图动画、ezcustomtransition、自定义NavContentTransition、longtake、连续转场、沉浸式转场等关键词时，务必使用此skill。也适用于用户想要在鸿蒙应用中实现类似iOS的…
  Source path: `04-development/01-application-framework/ArkUI/hmos-arkui-longtake-transition`
- `hmos-arkui-mvvm-pattern`: HarmonyOS ArkUI 的 MVVM 架构技能。适用于：(1) 项目分层设计 Model/ViewModel/View (2) 目录结构规划 (3) 组件职责与数据流规范 (4) 视图架构检视以及整改项目为MVVM模式等场景
  Source path: `04-development/01-application-framework/ArkUI/hmos-arkui-mvvm-pattern`
- `hmos-arkui-statemgt-migration`: 帮助开发者将ArkUI状态管理从V1迁移到V2。触发场景：(1) V1项目升级到V2；(2) 迁移@Component/@State/@Prop/@Link/@Observed/@ObjectLink/@Provide/@Consume/@Watch/@Reusable装饰器；(3) 迁移LocalStorage/AppStorage/Persistent…
  Source path: `04-development/01-application-framework/ArkUI/hmos-arkui-statemgt-migration`
- `kits_ui`: HarmonyOS ArkUI UI能力集使用规范。包含 Router、PromptAction、动画、媒体查询、组件快照、拖拽等 UI 相关能力。Use when: (1) 系统提示弹窗，(2) 动画效果，(3) 响应式布局，(4) 组件截图。Triggers: promptAction、Toast、Dialog、动画、curves、animator、媒…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_ui`

## ArkTS

- `hmos-arkts-deprecated-interface-checker`: 检查 HarmonyOS 项目中的废弃 SDK 接口并提供修复建议。当需要清理废弃 API、升级 API 版本、优化代码质量或进行静态语法检查时使用。提供详细的迁移方案、修复优先级分类和代码示例。
  Source path: `04-development/01-application-framework/ArkTS/hmos-arkts-deprecated-interface-checker`
- `hmos-arkts-syntax-checker`: 检查并修复 HarmonyOS 项目的 ArkTS 语法错误，自动化构建项目。当需要编译项目、修复编译错误、生成 HAP/App 产物时使用。提供静态语法检查、错误自动修复、循环构建直到成功的完整工作流程。支持错误优先级分类（P0/P1/P2）、最大重试机制、构建产物自动定位。
  Source path: `04-development/01-application-framework/ArkTS/hmos-arkts-syntax-checker`
- `kits_arkts`: HarmonyOS ArkTS 基础能力集使用规范。包含并发编程（TaskPool、Worker）、工具类（util、buffer、process）、数据结构（ArrayList、HashMap等）、XML处理、URI/URL处理、UUID生成等核心能力。Use when: (1) 多线程并发，(2) Worker线程，(3) 工具函数，(4) 数据结构，…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_arkts`
- `lang-syntax`: HarmonyOS ArkTS 语言语法规范知识库，涵盖类型约束、类约束、函数约束、模块约束、声明式UI语法、状态管理等核心规则。When to use: 在实现任何 HarmonyOS ArkTS 代码时都必须使用本 Skill，包括 - 编写或生成 ArkTS 代码、 - 编写/修改 struct/@Component、 - 使用或接入组件、 - 查阅…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/lang-syntax`

## DevEco tools

- `deveco-autobugfix`: 自动执行鸿蒙应用 Bug 全流程修复，涵盖问题复现、根因分析、最小化代码修复、构建编译与运行验证。 依赖 deveco-mcp 提供 verify_ui/build_project/start_app 等能力。 使用场景：用户要求自动修复鸿蒙项目 Bug，或输入触发词：自动修复、auto fix、auto-fix、自动bug修复、autofix。 提供熔断…
  Source path: `07-tools/tools/deveco-studio/deveco-autobugfix`
- `deveco-requirement-development`: 覆盖鸿蒙/HarmonyOS/ArkTS 应用需求开发流程。在用户要落地新功能/新页面/新模块、PRD、端到端需求开发或鸿蒙应用开发时使用。不用于仅询问 API 语法用法、或未声明走完整链路的单文件 Bug 修复。用户意图模糊时先澄清是否需要端到端交付。
  Source path: `07-tools/tools/deveco-studio/deveco-requirement-development`
- `deveco-studio-codelinter`: 对 HarmonyOS（鸿蒙）项目运行 DevEco Studio CodeLinter 静态代码检查，解读检查结果并提供修复建议。支持 ArkTS、TS、JS 文件，涵盖性能、安全、代码规范、正确性、跨设备适配、API 兼容性等规则集。当用户提到 codelinter、code linting、鸿蒙代码检查、鸿蒙应用质量、HarmonyOS 代码质量检查…
  Source path: `07-tools/tools/deveco-studio/deveco-studio-codelinter`
- `deveco-studio-emulator`: HarmonyOS模拟器管理助手。**首次使用必须先运行 `node scripts/setup.js --force` 配置路径**，然后才能执行模拟器启动、应用安装调试等操作。包含完整的场景化设备控制命令（旋转、电源、截屏、音量、摇一摇、折叠）。支持Windows/macOS/Linux。触发词：模拟器、emulator、hdc、推包、安装应用、启动模…
  Source path: `07-tools/tools/deveco-studio/deveco-studio-emulator`
- `deveco-studio-hilog`: HarmonyOS日志分析助手，专注于hilog日志查看、崩溃日志分析、日志导出(-logZip)、手动日志分析。包含完整的hilog命令、hidumper堆栈转储、崩溃日志自动解压分析功能。支持Windows/macOS/Linux跨平台。
  Source path: `07-tools/tools/deveco-studio/deveco-studio-hilog`
- `deveco-studio-hvigor`: HarmonyOS应用构建工具助手，专注于使用Hvigor命令行工具构建HarmonyOS应用。包含完整的构建命令、参数说明、清理操作和CI/CD集成指南。触发词：hvigor、构建、编译、assembleHap、clean、build。
  Source path: `07-tools/tools/deveco-studio/deveco-studio-hvigor`
- `deveco-studio-verify`: HarmonyOS 设备验证工具 - 支持多设备类型验证（手机/折叠屏/平板）、应用安装、UI自动化操作、截图验证、日志收集和 Journey 测试框架。使用 hdc 命令行工具直接操作设备。适用于测试 HarmonyOS 应用在不同设备类型上的表现、验证 UI 在不同屏幕尺寸下的适配、执行 Journey 自动化测试、收集设备日志进行调试、构建产物发布前…
  Source path: `07-tools/tools/deveco-studio/deveco-studio-verify`
- `harmony-build-fix`: Incrementally build and fix HarmonyOS project errors after code generation. Use when: (1) HarmonyOS ArkTS code was just generated and needs to compile, (2) user asks to build and…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/harmony-build-fix`
- `harmony-verify`: HarmonyOS 设备验证助手 - 支持模拟器管理、获取应用UI结构、执行UI自动化操作、打开网址、截图验证和日志获取。使用 hdc 命令行工具直接操作设备。
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/harmony-verify`

## Native

- `deveco-native-flow`: 三端一致开发流水线（HarmonyOS/Android/iOS）：analyse → plan → coding → build → verify。 自包含：内嵌 HarmonyOS ArkTS 知识路由，无需外部 skill 依赖。 支持正向开发和翻译开发两种模式。 执行流程： 1. 自动检测项目类型和平台 2. 执行 analyse 阶段生成跨端技术方…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow`
- `native-analyse`: 跨端技术方案分析专家 - 综合多端代码和知识库，产出全平台通用的技术方案，指导后续各端 plan 细化。 工作流程： 1. 需求澄清：brain-storm 式迭代问询，消除歧义 2. 现有流程分析：逐端读取代码/知识库，合并为统一业务流程全貌 3. 整体设计：目标架构图/交互图/数据流/前后台交互/技术选型 4. 子任务拆分：结合模块和代码，定义跨端通用…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/native-analyse`
- `native-build-fix`: 原生多平台构建错误修复专家 - 增量修复 Android/iOS/HarmonyOS/KMP 项目的编译错误 工作流程： 1. 检测构建系统：根据平台参数或特征文件自动识别 2. 执行构建并捕获错误：运行平台对应的构建命令 3. 解析和分组错误：按 bundle → 文件路径 → 依赖层次排序 4. 逐个修复：Read → Diagnose → Fix →…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/native-build-fix`
- `native-coding`: 原生开发编码实施专家 - 基于 tech-spec 和 plan 产物，按子任务依赖链自下而上编码，每个子任务完成后自动构建验证。 工作流程： 1. 上下文恢复：检查已有编码进度和 plan-{platform}.md 2. 依赖分析：解析子任务依赖 DAG，拓扑排序生成自下而上执行顺序 3. 知识库加载：加载平台规范、架构文档、项目知识库 4. 逐子任务…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/native-coding`
- `native-plan`: 原生开发实施规划专家 - 基于跨端技术方案（tech-spec），生成端级细化实施计划，持久化到本地支持上下文恢复。 工作流程： 1. 上下文恢复：检测项目结构、已有计划和 tech-spec.md 2. 需求重述：引用 tech-spec 或独立分析（向后兼容） 3. 影响分析：有 tech-spec 时精简，聚焦当前端特有细节 4. 架构设计：有 te…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/native-plan`

## HarmonyOS kits

- `kits_ability`: HarmonyOS AbilityKit 应用能力集使用规范。包含 UIAbility、Want、Router、权限管理、应用生命周期等应用核心能力。Use when: (1) 页面路由跳转，(2) 应用生命周期管理，(3) 权限申请，(4) Ability 启动与通信。Triggers: Ability、UIAbility、Want、Router、页面跳…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_ability`
- `kits_accessibility`: HarmonyOS AccessibilityKit 无障碍能力集使用规范。包含 AccessibilityExtensionAbility、accessibility、AccessibilityElement 等无障碍服务开发能力。Use when: (1) 开发无障碍服务，(2) 屏幕阅读，(3) 无障碍事件监听，(4) 辅助功能开发。Triggers…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_accessibility`
- `kits_avsession`: HarmonyOS AVSessionKit 音视频会话能力集使用规范。包含 avSession 媒体会话、AVCastPicker 投播组件等音视频会话管理能力。Use when: (1) 媒体会话管理，(2) 后台音乐控制，(3) 蓝牙耳机控制，(4) 多设备投播。Triggers: AVSession、avSession、AVCastPicker、媒…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_avsession`
- `kits_basic_services`: HarmonyOS BasicServicesKit 基础服务能力集使用规范。包含账号管理、剪贴板、电源管理、打印、下载上传、系统设置、日期时间、USB、壁纸、压缩等。Use when: (1) 剪贴板操作，(2) 下载上传文件，(3) 系统设置，(4) 电源管理。Triggers: 剪贴板、粘贴板、下载、上传、打印、电源、USB、壁纸、压缩、pasteb…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_basic_services`
- `kits_connectivity`: HarmonyOS ConnectivityKit 连接能力集使用规范。包含蓝牙（BLE、A2DP、HFP等）、NFC、WiFi等连接能力。Use when: (1) 蓝牙通信，(2) BLE设备连接，(3) NFC读写，(4) WiFi管理。Triggers: 蓝牙、BLE、NFC、WiFi、连接、bluetooth、wifi、nfc、@ohos.blu…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_connectivity`
- `kits_data`: HarmonyOS ArkData 数据能力集使用规范。包含 Preferences、RelationalStore、KVStore、分布式数据、数据共享等数据存储能力。Use when: (1) 本地数据存储，(2) 关系型数据库，(3) 键值对存储，(4) 分布式数据同步。Triggers: 数据库、存储、Preferences、RdbStore、KV…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_data`
- `kits_device`: HarmonyOS 设备能力集使用规范。包含 SensorServiceKit（传感器、振动）、LocationKit（定位）、NotificationKit（通知）、BackgroundTasksKit（后台任务）等设备相关能力。Use when: (1) 使用传感器，(2) 获取位置信息，(3) 发送通知，(4) 后台任务。Triggers: 传感器、…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_device`
- `kits_distributed`: HarmonyOS DistributedServiceKit 分布式能力集使用规范。包含分布式设备管理、设备发现、跨设备协同、设备认证等功能。Use when: (1) 设备发现，(2) 跨设备协同，(3) 分布式数据同步，(4) 设备认证。Triggers: 分布式、跨设备、设备发现、协同、distributed、deviceManager、多设备。
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_distributed`
- `kits_file`: HarmonyOS CoreFileKit 文件能力集使用规范。包含文件读写、文件选择器、文件管理、云同步等文件操作能力。Use when: (1) 读写文件，(2) 选择文件/保存文件，(3) 文件管理，(4) 获取存储信息。Triggers: 文件操作、读写文件、文件选择、picker、fs、fileIo、存储、@ohos.file。
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_file`
- `kits_form`: HarmonyOS FormKit 卡片能力集使用规范。包含 FormExtensionAbility、formProvider、formBindingData 等卡片开发核心能力。Use when: (1) 开发服务卡片，(2) 更新卡片数据，(3) 卡片生命周期管理，(4) 桌面小组件。Triggers: 卡片、FormExtensionAbility…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_form`
- `kits_graphics2d`: HarmonyOS ArkGraphics2D 2D图形能力集使用规范。包含 drawing 绑制、effectKit 特效、colorSpaceManager 色彩空间、displaySync 显示同步等2D图形绑制能力。Use when: (1) 自定义绑制，(2) Canvas绘图，(3) 图像特效，(4) 颜色管理。Triggers: drawin…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_graphics2d`
- `kits_ime`: HarmonyOS IMEKit 输入法能力集使用规范。包含 InputMethodExtensionAbility、inputMethod、inputMethodEngine 等输入法开发和调用能力。Use when: (1) 开发输入法应用，(2) 管理系统输入法，(3) 输入法设置，(4) 自定义键盘。Triggers: 输入法、InputMetho…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_ime`
- `kits_input`: HarmonyOS InputKit 多模态输入能力集使用规范。包含 KeyEvent 按键事件、MouseEvent 鼠标事件、TouchEvent 触摸事件、inputDevice 输入设备管理等能力。Use when: (1) 处理键盘事件，(2) 处理鼠标事件，(3) 处理触摸事件，(4) 输入设备管理。Triggers: KeyEvent、Mou…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_input`
- `kits_ipc`: HarmonyOS IPCKit 进程间通信能力集使用规范。包含 RPC 远程过程调用、IPC 进程间通信、序列化反序列化等核心能力。Use when: (1) 跨进程通信，(2) IPC 数据传递，(3) 远程服务调用，(4) 进程间数据序列化。Triggers: IPC、RPC、跨进程、rpc、MessageSequence、RemoteObject、…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_ipc`
- `kits_localization`: HarmonyOS LocalizationKit 本地化能力集使用规范。包含 i18n 国际化、intl 格式化、resourceManager 资源管理等能力。Use when: (1) 多语言国际化，(2) 日期时间格式化，(3) 数字货币格式化，(4) 资源管理。Triggers: 国际化、多语言、i18n、intl、resourceManager…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_localization`
- `kits_media`: HarmonyOS MediaKit、AudioKit、ImageKit、CameraKit 媒体能力集使用规范。包含音视频播放录制、图片处理、相机等功能。Use when: (1) 播放音视频，(2) 录制音视频，(3) 图片编解码，(4) 相机功能。Triggers: 音频、视频、播放器、录制、相机、图片处理、audio、media、camera、im…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_media`
- `kits_network`: HarmonyOS NetworkKit 网络能力集使用规范。包含 HTTP 请求、WebSocket、Socket 连接、网络状态检测、VPN 等网络相关能力。Use when: (1) 发送 HTTP 请求，(2) 实现 WebSocket 通信，(3) 检测网络状态，(4) 处理网络连接。Triggers: HTTP、fetch、axios、网络请求…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_network`
- `kits_security`: HarmonyOS 安全能力集使用规范。包含 CryptoArchitectureKit 加密解密、UserAuthenticationKit 用户认证（指纹、人脸）、UniversalKeystoreKit 密钥库等安全功能。Use when: (1) 数据加密解密，(2) 用户认证，(3) 指纹/人脸识别，(4) 密钥管理。Triggers: 加密、解…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_security`
- `kits_telephony`: HarmonyOS TelephonyKit 电话能力集使用规范。包含电话拨打、短信发送、SIM卡管理、网络状态监听等功能。Use when: (1) 拨打电话，(2) 发送短信，(3) 读取SIM卡信息，(4) 监听通话状态。Triggers: 电话、拨打、短信、SMS、SIM卡、通话、telephony、call、sms、sim、@ohos.telep…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_telephony`
- `kits_web`: HarmonyOS ArkWeb Web组件能力集使用规范。包含 Web 组件、WebView 控制器、网页加载、JavaScript交互、Cookie管理等功能。Use when: (1) 加载网页，(2) WebView交互，(3) JS桥接，(4) 网页调试。Triggers: 网页、WebView、浏览器、HTML、JavaScript、Web组件…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_web`

## Application services

- `hmos-account-kit-quicklogin-client`: 基于 HarmonyOS Account Kit 提供华为账号一键登录客户端接入指引，实现获取匿名手机号接口与华为账号一键登录组件集成。支持获取匿名手机号后一键登录页面跳转、失败Toast提示等。在用户提及"华为账号一键登录"、"接入华为账号一键登录"、"Account Kit一键登录"或要求实现华为账号一键登录功能时使用（当前仅支持原生ArkTS框架）
  Source path: `04-development/05-application-services/account-kit/hmos-account-kit-quicklogin-client`
- `hmos-live-view-kit-build-location`: HarmonyOS实况窗（LiveView）代码生成助手，支持创建、更新、停止实况窗。用户输入创建/更新/结束/完整/补全实况窗代码时触发，覆盖即时配送、打车、排队、计时、航班、高铁、共享租赁、运动锻炼、导航九大场景。
  Source path: `04-development/05-application-services/live-view-kit/hmos-live-view-kit-build-location`
- `hmos-push-kit`: 华为Push Kit推送服务集成助手（Master Skill/大路由）。帮助开发者快速集成HarmonyOS推送功能，获取Push Token， 配置推送服务，开通场景化消息权益。支持发送通知消息、应用内通话消息、后台消息等场景。 ============================================================ 触…
  Source path: `04-development/05-application-services/push-kit/hmos-push-kit`
- `hmos-push-kit-background`: 推送后台消息助手。当开发者需要实现后台消息接收、数据静默更新、或消息缓存功能时触发。 ============================================================ 触发条件（只有满足以下意图时才触发）： =======================================================…
  Source path: `04-development/05-application-services/push-kit/hmos-push-kit/hmos-push-kit-background`
- `hmos-push-kit-notification`: 发送通知消息助手。当开发者需要实现推送通知功能、发送消息提醒、配置通知样式或点击动作时触发。 ============================================================ 触发条件（只有满足以下意图时才触发）： ===================================================…
  Source path: `04-development/05-application-services/push-kit/hmos-push-kit/hmos-push-kit-notification`
- `hmos-push-kit-token`: Push Token 获取助手。可作为单独接入能力使用。当开发者需要集成华为推送服务、首次获取 Push Token、或 Token 获取失败时触发。 ============================================================ 触发条件（只有满足以下意图时才触发）： ======================…
  Source path: `04-development/05-application-services/push-kit/hmos-push-kit/hmos-push-kit-token`
- `hmos-push-kit-voip`: 推送应用内通话消息助手（VOIP）。当开发者需要实现语音/视频来电通知、voip功能、或呼叫接听界面时触发。 ============================================================ 触发条件（只有满足以下意图时才触发）： ===========================================…
  Source path: `04-development/05-application-services/push-kit/hmos-push-kit/hmos-push-kit-voip`
- `hmos-scan-kit-customscan`: 帮助开发者快速接入华为 Scan Kit 自定义界面扫码能力，仅在需要支持完全自定义相机预览流 UI 界面、闪光灯控制、变焦、对焦等功能的场景使用
  Source path: `04-development/03-media/scan-kit/hmos-scan-kit-customscan`

## Atomic service

- `hmos-ascf-assistant`: 辅助开发者使用 ASCF 工具链开发 HarmonyOS 元服务。触发场景：(1) 任何提到 ASCF 的问题；(2) 检测到项目包含 ascf/ascf_src 目录（即 ASCF 项目）；(3) 需要生成元服务睫毛图；(4) 将小程序转换为 ASCF 元服务；(5) 开发ASCF元服务页面/组件/平台能力（华为账号登录、隐私托管、授权、支付、分享、we…
  Source path: `04-development/01-application-framework/atomic-service/hmos-ascf-assistant`
- `hmos-ascf-convert-taro`: 辅助开发者将 Taro 项目适配（转换）为 ASCF 元服务。当需要在 Taro（React/Vue）项目中支持 ASCF 元服务平台，或将现有 Taro 项目迁移到 ASCF 时使用此技能。提供完整的环境搭建、项目配置、package.json 脚本、常见问题排查和发布流程。
  Source path: `04-development/01-application-framework/atomic-service/hmos-ascf-convert-taro`
- `hmos-ascf-convert-uniapp`: 辅助开发者将 uni-app 项目适配(转换)为 ASCF 元服务。当需要使用 uni-app（HBuilderX 或 CLI）开发 HarmonyOS 元服务（MP-HARMONY），或将现有 uni-app 项目迁移(转换)到 ASCF 时使用此技能。提供完整的环境搭建、HBuilderX 开发流程、CLI 配置、常见问题排查和上架审核指引。
  Source path: `04-development/01-application-framework/atomic-service/hmos-ascf-convert-uniapp`
- `hmos-atomicservice-assistant`: 辅助鸿蒙开发者构建元服务（Atomic Service / 免安装应用）。只要用户提到元服务、atomicService、免安装、atomic service，或遇到以下任意问题，都必须使用本 Skill：创建/改造元服务项目、@atomicservice API 报错、配置隐私托管、设置可信域名、静默登录/免密登录、接入鸿蒙支付、包大小超限、Atomic…
  Source path: `04-development/01-application-framework/atomic-service/hmos-atomicservice-assistant`

## Multi-device

- `hmos-multidevice-avoid-areas`: Handle HarmonyOS avoid-area adaptation through a declarative scene and resource index. Use when the task involves safe area expansion, status bar or navigation bar avoidance, notc…
  Source path: `03-solutions/HMOS-technologies/multi-device/hmos-multidevice-avoid-areas`
- `hmos-multidevice-fold-state`: HarmonyOS foldable-device adaptation skill for requirements, development, bug-fix, and verification phases. Activate when the task involves fold status detection, hover-mode split…
  Source path: `03-solutions/HMOS-technologies/multi-device/hmos-multidevice-fold-state`
- `hmos-multidevice-hardware-access`: Handle HarmonyOS hardware-capability adaptation through a declarative scene and resource index. Use when the task involves camera selection, camera rotation/stride/foldable adapta…
  Source path: `03-solutions/HMOS-technologies/multi-device/hmos-multidevice-hardware-access`
- `hmos-multidevice-interaction-methods`: HarmonyOS应用多设备交互适配开发方案skill，提供触摸、鼠标、键盘、手写笔等多输入方式的交互方案和事件归一策略。当涉及触摸、鼠标、键盘、手写笔等设备的交互以及实现交互归一化、悬停效果、右键菜单、焦点导航、键盘快捷键、手写板输入和压感等功能时调用。
  Source path: `03-solutions/HMOS-technologies/multi-device/hmos-multidevice-interaction-methods`
- `hmos-multidevice-natural-orientation`: 鸿蒙 HarmonyOS 屏幕方向与旋转相关的需求分析、开发实现、问题修复和功能验证。当任务涉及以下场景时使用：setPreferredOrientation、屏幕旋转(rotation)、屏幕方向(orientation)、自然方向、折叠屏方向、三折叠G态、follow_desktop、视频横竖屏切换、短视频自适应旋转、多设备方向策略、module.js…
  Source path: `03-solutions/HMOS-technologies/multi-device/hmos-multidevice-natural-orientation`
- `hmos-multidevice-scenario-entry`: Entry skill for HarmonyOS multi-device adaptation. Use when the task broadly concerns HarmonyOS multi-device adaptation, the task involves foldable device verification or when the…
  Source path: `03-solutions/HMOS-technologies/multi-device/hmos-multidevice-scenario-entry`
- `hmos-multidevice-screen-window-size`: HarmonyOS 多设备屏幕窗口尺寸适配。当任务涉及以下任一场景时必须调用：（1）比价与分屏：比价/比价场景/比价窗口/价格对比/创建新窗口/多窗口并行/双窗口；（2）平行视界与分栏：平行视界/EasyGo/easy_go.json/分栏效果/分栏布局/列表详情分栏/navigationSplit/routerSplit/Navigation分栏；（3）…
  Source path: `03-solutions/HMOS-technologies/multi-device/hmos-multidevice-screen-window-size`

## Stability

- `hmos-apifault-analysis`: DFX Skills，定位开发者问题。当用户输入错误码、错误信息、错误日志、执行失败或需要定位问题时使用。
  Source path: `03-solutions/quality/stability/hmos-apifault-analysis`
- `hmos-appfreeze-analysis`: DFX Skills，自动分析 HarmonyOS / OpenHarmony Freeze（冻屏/卡死）故障日志，定位根因并输出完整证据链。 当用户提供完整的faultlog 文件和采样栈文件、询问应用无响应/卡死/ANR 问题的根因， 或上传包含 APPFREEZE / INPUT_BLOCK / LIFECYCLE_TIMEOUT 等关键字的日志时，…
  Source path: `03-solutions/quality/stability/hmos-appfreeze-analysis`
- `hmos-cppcrash-analysis`: DFX Skills，分析 HarmonyOS/OpenHarmony 应用的 CppCrash（Native 层崩溃）故障日志，定位根因并给出修复建议。当用户提供 cppcrash 日志、粘贴 Native 崩溃堆栈、询问 SIGSEGV/SIGABRT/SIGILL/SIGBUS 崩溃原因、或上传含有信号值/寄存器/调用栈的故障日志时，必须使用此技能。…
  Source path: `03-solutions/quality/stability/hmos-cppcrash-analysis`
- `hmos-jscrash-analysis`: DFX Skills，分析 HarmonyOS/OpenHarmony 应用的 JS Crash（ArkTS/JS 层闪退）faultlogger 日志， 按 Reason、Error name、Error message、Error code 和 Stacktrace 定位根因并给出修复建议。 当用户提供包含 JS Crash、Reason:Error/…
  Source path: `03-solutions/quality/stability/hmos-jscrash-analysis`
- `hmos-jsleak-analysis`: DFX Skills，分析 rawheap / heapsnapshot 聚类后的内存对象数据，识别疑似内存泄漏。当用户提供 .rawheap 文件、.heapsnapshot 文件、堆内存聚类报告、heap_cluster.mjs 输出结果，或询问"哪些对象在泄漏""哪些对象没有释放""分析这份内存报告""帮看下内存泄漏""为什么内存涨这么多"时，必须使…
  Source path: `03-solutions/quality/stability/hmos-jsleak-analysis`
- `hmos-memleak-analysis`: Analyzes HarmonyOS source code (ArkTS, JS, C/C++) to detect memory leaks.Use when (1) Performing static code analysis to catch potential leaks before deployment, (2) Reviewing PRs…
  Source path: `03-solutions/quality/stability/hmos-memleak-analysis`
- `hmos-scan-kit-defaultscan`: 帮助开发者快速接入华为 Scan Kit 默认界面扫码能力，在不需要完全自定义相机界面、闪光灯控制、变焦、对焦等高级功能时优先使用
  Source path: `04-development/03-media/scan-kit/hmos-scan-kit-defaultscan`
- `kits_performance`: HarmonyOS PerformanceAnalysisKit 性能分析能力集使用规范。包含 hilog 日志、hiAppEvent 事件、hidebug 调试、FaultLogger 故障日志、bytrace/hiTraceMeter 性能追踪等能力。Use when: (1) 日志输出，(2) 性能分析，(3) 故障排查，(4) 事件打点。Trigg…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_performance`

## Testing

- `hmos-instrument-test`: 在 HarmonyOS 应用/服务开发中执行模块的 Instrument Test（包括 ArkTS/JS 和 C++ 测试），支持运行、覆盖率统计、ASan 检测等模式，并可指定测试范围（模块、测试套件、单个用例）。
  Source path: `05-test/hmos-instrument-test`
- `hmos-local-test`: 在 HarmonyOS 应用/服务开发中执行模块的 Local Test（ArkTS/JS 单元测试），支持运行、覆盖率统计等模式，并可指定测试范围（模块、测试套件、单个用例）。
  Source path: `05-test/hmos-local-test`
- `kits_test`: HarmonyOS TestKit 测试能力集使用规范。包含UI自动化测试、单元测试、测试驱动、组件查找和操作等功能。Use when: (1) 编写UI测试，(2) 自动化测试脚本，(3) 组件查找和操作，(4) 测试运行器。Triggers: 测试、UI测试、自动化、单元测试、uitest、Driver、testRunner。
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/kits_test`

## Design

- `hmos-design-visual-mobile`: HarmonyOS 移动端页面视觉还原技能。基于仓库内设计规范文档与组件模板，生成符合 HarmonyOS Design Token 标准的高保真移动端 HTML 页面。触发场景：(1) 用户要求生成/还原 HarmonyOS 移动端页面 (2) 用户提供设计稿/截图/参考图，要求还原为 HarmonyOS 风格 HTML 页面 (3) 用户提到"视觉还原…
  Source path: `02-design/mobile/hmos-design-visual-mobile`

## Knowledge

- `harmony-learner`: HarmonyOS 知识学习助手 - 基于已加载的本地知识，按需搜索云端文档，以结构化格式回答学习咨询类问题（新特性、版本差异、API 对比、概念解释、迁移指南等）。Triggers: 新特性, 版本差异, target区别, API对比, 最佳实践, 设计理念, 变更说明, 迁移指南, 有什么区别, 怎么理解, 是什么意思
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/harmony-learner`
- `hmos-arkts-knowledge-retriever`: Retrieve grounded ArkTS references for pure non-UI ArkTS work and ArkTS API usage. Use this skill whenever the user is writing, reviewing, testing, validating, running, or debuggi…
  Source path: `04-development/01-application-framework/ArkTS/hmos-arkts-knowledge-retriever`
- `hmos-arkui-develop-skill`: ArkUI 代码开发助手，面向 HarmonyOS UI 开发，提供基于知识库的UI开发能力。内部调用 hmos-arkui-knowledge-retriever 的检索能力获取 API 证据。包含编码约束规则和最佳实践参考。触发场景：(1) 用户要求生成 ArkUI 页面或组件 (2) 用户在现有 .ets 工程上要求增删改功能 (3) 用户提供报错/…
  Source path: `04-development/01-application-framework/ArkUI/hmos-arkui-develop-skill`
- `hmos-arkui-knowledge-retriever`: ArkUI 知识检索层，按问题语境自动路由到 ArkTS 声明式或 NDK(C-API)知识库进行精准检索，不涉及代码生成或修改。触发场景：(1) 用户查询 ArkUI/ArkTS API 用法、参数细节或版本支持 (2) 验证组件/装饰器的正确用法 (3) 排查 ArkUI 编译错误码或运行时异常 (4) 询问状态管理 V1/V2 差异或迁移 (5) 查…
  Source path: `04-development/01-application-framework/ArkUI/hmos-arkui-knowledge-retriever`
- `knowledge_search`: HarmonyOS 知识搜索能力。通过 MCP 工具 harmonyos_knowledge_search（按后缀匹配）搜索鸿蒙官方文档和知识库。Use when: (1) 用户对鸿蒙问题进行提问，(2) 现有知识不足以支撑 HarmonyOS 功能开发，(3) 需要查询最新的 API 文档或最佳实践，(4) 遇到未知错误或问题需要查找解决方案。Trigg…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/knowledge_search`

## Refactoring

- `refactoring`: Safe ArkTS code refactoring using LSP-powered reference finding. Use when: (1) renaming a function/class/variable, (2) moving code between files, (3) deleting code and verifying n…
  Source path: `07-tools/tools/deveco-studio/deveco-native-flow/references/refactoring`

## Skill quality

- `hmos-skill-reviewer`: Review and validate Agent Skills for compliance with Claude Skills specification. Use when evaluating SKILL.md files, checking naming conventions, validating content structure, or…
  Source path: `.hmos-skill-reviewer`

## General

- `hmos-ability-insight-intent-generator`: Generates OpenHarmony intent decorator code from user requirements with automatic decorator selection. Use when the user mentions "intent", "@InsightIntent", or needs to integrate…
  Source path: `04-development/01-application-framework/ability/hmos-ability-insight-intent-generator`

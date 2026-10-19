from __future__ import annotations

SKILLSMP_ROOT_CATEGORIES: list[dict[str, object]] = [
    {
        "id": "tools",
        "name": "工具",
        "description": "开发工具和实用程序，包括调试、生产力、系统管理、自动化、IDE、CLI、域名与 DNS。",
        "children": [
            {"id": "debugging", "name": "调试工具", "description": "Debugging tools and troubleshooting skills."},
            {"id": "productivity-tools", "name": "生产力工具", "description": "Productivity and integration tools."},
            {
                "id": "system-admin",
                "name": "系统管理",
                "description": "System administration and operations utilities.",
            },
            {"id": "automation-tools", "name": "自动化工具", "description": "Automation tools and workflow helpers."},
            {"id": "ide-plugins", "name": "IDE 插件", "description": "IDE plugins and editor integrations."},
            {"id": "cli-tools", "name": "命令行工具", "description": "Command-line tools and terminal workflows."},
            {
                "id": "domain-utilities",
                "name": "域名与 DNS 工具",
                "description": "Domain, DNS, and network utility skills.",
            },
        ],
    },
    {
        "id": "business",
        "name": "商业",
        "description": "商业场景技能，包括销售营销、项目管理、金融投资、法律地产、健康健身、支付、电商和商业应用。",
        "children": [
            {
                "id": "sales-marketing",
                "name": "销售与营销",
                "description": "Sales, marketing, growth, SEO, and customer acquisition.",
            },
            {
                "id": "project-management",
                "name": "项目管理",
                "description": "Project planning, tracking, coordination, and delivery.",
            },
            {
                "id": "finance-investment",
                "name": "金融与投资",
                "description": "Finance, investment, accounting, and market analysis.",
            },
            {
                "id": "real-estate-legal",
                "name": "房地产与法律",
                "description": "Real estate and legal workflow skills.",
            },
            {
                "id": "health-fitness",
                "name": "健康健身",
                "description": "Health, fitness, and wellness business workflows.",
            },
            {"id": "payment", "name": "支付", "description": "Payment, billing, and checkout workflows."},
            {"id": "ecommerce", "name": "电子商务", "description": "E-commerce operations and storefront workflows."},
            {"id": "business-apps", "name": "商业应用", "description": "Business applications and office workflows."},
        ],
    },
    {
        "id": "development",
        "name": "开发",
        "description": "软件开发技能，包括架构、前端、后端、CMS、游戏、脚本、移动端、全栈、包发布和框架内核。",
        "children": [
            {
                "id": "architecture-patterns",
                "name": "架构模式",
                "description": "Software architecture, patterns, and design decisions.",
            },
            {
                "id": "frontend",
                "name": "前端开发",
                "description": "Frontend development, UI engineering, and web clients.",
            },
            {
                "id": "backend",
                "name": "后端开发",
                "description": "Backend services, APIs, and server-side development.",
            },
            {"id": "cms-platforms", "name": "CMS 与平台开发", "description": "CMS and platform development skills."},
            {"id": "gaming", "name": "游戏开发", "description": "Game development and interactive experiences."},
            {"id": "scripting", "name": "脚本编程", "description": "Scripting, automation code, and small utilities."},
            {"id": "mobile", "name": "移动开发", "description": "Mobile app development and device clients."},
            {"id": "full-stack", "name": "全栈开发", "description": "Full-stack product and application development."},
            {
                "id": "package-distribution",
                "name": "包管理与发布",
                "description": "Package management, distribution, and publishing.",
            },
            {
                "id": "framework-internals",
                "name": "框架内核开发",
                "description": "Framework internals and low-level platform work.",
            },
            {
                "id": "ecommerce-development",
                "name": "电商开发",
                "description": "E-commerce development and commerce integrations.",
            },
        ],
    },
    {
        "id": "data-ai",
        "name": "数据与AI",
        "description": "数据和 AI 技能，包括 LLM/AI、机器学习、数据工程、数据分析。",
        "children": [
            {
                "id": "llm-ai",
                "name": "LLM 与 AI",
                "description": "Large language models, AI agents, prompting, and model workflows.",
            },
            {
                "id": "machine-learning",
                "name": "机器学习",
                "description": "Machine learning modeling, training, and evaluation.",
            },
            {
                "id": "data-engineering",
                "name": "数据工程",
                "description": "Data pipelines, ETL, and data infrastructure.",
            },
            {
                "id": "data-analysis",
                "name": "数据分析",
                "description": "Data analysis, BI, reporting, and visualization.",
            },
        ],
    },
    {
        "id": "testing-security",
        "name": "测试与安全",
        "description": "测试、安全和代码质量技能。",
        "children": [
            {
                "id": "code-quality",
                "name": "代码质量",
                "description": "Code quality, linting, refactoring, and maintainability.",
            },
            {"id": "testing", "name": "测试", "description": "Testing, QA, test automation, and validation."},
            {
                "id": "security",
                "name": "安全",
                "description": "Security analysis, hardening, auditing, and vulnerability workflows.",
            },
        ],
    },
    {
        "id": "devops",
        "name": "DevOps",
        "description": "DevOps 技能，包括 Git 工作流、CI/CD、云平台、容器和监控。",
        "children": [
            {
                "id": "git-workflows",
                "name": "Git 工作流",
                "description": "Git, version control, branching, and collaboration workflows.",
            },
            {
                "id": "cicd",
                "name": "CI/CD",
                "description": "Continuous integration, deployment, and release automation.",
            },
            {"id": "cloud", "name": "云平台", "description": "Cloud platform operations and infrastructure."},
            {"id": "containers", "name": "容器", "description": "Containers, Docker, Kubernetes, and orchestration."},
            {"id": "monitoring", "name": "监控", "description": "Monitoring, observability, logging, and alerts."},
        ],
    },
    {
        "id": "documentation",
        "name": "文档",
        "description": "文档技能，包括知识库、技术文档和教育材料。",
        "children": [
            {
                "id": "knowledge-base",
                "name": "知识库",
                "description": "Knowledge base creation, maintenance, and retrieval.",
            },
            {
                "id": "technical-docs",
                "name": "技术文档",
                "description": "Technical documentation, API docs, and developer guides.",
            },
            {"id": "education", "name": "教育", "description": "Education, tutorials, courses, and learning content."},
        ],
    },
    {
        "id": "content-media",
        "name": "内容与媒体",
        "description": "内容和媒体技能，包括文档处理、内容创作、设计、媒体处理。",
        "children": [
            {
                "id": "documents",
                "name": "文档处理",
                "description": "Document processing, conversion, extraction, and editing.",
            },
            {
                "id": "content-creation",
                "name": "内容创作",
                "description": "Content creation, writing, publishing, and creative workflows.",
            },
            {"id": "design", "name": "设计", "description": "Design, visual assets, UX, and creative production."},
            {"id": "media", "name": "媒体处理", "description": "Image, audio, video, and media processing."},
        ],
    },
    {
        "id": "research",
        "name": "研究",
        "description": "研究技能，包括学术研究、生物信息、计算化学、实验室工具、科学计算和天文物理。",
        "children": [
            {
                "id": "academic",
                "name": "学术研究",
                "description": "Academic research, papers, citations, and literature review.",
            },
            {
                "id": "bioinformatics",
                "name": "生物信息学",
                "description": "Bioinformatics, genomics, and computational biology.",
            },
            {
                "id": "computational-chemistry",
                "name": "计算化学",
                "description": "Computational chemistry and molecular workflows.",
            },
            {
                "id": "lab-tools",
                "name": "实验室工具",
                "description": "Laboratory tools, protocols, and research operations.",
            },
            {
                "id": "scientific-computing",
                "name": "科学计算",
                "description": "Scientific computing, simulations, and numerical analysis.",
            },
            {
                "id": "astronomy-physics",
                "name": "天文物理",
                "description": "Astronomy, physics, and space-science workflows.",
            },
        ],
    },
    {
        "id": "lifestyle",
        "name": "生活方式",
        "description": "生活方式技能，包括健康养生、文学写作、哲学伦理、占卜玄学、艺术手工、烹饪艺术。",
        "children": [
            {
                "id": "wellness-health",
                "name": "健康养生",
                "description": "Wellness, health, habits, and personal care.",
            },
            {
                "id": "literature-writing",
                "name": "文学与写作",
                "description": "Literature, writing, journaling, and creative text.",
            },
            {
                "id": "philosophy-ethics",
                "name": "哲学与伦理",
                "description": "Philosophy, ethics, reasoning, and reflection.",
            },
            {
                "id": "divination-mysticism",
                "name": "占卜与玄学",
                "description": "Divination, mysticism, and esoteric workflows.",
            },
            {"id": "arts-crafts", "name": "艺术与手工", "description": "Arts, crafts, and hands-on creative projects."},
            {
                "id": "culinary-arts",
                "name": "烹饪艺术",
                "description": "Cooking, recipes, food, and culinary workflows.",
            },
        ],
    },
    {
        "id": "databases",
        "name": "数据库",
        "description": "数据库技能，包括 SQL 数据库、数据库工具和 NoSQL 数据库。",
        "children": [
            {
                "id": "sql-databases",
                "name": "SQL 数据库",
                "description": "SQL databases, schema, queries, and relational data.",
            },
            {
                "id": "database-tools",
                "name": "数据库工具",
                "description": "Database tools, administration, migration, and maintenance.",
            },
            {
                "id": "nosql-databases",
                "name": "NoSQL 数据库",
                "description": "NoSQL databases, document stores, key-value, and graph data.",
            },
        ],
    },
    {
        "id": "blockchain",
        "name": "区块链",
        "description": "区块链技能，包括 Web3 工具、智能合约和 DeFi。",
        "children": [
            {
                "id": "web3-tools",
                "name": "Web3 工具",
                "description": "Web3 tools, wallets, chains, and dApp workflows.",
            },
            {
                "id": "smart-contracts",
                "name": "智能合约",
                "description": "Smart contract development, auditing, and deployment.",
            },
            {"id": "defi", "name": "DeFi", "description": "DeFi protocols, trading, liquidity, and on-chain finance."},
        ],
    },
]

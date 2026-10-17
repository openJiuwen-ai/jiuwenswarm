export const settingsExtensionLocale = {
  zh: {
    settingsExtension: {
      categories: { organization: '组织管理' },
      moduleDescriptions: { organization: '管理组织信息和审计策略。' },
      organization: {
        section: '组织信息',
        loading: '正在加载组织设置',
        name: '组织名称',
        nameDescription: '当前工作区中展示的组织名称。',
        audit: '启用审计记录',
        auditDescription: '记录组织设置的修改操作。',
        save: '保存组织设置',
        loadFailed: '组织设置加载失败，请稍后重试。',
        saveFailed: '组织设置保存失败，请稍后重试。',
      },
      access: {
        centrallyManaged: '此设置由管理员统一管理，当前账号仅可查看。',
      },
    },
  },
  en: {
    settingsExtension: {
      categories: { organization: 'Organization' },
      moduleDescriptions: { organization: 'Manage organization information and audit policies.' },
      organization: {
        section: 'Organization information',
        loading: 'Loading organization settings',
        name: 'Organization name',
        nameDescription: 'The organization name displayed in the workspace.',
        audit: 'Enable audit records',
        auditDescription: 'Record changes to organization settings.',
        save: 'Save organization settings',
        loadFailed: 'Failed to load organization settings. Try again later.',
        saveFailed: 'Failed to save organization settings. Try again later.',
      },
      access: {
        centrallyManaged: 'This setting is centrally managed and is read-only for your account.',
      },
    },
  },
} as const;

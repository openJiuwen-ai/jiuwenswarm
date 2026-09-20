export const AGENT_TAG_OPTIONS = [
  { id: 'product-development', labelKey: 'agentManagement.categories.ProductDevelopment', labels: { zh: '产品研发', en: 'Product Development' } },
  { id: 'marketing', labelKey: 'agentManagement.categories.Marketing', labels: { zh: '市场营销', en: 'Marketing' } },
  { id: 'efficiency', labelKey: 'agentManagement.categories.Efficiency', labels: { zh: '效率提升', en: 'Efficiency' } },
  { id: 'data-analysis', labelKey: 'agentManagement.categories.DataAnalysis', labels: { zh: '数据分析', en: 'Data Analysis' } },
  { id: 'content-creation', labelKey: 'agentManagement.categories.ContentCreation', labels: { zh: '内容创作', en: 'Content Creation' } },
  { id: 'safety-compliance', labelKey: 'agentManagement.categories.SafetyCompliance', labels: { zh: '安全合规', en: 'Safety & Compliance' } },
  { id: 'communication', labelKey: 'agentManagement.categories.Communication', labels: { zh: '通讯协作', en: 'Communication' } },
] as const;

export function resolveAgentTagPayload(tagIds: string[], customTags: string[]) {
  return [
    ...tagIds.flatMap(tagId => {
      const labels = AGENT_TAG_OPTIONS.find(option => option.id === tagId)?.labels;
      return labels ? [labels] : [];
    }),
    ...customTags.map(label => ({ zh: label, en: label })),
  ];
}

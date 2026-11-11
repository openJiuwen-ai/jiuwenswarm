import type { TFunction } from 'i18next';

export function marketplaceCategoryLabel(
  t: TFunction,
  category: string,
  section: 'agentManagement' | 'connectorMarket',
): string {
  const slug = category.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  return t(
    [`${section}.categories.${category}`, `skills.marketplaceCategories.${slug}`],
    { defaultValue: category },
  );
}

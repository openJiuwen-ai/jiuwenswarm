// "从模板创建" 图标：3 个六边形堆叠（1 上 2 下），右下角六边形中间还有一个圆形。
// lucide 与项目 svg 导出资产里都没有等价图标，手绘还原（图标策略第③级）。
export default function TemplateClusterIcon({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
      <polygon
        points="8,2 10.8,3.6 10.8,6.8 8,8.4 5.2,6.8 5.2,3.6"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
      <polygon
        points="4.6,7.8 7.4,9.4 7.4,12.6 4.6,14.2 1.8,12.6 1.8,9.4"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
      <polygon
        points="11.4,7.8 14.2,9.4 14.2,12.6 11.4,14.2 8.6,12.6 8.6,9.4"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
      <circle cx="11.4" cy="11" r="1.3" stroke="currentColor" strokeWidth="1.1" />
    </svg>
  );
}

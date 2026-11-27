const fs = require('fs');
const path = require('path');

const scriptDir = __dirname;
const envRoot = process.env.JIUWENCLAW_ROOT ? path.resolve(process.env.JIUWENCLAW_ROOT) : '';
const agentFromEnv = envRoot ? path.join(envRoot, 'agent') : '';
const packageAgentFromResources = path.join(scriptDir, '../resources/agent');

const agentRoot = [agentFromEnv, packageAgentFromResources]
  .filter(Boolean)
  .find((candidate) => fs.existsSync(candidate));

if (!agentRoot) {
  console.error('❌ 错误: 无法定位 agent 目录');
  process.exit(1);
}

const outputPath = path.join(agentRoot, 'jiuwenclaw_workspace', 'agent-data.json');

console.log('扫描目录:', agentRoot);

try {
  if (!fs.existsSync(agentRoot)) {
    console.error('❌ 错误: agent 目录不存在！');
    process.exit(1);
  }

  const isMarkdownFile = (fileName) => fileName.endsWith('.md') || fileName.endsWith('.mdx');
  const ROOT_FOLDER_KEY = '__root__';
  const folderData = {};
  const seenPaths = {}; // folderKey -> Set of normalized paths，用于去重 _zh/_en

  const normalizeLangSuffix = (name) => {
    const lastDot = name.lastIndexOf('.');
    if (lastDot === -1) return name;
    const stem = name.slice(0, lastDot);
    const suffix = name.slice(lastDot + 1);
    if (!/\.(md|mdx)$/i.test('.' + suffix)) return name;
    const stemLower = stem.toLowerCase();
    if (stemLower.endsWith('_zh')) return stem.slice(0, -3) + '.' + suffix;
    if (stemLower.endsWith('_en')) return stem.slice(0, -3) + '.' + suffix;
    return name;
  };

  const upsertFileToFolder = (folderKey, relativeFilePath) => {
    const rawName = path.basename(relativeFilePath);
    let displayName = normalizeLangSuffix(rawName);
    const relativeFolderPath = path.dirname(relativeFilePath);
    let displayPath = relativeFolderPath === '.'
      ? `agent/${displayName}`
      : `agent/${relativeFolderPath.replace(/\\/g, '/')}/${displayName}`;

    if (!seenPaths[folderKey]) seenPaths[folderKey] = new Set();
    if (seenPaths[folderKey].has(displayPath)) return;
    seenPaths[folderKey].add(displayPath);

    if (!folderData[folderKey]) {
      folderData[folderKey] = [];
    }
    folderData[folderKey].push({
      name: displayName,
      path: displayPath,
      isMarkdown: isMarkdownFile(relativeFilePath)
    });
  };

  const walkDirectory = (absoluteDirPath, relativeDirPath = '') => {
    const entries = fs.readdirSync(absoluteDirPath, { withFileTypes: true });
    entries.forEach((entry) => {
      if (entry.name.startsWith('.')) {
        return;
      }
      const absoluteEntryPath = path.join(absoluteDirPath, entry.name);
      const relativeEntryPath = relativeDirPath
        ? path.join(relativeDirPath, entry.name)
        : entry.name;
      if (entry.isDirectory()) {
        walkDirectory(absoluteEntryPath, relativeEntryPath);
        return;
      }
      if (!entry.isFile()) {
        return;
      }
      const relativeFolderPath = path.dirname(relativeEntryPath);
      const folderKey = relativeFolderPath === '.' ? ROOT_FOLDER_KEY : relativeFolderPath.replace(/\\/g, '/');
      upsertFileToFolder(folderKey, relativeEntryPath);
    });
  };

  walkDirectory(agentRoot);

  // 为了稳定输出，统一排序文件夹与文件
  const sortedFolderData = Object.keys(folderData)
    .sort((a, b) => a.localeCompare(b))
    .reduce((acc, folder) => {
      const sortedFiles = folderData[folder]
        .slice()
        .sort((a, b) => a.path.localeCompare(b.path));
      acc[folder] = sortedFiles;
      return acc;
    }, {});

  // 确保输出目录存在后写入 JSON 文件
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(outputPath, JSON.stringify(sortedFolderData, null, 2));
  console.log('✅ 成功生成文件结构:', outputPath);
  console.log('📁 找到的文件夹:', Object.keys(sortedFolderData));
  
} catch (error) {
  console.error('❌ 读取目录失败:', error.message);
  fs.writeFileSync(outputPath, JSON.stringify({}, null, 2));
}

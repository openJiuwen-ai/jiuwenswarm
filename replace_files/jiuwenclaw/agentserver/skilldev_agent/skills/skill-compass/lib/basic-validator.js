/**
 * basic-validator.js — Basic Validator
 *
 * Provides common basic checks for all dimensions to reduce LLM token consumption
 * Includes word count, format check, tool declaration validation, etc.
 */

const fs = require('node:fs');
const yaml = require('./yaml-loader.js');

class BasicValidator {
  constructor() {
    this.findings = [];
  }

  /**
   * Common basic checks
   * @param {string} filePath - SKILL.md file path
   * @returns {Object} Basic statistics
   */
  validateBasics(filePath) {
    const content = fs.readFileSync(filePath, 'utf-8');
    const frontmatter = this.extractFrontmatter(content);
    const body = this.extractBody(content);
    const description = typeof frontmatter.description === 'string' ? frontmatter.description : '';
    const codeBlocks = this.extractCodeBlocks(body);

    return {
      wordCount: this.countWords(body),
      lineCount: content.split('\n').length,
      frontmatter: {
        name: frontmatter.name || null,
        keys: Object.keys(frontmatter),
        hasDescription: description.trim().length > 0,
        descriptionLength: description.length
      },
      body: {
        charCount: body.length,
        nonEmptyLineCount: body.split('\n').filter(line => line.trim().length > 0).length,
        headingCount: (body.match(/^#{1,6}\s+/gm) || []).length
      },
      hasTools: this.checkToolDeclarations(frontmatter),
      codeBlocks: codeBlocks.map(block => ({
        language: block.language,
        length: block.length
      })),
      complexity: this.assessComplexity(frontmatter, body)
    };
  }

  /**
   * Check tool declarations
   */
  checkToolDeclarations(frontmatter) {
    const tools = frontmatter.tools || [];
    const declared = Array.isArray(tools) ? tools : [];

    return {
      declared: declared.map(tool => typeof tool === 'string' ? tool : tool.name || tool),
      count: declared.length,
      hasPowerfulTools: declared.some(tool =>
        ['Bash', 'Write', 'Edit', 'MultiEdit'].includes(typeof tool === 'string' ? tool : tool.name)
      ),
      hasNetworkTools: declared.some(tool =>
        ['WebFetch', 'WebSearch'].includes(typeof tool === 'string' ? tool : tool.name)
      )
    };
  }

  /**
   * Extract code blocks
   */
  extractCodeBlocks(content) {
    const codeBlocks = [];
    const pattern = /```(\w+)?\n([\s\S]*?)\n```/g;
    let match;

    while ((match = pattern.exec(content)) !== null) {
      codeBlocks.push({
        language: match[1] || 'text',
        content: match[2],
        length: match[2].length
      });
    }

    return codeBlocks;
  }

  /**
   * Assess complexity
   */
  assessComplexity(frontmatter, body) {
    let score = 0;

    // Tool usage increases complexity
    const tools = frontmatter.tools || [];
    score += tools.length * 2;

    // Parameters increase complexity
    const args = frontmatter.arguments || [];
    score += args.length * 3;

    // Multiple commands increase complexity
    const commands = frontmatter.commands || [];
    score += Math.max(0, commands.length - 1) * 2;

    // Code blocks increase complexity
    const codeBlocks = this.extractCodeBlocks(body);
    score += codeBlocks.length * 5;

    // Length increases complexity
    const words = this.countWords(body);
    if (words > 500) score += Math.floor((words - 500) / 100);

    // Categorize complexity
    if (score <= 5) return 'simple';
    if (score <= 15) return 'moderate';
    if (score <= 30) return 'complex';
    return 'very_complex';
  }

  /**
   * Count words
   */
  countWords(text) {
    return text.trim().split(/\s+/).filter(word => word.length > 0).length;
  }

  /**
   * Extract frontmatter
   */
  extractFrontmatter(content) {
    if (!content.startsWith('---')) return {};

    const endIndex = content.indexOf('---', 3);
    if (endIndex === -1) return {};

    const yamlContent = content.substring(3, endIndex);

    try {
      return yaml.load(yamlContent) || {};
    } catch (error) {
      console.warn('Failed to parse YAML frontmatter:', error.message);
      return {};
    }
  }

  /**
   * Extract body content
   */
  extractBody(content) {
    if (!content.startsWith('---')) return content;

    const endIndex = content.indexOf('---', 3);
    if (endIndex === -1) return content;

    return content.substring(endIndex + 3);
  }

  /**
   * Detect skill type
   */
  detectSkillType(frontmatter, body) {
    const description = (frontmatter.description || '').toLowerCase();
    const bodyLower = body.toLowerCase();
    const tools = frontmatter.tools || [];
    const toolNames = tools.map(t => typeof t === 'string' ? t : t.name || t);

    let type = 'atom';

    // Atom: Single operation, usually with specific input/output
    if (toolNames.length <= 2 &&
        (description.includes('convert') || description.includes('format') ||
         description.includes('validate') || description.includes('generate'))) {
      type = 'atom';
    }
    // Composite: Multi-step process, multiple tools
    else if (toolNames.length >= 3 ||
        bodyLower.includes('step') || bodyLower.includes('then') ||
        bodyLower.includes('next') || bodyLower.includes('finally')) {
      type = 'composite';
    }
    // Meta: Decision framework, guiding content
    else if (description.includes('guide') || description.includes('framework') ||
        description.includes('strategy') || description.includes('approach') ||
        bodyLower.includes('consider') || bodyLower.includes('evaluate') ||
        bodyLower.includes('decide')) {
      type = 'meta';
    }

    return { type, triggerType: this.detectTriggerType(frontmatter) };
  }

  /**
   * Detect trigger type from frontmatter
   */
  detectTriggerType(frontmatter) {
    if (frontmatter.commands && frontmatter.commands.length > 0) return 'command';
    if (frontmatter.hooks && frontmatter.hooks.length > 0) return 'hook';
    if (frontmatter.globs && frontmatter.globs.length > 0) return 'glob';
    if (typeof frontmatter.description === 'string' && frontmatter.description.length > 10) return 'description';
    return 'unknown';
  }

  /**
   * Quick quality check
   */
  quickQualityCheck(content) {
    const issues = [];

    // Check placeholder text
    const placeholders = /TODO|FIXME|placeholder|your_|<[^>]+>|\[.*\]/gi;
    if (placeholders.test(content)) {
      issues.push('Contains placeholder text or TODOs');
    }

    // Check typos (simple version)
    const typos = /teh |recieve|seperate|occurence|neccessary/gi;
    if (typos.test(content)) {
      issues.push('Contains potential spelling errors');
    }

    // Check short paragraphs
    const paragraphs = content.split('\n\n').filter(p => p.trim().length > 0);
    const shortParas = paragraphs.filter(p => p.trim().length < 20).length;
    if (shortParas > paragraphs.length / 2) {
      issues.push('Many very short paragraphs - consider consolidating');
    }

    return issues;
  }
}

module.exports = { BasicValidator };

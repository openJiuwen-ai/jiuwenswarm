const fs = require('node:fs');
const path = require('node:path');
const yaml = require('./yaml-loader.js');

const SKILL_NAME_PATTERN = /^[a-z0-9-]{1,64}$/;
const DESCRIPTION_MAX_TOKENS = 300;
const DESCRIPTION_MAX_CHARS_CJK = 256;
const DESCRIPTION_MAX_CHARS_EN = 512;
const BODY_MAX_LINES = 500;
const BODY_MAX_TOKENS = 5000;

class ComplianceValidator {
  validate(filePath) {
    const resolved = path.resolve(filePath);
    const content = fs.readFileSync(resolved, 'utf-8');
    const parsed = this.parseSkill(content);
    const issues = [];

    const directoryName = path.basename(path.dirname(resolved));
    const frontmatter = parsed.frontmatter || {};
    const name = typeof frontmatter.name === 'string' ? frontmatter.name.trim() : '';
    const description = typeof frontmatter.description === 'string'
      ? frontmatter.description.trim()
      : '';
    const body = parsed.body || '';

    const subScores = {
      name: this.validateName(name, directoryName, issues),
      description: this.validateDescription(description, issues),
      body: this.validateBody(body, issues)
    };

    if (parsed.frontmatterError) {
      issues.unshift({
        category: 'frontmatter',
        severity: 'error',
        item: parsed.frontmatterError,
        location: 'SKILL.md frontmatter'
      });
      subScores.name = Math.min(subScores.name, 2);
      subScores.description = Math.min(subScores.description, 2);
    }

    const score = Math.round(
      subScores.name * 0.4 +
      subScores.description * 0.3 +
      subScores.body * 0.3
    );
    const pass = issues.length === 0;

    return {
      dimension: 'D6',
      dimension_name: 'compliance',
      score,
      max: 10,
      pass,
      details: pass
        ? 'Mandatory skill-name, description, and body constraints pass.'
        : 'Mandatory skill-name, description, or body constraints failed.',
      sub_scores: subScores,
      issues,
      tools_used: ['local'],
      metadata: {
        skill_name: name,
        directory_name: directoryName,
        description_language: this.containsCjk(description) ? 'cjk' : 'non-cjk',
        description_chars: description.length,
        description_tokens: this.estimateTokens(description),
        body_lines: body.split(/\r?\n/).length,
        body_tokens: this.estimateTokens(body)
      }
    };
  }

  parseSkill(content) {
    if (!content.startsWith('---')) {
      return {
        frontmatter: {},
        body: content,
        frontmatterError: 'SKILL.md must start with YAML frontmatter delimited by ---'
      };
    }

    const match = content.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
    if (!match) {
      return {
        frontmatter: {},
        body: '',
        frontmatterError: 'YAML frontmatter is not closed with ---'
      };
    }

    try {
      const frontmatter = yaml.load(match[1]) || {};
      if (!frontmatter || typeof frontmatter !== 'object' || Array.isArray(frontmatter)) {
        return {
          frontmatter: {},
          body: match[2],
          frontmatterError: 'YAML frontmatter must be a mapping'
        };
      }
      return { frontmatter, body: match[2] };
    } catch (error) {
      return {
        frontmatter: {},
        body: match[2],
        frontmatterError: `Invalid YAML frontmatter: ${error.message}`
      };
    }
  }

  validateName(name, directoryName, issues) {
    let score = 10;
    if (!name) {
      issues.push({
        category: 'skill-name',
        severity: 'error',
        item: 'skill-name is required',
        location: 'frontmatter.name'
      });
      return 0;
    }
    if (!SKILL_NAME_PATTERN.test(name)) {
      issues.push({
        category: 'skill-name',
        severity: 'error',
        item: `skill-name must be 1-64 characters and use only [a-z0-9-]; got "${name}"`,
        location: 'frontmatter.name'
      });
      score -= 3;
    }
    if (name.startsWith('-') || name.endsWith('-')) {
      issues.push({
        category: 'skill-name',
        severity: 'error',
        item: 'skill-name must not start or end with -',
        location: 'frontmatter.name'
      });
      score -= 2;
    }
    if (name.includes('--')) {
      issues.push({
        category: 'skill-name',
        severity: 'error',
        item: 'skill-name must not contain consecutive --',
        location: 'frontmatter.name'
      });
      score -= 2;
    }
    if (name !== directoryName) {
      issues.push({
        category: 'skill-name',
        severity: 'error',
        item: `skill-name must match parent directory "${directoryName}"`,
        location: 'frontmatter.name'
      });
      score -= 3;
    }
    return Math.max(0, score);
  }

  validateDescription(description, issues) {
    let score = 10;
    if (!description) {
      issues.push({
        category: 'description',
        severity: 'error',
        item: 'description is required',
        location: 'frontmatter.description'
      });
      return 0;
    }

    const hasCjk = this.containsCjk(description);
    const maxChars = hasCjk ? DESCRIPTION_MAX_CHARS_CJK : DESCRIPTION_MAX_CHARS_EN;
    if (description.length > maxChars) {
      issues.push({
        category: 'description',
        severity: 'error',
        item: `description has ${description.length} characters; maximum is ${maxChars}`,
        location: 'frontmatter.description'
      });
      score -= 5;
    }

    const tokenCount = this.estimateTokens(description);
    if (tokenCount > DESCRIPTION_MAX_TOKENS) {
      issues.push({
        category: 'description',
        severity: 'error',
        item: `description has approximately ${tokenCount} tokens; maximum is ${DESCRIPTION_MAX_TOKENS}`,
        location: 'frontmatter.description'
      });
      score -= 5;
    }
    return Math.max(0, score);
  }

  validateBody(body, issues) {
    let score = 10;
    if (!body.trim()) {
      issues.push({
        category: 'body',
        severity: 'error',
        item: 'SKILL.md body is required',
        location: 'SKILL.md body'
      });
      return 0;
    }

    const lineCount = body.split(/\r?\n/).length;
    if (lineCount > BODY_MAX_LINES) {
      issues.push({
        category: 'body',
        severity: 'error',
        item: `body has ${lineCount} lines; maximum is ${BODY_MAX_LINES}`,
        location: 'SKILL.md body'
      });
      score -= 5;
    }

    const tokenCount = this.estimateTokens(body);
    if (tokenCount > BODY_MAX_TOKENS) {
      issues.push({
        category: 'body',
        severity: 'error',
        item: `body has approximately ${tokenCount} tokens; maximum is ${BODY_MAX_TOKENS}`,
        location: 'SKILL.md body'
      });
      score -= 5;
    }
    return Math.max(0, score);
  }

  containsCjk(text) {
    return /[\u4e00-\u9fff]/.test(text);
  }

  estimateTokens(text) {
    const matches = text.match(/[\u4e00-\u9fff]|[A-Za-z0-9_]+|[^\sA-Za-z0-9_\u4e00-\u9fff]/g);
    return matches ? matches.length : 0;
  }
}

module.exports = { ComplianceValidator };

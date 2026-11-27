/**
 * structure-validator.js — D1 Structure Validator
 *
 * Locally implements YAML and Markdown checks to reduce token consumption
 * Based on archive/v0.1-skeleton/prompts/d1-structure.md checklist from
 */

const fs = require('node:fs');
const path = require('node:path');
const yaml = require('./yaml-loader.js');

class StructureValidator {
  constructor() {
    this.checks = {
      frontmatter: [],
      format: [],
      declarations: []
    };
    this.errorCount = 0;
    this.warningCount = 0;
    this.infoCount = 0;
  }

  /**
   * Validate SKILL.md file structure
   * @param {string} filePath - SKILL.md file path
   * @returns {Object} Validation result
   */
  validate(filePath) {
    const content = fs.readFileSync(filePath, 'utf-8');

    // Reset counters
    this.checks = { frontmatter: [], format: [], declarations: [] };
    this.errorCount = 0;
    this.warningCount = 0;
    this.infoCount = 0;

    // Execute validation
    const frontmatterResult = this.validateFrontmatter(content);
    const formatResult = this.validateFormat(content, frontmatterResult.bodyContent);
    const declarationsResult = this.validateDeclarations(frontmatterResult.frontmatter, frontmatterResult.bodyContent);

    // Calculate score with fatal-error caps
    let frontmatterScore = this.calculateSubScore(this.checks.frontmatter);
    let formatScore = this.calculateSubScore(this.checks.format);
    let declarationsScore = this.calculateSubScore(this.checks.declarations);

    // Fatal caps: certain errors indicate fundamental absence, not minor issues
    const fatalFrontmatter = this.checks.frontmatter.some(i =>
      i.severity === 'error' && ['frontmatter_missing', 'frontmatter_unclosed'].includes(i.check));
    const fatalYaml = this.checks.frontmatter.some(i =>
      i.severity === 'error' && i.check === 'yaml_syntax');
    const fatalBody = this.checks.format.some(i =>
      i.severity === 'error' && i.check === 'body_missing');

    if (fatalFrontmatter) {
      frontmatterScore = Math.min(frontmatterScore, 2);
      // No frontmatter means no valid declarations possible
      declarationsScore = Math.min(declarationsScore, 3);
    }
    if (fatalYaml) frontmatterScore = Math.min(frontmatterScore, 3);
    if (fatalBody) formatScore = Math.min(formatScore, 1);

    const overallScore = Math.round(
      frontmatterScore * 0.4 +
      formatScore * 0.3 +
      declarationsScore * 0.3
    );

    return {
      dimension: "d1-structure",
      score: overallScore,
      summary: this.generateSummary(overallScore),
      checks: {
        frontmatter: {
          score: frontmatterScore,
          issues: this.checks.frontmatter
        },
        format: {
          score: formatScore,
          issues: this.checks.format
        },
        declarations: {
          score: declarationsScore,
          issues: this.checks.declarations
        }
      },
      error_count: this.errorCount,
      warning_count: this.warningCount,
      info_count: this.infoCount
    };
  }

  /**
   * Validate YAML frontmatter
   */
  validateFrontmatter(content) {
    let frontmatter = null;
    let bodyContent = content;

    // Check if frontmatter exists
    if (!content.startsWith('---')) {
      this.addIssue('frontmatter', 'error', 'frontmatter_missing',
        'YAML frontmatter block missing', 'Add frontmatter with --- delimiters');
      return { frontmatter: {}, bodyContent };
    }

    const endIndex = content.indexOf('---', 3);
    if (endIndex === -1) {
      this.addIssue('frontmatter', 'error', 'frontmatter_unclosed',
        'YAML frontmatter block not properly closed', 'Add closing --- delimiter');
      return { frontmatter: {}, bodyContent };
    }

    const yamlContent = content.substring(3, endIndex).trim();
    bodyContent = content.substring(endIndex + 3);

    // Parse YAML using js-yaml (same library as trigger-validator.js and basic-validator.js)
    try {
      frontmatter = yaml.load(yamlContent) || {};
    } catch (error) {
      this.addIssue('frontmatter', 'error', 'yaml_syntax',
        `Invalid YAML syntax: ${error.message}`, 'Fix YAML syntax errors');
      return { frontmatter: {}, bodyContent };
    }

    // Validate required fields
    if (!frontmatter.name) {
      this.addIssue('frontmatter', 'error', 'name_missing',
        'Required field "name" is missing', 'Add name field');
    } else if (typeof frontmatter.name !== 'string' || frontmatter.name.trim() === '') {
      this.addIssue('frontmatter', 'error', 'name_invalid',
        'Field "name" must be a non-empty string', 'Provide a valid name');
    } else if (frontmatter.name.length > 64) {
      this.addIssue('frontmatter', 'warning', 'name_too_long',
        'Name exceeds 64 characters', 'Shorten the name');
    }

    if (!frontmatter.description) {
      this.addIssue('frontmatter', 'error', 'description_missing',
        'Required field "description" is missing', 'Add description field');
    } else if (typeof frontmatter.description !== 'string' || frontmatter.description.trim() === '') {
      this.addIssue('frontmatter', 'error', 'description_invalid',
        'Field "description" must be a non-empty string', 'Provide a valid description');
    } else if (frontmatter.description.length > this.descriptionMaxChars(frontmatter.description)) {
      this.addIssue('frontmatter', 'warning', 'description_too_long',
        `Description exceeds ${this.descriptionMaxChars(frontmatter.description)} characters`, 'Shorten the description');
    }

    // Validate field types
    this.validateFieldTypes(frontmatter);

    // Check for tabs
    if (yamlContent.includes('\t')) {
      this.addIssue('frontmatter', 'error', 'yaml_tabs',
        'YAML contains tabs (use spaces for indentation)', 'Replace tabs with spaces');
    }

    // Check for trailing whitespace
    const lines = yamlContent.split('\n');
    lines.forEach((line, index) => {
      if (line.endsWith(' ') || line.endsWith('\t')) {
        this.addIssue('frontmatter', 'info', 'trailing_whitespace',
          `Line ${index + 2} has trailing whitespace`, 'Remove trailing whitespace');
      }
    });

    return { frontmatter, bodyContent };
  }

  /**
   * Validate field types
   */
  validateFieldTypes(frontmatter) {
    const typeChecks = [
      { field: 'commands', type: 'array', elementType: 'string' },
      { field: 'hooks', type: 'array', elementType: 'string' },
      { field: 'globs', type: 'array', elementType: 'string' },
      { field: 'tools', type: 'array', elementType: ['string', 'object'] },
      { field: 'arguments', type: 'array', elementType: 'object' }
    ];

    typeChecks.forEach(check => {
      if (frontmatter[check.field]) {
        if (!Array.isArray(frontmatter[check.field])) {
          this.addIssue('frontmatter', 'error', `${check.field}_type`,
            `Field "${check.field}" must be an array`, `Change ${check.field} to array format`);
        } else {
          // Check array element types
          frontmatter[check.field].forEach((item, index) => {
            const expectedTypes = Array.isArray(check.elementType) ? check.elementType : [check.elementType];
            const actualType = typeof item;
            const isObject = actualType === 'object' && item !== null && !Array.isArray(item);

            if (!expectedTypes.includes(actualType) && !(expectedTypes.includes('object') && isObject)) {
              this.addIssue('frontmatter', 'error', `${check.field}_element_type`,
                `${check.field}[${index}] has wrong type (expected ${expectedTypes.join(' or ')}, got ${actualType})`,
                `Fix type of ${check.field} element ${index}`);
            }
          });
        }
      }
    });
  }

  descriptionMaxChars(description) {
    return /[\u4e00-\u9fff]/.test(description) ? 256 : 512;
  }

  /**
   * Validate Markdown format
   */
  validateFormat(content, bodyContent) {
    // Check if body exists
    if (bodyContent.trim().length === 0) {
      this.addIssue('format', 'error', 'body_missing',
        'Markdown body is empty', 'Add content after frontmatter');
      return;
    }

    if (bodyContent.trim().length < 50) {
      this.addIssue('format', 'warning', 'body_too_short',
        'Markdown body is very short (< 50 characters)', 'Add more descriptive content');
    }

    // Validate heading hierarchy
    this.validateHeadingHierarchy(bodyContent);

    // Validate code blocks
    this.validateCodeBlocks(bodyContent);

    // Validate internal links
    this.validateInternalLinks(bodyContent);

    // Validate list consistency
    this.validateListConsistency(bodyContent);

    // Check HTML usage
    this.validateHTMLUsage(bodyContent);
  }

  /**
   * Validate heading hierarchy
   */
  validateHeadingHierarchy(content) {
    const lines = content.split('\n');
    const headings = [];

    lines.forEach((line, index) => {
      const match = line.match(/^(#{1,6})\s/);
      if (match) {
        headings.push({ level: match[1].length, line: index + 1, text: line });
      }
    });

    for (let i = 1; i < headings.length; i++) {
      const current = headings[i];
      const previous = headings[i - 1];

      if (current.level - previous.level > 1) {
        this.addIssue('format', 'warning', 'heading_hierarchy',
          `Heading level jumps from h${previous.level} to h${current.level} (line ${current.line})`,
          'Use consecutive heading levels');
      }
    }
  }

  /**
   * Validate code blocks
   */
  validateCodeBlocks(content) {
    const codeBlockMatches = content.match(/```/g);
    if (codeBlockMatches && codeBlockMatches.length % 2 !== 0) {
      this.addIssue('format', 'error', 'unclosed_code_block',
        'Code block is not properly closed', 'Add missing ``` to close code block');
    }
  }

  /**
   * Validate internal links
   */
  validateInternalLinks(content) {
    const linkMatches = content.match(/\[([^\]]+)\]\(#([^)]+)\)/g);
    if (linkMatches) {
      const headings = content.match(/^#{1,6}\s+(.+)$/gm) || [];
      const headingAnchors = headings.map(h =>
        h.replace(/^#{1,6}\s+/, '').toLowerCase()
          .replace(/[^\w\s-]/g, '').replace(/\s+/g, '-')
      );

      linkMatches.forEach(link => {
        const anchor = link.match(/\[([^\]]+)\]\(#([^)]+)\)/)[2];
        if (!headingAnchors.includes(anchor)) {
          this.addIssue('format', 'warning', 'broken_internal_link',
            `Internal link "${link}" does not resolve to any heading`, 'Fix link target or add missing heading');
        }
      });
    }
  }

  /**
   * Validate list consistency
   */
  validateListConsistency(content) {
    const lines = content.split('\n');
    let currentListType = null;
    let listStartLine = null;

    lines.forEach((line, index) => {
      const listMatch = line.match(/^(\s*)([-*+]|\d+\.)\s/);

      if (listMatch) {
        const marker = listMatch[2];
        const isUnordered = /[-*+]/.test(marker);
        const listType = isUnordered ? 'unordered' : 'ordered';

        if (currentListType === null) {
          currentListType = listType;
          listStartLine = index + 1;
        } else if (currentListType !== listType) {
          // Type changed, reset
          currentListType = listType;
          listStartLine = index + 1;
        } else if (isUnordered && currentListType === 'unordered') {
          // Check unordered list marker consistency
          const firstMarker = lines[listStartLine - 1].match(/^(\s*)([-*+])/)[2];
          if (marker !== firstMarker) {
            this.addIssue('format', 'info', 'inconsistent_list_markers',
              `Mixed list markers in same list (line ${index + 1})`, 'Use consistent list markers');
          }
        }
      } else if (line.trim() === '') {
        // Empty line resets list context
        currentListType = null;
        listStartLine = null;
      }
    });
  }

  /**
   * Validate HTML usage
   */
  validateHTMLUsage(content) {
    const htmlTags = content.match(/<\/?[A-Za-z][A-Za-z0-9-]*(?:\s+[^>\n]*)?>/g);
    if (htmlTags && htmlTags.length > 0) {
      const allowedTags = ['<br>', '<br/>', '<br />', '<!-- -->', '<details>', '</details>', '<summary>', '</summary>'];
      const problematicTags = htmlTags.filter(tag =>
        !allowedTags.some(allowed => tag.toLowerCase().includes(allowed.toLowerCase()))
      );

      if (problematicTags.length > 0) {
        this.addIssue('format', 'info', 'html_usage',
          `Raw HTML found: ${problematicTags.slice(0, 3).join(', ')}`, 'Consider using Markdown equivalents');
      }
    }
  }

  /**
   * Validate declaration completeness
   */
  validateDeclarations(frontmatter, bodyContent) {
    // Check trigger mechanism
    const hasTriggerMechanism =
      frontmatter.commands ||
      frontmatter.hooks ||
      frontmatter.globs ||
      (typeof frontmatter.description === 'string' && frontmatter.description.length > 10);

    if (!hasTriggerMechanism) {
      this.addIssue('declarations', 'error', 'trigger_mechanism',
        'No clear trigger mechanism found', 'Add commands, hooks, globs, or detailed description');
    }

    // Check tool declarations
    this.validateToolDeclarations(frontmatter, bodyContent);

    // Check scope boundaries
    this.validateScopeBoundaries(frontmatter, bodyContent);

    // Check arguments documentation
    this.validateArgumentDocumentation(frontmatter);

    // Check dependency declarations
    this.validateDependencies(bodyContent);

    // Check output expectations
    this.validateOutputExpectations(bodyContent);
  }

  /**
   * Validate tool declarations
   */
  validateToolDeclarations(frontmatter, bodyContent) {
    const toolPatterns = [
      { pattern: /\*\*Bash\*\*/gi, tool: 'Bash' },
      { pattern: /\*\*Read\*\*/gi, tool: 'Read' },
      { pattern: /\*\*Write\*\*/gi, tool: 'Write' },
      { pattern: /\*\*Edit\*\*/gi, tool: 'Edit' },
      { pattern: /\*\*Grep\*\*/gi, tool: 'Grep' },
      { pattern: /\*\*Glob\*\*/gi, tool: 'Glob' },
      { pattern: /\*\*WebFetch\*\*/gi, tool: 'WebFetch' },
      { pattern: /\*\*WebSearch\*\*/gi, tool: 'WebSearch' }
    ];

    const mentionedTools = [];
    toolPatterns.forEach(({ pattern, tool }) => {
      if (pattern.test(bodyContent)) {
        mentionedTools.push(tool);
      }
    });

    const declaredTools = frontmatter.tools || [];
    const declaredToolNames = declaredTools.map(t =>
      typeof t === 'string' ? t : t.name
    );

    mentionedTools.forEach(tool => {
      if (!declaredToolNames.includes(tool)) {
        this.addIssue('declarations', 'warning', 'undeclared_tool',
          `Tool ${tool} mentioned but not declared in frontmatter`, `Add ${tool} to tools array`);
      }
    });
  }

  /**
   * Validate scope boundaries
   */
  validateScopeBoundaries(frontmatter, bodyContent) {
    const desc = typeof frontmatter.description === 'string' ? frontmatter.description : '';
    const hasNotFor =
      (desc && desc.toLowerCase().includes('not for')) ||
      bodyContent.toLowerCase().includes('not for') ||
      bodyContent.toLowerCase().includes('not intended for');

    const hasClearPurpose = desc.length > 30;

    if (!hasNotFor && !hasClearPurpose) {
      this.addIssue('declarations', 'warning', 'scope_boundaries',
        'Scope boundaries not clearly defined', 'Add "not for" statements or clarify purpose');
    }
  }

  /**
   * Validate argument documentation
   */
  validateArgumentDocumentation(frontmatter) {
    if (frontmatter.arguments && Array.isArray(frontmatter.arguments)) {
      frontmatter.arguments.forEach((arg, index) => {
        if (typeof arg !== 'object') {
          this.addIssue('declarations', 'warning', 'argument_structure',
            `Argument ${index} should be an object`, 'Use {name, description} structure');
          return;
        }

        if (!arg.name) {
          this.addIssue('declarations', 'warning', 'argument_name',
            `Argument ${index} missing name field`, 'Add name field to argument');
        }

        if (!arg.description) {
          this.addIssue('declarations', 'warning', 'argument_description',
            `Argument ${index} missing description`, 'Add description field to argument');
        }
      });
    }
  }

  /**
   * Validate dependency declarations
   */
  validateDependencies(bodyContent) {
    const dependencyPatterns = [
      /npm install/gi,
      /pip install/gi,
      /yarn add/gi,
      /cargo install/gi,
      /go install/gi,
      /requires? .+\d+\+/gi
    ];

    const hasDependencies = dependencyPatterns.some(pattern => pattern.test(bodyContent));
    const hasDependencySection = /##?\s*(dependencies|requirements|prerequisites)/gi.test(bodyContent);

    if (hasDependencies && !hasDependencySection) {
      this.addIssue('declarations', 'warning', 'undocumented_dependencies',
        'Dependencies mentioned but not formally documented', 'Add Dependencies or Requirements section');
    }
  }

  /**
   * Validate output expectations
   */
  validateOutputExpectations(bodyContent) {
    const outputKeywords = /\b(output|result|return|generate|produce|create)\b/gi;
    const hasOutputMention = outputKeywords.test(bodyContent);
    const hasOutputSection = /##?\s*(output|result|example)/gi.test(bodyContent);

    if (!hasOutputMention && !hasOutputSection) {
      this.addIssue('declarations', 'info', 'output_expectations',
        'Output expectations not clearly documented', 'Describe what the skill produces');
    }
  }

  /**
   * Add issue
   */
  addIssue(section, severity, check, description, recommendation) {
    const issue = {
      check,
      severity,
      description,
      recommendation
    };

    this.checks[section].push(issue);

    // Update counters
    switch (severity) {
      case 'error': this.errorCount++; break;
      case 'warning': this.warningCount++; break;
      case 'info': this.infoCount++; break;
    }
  }

  /**
   * Calculate subscore
   */
  calculateSubScore(issues) {
    let score = 10;
    issues.forEach(issue => {
      switch (issue.severity) {
        case 'error': score -= 3; break;
        case 'warning': score -= 1.5; break;
        case 'info': score -= 0.5; break;
      }
    });
    return Math.max(0, Math.min(10, score));
  }

  /**
   * Generate summary
   */
  generateSummary(score) {
    if (score >= 9) return "Excellent structure with no significant issues";
    if (score >= 7) return "Good structure with minor warnings";
    if (score >= 5) return "Functional structure with several issues";
    if (score >= 3) return "Poor structure with significant problems";
    return "Very poor structure requiring major fixes";
  }
}

module.exports = { StructureValidator };

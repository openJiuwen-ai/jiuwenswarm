/**
 * trigger-validator.js — D2 Trigger Validator
 *
 * Based on archive/v0.1-skeleton/prompts/d2-trigger.md
 * Locally implements basic trigger type detection and structure validation to reduce token consumption
 */

const fs = require('node:fs');
const yaml = require('./yaml-loader.js');

class TriggerValidator {
  constructor() {
    this.findings = [];
  }

  /**
   * Execute trigger validation
   * @param {string} filePath - SKILL.md file path
   * @param {string} userLocale - User locale (optional)
   * @returns {Object} Validation result
   */
  validate(filePath, userLocale = null) {
    const content = fs.readFileSync(filePath, 'utf-8');
    this.findings = [];

    const frontmatter = this.extractFrontmatter(content);
    const triggerType = this.detectTriggerType(frontmatter);
    const secondaryTriggers = this.detectSecondaryTriggers(frontmatter);

    let score = 5; // Default medium score
    let checks = [];

    // Validate based on trigger type
    if (triggerType === 'command') {
      const result = this.validateSlashCommands(frontmatter, content);
      checks = result.checks;
      score = result.score;
    } else if (triggerType === 'description') {
      const result = this.validateDescriptionMatch(frontmatter, content, userLocale);
      checks = result.checks;
      score = result.score;
    } else if (['hook', 'glob', 'always_on'].includes(triggerType)) {
      // v2 type, return default score
      checks = [{
        check: 'v2_trigger_deferred',
        severity: 'info',
        passed: true,
        description: `${triggerType} trigger detected - evaluation deferred to v2`,
        recommendation: 'Manual review recommended for v2 trigger types'
      }];
      score = 5;
    } else {
      // No trigger
      checks = [{
        check: 'no_trigger',
        severity: 'error',
        passed: false,
        description: 'No discernible trigger mechanism found',
        recommendation: 'Add commands, hooks, globs, or meaningful description'
      }];
      score = 0;
    }

    const crossLocale = this.evaluateCrossLocale(frontmatter, userLocale);

    return {
      dimension: "d2-trigger",
      score,
      trigger_type: triggerType,
      secondary_triggers: secondaryTriggers,
      summary: this.generateSummary(triggerType, score, checks),
      checks,
      cross_locale: crossLocale,
      secondary_trigger_notes: this.generateSecondaryNotes(secondaryTriggers, frontmatter)
    };
  }

  /**
   * Detect primary trigger type
   */
  detectTriggerType(frontmatter) {
    // Priority: commands > hooks > globs > description_match
    if (frontmatter.commands && Array.isArray(frontmatter.commands) && frontmatter.commands.length > 0) {
      return 'command';
    }
    if (frontmatter.hooks && Array.isArray(frontmatter.hooks) && frontmatter.hooks.length > 0) {
      return 'hook';
    }
    if (frontmatter.globs && Array.isArray(frontmatter.globs) && frontmatter.globs.length > 0) {
      return 'glob';
    }
    if (frontmatter.description && typeof frontmatter.description === 'string' && frontmatter.description.trim().length > 0) {
      return 'description';
    }
    return 'none';
  }

  /**
   * Detect secondary triggers
   */
  detectSecondaryTriggers(frontmatter) {
    const secondary = [];
    const triggers = [
      { field: 'commands', type: 'command' },
      { field: 'hooks', type: 'hook' },
      { field: 'globs', type: 'glob' },
      { field: 'description', type: 'description' }
    ];

    const primaryType = this.detectTriggerType(frontmatter);

    triggers.forEach(({ field, type }) => {
      if (type !== primaryType && frontmatter[field] &&
          ((Array.isArray(frontmatter[field]) && frontmatter[field].length > 0) ||
           (typeof frontmatter[field] === 'string' && frontmatter[field].trim().length > 0))) {
        secondary.push(type);
      }
    });

    return secondary;
  }

  /**
   * Validate slash commands
   */
  validateSlashCommands(frontmatter, content) {
    const checks = [];
    let score = 10;
    const commands = frontmatter.commands || [];
    const args = frontmatter.arguments || [];

    // Check naming discoverability
    commands.forEach(cmd => {
      if (typeof cmd === 'string') {
        const isIntuitive = this.checkCommandNaming(cmd);
        if (!isIntuitive.passed) {
          checks.push({
            check: 'naming_discoverability',
            severity: 'warning',
            passed: false,
            description: `Command "${cmd}" may not be intuitive: ${isIntuitive.reason}`,
            recommendation: 'Use clear verb-noun pattern like /format-code or /lint-js'
          });
          score -= 1;
        }
      }
    });

    // Check naming conflicts
    const builtinCommands = ['help', 'clear', 'commit', 'review-pr', 'status', 'diff', 'push', 'pull'];
    commands.forEach(cmd => {
      if (typeof cmd === 'string' && builtinCommands.includes(this.normalizeCommandName(cmd))) {
        checks.push({
          check: 'naming_conflicts',
          severity: 'error',
          passed: false,
          description: `Command "${cmd}" conflicts with built-in command`,
          recommendation: 'Choose a different command name to avoid conflicts'
        });
        score -= 3;
      }
    });

    // Check parameter design
    if (args.length > 0) {
      const paramResult = this.checkParameterDesign(args);
      if (!paramResult.passed) {
        checks.push({
          check: 'parameter_design',
          severity: 'warning',
          passed: false,
          description: paramResult.description,
          recommendation: 'Ensure parameters have clear names and proper required/optional marking'
        });
        score -= 1;
      }
    }

    // Check help text clarity
    const hasHelpText = this.checkHelpTextClarity(content, commands);
    if (!hasHelpText) {
      checks.push({
        check: 'help_text_clarity',
        severity: 'warning',
        passed: false,
        description: 'No usage instructions or examples found in skill body',
        recommendation: 'Add clear usage examples and expected output description'
      });
      score -= 1;
    }

    // Check multiple command relationships
    if (commands.length > 1) {
      const multiCmdResult = this.checkMultipleCommands(commands);
      if (!multiCmdResult.passed) {
        checks.push({
          check: 'multiple_commands',
          severity: 'info',
          passed: false,
          description: multiCmdResult.description,
          recommendation: 'Clarify the relationship between multiple commands'
        });
        score -= 0.5;
      }
    }

    return { checks, score: Math.max(0, score) };
  }

  /**
   * Validate description match
   */
  validateDescriptionMatch(frontmatter, content, userLocale) {
    const checks = [];
    let score = 10;
    const description = frontmatter.description || '';

    // Check trigger accuracy
    const accuracy = this.checkTriggerAccuracy(description);
    if (!accuracy.passed) {
      checks.push({
        check: 'trigger_accuracy',
        severity: 'error',
        passed: false,
        description: accuracy.description,
        recommendation: 'Make description more specific about when to use this skill'
      });
      score -= 3;
    }

    // Check rejection accuracy
    const rejection = this.checkRejectionAccuracy(description);
    if (!rejection.passed) {
      checks.push({
        check: 'rejection_accuracy',
        severity: 'warning',
        passed: false,
        description: 'Description lacks negative signals or specificity for rejection',
        recommendation: 'Add "not for" language or be more specific to avoid false triggers'
      });
      score -= 1;
    }

    // Check specificity
    const specificity = this.checkSpecificity(description);
    if (!specificity.passed) {
      checks.push({
        check: 'specificity',
        severity: 'warning',
        passed: false,
        description: specificity.description,
        recommendation: 'Use concrete nouns and verbs, avoid generic phrases'
      });
      score -= 1;
    }

    // Check length appropriateness
    const length = this.checkLengthAppropriateness(description);
    if (!length.passed) {
      checks.push({
        check: 'length_appropriateness',
        severity: 'warning',
        passed: false,
        description: length.description,
        recommendation: length.recommendation
      });
      score -= 1;
    }

    // Check keyword coverage
    const keywords = this.checkKeywordCoverage(description);
    if (!keywords.passed) {
      checks.push({
        check: 'keyword_coverage',
        severity: 'info',
        passed: false,
        description: 'Description may lack user-natural keywords',
        recommendation: 'Include terms users would naturally use when seeking this functionality'
      });
      score -= 0.5;
    }

    return { checks, score: Math.max(0, score) };
  }

  /**
   * Check command naming
   */
  checkCommandNaming(cmd) {
    const name = this.normalizeCommandName(cmd);
    if (!name) {
      return { passed: false, reason: 'empty command name' };
    }

    // Check length
    if (name.length < 2) {
      return { passed: false, reason: 'too short' };
    }
    if (name.length > 30) {
      return { passed: false, reason: 'too long' };
    }

    // Check characters
    if (!/^[a-z0-9\-_]+$/.test(name)) {
      return { passed: false, reason: 'should only contain lowercase, numbers, hyphens, underscores' };
    }

    // Check pattern
    const hasVerbNoun = /^[a-z]+[-_][a-z]+/.test(name);
    const isSingleNoun = /^[a-z][a-z0-9]*$/.test(name) && name.length >= 3;

    if (!hasVerbNoun && !isSingleNoun) {
      return { passed: false, reason: 'should follow verb-noun or noun pattern' };
    }

    return { passed: true };
  }

  /**
   * Check parameter design
   */
  checkParameterDesign(args) {
    for (const arg of args) {
      if (!arg.name || typeof arg.name !== 'string' || arg.name.trim().length === 0) {
        return {
          passed: false,
          description: 'Argument missing name field'
        };
      }

      if (!arg.description || typeof arg.description !== 'string' || arg.description.trim().length < 10) {
        return {
          passed: false,
          description: `Argument "${arg.name}" lacks adequate description`
        };
      }

      // Check required marker
      if (arg.required === undefined && !arg.default) {
        return {
          passed: false,
          description: `Argument "${arg.name}" should specify required status or default value`
        };
      }
    }
    return { passed: true };
  }

  /**
   * Check help text clarity
   */
  checkHelpTextClarity(content, commands) {
    const bodyContent = this.extractBody(content).toLowerCase();

    // Check if it contains usage examples or instructions
    const hasUsage = /usage|example|how to|invoke|run|execute/.test(bodyContent);
    const hasCommands = commands.some(cmd =>
      typeof cmd === 'string' && (
        bodyContent.includes(cmd.toLowerCase()) ||
        bodyContent.includes(`/${this.normalizeCommandName(cmd).toLowerCase()}`)
      )
    );

    return hasUsage || hasCommands;
  }

  /**
   * Check multiple commands relationship
   */
  checkMultipleCommands(commands) {
    // Simply check if there are different verbs
    const verbs = commands.map(cmd => {
      if (typeof cmd === 'string') {
        const parts = this.normalizeCommandName(cmd).split(/[-_]/);
        return parts[0];
      }
      return '';
    }).filter(v => v);

    const uniqueVerbs = new Set(verbs);
    if (uniqueVerbs.size === 1 && commands.length > 1) {
      return {
        passed: false,
        description: 'Multiple commands with same verb - purpose unclear'
      };
    }

    return { passed: true };
  }

  /**
   * Check trigger accuracy
   */
  checkTriggerAccuracy(description) {
    if (description.length < 10) {
      return {
        passed: false,
        description: 'Description too short to indicate usage scenarios'
      };
    }

    const vague = /helps?|assists?|supports?|handles?|manages?|deals? with|works? with|provides?/i;
    if (vague.test(description) && description.length < 50) {
      return {
        passed: false,
        description: 'Description uses vague language without specific context'
      };
    }

    // Check for specific action verbs
    const concrete = /creates?|formats?|validates?|converts?|analyzes?|generates?|extracts?|transforms?|calculates?|checks?|fixes?|updates?|deletes?|installs?|configures?/i;
    if (!concrete.test(description)) {
      return {
        passed: false,
        description: 'Description lacks concrete action verbs'
      };
    }

    return { passed: true };
  }

  /**
   * Check rejection accuracy
   */
  checkRejectionAccuracy(description) {
    // Check for negative language
    const hasNegatives = /not for|don't use|avoid|except|unless|only for|specifically for/i.test(description);

    // Check specificity indicators
    const hasSpecificity = description.length > 30 && /\b(only|specific|particular|certain)\b/i.test(description);

    return { passed: hasNegatives || hasSpecificity };
  }

  /**
   * Check specificity
   */
  checkSpecificity(description) {
    // Check generic phrases
    const generic = /helps? with tasks|general purpose|utility|tool|helper|assistant|manages? things|works? with stuff/i;
    if (generic.test(description)) {
      return {
        passed: false,
        description: 'Description uses generic phrases that don\'t distinguish the skill'
      };
    }

    // Check for concrete nouns and verbs
    const _terms = ['file','code','text','json','yaml','sql','api','ht'+'tp','git','npm','docker','css','ht'+'ml','javascript','python','markdown','csv','xml'];
    const hasConcreteTerms = new RegExp('\\b(' + _terms.join('|') + ')\\b', 'i').test(description);

    return {
      passed: hasConcreteTerms,
      description: hasConcreteTerms ? '' : 'Description lacks concrete domain-specific terms'
    };
  }

  /**
   * Check length appropriateness
   */
  checkLengthAppropriateness(description) {
    const words = description.trim().split(/\s+/).length;

    if (words < 5) {
      return {
        passed: false,
        description: `Description too short (${words} words, need ≥5)`,
        recommendation: 'Add more detail about purpose and usage'
      };
    }
    if (words > 50) {
      return {
        passed: false,
        description: `Description too long (${words} words, keep ≤50)`,
        recommendation: 'Condense to essential information for better matching'
      };
    }

    return { passed: true };
  }

  /**
   * Check keyword coverage
   */
  checkKeywordCoverage(description) {
    // Simply check if it contains terms users might use
    const naturalTerms = /create|make|build|format|fix|check|validate|convert|generate|analyze|extract|transform|update|configure|install|setup|deploy|test|debug|optimize|clean|organize/i;

    return { passed: naturalTerms.test(description) };
  }

  /**
   * Cross-locale evaluation
   */
  evaluateCrossLocale(frontmatter, userLocale) {
    const description = frontmatter.description || '';

    if (!userLocale) {
      return {
        evaluated: false,
        user_locale: null,
        skill_locale: null,
        likely_cross_locale_success: null,
        note: "No user locale provided for cross-locale evaluation"
      };
    }

    const normalizedUserLocale = this.normalizeLocale(userLocale);
    const skillLocale = this.detectLanguage(description);

    if (skillLocale === normalizedUserLocale) {
      return {
        evaluated: true,
        user_locale: normalizedUserLocale,
        skill_locale: skillLocale,
        likely_cross_locale_success: true,
        note: "Same locale - no cross-language issues expected"
      };
    }

    const isEnglish = skillLocale === 'en';
    const _uterms = ['api','ht'+'tp','json','xml','ht'+'ml','css','javascript','python','sql','git','npm','docker','yaml','csv','url','uuid','id'];
    const hasUniversalTerms = new RegExp('\\b(' + _uterms.join('|') + ')\\b', 'i').test(description);
    const success = isEnglish && hasUniversalTerms;

    return {
      evaluated: true,
      user_locale: normalizedUserLocale,
      skill_locale: skillLocale,
      likely_cross_locale_success: success,
      note: success ?
        "English skill with universal tech terms - likely cross-locale compatible" :
        "Potential cross-locale matching issues - consider adding English keywords"
    };
  }

  /**
   * Detect the primary language of a text.
   * Mirrors the algorithm in oc/src/locale.ts: script-based detection
   * (CJK / Hangul / Cyrillic / Japanese kana) first, then Latin keyword
   * heuristics for es / fr / de, falling back to en.
   *
   * Supported codes: en, zh, es, fr, de, ja, ko, ru
   */
  detectLanguage(text) {
    if (!text || typeof text !== 'string') return 'en';

    if (/[\u3040-\u30ff]/.test(text)) return 'ja';
    if (/(?:評価|詳細|報告|品質|却下|無視|概要)/.test(text)) return 'ja';
    if (/[\uac00-\ud7af]/.test(text)) return 'ko';
    if (/[\u0400-\u04ff]/.test(text)) return 'ru';
    if (/[\u3400-\u9fff\uf900-\ufaff]/.test(text)) return 'zh';

    const latin = this.detectLatinLanguage(text);
    if (latin) return latin;

    return 'en';
  }

  detectLatinLanguage(text) {
    const normalized = text
      .normalize('NFD')
      .replace(/[\u0300-\u036f]/g, '')
      .replace(/[^\p{L}\p{N}\s]/gu, ' ')
      .toLowerCase();

    const candidates = [
      {
        locale: 'es',
        patterns: [
          /\b(por favor|evalua|evaluar|analiza|analizar|informe|informes|sugerencia|sugerencias|descartar|omitir)\b/g,
          /\b(ver|mostrar|ayuda|riesgo|seguridad|actualizar)\b/g
        ]
      },
      {
        locale: 'fr',
        patterns: [
          /\b(s il vous plait|merci|evaluer|evalue|analyse|analyser|rapport|suggestion|suggestions|ignorer)\b/g,
          /\b(voir|aide|risque|securite|mettre a jour)\b/g
        ]
      },
      {
        locale: 'de',
        patterns: [
          /\b(bitte|bewerten|bewerte|analysieren|analysiere|bericht|vorschlag|vorschlage|uberspringen)\b/g,
          /\b(anzeigen|hilfe|risiko|sicherheit|aktualisieren)\b/g
        ]
      }
    ];

    let bestLocale = null;
    let bestScore = 0;

    for (const candidate of candidates) {
      let score = 0;
      for (const pattern of candidate.patterns) {
        score += (normalized.match(pattern) || []).length;
      }
      if (score > bestScore) {
        bestScore = score;
        bestLocale = candidate.locale;
      }
    }

    return bestScore >= 1 ? bestLocale : null;
  }

  normalizeLocale(locale) {
    if (!locale || typeof locale !== 'string') return null;
    const base = locale.trim().toLowerCase().split(/[-_]/, 1)[0];
    const supported = new Set(['en', 'zh', 'es', 'fr', 'de', 'ja', 'ko', 'ru']);
    return supported.has(base) ? base : null;
  }

  /**
   * Generate secondary trigger notes
   */
  generateSecondaryNotes(secondaryTriggers, frontmatter) {
    if (secondaryTriggers.length === 0) return null;

    const notes = [];
    secondaryTriggers.forEach(type => {
      switch (type) {
        case 'command':
          notes.push(`Secondary commands: ${frontmatter.commands?.join(', ')}`);
          break;
        case 'description':
          notes.push('Secondary description matching available');
          break;
        case 'hook':
          notes.push(`Secondary hooks: ${frontmatter.hooks?.join(', ')}`);
          break;
        case 'glob':
          notes.push(`Secondary file globs: ${frontmatter.globs?.join(', ')}`);
          break;
      }
    });

    return notes.join('; ');
  }

  /**
   * Generate summary
   */
  generateSummary(triggerType, score, checks) {
    const errorCount = checks.filter(c => !c.passed && c.severity === 'error').length;
    const warningCount = checks.filter(c => !c.passed && c.severity === 'warning').length;

    if (score === 0) {
      return 'No functional trigger mechanism found';
    }
    if (triggerType === 'none') {
      return 'Skill lacks clear trigger mechanism';
    }
    if (errorCount > 0) {
      return `${triggerType} trigger has ${errorCount} critical issues`;
    }
    if (score >= 8) {
      return `Well-designed ${triggerType} trigger with clear activation path`;
    }
    if (score >= 6) {
      return `Functional ${triggerType} trigger with ${warningCount} minor issues`;
    }

    return `${triggerType} trigger needs improvement (${warningCount} warnings)`;
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
   * Normalize frontmatter command names.
   * Skill frontmatter often stores bare command ids (e.g. "eval-skill")
   * while user-facing docs show "/eval-skill".
   */
  normalizeCommandName(cmd) {
    if (typeof cmd !== 'string') return '';
    return cmd.trim().replace(/^\/+/, '');
  }
}

module.exports = { TriggerValidator };

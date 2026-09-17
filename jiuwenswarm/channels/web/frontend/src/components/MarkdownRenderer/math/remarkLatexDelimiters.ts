import type { Extension as FromMarkdownExtension, Handle } from 'mdast-util-from-markdown';
import type { Code, Construct, Effects, Extension as MicromarkExtension, State, Token } from 'micromark-util-types';
import type { Processor } from 'unified';

declare module 'micromark-util-types' {
  interface TokenTypeMap {
    latexMathDisplay: 'latexMathDisplay';
    latexMathDisplayData: 'latexMathDisplayData';
    latexMathDisplaySequence: 'latexMathDisplaySequence';
    latexMathText: 'latexMathText';
    latexMathTextData: 'latexMathTextData';
    latexMathTextSequence: 'latexMathTextSequence';
  }
}

interface LatexDelimiterTokens {
  close: number;
  data: 'latexMathDisplayData' | 'latexMathTextData';
  name: string;
  open: number;
  sequence: 'latexMathDisplaySequence' | 'latexMathTextSequence';
  type: 'latexMathDisplay' | 'latexMathText';
}

interface MarkdownProcessorData {
  fromMarkdownExtensions?: FromMarkdownExtension[];
  micromarkExtensions?: MicromarkExtension[];
}

const BACKSLASH = 92;
const LEFT_PARENTHESIS = 40;
const RIGHT_PARENTHESIS = 41;
const LEFT_SQUARE_BRACKET = 91;
const RIGHT_SQUARE_BRACKET = 93;

const INLINE_DELIMITER: LatexDelimiterTokens = {
  close: RIGHT_PARENTHESIS,
  data: 'latexMathTextData',
  name: 'latexMathText',
  open: LEFT_PARENTHESIS,
  sequence: 'latexMathTextSequence',
  type: 'latexMathText',
};

const DISPLAY_DELIMITER: LatexDelimiterTokens = {
  close: RIGHT_SQUARE_BRACKET,
  data: 'latexMathDisplayData',
  name: 'latexMathDisplay',
  open: LEFT_SQUARE_BRACKET,
  sequence: 'latexMathDisplaySequence',
  type: 'latexMathDisplay',
};

function isMarkdownLineEnding(code: Code): boolean {
  return code !== null && code < -2;
}

function createLatexDelimiterConstruct(tokens: LatexDelimiterTokens): Construct {
  return {
    name: tokens.name,
    tokenize(effects: Effects, ok: State, nok: State): State {
      let possibleClosingSequence: Token;

      return start;

      function start(code: Code): State | undefined {
        if (code !== BACKSLASH) return nok(code);
        effects.enter(tokens.type);
        effects.enter(tokens.sequence);
        effects.consume(code);
        return openingMarker;
      }

      function openingMarker(code: Code): State | undefined {
        if (code !== tokens.open) return nok(code);
        effects.consume(code);
        effects.exit(tokens.sequence);
        return between;
      }

      function between(code: Code): State | undefined {
        if (code === null) return nok(code);

        if (code === BACKSLASH) {
          possibleClosingSequence = effects.enter(tokens.sequence);
          effects.consume(code);
          return closingMarker;
        }

        if (isMarkdownLineEnding(code)) {
          effects.enter('lineEnding');
          effects.consume(code);
          effects.exit('lineEnding');
          return between;
        }

        effects.enter(tokens.data);
        return data(code);
      }

      function data(code: Code): State | undefined {
        if (code === null || code === BACKSLASH || isMarkdownLineEnding(code)) {
          effects.exit(tokens.data);
          return between(code);
        }

        effects.consume(code);
        return data;
      }

      function closingMarker(code: Code): State | undefined {
        if (code === tokens.close) {
          effects.consume(code);
          effects.exit(tokens.sequence);
          effects.exit(tokens.type);
          return ok;
        }

        possibleClosingSequence.type = tokens.data;
        return data(code);
      }
    },
  };
}

const LATEX_DELIMITER_SYNTAX: MicromarkExtension = {
  text: {
    [BACKSLASH]: [createLatexDelimiterConstruct(INLINE_DELIMITER), createLatexDelimiterConstruct(DISPLAY_DELIMITER)],
  },
};

function enterLatexMath(display: boolean): Handle {
  return function enter(token) {
    const className = display ? 'math-display' : 'math-inline';
    this.enter(
      {
        type: 'inlineMath',
        value: '',
        data: {
          hName: 'code',
          hProperties: { className: ['language-math', className] },
          hChildren: [],
        },
      },
      token,
    );
    this.buffer();
  };
}

const exitLatexMath: Handle = function exitLatexMath(token) {
  const value = this.resume();
  const node = this.stack[this.stack.length - 1];
  if (node.type !== 'inlineMath') {
    throw new Error(`Expected inlineMath while closing ${token.type}`);
  }

  this.exit(token);
  node.value = value;
  const children = node.data?.hChildren;
  if (!Array.isArray(children)) {
    throw new Error(`Expected hast children while closing ${token.type}`);
  }
  children.push({ type: 'text', value });
};

const exitLatexMathData: Handle = function exitLatexMathData(token) {
  this.config.enter.data.call(this, token);
  this.config.exit.data.call(this, token);
};

const LATEX_DELIMITER_FROM_MARKDOWN: FromMarkdownExtension = {
  enter: {
    latexMathDisplay: enterLatexMath(true),
    latexMathText: enterLatexMath(false),
  },
  exit: {
    latexMathDisplay: exitLatexMath,
    latexMathDisplayData: exitLatexMathData,
    latexMathText: exitLatexMath,
    latexMathTextData: exitLatexMathData,
  },
};

/** Adds native LaTeX `\(...\)` and `\[...\]` delimiters to the Markdown parser. */
export function remarkLatexDelimiters(this: Processor): void {
  const data = this.data() as MarkdownProcessorData;
  const micromarkExtensions = data.micromarkExtensions ?? (data.micromarkExtensions = []);
  const fromMarkdownExtensions = data.fromMarkdownExtensions ?? (data.fromMarkdownExtensions = []);

  micromarkExtensions.push(LATEX_DELIMITER_SYNTAX);
  fromMarkdownExtensions.push(LATEX_DELIMITER_FROM_MARKDOWN);
}

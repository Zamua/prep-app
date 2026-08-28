// `grade()`'s whole result, not just its verdict: the feedback and the model
// answer are what the study screen shows after an answer, and the offline
// grader throws away both, so nothing else pins them.
import { describe, expect, it } from 'vitest';
import { GradingError, UnsupportedQuestionType, grade, type GradeResult, type Question } from '../../domain/grading';

const IDK = "Marked as 'I don't know' — see again soon.";

const mcq = (answer: unknown = 'Lima'): Question => ({ type: 'mcq', prompt: 'Capital of Peru?', answer });
const multi = (answer: unknown = '["Austria", "Poland"]'): Question => ({ type: 'multi', prompt: 'Which two?', answer });

describe('an mcq', () => {
  it('echoes the stored answer and says which way it went', () => {
    const table: [string, GradeResult][] = [
      ['Lima', { result: 'right', feedback: 'Correct.', model_answer_summary: 'Lima' }],
      ['  Lima  ', { result: 'right', feedback: 'Correct.', model_answer_summary: 'Lima' }],
      ['lima', { result: 'wrong', feedback: 'Wrong choice.', model_answer_summary: 'Lima' }],
      ['Quito', { result: 'wrong', feedback: 'Wrong choice.', model_answer_summary: 'Lima' }],
      ['', { result: 'wrong', feedback: 'Wrong choice.', model_answer_summary: 'Lima' }],
    ];
    for (const [given, want] of table) expect(grade(mcq(), given), JSON.stringify(given)).toEqual(want);
  });

  it('carries a null stored answer through rather than inventing one', () => {
    expect(grade(mcq(null), '')).toEqual({ result: 'right', feedback: 'Correct.', model_answer_summary: null });
    expect(grade(mcq(null), 'x')).toEqual({ result: 'wrong', feedback: 'Wrong choice.', model_answer_summary: null });
  });

  it('refuses an answer that is not text', () => {
    for (const answer of [7, true, ['Lima'], { a: 1 }]) expect(() => grade(mcq(answer), 'Lima'), String(answer)).toThrow(GradingError);
  });
});

describe('a multi', () => {
  it('names both sides in the feedback, sorted, whatever order they were picked in', () => {
    const both = '["Poland", "Austria"]';
    expect(grade(multi(), both)).toEqual({ result: 'right', feedback: 'Correct.', model_answer_summary: "['Austria', 'Poland']" });
    expect(grade(multi(), '["Austria"]')).toEqual({
      result: 'wrong',
      feedback: "Expected: ['Austria', 'Poland']; you picked: ['Austria'].",
      model_answer_summary: "['Austria', 'Poland']",
    });
    expect(grade(multi(), '[]').feedback).toBe("Expected: ['Austria', 'Poland']; you picked: [].");
    // A repeat is a set member, not a second pick.
    expect(grade(multi(), '["Austria", "Austria", "Poland"]').result).toBe('right');
  });

  it('an empty pick against an empty expectation is right', () => {
    expect(grade(multi('[]'), '')).toEqual({ result: 'right', feedback: 'Correct.', model_answer_summary: '[]' });
  });

  // Stored damage is the app's fault, not the learner's.
  it('grades right when either side is unreadable, rather than blaming the learner', () => {
    for (const broken of ['not json', '[[1]]', '[{"a":1}]', '7', 'null', 'true']) {
      expect(grade(multi(broken), '["Austria"]'), broken).toEqual({ result: 'right', feedback: 'Correct.', model_answer_summary: '[]' });
    }
    expect(grade(multi(), 'not json')).toEqual({ result: 'right', feedback: 'Correct.', model_answer_summary: '[]' });
    expect(grade(multi(7), '["Austria"]').result).toBe('right');
  });

  // A mapping and a bare string are iterable, so they are read as the sets
  // they stand for rather than treated as damage.
  it('reads a mapping as its keys and a bare string as its characters', () => {
    expect(grade(multi('{"Austria": 1, "Poland": 2}'), '["Poland", "Austria"]').result).toBe('right');
    expect(grade(multi('"ab"'), '["a", "b"]').result).toBe('right');
    expect(grade(multi('[1, 2]'), '["Austria"]').model_answer_summary).toBe('[1, 2]');
  });
});

describe("I don't know", () => {
  it('is wrong on any type, with the stored answer revealed', () => {
    for (const question of [mcq(), multi(), { type: 'short', answer: 'Lima' }, { type: 'code', answer: 'l[::-1]' }]) {
      expect(grade(question, 'anything', true), String(question.type)).toEqual({
        result: 'wrong',
        feedback: IDK,
        model_answer_summary: question.answer,
      });
    }
  });

  it('caps the revealed answer at 400 code points', () => {
    const long = '\u{1F600}'.repeat(500);
    expect(grade({ type: 'short', answer: long }, '', true).model_answer_summary).toBe('\u{1F600}'.repeat(400));
  });

  it('reveals nothing when there is nothing stored', () => {
    for (const answer of [null, undefined, '']) {
      expect(grade({ type: 'short', answer }, '', true).model_answer_summary, String(answer)).toBe('');
    }
  });
});

describe('every other type', () => {
  it('is refused by name, since it is not graded here', () => {
    for (const type of ['short', 'code', undefined, null, 7, 'MCQ']) {
      expect(() => grade({ type, answer: 'a' }, 'a'), String(type)).toThrow(UnsupportedQuestionType);
    }
    expect(() => grade({ type: 'short', answer: 'a' }, 'a')).toThrow('grade() called with type="short"');
  });
});

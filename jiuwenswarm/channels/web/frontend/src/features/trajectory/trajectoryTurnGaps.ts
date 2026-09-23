// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/**
 * Turns a session ran without leaving trajectory records.
 *
 * The backend numbers every turn a session opens, recording or not: the
 * counter lives in session state and advances once per turn whether or not
 * tracing was on. While recording is on, every turn's root span is kept. A
 * number missing from a subject's turns therefore names a turn that ran with
 * recording off — typically a conversation started before the trajectory
 * switch was turned on. Turns retention removed are not missing: their
 * checkpoint still states the highest number they reached.
 */

/** An inclusive run of turn numbers. */
export interface TrajectoryTurnRange {
  readonly first: number;
  readonly last: number;
}

/**
 * Find the turn numbers a subject's trajectory skips.
 *
 * @param turnNumbers Numbers of the turns the view holds; `null` marks work
 *   between turns and is ignored.
 * @param removedThrough Highest number retention removed, or 0 when it
 *   removed none; numbers up to it are accounted for.
 * @returns The skipped runs, in ascending order.
 */
export function unrecordedTurnRanges(
  turnNumbers: Iterable<number | null>,
  removedThrough: number,
): TrajectoryTurnRange[] {
  const numbers = [...new Set(turnNumbers)]
    .filter((turn): turn is number => turn !== null && Number.isSafeInteger(turn) && turn > 0)
    .sort((left, right) => left - right);
  const ranges: TrajectoryTurnRange[] = [];
  let expected = Math.max(0, removedThrough) + 1;
  for (const turn of numbers) {
    if (turn > expected) ranges.push({ first: expected, last: turn - 1 });
    expected = Math.max(expected, turn + 1);
  }
  return ranges;
}

/**
 * Count the turns a set of ranges covers.
 *
 * @param ranges Ranges from {@link unrecordedTurnRanges}.
 * @returns Total number of turns across every range.
 */
export function countTurns(ranges: readonly TrajectoryTurnRange[]): number {
  return ranges.reduce((total, range) => total + range.last - range.first + 1, 0);
}

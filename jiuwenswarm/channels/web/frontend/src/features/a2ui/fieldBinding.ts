// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useCallback, useEffect, useState } from 'react';
import type { CSSProperties } from 'react';
import type { DataValue } from '@a2ui/react';
import { dualWriteA2UIValue } from './formDefaults';

type ReadModelValue = (path: string) => unknown;
type WriteModelValue = (path: string, value: DataValue) => void;

interface BoundValueOptions<T extends DataValue> {
  path?: string;
  initialValue: T;
  getValue: ReadModelValue;
  setValue: WriteModelValue;
  fromModelValue: (value: unknown) => T;
  shouldSeedInitial?: (value: T) => boolean;
  literalValue?: T;
}

function alwaysSeedInitial(): boolean {
  return true;
}

export function hostWeightStyle(weight: unknown): CSSProperties {
  return weight !== undefined ? ({ '--weight': weight } as CSSProperties) : {};
}

export function useA2UIBoundValue<T extends DataValue>({
  path,
  initialValue,
  getValue,
  setValue,
  fromModelValue,
  shouldSeedInitial,
  literalValue,
}: BoundValueOptions<T>): [T, (nextValue: T) => void] {
  const [currentValue, setCurrentValue] = useState(initialValue);
  const shouldSeed = shouldSeedInitial ?? alwaysSeedInitial;

  useEffect(() => {
    if (!path) return;

    const storedValue = getValue(path);
    if (storedValue === null) {
      if (shouldSeed(initialValue)) {
        dualWriteA2UIValue(setValue, path, initialValue);
      }
      return;
    }

    const nextValue = fromModelValue(storedValue);
    if (!Object.is(nextValue, currentValue)) {
      setCurrentValue(nextValue);
    }
  }, [currentValue, fromModelValue, getValue, initialValue, path, setValue, shouldSeed]);

  useEffect(() => {
    if (literalValue !== undefined) {
      setCurrentValue(literalValue);
    }
  }, [literalValue]);

  const commitValue = useCallback((nextValue: T) => {
    setCurrentValue(nextValue);
    if (path) {
      dualWriteA2UIValue(setValue, path, nextValue);
    }
  }, [path, setValue]);

  return [currentValue, commitValue];
}

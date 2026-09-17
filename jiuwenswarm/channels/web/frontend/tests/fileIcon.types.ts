import type { FileIconProps } from '../src/components/FileIcon';

const validProps: FileIconProps[] = [{}, { fileName: 'report.pdf' }, { iconType: 'word' }];

// @ts-expect-error fileName and iconType are mutually exclusive.
const conflictingProps: FileIconProps = { fileName: 'report.pdf', iconType: 'word' };

void validProps;
void conflictingProps;

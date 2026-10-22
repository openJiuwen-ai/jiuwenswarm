import test from 'node:test';
import assert from 'node:assert/strict';

import {
    parseWebUIRenderClearFrame,
    parseWebUIRenderErrorFrame,
    parseWebUIRenderFrame,
    parseWebUITarget,
    webUIInstanceKey,
} from '../node_modules/.cache/webui-store/types.js';
import { selectSlotInstances, useWebUIStore } from '../node_modules/.cache/webui-store/webuiStore.js';

const FRAME = {
    target: {
        session_id: 'sess-1',
        slot_id: 'conversation.panel.right',
        package_id: 'pkg.demo',
        instance_key: 'main',
    },
    revision: 1,
    mode: 'replace',
    root: { component: 'Text', props: { value: 'hello' } },
    schema_version: 1,
};

function resetStore() {
    useWebUIStore.setState({ instances: {} });
}

test('target parsing rejects unknown slots and missing session', () => {
    assert.deepEqual(parseWebUITarget(FRAME), FRAME.target);
    assert.equal(parseWebUITarget({ target: { slot_id: 'application.menu', session_id: 's' } }), null);
    assert.equal(parseWebUITarget({ target: { slot_id: 'conversation.panel.right' } }), null);
    assert.equal(parseWebUITarget({}), null);
});

test('frame parsing requires revision and a known mode', () => {
    assert.deepEqual(parseWebUIRenderFrame(FRAME), { ...FRAME, mode: 'replace' });
    assert.equal(parseWebUIRenderFrame({ ...FRAME, revision: 'x' }), null);
    assert.equal(parseWebUIRenderFrame({ ...FRAME, mode: 'append' }), null);
    assert.deepEqual(parseWebUIRenderErrorFrame({ target: FRAME.target, code: 'SCHEMA_ERROR' }), {
        target: FRAME.target,
        revision: 0,
        code: 'SCHEMA_ERROR',
        message: undefined,
    });
});

test('instance key is stable across the four target fields', () => {
    assert.equal(webUIInstanceKey(FRAME.target), 'sess-1|conversation.panel.right|pkg.demo|main');
});

test('applyFrame ignores stale revisions and keeps the latest tree', () => {
    resetStore();
    const applyFrame = useWebUIStore.getState().applyFrame;
    applyFrame(parseWebUIRenderFrame(FRAME));
    applyFrame(parseWebUIRenderFrame({ ...FRAME, revision: 2, root: { component: 'Text', props: { value: 'v2' } } }));
    applyFrame(parseWebUIRenderFrame({ ...FRAME, revision: 1, root: { component: 'Text', props: { value: 'stale' } } }));

    const instances = Object.values(useWebUIStore.getState().instances);
    assert.equal(instances.length, 1);
    assert.equal(instances[0].revision, 2);
    assert.equal(instances[0].root.props.value, 'v2');
});

test('clear mode drops the tree but keeps the instance', () => {
    resetStore();
    const applyFrame = useWebUIStore.getState().applyFrame;
    applyFrame(parseWebUIRenderFrame(FRAME));
    applyFrame(parseWebUIRenderFrame({ ...FRAME, revision: 2, mode: 'clear', root: null }));

    const instances = Object.values(useWebUIStore.getState().instances);
    assert.equal(instances.length, 1);
    assert.equal(instances[0].mode, 'clear');
    assert.equal(instances[0].root, null);
});

test('render.clear removes the instance so a later revision 1 starts fresh', () => {
    resetStore();
    const applyFrame = useWebUIStore.getState().applyFrame;
    const applyClear = useWebUIStore.getState().applyClear;
    applyFrame(parseWebUIRenderFrame({ ...FRAME, revision: 5 }));
    applyClear(parseWebUIRenderClearFrame({ target: FRAME.target, revision: 6, reason: 'session_closed' }));

    assert.deepEqual(useWebUIStore.getState().instances, {});
    applyFrame(parseWebUIRenderFrame({ ...FRAME, revision: 1 }));
    const instances = Object.values(useWebUIStore.getState().instances);
    assert.equal(instances.length, 1);
    assert.equal(instances[0].revision, 1);
});

test('render.clear ignores a stale revision', () => {
    resetStore();
    const applyFrame = useWebUIStore.getState().applyFrame;
    const applyClear = useWebUIStore.getState().applyClear;
    applyFrame(parseWebUIRenderFrame({ ...FRAME, revision: 4 }));
    applyClear(parseWebUIRenderClearFrame({ target: FRAME.target, revision: 3 }));

    assert.equal(Object.values(useWebUIStore.getState().instances).length, 1);
});

test('selectSlotInstances filters by session and slot, single slot keeps newest', () => {
    resetStore();
    const applyFrame = useWebUIStore.getState().applyFrame;
    applyFrame(parseWebUIRenderFrame(FRAME));
    applyFrame(parseWebUIRenderFrame({ ...FRAME, target: { ...FRAME.target, package_id: 'pkg.other' } }));
    applyFrame(parseWebUIRenderFrame({ ...FRAME, target: { ...FRAME.target, session_id: 'sess-2' } }));
    applyFrame(
        parseWebUIRenderFrame({
            ...FRAME,
            target: { ...FRAME.target, slot_id: 'conversation.input.below' },
            revision: 7,
        }),
    );
    applyFrame(
        parseWebUIRenderFrame({
            ...FRAME,
            target: { ...FRAME.target, slot_id: 'conversation.input.below', instance_key: 'strip' },
            revision: 3,
        }),
    );

    const instances = useWebUIStore.getState().instances;
    assert.equal(selectSlotInstances(instances, 'sess-1', 'conversation.panel.right').length, 2);
    assert.equal(selectSlotInstances(instances, 'sess-2', 'conversation.panel.right').length, 1);
    assert.equal(selectSlotInstances(instances, null, 'conversation.panel.right').length, 0);

    const single = selectSlotInstances(instances, 'sess-1', 'conversation.input.below');
    assert.equal(single.length, 1);
    assert.equal(single[0].revision, 7);
});

test('clearSession drops every instance of one session', () => {
    resetStore();
    const applyFrame = useWebUIStore.getState().applyFrame;
    applyFrame(parseWebUIRenderFrame(FRAME));
    applyFrame(parseWebUIRenderFrame({ ...FRAME, target: { ...FRAME.target, session_id: 'sess-2' } }));
    useWebUIStore.getState().clearSession('sess-1');

    const remaining = Object.values(useWebUIStore.getState().instances);
    assert.equal(remaining.length, 1);
    assert.equal(remaining[0].target.session_id, 'sess-2');
});

test('a render error without a tree is still visible in its slot', () => {
    resetStore();
    const applyError = useWebUIStore.getState().applyError;
    applyError(parseWebUIRenderErrorFrame({ target: FRAME.target, code: 'SCHEMA_ERROR' }));

    const visible = selectSlotInstances(useWebUIStore.getState().instances, 'sess-1', 'conversation.panel.right');
    assert.equal(visible.length, 1);
    assert.equal(visible[0].error.code, 'SCHEMA_ERROR');
});
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ComfyuiImportError,
  buildComfyuiCanvasGraph,
  isComfyuiNodeConfig,
  parseComfyuiWorkflow,
} from '../node_modules/.cache/comfyui-workflow/comfyuiWorkflow.js';

function uiInput(name, link = null, widget = false) {
  return { name, type: 'ANY', link, ...(widget ? { widget: { name } } : {}) };
}

/** Shaped like "vLLM-Omni MiniMax-H3 Reference to Video.json" from the plugin. */
const referenceToVideo = {
  last_node_id: 12,
  nodes: [
    { id: 1, type: 'LoadImage', inputs: [], widgets_values: ['h3_reference.png', 'image'] },
    { id: 2, type: 'LoadVideo', inputs: [], widgets_values: ['clips/h3_reference.mp4'] },
    { id: 3, type: 'LoadAudio', inputs: [], widgets_values: ['h3_reference.wav', null, ''] },
    {
      id: 4,
      type: 'VLLMOmniVideoReferences',
      inputs: [uiInput('image_1', 1), uiInput('audio_1', 3), uiInput('video_1', 2)],
      widgets_values: [],
    },
    {
      id: 5,
      type: 'VLLMOmniDiffusionSampling',
      inputs: [],
      widgets_values: [2, 30, 5.5, 1.0, false, true, 42, 'fixed'],
    },
    { id: 6, type: 'VLLMOmniMiniMaxH3Params', inputs: [], widgets_values: [3.0, 12.0] },
    { id: 7, type: 'VLLMOmniRemoteLoRA', inputs: [], widgets_values: ['', 'turbo', 1.0, 0] },
    { id: 12, type: 'LoadImage', inputs: [], widgets_values: ['first.png', 'image'] },
    {
      id: 8,
      type: 'VLLMOmniGenerateVideo',
      title: 'Ref2VA',
      inputs: [
        uiInput('frame'),
        uiInput('first_frame', 7),
        uiInput('references', 4),
        uiInput('sampling_params', 5),
        uiInput('lora', 6),
        uiInput('model_params', 8),
        uiInput('url', null, true),
      ],
      widgets_values: [
        'http://omni:8000/v1',
        'MiniMaxAI/MiniMax-H3',
        'Use <Picture 1>.',
        'blurry',
        1344,
        768,
        24,
        5.167,
      ],
    },
    { id: 9, type: 'SaveVideo', inputs: [uiInput('video', 9)], widgets_values: [] },
  ],
  links: [
    [1, 1, 0, 4, 0, 'IMAGE'],
    [2, 2, 0, 4, 2, 'VIDEO'],
    [3, 3, 0, 4, 1, 'AUDIO'],
    [4, 4, 0, 8, 2, 'VIDEO_REFERENCES'],
    [5, 5, 0, 8, 3, 'SAMPLING_PARAMS'],
    [6, 7, 0, 8, 4, 'REMOTE_LORA'],
    [7, 12, 0, 8, 1, 'IMAGE'],
    [8, 6, 0, 8, 5, 'VIDEO_PARAMS'],
    [9, 8, 0, 9, 0, 'VIDEO'],
  ],
};

/** Shaped like "vLLM-Omni Chaining Services.json": text-to-image feeding an edit. */
const chainedImages = {
  nodes: [
    {
      id: 6,
      type: 'VLLMOmniGenerateImage',
      inputs: [uiInput('image'), uiInput('mask'), uiInput('sampling_params', 5)],
      widgets_values: ['http://localhost:8000/v1', 'Z-Image-Turbo', 'A kitty.', 'Cartoonish.', 800, 800],
    },
    {
      id: 7,
      type: 'VLLMOmniDiffusionSampling',
      inputs: [],
      widgets_values: [1, 50, 1, 1, false, false, -1, 'randomize'],
    },
    {
      id: 5,
      type: 'VLLMOmniGenerateImage',
      title: 'Pop art edit',
      inputs: [uiInput('image', 4), uiInput('mask', 10), uiInput('sampling_params')],
      widgets_values: ['http://localhost:8001/v1', 'Qwen-Image-Edit', 'Pop art.', '', 640, 480],
    },
    { id: 11, type: 'SolidMask', inputs: [], widgets_values: [1, 512, 512] },
  ],
  links: [
    [4, 6, 0, 5, 0, 'IMAGE'],
    [5, 7, 0, 6, 2, 'SAMPLING_PARAMS'],
    [10, 11, 0, 5, 1, 'MASK'],
  ],
};

test('UI export: video fields, sampling, model params and ordered references', () => {
  const parsed = parseComfyuiWorkflow(JSON.stringify(referenceToVideo));

  assert.equal(parsed.generations.length, 1);
  const [video] = parsed.generations;
  assert.equal(video.class_type, 'VLLMOmniGenerateVideo');
  assert.equal(video.title, 'Ref2VA');
  assert.equal(video.prompt, 'Use <Picture 1>.');
  assert.deepEqual(video.fields, {
    url: 'http://omni:8000/v1',
    model: 'MiniMaxAI/MiniMax-H3',
    negative_prompt: 'blurry',
    width: 1344,
    height: 768,
    fps: 24,
    duration: 5.167,
  });
  // `n` is dropped: one canvas node is one output.
  assert.deepEqual(video.sampling_params, {
    num_inference_steps: 30,
    guidance_scale: 5.5,
    true_cfg_scale: 1,
    vae_use_slicing: false,
    vae_use_tiling: true,
    seed: 42,
  });
  assert.deepEqual(video.model_params, { type: 'minimax_h3', audio_flow_shift: 3, flow_shift: 12 });
  // Plugin order: images, then videos, then audios.
  assert.deepEqual(
    video.references.map((ref) => [ref.source_id, ref.kind]),
    [
      ['1', 'image'],
      ['2', 'video'],
      ['3', 'audio'],
    ],
  );
  assert.deepEqual(parsed.references, [
    { node_id: '1', kind: 'image', label: 'h3_reference.png', filename: 'h3_reference.png' },
    { node_id: '2', kind: 'video', label: 'h3_reference.mp4', filename: 'h3_reference.mp4' },
    { node_id: '3', kind: 'audio', label: 'h3_reference.wav', filename: 'h3_reference.wav' },
  ]);
  assert.deepEqual(parsed.warnings.map((warning) => [warning.code, warning.detail]).sort(), [
    ['skipped_input', 'first_frame'],
    ['skipped_input', 'lora'],
  ]);
});

test('UI export: a generate node feeding another becomes an edge, not a placeholder', () => {
  const parsed = parseComfyuiWorkflow(chainedImages);

  assert.deepEqual(
    parsed.generations.map((item) => item.node_id),
    ['6', '5'],
  );
  const edit = parsed.generations[1];
  assert.equal(edit.title, 'Pop art edit');
  assert.deepEqual(edit.references, [{ source_id: '6', kind: 'image' }]);
  assert.equal(edit.sampling_params, null);
  assert.equal(edit.model_params, null);
  assert.equal(parsed.generations[0].title, 'ComfyUI Image #6');
  assert.equal(parsed.generations[0].sampling_params.seed, -1);
  assert.deepEqual(parsed.references, []);
  assert.deepEqual(parsed.warnings, [{ code: 'skipped_input', node_id: '5', detail: 'mask' }]);
});

test('API export with a sampling list of one and Wan model params', () => {
  const parsed = parseComfyuiWorkflow({
    3: {
      class_type: 'VLLMOmniGenerateVideo',
      _meta: { title: 'Wan T2V' },
      inputs: {
        url: 'http://wan:8000/v1',
        model: 'Wan-AI/Wan2.2-T2V-A14B-Diffusers',
        prompt: 'A boat.',
        negative_prompt: '',
        width: 832,
        height: 480,
        fps: 16,
        duration: 4,
        sampling_params: ['4', 0],
        model_params: ['5', 0],
      },
    },
    4: { class_type: 'VLLMOmniSamplingParamsList', inputs: { param1: ['6', 0] } },
    5: {
      class_type: 'VLLMOmniWanParams',
      inputs: { guidance_scale_2: 4, boundary_ratio: 0.875, flow_shift: 5 },
    },
    6: {
      class_type: 'VLLMOmniDiffusionSampling',
      inputs: {
        n: 1,
        num_inference_steps: 40,
        guidance_scale: 4,
        true_cfg_scale: 1,
        vae_use_slicing: true,
        vae_use_tiling: false,
        seed: 9,
      },
    },
  });

  const [video] = parsed.generations;
  assert.equal(video.title, 'Wan T2V');
  assert.equal(video.fields.fps, 16);
  assert.equal(video.sampling_params.num_inference_steps, 40);
  assert.equal(video.sampling_params.seed, 9);
  assert.deepEqual(video.model_params, {
    type: 'wan',
    guidance_scale_2: 4,
    boundary_ratio: 0.875,
    flow_shift: 5,
  });
  assert.deepEqual(parsed.warnings, []);
});

test('multi-stage sampling lists are skipped with a warning', () => {
  const parsed = parseComfyuiWorkflow({
    1: {
      class_type: 'VLLMOmniGenerateImage',
      inputs: { prompt: 'x', sampling_params: ['2', 0] },
    },
    2: { class_type: 'VLLMOmniSamplingParamsList', inputs: { param1: ['3', 0], param2: ['4', 0] } },
    3: { class_type: 'VLLMOmniARSampling', inputs: {} },
    4: { class_type: 'VLLMOmniDiffusionSampling', inputs: {} },
  });

  assert.equal(parsed.generations[0].sampling_params, null);
  assert.deepEqual(parsed.warnings, [{ code: 'multi_stage_sampling', node_id: '1', detail: '2' }]);
  assert.equal(parsed.generations[0].fields.width, 512);
});

test('rejects bad JSON, foreign JSON and workflows without supported nodes', () => {
  const codeOf = (input) => {
    try {
      parseComfyuiWorkflow(input);
    } catch (error) {
      assert.ok(error instanceof ComfyuiImportError);
      return error.code;
    }
    return 'ok';
  };
  assert.equal(codeOf('{nope'), 'invalid_json');
  assert.equal(codeOf('[1, 2]'), 'unrecognized_format');
  assert.equal(codeOf({ hello: 'world' }), 'unrecognized_format');
  assert.equal(
    codeOf({ nodes: [{ id: 1, type: 'KSampler', inputs: [], widgets_values: [] }], links: [] }),
    'no_supported_nodes',
  );
});

test('canvas graph: references left of the generate node, params on the hidden config', () => {
  const parsed = parseComfyuiWorkflow(referenceToVideo);
  const existing = [{ id: 'n_image_1', type: 'image', label: 'Image 1' }];

  const graph = buildComfyuiCanvasGraph({ workflow: parsed, existing, origin: { x: 500, y: 300 } });

  assert.equal(graph.nodes.length, 4);
  const video = graph.nodes.find((node) => node.type === 'video' && node.config.is_comfyui);
  const refs = graph.nodes.filter((node) => node !== video);
  assert.ok(video);
  assert.ok(!graph.nodes.some((node) => node.id === 'n_image_1'));
  assert.equal(new Set(graph.nodes.map((node) => node.id)).size, 4);
  assert.equal(video.label, 'Ref2VA');
  assert.equal(video.config.force_handler, true);
  assert.equal(video.config.delegate, 'handler');
  assert.equal(video.config.generate.prompt, 'Use <Picture 1>.');
  assert.equal(video.config.comfyui.class_type, 'VLLMOmniGenerateVideo');
  assert.equal(video.config.comfyui.source_node_id, '8');
  assert.equal(video.config.comfyui.fields.duration, 5.167);
  assert.deepEqual(video.config.comfyui.model_params.type, 'minimax_h3');
  assert.ok(isComfyuiNodeConfig(video.config));

  assert.deepEqual(
    refs.map((node) => [node.type, node.label, node.config.interaction_mode, node.config.upload?.filename]),
    [
      ['image', 'h3_reference.png', 'upload', 'h3_reference.png'],
      ['video', 'h3_reference.mp4', 'upload', 'h3_reference.mp4'],
      ['audio', 'h3_reference.wav', 'upload', 'h3_reference.wav'],
    ],
  );
  for (const ref of refs) {
    assert.ok(!isComfyuiNodeConfig(ref.config));
    assert.ok(ref.layout.x < video.layout.x, 'references sit left of the generate node');
  }
  assert.deepEqual(
    video.config.inputs,
    refs.map((node) => node.id),
  );
  assert.deepEqual(
    graph.edges.map((edge) => [edge.source, edge.target, edge.kind]),
    refs.map((node) => [node.id, video.id, 'data']),
  );
  assert.equal(Math.min(...graph.nodes.map((node) => node.layout.x)), 500);
  assert.equal(Math.min(...graph.nodes.map((node) => node.layout.y)), 300);
});

test('canvas graph: chained generate nodes are wired to each other', () => {
  const graph = buildComfyuiCanvasGraph({
    workflow: parseComfyuiWorkflow(chainedImages),
    existing: [],
    origin: { x: 0, y: 0 },
  });

  assert.equal(graph.nodes.length, 2);
  const [first, second] = graph.nodes;
  assert.deepEqual(
    graph.edges.map((edge) => [edge.source, edge.target]),
    [[first.id, second.id]],
  );
  assert.ok(first.layout.x < second.layout.x);
});

/*!
 * WorkSwarm 主题背景 2.0：科研终端合成声景。
 * LabSynth 与 SILENT 原样移植自主题引擎 1.3.2 运行时，在沙箱页面里由兼容 SDK 与科研终端播放页使用。
 */
(function () {
  'use strict';
  var doc = document;
  var html = doc.documentElement;
  function report(error) { if (window.console) console.error(error); }
  function noop() {}

  // 没有声音时传给渲染器的空实现，渲染器无需判断。
  var SILENT = { ambient: noop, intro: noop, tick: noop, status: noop, phase: noop, meter: noop, row: noop, scan: noop, check: noop, result: noop, grant: noop };

  function noiseBuffer(ac, seconds) {
    var buffer = ac.createBuffer(1, Math.max(1, Math.round(ac.sampleRate * seconds)), ac.sampleRate);
    var data = buffer.getChannelData(0);
    for (var i = 0; i < data.length; i += 1) data[i] = Math.random() * 2 - 1;
    return buffer;
  }

  // 程序生成的短房间混响：双声道指数衰减噪声。
  function roomImpulse(ac, seconds, decay) {
    var length = Math.max(1, Math.round(ac.sampleRate * seconds));
    var buffer = ac.createBuffer(2, length, ac.sampleRate);
    for (var c = 0; c < 2; c += 1) {
      var data = buffer.getChannelData(c);
      for (var i = 0; i < length; i += 1) data[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / length, decay);
    }
    return buffer;
  }

  /**
   * “科研终端”原创合成声景：洁净实验室的低频铺底（D 大调加九和弦，缓慢呼吸的低通滤波），
   * 加上终端打字、状态提示、阶段切换、身份扫描与授权和弦。全部实时合成，不需要音频文件，
   * 因此开场一出现就能发声。各方法的 at 为 AudioContext 时间（秒），缺省为当前时刻。
   */
  function LabSynth(ac, destination) {
    function at0(at) { return at == null ? ac.currentTime : at; }

    // 整体电平：音量 0.6 时峰值约 -9 dBFS，与系统提示音相当，不盖过其他声音。
    var out = ac.createGain();
    out.gain.value = 2;
    out.connect(destination);

    var reverb = ac.createConvolver();
    reverb.buffer = roomImpulse(ac, 1.8, 2.6);
    var wet = ac.createGain();
    wet.gain.value = 0.3;
    reverb.connect(wet);
    wet.connect(out);
    // 短音效总线：干声直出，同时送一份进混响。
    var fx = ac.createGain();
    fx.connect(out);
    fx.connect(reverb);
    var noise = noiseBuffer(ac, 1);

    var padFilter = ac.createBiquadFilter();
    padFilter.type = 'lowpass';
    padFilter.frequency.value = 520;
    padFilter.Q.value = 0.6;
    var padGain = ac.createGain();
    padGain.gain.value = 0;
    padFilter.connect(padGain);
    padGain.connect(out);
    padGain.connect(reverb);
    var lfo = ac.createOscillator();
    lfo.frequency.value = 0.09;
    var lfoDepth = ac.createGain();
    lfoDepth.gain.value = 160;
    lfo.connect(lfoDepth);
    lfoDepth.connect(padFilter.frequency);
    var sources = [lfo];
    // D2 / D3 / A3 / E4，每个音两支略微失谐的振荡器。
    [[73.42, 'sine', 0.5], [146.83, 'triangle', 0.3], [220, 'triangle', 0.22], [329.63, 'sine', 0.14]].forEach(function (voice) {
      [-5, 5].forEach(function (cents) {
        var osc = ac.createOscillator();
        osc.type = voice[1];
        osc.frequency.value = voice[0];
        osc.detune.value = cents;
        var gain = ac.createGain();
        gain.gain.value = voice[2] / 2;
        osc.connect(gain);
        gain.connect(padFilter);
        sources.push(osc);
      });
    });
    // 空气感：带通后的极轻噪声，随铺底一起起落。
    var air = ac.createBufferSource();
    air.buffer = noise;
    air.loop = true;
    var airFilter = ac.createBiquadFilter();
    airFilter.type = 'bandpass';
    airFilter.frequency.value = 5200;
    airFilter.Q.value = 0.5;
    var airGain = ac.createGain();
    airGain.gain.value = 0.05;
    air.connect(airFilter);
    airFilter.connect(airGain);
    airGain.connect(padGain);
    sources.push(air);
    var started = false;
    var lastTick = -1;

    function envelope(gain, t, peak, attack, release) {
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(peak, t + attack);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + attack + release);
    }

    function tone(freq, t, peak, release, type, glideTo) {
      var osc = ac.createOscillator();
      osc.type = type || 'sine';
      osc.frequency.setValueAtTime(freq, t);
      if (glideTo) osc.frequency.exponentialRampToValueAtTime(glideTo, t + release);
      var gain = ac.createGain();
      envelope(gain, t, peak, 0.008, release);
      osc.connect(gain);
      gain.connect(fx);
      osc.start(t);
      osc.stop(t + release + 0.05);
    }

    // 玻璃质感的铃音：基音加两个弱泛音。
    function bell(freq, t, peak, release) {
      tone(freq, t, peak, release);
      tone(freq * 2.01, t, peak * 0.32, release * 0.6);
      tone(freq * 3.02, t, peak * 0.12, release * 0.35);
    }

    function burst(t, peak, freq, q, release) {
      var src = ac.createBufferSource();
      src.buffer = noise;
      var filter = ac.createBiquadFilter();
      filter.type = 'bandpass';
      filter.frequency.value = freq;
      filter.Q.value = q;
      var gain = ac.createGain();
      envelope(gain, t, peak, 0.002, release);
      src.connect(filter);
      filter.connect(gain);
      gain.connect(fx);
      src.start(t, Math.random() * 0.8);
      src.stop(t + release + 0.05);
    }

    function startPad(t) {
      if (started) return;
      started = true;
      sources.forEach(function (node) { node.start(t); });
      padGain.gain.setValueAtTime(0.0001, t);
      padGain.gain.exponentialRampToValueAtTime(0.16, t + 1.6);
    }

    return {
      // 只有铺底（HTML/视频开场使用合成声景时）。
      ambient: function (at) {
        startPad(at0(at));
      },
      intro: function (at) {
        var t = at0(at);
        startPad(t);
        // 上电：低频滑音与渐亮的噪声，随后一声铃音落在标志出现时。
        tone(110, t, 0.05, 0.7, 'sine', 330);
        var src = ac.createBufferSource();
        src.buffer = noise;
        var filter = ac.createBiquadFilter();
        filter.type = 'bandpass';
        filter.Q.value = 1.2;
        filter.frequency.setValueAtTime(400, t);
        filter.frequency.exponentialRampToValueAtTime(3200, t + 0.8);
        var gain = ac.createGain();
        gain.gain.setValueAtTime(0.0001, t);
        gain.gain.exponentialRampToValueAtTime(0.035, t + 0.6);
        gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.95);
        src.connect(filter);
        filter.connect(gain);
        gain.connect(fx);
        src.start(t);
        src.stop(t + 1);
        bell(587.33, t + 0.55, 0.07, 1.6);
      },
      tick: function (at) {
        var t = at0(at);
        // 打字声过密时只保留一部分，避免连成噪音。
        if (t - lastTick < 0.035) return;
        lastTick = t;
        burst(t, 0.03, 3000 + Math.random() * 1800, 4, 0.018);
      },
      status: function (toneName, at) {
        var t = at0(at);
        if (toneName === 'accent') {
          tone(1174.66, t, 0.045, 0.09);
          tone(1567.98, t + 0.07, 0.05, 0.14);
        } else if (toneName === 'muted') {
          tone(783.99, t, 0.025, 0.07);
        } else if (toneName === 'warn') {
          tone(932.33, t, 0.04, 0.09, 'triangle');
          tone(739.99, t + 0.09, 0.04, 0.12, 'triangle');
        } else if (toneName === 'danger') {
          tone(196, t, 0.06, 0.22, 'triangle');
        } else {
          tone(1567.98, t, 0.03, 0.07);
        }
      },
      phase: function (panelType, at) {
        var t = at0(at);
        bell(880, t, 0.05, 0.9);
        bell(1318.51, t + 0.09, 0.04, 1.1);
        // 每进入一个阶段，铺底略微变亮。
        padFilter.frequency.setTargetAtTime(panelType === 'identity' ? 900 : 700, t, 0.4);
      },
      meter: function (at) {
        tone(660, at0(at), 0.018, 0.12, 'triangle', 990);
      },
      row: function (at) {
        burst(at0(at), 0.025, 1800, 2.5, 0.03);
      },
      scan: function (seconds, at) {
        var t = at0(at);
        var duration = Math.max(0.4, seconds);
        var osc = ac.createOscillator();
        osc.frequency.setValueAtTime(320, t);
        osc.frequency.exponentialRampToValueAtTime(1500, t + duration);
        var gain = ac.createGain();
        gain.gain.setValueAtTime(0.0001, t);
        gain.gain.exponentialRampToValueAtTime(0.016, t + 0.2);
        gain.gain.setValueAtTime(0.016, t + duration - 0.2);
        gain.gain.exponentialRampToValueAtTime(0.0001, t + duration);
        // 扫描时的颤音。
        var wobble = ac.createOscillator();
        wobble.frequency.value = 9;
        var depth = ac.createGain();
        depth.gain.value = 0.008;
        wobble.connect(depth);
        depth.connect(gain.gain);
        osc.connect(gain);
        gain.connect(fx);
        osc.start(t);
        wobble.start(t);
        osc.stop(t + duration + 0.05);
        wobble.stop(t + duration + 0.05);
      },
      check: function (at) {
        var t = at0(at);
        tone(1567.98, t, 0.03, 0.06);
        tone(2093, t + 0.05, 0.03, 0.1);
      },
      result: function (at) {
        var t = at0(at);
        bell(1174.66, t, 0.05, 0.8);
        bell(1760, t + 0.1, 0.04, 1);
      },
      grant: function (at) {
        var t = at0(at);
        // 授权：D 大调加九和弦渐起，低频一声落地，铺底完全打开。
        tone(98, t, 0.16, 0.45, 'sine', 55);
        [293.66, 369.99, 440, 587.33, 659.25].forEach(function (freq, i) {
          var osc = ac.createOscillator();
          osc.type = i % 2 ? 'sine' : 'triangle';
          osc.frequency.value = freq;
          var gain = ac.createGain();
          var start = t + i * 0.035;
          gain.gain.setValueAtTime(0.0001, start);
          gain.gain.exponentialRampToValueAtTime(0.05, start + 0.06);
          gain.gain.exponentialRampToValueAtTime(0.025, start + 0.6);
          gain.gain.exponentialRampToValueAtTime(0.0001, start + 2.6);
          osc.connect(gain);
          gain.connect(fx);
          osc.start(start);
          osc.stop(start + 2.7);
        });
        bell(2349.32, t + 0.18, 0.018, 1.8);
        padFilter.frequency.setTargetAtTime(1800, t, 0.25);
      }
    };
  }

  // 用媒体元素播放的声音（包内音频文件或视频原声）：用定时器做音量淡入淡出。

  window.WorkSwarmLabSynth = Object.freeze({ create: LabSynth, silent: SILENT });
})();

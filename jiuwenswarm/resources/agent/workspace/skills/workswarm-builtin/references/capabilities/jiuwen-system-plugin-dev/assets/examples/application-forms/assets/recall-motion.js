// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
(() => {
  'use strict';
  const protocol = 'workswarm.presentation';
  const settings = { intensity: 1 };
  const clamp = value => Math.max(0, Math.min(1, value));
  const smooth = value => { const t = clamp(value); return t * t * (3 - 2 * t); };
  const out = value => 1 - Math.pow(1 - clamp(value), 4);
  const mix = (a, b, t) => a + (b - a) * t;
  const random = value => { const x = Math.sin(value * 127.1 + 311.7) * 43758.5453; return x - Math.floor(x); };

  function create({ root, bee, onError }) {
    let active = null, disposed = false;
    const send = (type, id) => parent.postMessage({ protocol, version: 1, type, id }, '*');

    function release() {
      const current = active;
      if (!current) return;
      active = null;
      if (current.raf !== null) cancelAnimationFrame(current.raf);
      current.image.onload = null; current.image.onerror = null; current.image.src = '';
      current.layer.remove();
      root.inert = current.inert;
      bee.style.transform = current.beeTransform; bee.style.opacity = current.beeOpacity;
      document.body.classList.remove('window-transition');
    }

    function fail(current) {
      if (active !== current || disposed) return;
      const id = current.id;
      release(); send('transition-failed', id); onError();
    }

    function paint(current, p) {
      const { canvas, ctx, image, start: s, end: d } = current;
      ctx.clearRect(0, 0, current.width, current.height);
      const motion = flare(current, p);
      const heightScale = motion.scale * (1 + Math.sin(clamp((p - .1) / .62) * Math.PI) * .08);
      image.style.transform = `translate(${motion.location.x - s.x}px,${motion.location.y - s.y}px) rotate(${motion.rotation}deg) skewX(${motion.skew}deg) scale(${motion.scale},${heightScale})`;
      image.style.opacity = motion.alpha;
      image.style.filter = `brightness(${1 + motion.charge * .8}) saturate(${1 - motion.charge * .5})`;
      const reveal = out((p - .74) / .26);
      bee.style.opacity = smooth((p - .74) / .16);
      bee.style.transform = `translate(${(1 - reveal) * -12}px,${(1 - reveal) * 14}px) rotate(${(1 - reveal) * -14}deg) scale(${mix(.18, 1, reveal)})`;
      canvas.dataset.progress = p.toFixed(3);
    }

    function complete(current) {
      if (current.finished || active !== current) return;
      if (current.raf !== null) cancelAnimationFrame(current.raf);
      current.raf = null; current.finished = true;
      paint(current, current.direction === 'compact' ? 1 : 0);
      send('transition-finished', current.id);
    }

    async function prepare(value) {
      if (disposed || active) throw new Error('A window transition is already active');
      const { surface, workspace } = value;
      const layer = document.createElement('div'); layer.className = 'recall-transition'; layer.setAttribute('aria-hidden', 'true');
      const image = new Image(), canvas = document.createElement('canvas');
      canvas.dataset.testid = 'application-forms-recall-canvas';
      layer.append(image, canvas); document.body.append(layer);
      const current = { ...value, layer, image, canvas, ctx: canvas.getContext('2d'), raf: null, started: false, finished: false,
        width: value.viewport.width, height: value.viewport.height, appWidth: workspace.width, appHeight: workspace.height,
        start: { x: workspace.x + workspace.width / 2, y: workspace.y + workspace.height / 2 },
        inert: root.inert, beeTransform: bee.style.transform, beeOpacity: bee.style.opacity };
      active = current;
      root.inert = true; document.body.classList.add('window-transition');
      const beeRect = bee.getBoundingClientRect();
      // The hidden native window still has its final size until the first-frame receipt.
      current.end = { x: surface.x + beeRect.left + beeRect.width / 2, y: surface.y + beeRect.top + beeRect.height / 2 };
      canvas.dataset.targetX = String(current.end.x); canvas.dataset.targetY = String(current.end.y);
      image.style.left = `${workspace.x}px`; image.style.top = `${workspace.y}px`;
      image.style.width = `${workspace.width}px`; image.style.height = `${workspace.height}px`;
      const dpr = Math.min(devicePixelRatio || 1, 2);
      canvas.width = Math.round(current.width * dpr); canvas.height = Math.round(current.height * dpr);
      current.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const palette = getComputedStyle(document.documentElement);
      const sample = document.createElement('canvas'); sample.width = sample.height = 1;
      const pixel = sample.getContext('2d');
      const color = name => { pixel.clearRect(0, 0, 1, 1); pixel.fillStyle = palette.getPropertyValue(name); pixel.fillRect(0, 0, 1, 1);
        const [r, g, b] = pixel.getImageData(0, 0, 1, 1).data; return `rgb(${r},${g},${b})`; };
      current.colors = { gold: color('--recall-gold'), glint: color('--recall-glint'), smokeLight: color('--recall-aura') };
      try {
        image.src = value.image; await image.decode();
        if (active !== current || disposed) return;
        paint(current, value.direction === 'compact' ? 0 : 1);
        send('transition-ready', value.id);
      } catch { fail(current); }
    }

    function start(id) {
      const current = active;
      if (!current || current.id !== id || current.started || disposed) return;
      current.started = true;
      let startTime = null;
      function tick(now) {
        current.raf = null;
        if (active !== current || disposed || current.finished) return;
        if (startTime === null) startTime = now;
        const progress = clamp((now - startTime) / current.durationMs);
        try {
          paint(current, current.direction === 'compact' ? progress : 1 - progress);
          if (progress < 1) current.raf = requestAnimationFrame(tick); else complete(current);
        } catch { fail(current); }
      }
      current.raf = requestAnimationFrame(tick);
    }

    return { prepare, start, end(id) { if (active?.id === id) release(); },
      reduce() { if (active?.started) complete(active); },
      dispose() { disposed = true; release(); } };
  }

  function bezier(a,b,c,d,t) {
    const u=1-t;return {x:u*u*u*a.x+3*u*u*t*b.x+3*u*t*t*c.x+t*t*t*d.x,y:u*u*u*a.y+3*u*u*t*b.y+3*u*t*t*c.y+t*t*t*d.y};
  }

  function glow(ctx,x,y,r,color,opacity) {
    if (opacity<=0 || r<=0) return;
    ctx.save(); ctx.globalAlpha=opacity;const gradient=ctx.createRadialGradient(x,y,0,x,y,r);gradient.addColorStop(0,color);gradient.addColorStop(.22,color);gradient.addColorStop(1,color.replace('rgb(','rgba(').replace(')',',0)'));ctx.fillStyle=gradient;ctx.fillRect(x-r,y-r,r*2,r*2);ctx.restore();
  }

  function sparkle(ctx,x,y,r,color,alpha) {
    ctx.save();ctx.globalAlpha=alpha;ctx.strokeStyle=color;ctx.lineWidth=1.2;ctx.beginPath();ctx.moveTo(x-r,y);ctx.lineTo(x+r,y);ctx.moveTo(x,y-r);ctx.lineTo(x,y+r);ctx.stroke();ctx.restore();
  }

  function ring(ctx,x,y,rx,ry,angle,color,alpha,width=1.5) {
    ctx.save();ctx.translate(x,y);ctx.rotate(angle);ctx.globalAlpha=alpha;ctx.strokeStyle=color;ctx.lineWidth=width;ctx.beginPath();ctx.ellipse(0,0,Math.max(.1,rx),Math.max(.1,ry),0,0,Math.PI*2);ctx.stroke();ctx.restore();
  }

  function lightTrail(ctx,curve,from,to,width,color,alpha) {
    if (to<=from || alpha<=0) return;
    const steps=30;
    ctx.save();ctx.lineCap='round';ctx.strokeStyle=color;
    for(let layer=0;layer<3;layer++) {
      for(let i=0;i<steps;i++) {
        const u=i/steps,v=(i+1)/steps;
        const a=curve(mix(from,to,u)),b=curve(mix(from,to,v));
        const taper=Math.pow(Math.sin((u+v)*Math.PI/2),.7);
        ctx.globalAlpha=alpha*taper*[.035,.13,.72][layer];
        ctx.lineWidth=width*[7,3,1][layer]*(.15+.85*taper);
        ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
      }
    }
    ctx.restore();
  }

  function orbit(ctx,center,rx,ry,turn,tilt,color,alpha,width,span=1.25) {
    lightTrail(ctx,t=>{
      const angle=turn+t*Math.PI*span,x=Math.cos(angle)*rx,y=Math.sin(angle)*ry;
      return {x:center.x+x*Math.cos(tilt)-y*Math.sin(tilt),y:center.y+x*Math.sin(tilt)+y*Math.cos(tilt)};
    },0,1,width,color,alpha);
  }

  function energyFan(c,location,flight,p,charge) {
    const {ctx,end:d,colors:k}=c,dx=location.x-d.x,dy=location.y-d.y;
    const distance=Math.hypot(dx,dy);
    if (distance<1 || charge<=0) return;
    const scale=1-flight*.985,angle=flight*18*Math.PI/180,skew=Math.tan(flight*8*Math.PI/180);
    const sx=c.appWidth*scale/2,sy=c.appHeight*scale*(1+Math.sin(clamp((p-.1)/.62)*Math.PI)*.08)/2;
    const localX=-dx*Math.cos(angle)-dy*Math.sin(angle),localY=dx*Math.sin(angle)-dy*Math.cos(angle);
    if (Math.abs(localX-skew*localY)<=sx && Math.abs(localY)<=sy) return;
    const axis=Math.atan2(dy,dx),nx=-dy/distance,ny=dx/distance;
    const corners=[[-sx,-sy],[sx,-sy],[sx,sy],[-sx,sy]].map(([x,y])=>{
      const skewed=x+skew*y;
      const point={x:location.x+skewed*Math.cos(angle)-y*Math.sin(angle),y:location.y+skewed*Math.sin(angle)+y*Math.cos(angle)};
      const a=Math.atan2(point.y-d.y,point.x-d.x)-axis;
      return {...point,angle:Math.atan2(Math.sin(a),Math.cos(a))};
    }).sort((a,b)=>a.angle-b.angle);
    const edgeA=corners[0],edgeB=corners[corners.length-1];
    const back={x:location.x+dx/distance*(sx*.85+sy*.35),y:location.y+dy/distance*(sx*.85+sy*.35)};
    const tint=(color,alpha)=>color.replace('rgb(','rgba(').replace(')',`,${alpha})`);
    const palette=k.gold+'|'+k.glint;
    if (c.flowPalette!==palette) {
      const field=document.createElement('canvas');field.width=field.height=64;
      const fieldContext=field.getContext('2d'),fade=fieldContext.createRadialGradient(32,32,0,32,32,32);
      fade.addColorStop(0,tint(k.glint,.76));fade.addColorStop(.2,tint(k.gold,.5));fade.addColorStop(.55,tint(k.gold,.16));fade.addColorStop(1,tint(k.gold,0));
      fieldContext.fillStyle=fade;fieldContext.fillRect(0,0,64,64);c.flowField=field;c.flowPalette=palette;
    }
    const bend=distance*.15*(1-flight),bands=[.08,.23,.41,.59,.76,.91];
    ctx.save();
    for(let i=0;i<bands.length;i++) {
      const u=bands[i],v=1-u,feather=Math.pow(Math.sin(u*Math.PI),1.6);
      const base={x:v*v*edgeA.x+2*v*u*back.x+u*u*edgeB.x,y:v*v*edgeA.y+2*v*u*back.y+u*u*edgeB.y};
      const drift=Math.sin(p*5+u*5.2)*distance*.025;
      const controlA={x:mix(base.x,d.x,.28)+nx*(bend+drift),y:mix(base.y,d.y,.28)+ny*(bend+drift)};
      const controlB={x:mix(base.x,d.x,.76)-nx*(bend*.48-drift*.5),y:mix(base.y,d.y,.76)-ny*(bend*.48-drift*.5)};
      for(let j=0;j<19;j++) {
        const t=(j+.5)/19,a=1-t,point=bezier(base,controlA,controlB,d,t);
        const vx=3*a*a*(controlA.x-base.x)+6*a*t*(controlB.x-controlA.x)+3*t*t*(d.x-controlB.x);
        const vy=3*a*a*(controlA.y-base.y)+6*a*t*(controlB.y-controlA.y)+3*t*t*(d.y-controlB.y);
        const breathe=.72+.28*Math.sin(p*7-t*9+i*.83),fade=Math.pow(Math.sin(t*Math.PI),.8);
        const along=Math.max(12,distance*.075),across=(5+24*(1-t))*(.6+.4*feather);
        ctx.save();ctx.translate(point.x,point.y);ctx.rotate(Math.atan2(vy,vx));ctx.globalAlpha=charge*settings.intensity*.13*feather*fade*breathe;
        ctx.drawImage(c.flowField,-along,-across,along*2,across*2);ctx.restore();
      }
      const t=(p*1.7+i*.173)%1,point=bezier(base,controlA,controlB,d,t);
      glow(ctx,point.x,point.y,4,k.glint,charge*feather*Math.sin(t*Math.PI)*.28);
    }
    ctx.restore();
  }

  function flare(c,p) {
    const {ctx,start:s,end:d,colors:k}=c,amount=settings.intensity;
    const pull=clamp((p-.13)/.5),flight=Math.pow(pull,2.6);
    const location=bezier(s,{x:s.x-20,y:s.y-70},{x:d.x-100,y:d.y-115},d,flight);
    const envelope=smooth((p-.015)/.1)*(1-smooth((p-.84)/.16));
    const charge=smooth((p-.07)/.19)*(1-smooth((p-.56)/.12));
    if (envelope>0) {
      const radius=59*(1-flight)+8;
      glow(ctx,d.x,d.y,72,k.smokeLight,envelope*.16*amount);
      glow(ctx,d.x,d.y,45,k.gold,envelope*.48*amount);
      glow(ctx,d.x,d.y,11,k.glint,envelope*.95);
      orbit(ctx,d,radius,radius*.36,p*8,-.5,k.gold,envelope*.8*amount,1.8);
      orbit(ctx,d,radius*.9,radius*.34,-p*7+2.5,.8,k.smokeLight,envelope*.7*amount,1.5,1.05);
      orbit(ctx,d,radius*.67,radius*.38,p*9+1.6,-.5,k.glint,envelope*.8,1.1,.85);
      if (p<.67) energyFan(c,location,flight,p,charge*1.3);
      const dx=d.x-s.x,dy=d.y-s.y,length=Math.hypot(dx,dy);
      const nx=length===0?0:-dy/length,ny=length===0?0:dx/length;
      for(let i=0;i<6;i++) {
        const spread=(i/5-.5),a={x:s.x+spread*c.appWidth*.74,y:s.y+Math.sin(i*1.2)*c.appHeight*.31};
        const bend=length*(.17+spread*.15);
        const b={x:mix(a.x,d.x,.32)+nx*bend,y:mix(a.y,d.y,.32)+ny*bend};
        const e={x:mix(a.x,d.x,.8)-nx*bend*.42,y:mix(a.y,d.y,.8)-ny*bend*.42};
        const curve=t=>bezier(a,b,e,d,t);
        const phase=clamp((p-.12-i*.022)/.49),head=Math.pow(phase,1.7),tail=Math.max(0,head-.32);
        const alpha=Math.sin(phase*Math.PI)*envelope*amount;
        lightTrail(ctx,curve,tail,head,1.2+i%3*.35,i%3===0?k.smokeLight:k.gold,alpha*.9);
        const point=curve(head);
        glow(ctx,point.x,point.y,7,i%3===0?k.smokeLight:k.glint,alpha*.75);
      }
      for(let i=0;i<50;i++) {
        const phase=clamp((p-.07-random(i+9)*.26)/.47);
        if(phase<=0 || phase>=1) continue;
        const angle=random(i+40)*Math.PI*2+p*3.7;
        const radiusStart=35+random(i+100)*Math.min(150,c.appWidth*.48);
        const a={x:s.x+Math.cos(angle)*radiusStart,y:s.y+Math.sin(angle)*radiusStart*.64};
        const curve=t=>bezier(a,{x:a.x+nx*50,y:a.y+ny*50},{x:d.x-nx*40-dx*.15,y:d.y-ny*40-dy*.15},d,t);
        const t=Math.pow(phase,2),point=curve(t),alpha=Math.sin(phase*Math.PI)*envelope;
        glow(ctx,point.x,point.y,2.4+random(i+4)*2,i%4===0?k.smokeLight:k.gold,alpha*.6*amount);
        if(i%11===0) sparkle(ctx,point.x,point.y,2.5,k.glint,alpha*.9);
      }
      const impact=smooth((p-.625)/.045)*(1-smooth((p-.72)/.12));
      glow(ctx,d.x,d.y,105,k.gold,impact*.46*amount);
      glow(ctx,d.x,d.y,49,k.glint,impact*.72*amount);
      const ripple=smooth((p-.65)/.035)*(1-smooth((p-.77)/.2));
      const expansion=out((p-.65)/.32),r=15+expansion*70;
      orbit(ctx,d,r,r*.43,-expansion*1.6,-.25,k.gold,ripple*.8*amount,1.35,1.45);
      orbit(ctx,d,r*.9,r*.46,Math.PI-expansion*.8,.45,k.smokeLight,ripple*.65*amount,1.15,1.1);
      for(let i=0;i<10;i++) {
        const angle=i*Math.PI/5+p*2.6,radius=18+expansion*(38+random(i+6)*25);
        glow(ctx,d.x+Math.cos(angle)*radius,d.y+Math.sin(angle)*radius*.62,3+random(i+8)*2,i%3===0?k.smokeLight:k.glint,ripple*.85*amount);
      }
    }
    return {location,scale:1-flight*.985,rotation:flight*18,skew:flight*8,alpha:1-smooth((pull-.74)/.23),charge:smooth((p-.24)/.16)*(1-smooth((p-.58)/.1))*.9};
  }

  window.WorkSwarmRecallMotion = Object.freeze({ create });
})();
